from datetime import time
from unittest.mock import patch
import pandas as pd
import pytest

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import OptionLeg, SpreadPosition, SpreadType
from strategy.silver_options.risk_manager import (
    check_exit_conditions,
    sequence_entry_legs,
    sequence_exit_legs,
)
from strategy.silver_options.technical_signals import compute_ema


@pytest.fixture
def sample_spread_position():
    """Multi-leg spread position (hedge + short)."""
    leg_buy = OptionLeg(strike=150000, option_type="PE", action="BUY", quantity=5, premium=400.0)
    leg_sell = OptionLeg(strike=155000, option_type="PE", action="SELL", quantity=5, premium=1000.0)
    return SpreadPosition(
        spread_type=SpreadType.BULL_PUT_SPREAD,
        legs=[leg_sell, leg_buy],  # Intentionally unordered
        entry_net_credit=3000.0,
        sl_price=1500.0,
        status="ACTIVE",
    )


@pytest.fixture
def sample_short_put_position():
    """Single short put position."""
    leg = OptionLeg(strike=155000, option_type="PE", action="SELL", quantity=5, premium=1000.0)
    return SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[leg],
        entry_net_credit=5000.0,
        sl_price=0.0,  # Will use config.sl_multiplier * entry_premium = 1500.0
        status="ACTIVE",
    )


@pytest.fixture
def sample_short_call_position():
    """Single short call position."""
    leg = OptionLeg(strike=165000, option_type="CE", action="SELL", quantity=5, premium=800.0)
    return SpreadPosition(
        spread_type=SpreadType.SHORT_CALL,
        legs=[leg],
        entry_net_credit=4000.0,
        sl_price=0.0,  # 800 * 1.5 = 1200.0
        status="ACTIVE",
    )


# ==============================================================================
# Leg Sequencing Tests
# ==============================================================================

def test_hedge_first_entry_sequencing(sample_spread_position):
    """Hedge-first: BUY leg first to secure margin discount, SELL leg second."""
    ordered = sequence_entry_legs(sample_spread_position.legs)
    assert len(ordered) == 2
    assert ordered[0].action == "BUY"
    assert ordered[1].action == "SELL"


def test_safety_first_exit_sequencing(sample_spread_position):
    """Safety-first: Short leg closed first (eliminate short liability), then long leg."""
    ordered = sequence_exit_legs(sample_spread_position.legs)
    assert len(ordered) == 2
    assert ordered[0].action == "SELL"
    assert ordered[1].action == "BUY"


def test_single_leg_sequencing(sample_short_put_position):
    """Sequencing single-leg positions preserves the leg."""
    entry_legs = sequence_entry_legs(sample_short_put_position.legs)
    assert len(entry_legs) == 1
    assert entry_legs[0].action == "SELL"

    exit_legs = sequence_exit_legs(sample_short_put_position.legs)
    assert len(exit_legs) == 1
    assert exit_legs[0].action == "SELL"


# ==============================================================================
# Stop Loss Tests (Super Order: 1.5x Entry Premium)
# ==============================================================================

def test_stop_loss_trigger_at_or_above_multiplier(sample_short_put_position):
    """Test SL triggers when current option price reaches or exceeds 1.5x entry premium."""
    cfg = SilverConfig(sl_multiplier=1.5)
    # Entry premium = 1000.0 -> SL target = 1500.0

    # At exact SL target (1500.0) -> triggers STOP_LOSS
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"

    # Beyond SL target (1600.0) -> triggers STOP_LOSS
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1600.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"


def test_stop_loss_with_explicit_sl_price(sample_spread_position):
    """Test SL uses position.sl_price if already set (> 0)."""
    cfg = SilverConfig(sl_multiplier=2.0)  # Multiplier is 2.0, but explicit sl_price is 1500.0
    should_exit, reason = check_exit_conditions(
        sample_spread_position,
        current_opt_price=1500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"

    # Below explicit sl_price
    should_exit, reason = check_exit_conditions(
        sample_spread_position,
        current_opt_price=1499.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


def test_stop_loss_not_triggered_on_decay_or_minor_rise(sample_short_put_position):
    """For option sellers, profit occurs when price decays. SL should not trigger."""
    cfg = SilverConfig(sl_multiplier=1.5)
    # Entry premium = 1000.0, SL target = 1500.0

    # Profitable decay: 500.0 (50% theta gain)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None

    # Unchanged: 1000.0
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1000.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None

    # Minor rise: 1400.0 (< 1.5x)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1400.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


def test_stop_loss_fallback_to_net_credit():
    """When legs have no premium, determine entry premium from entry_net_credit / lot_size."""
    cfg = SilverConfig(lot_size=5, sl_multiplier=1.5)
    pos = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[],
        entry_net_credit=5000.0,  # 5000 / 5 = 1000.0 entry premium
        sl_price=0.0,
        status="ACTIVE",
    )
    # Target = 1500.0
    should_exit, reason = check_exit_conditions(
        pos,
        current_opt_price=1500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"


def test_stop_loss_missing_or_none_sl_price(sample_short_put_position):
    """When sl_price is None or missing from the position, fallback to multiplier works safely."""
    cfg = SilverConfig(sl_multiplier=1.5)
    # 1. Explicit None
    sample_short_put_position.sl_price = None
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"

    # 2. Missing attribute
    delattr(sample_short_put_position, "sl_price")
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"


def test_empty_position_no_legs_no_credits():
    """Position with no legs and zero credit/debit does not raise errors."""
    cfg = SilverConfig()
    pos = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[],
        entry_net_credit=0.0,
        entry_net_debit=0.0,
        sl_price=0.0,
    )
    should_exit, reason = check_exit_conditions(
        pos,
        current_opt_price=100.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


# ==============================================================================
# Expiry Devolvement Cutoff Tests
# ==============================================================================

def test_expiry_devolvement_cutoff_on_expiry_day(sample_short_put_position):
    """On expiry day at or after devolvement_cutoff (15:30), mandatory exit."""
    cfg = SilverConfig(devolvement_cutoff=time(15, 30))

    # At exact cutoff (15:30)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=500.0,
        current_time=time(15, 30),
        is_expiry_day=True,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"

    # After cutoff (15:35)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=500.0,
        current_time=time(15, 35),
        is_expiry_day=True,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"


def test_expiry_devolvement_cutoff_before_time(sample_short_put_position):
    """On expiry day before 15:30, devolvement cutoff is not triggered."""
    cfg = SilverConfig(devolvement_cutoff=time(15, 30))
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=500.0,
        current_time=time(15, 29),
        is_expiry_day=True,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


def test_expiry_devolvement_cutoff_non_expiry_day(sample_short_put_position):
    """On non-expiry day, devolvement cutoff never triggers even after 15:30."""
    cfg = SilverConfig(devolvement_cutoff=time(15, 30))
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=500.0,
        current_time=time(15, 35),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


# ==============================================================================
# Time Cutoff Tests (Intraday EOD 23:15 & Session Close 18:00)
# ==============================================================================

def test_intraday_eod_cutoff(sample_short_put_position):
    """At or after eod_cutoff (23:15), mandatory INTRADAY_EOD exit."""
    cfg = SilverConfig(eod_cutoff=time(23, 15), session_end=time(18, 0))

    # At exact EOD cutoff (23:15)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(23, 15),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "INTRADAY_EOD"

    # Past EOD cutoff (23:25)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(23, 25),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "INTRADAY_EOD"


def test_session_close_cutoff(sample_short_put_position):
    """At or after session_end (18:00) and before EOD cutoff, SESSION_CLOSE triggers."""
    cfg = SilverConfig(session_end=time(18, 0), eod_cutoff=time(23, 15))

    # At exact session end (18:00)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(18, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "SESSION_CLOSE"

    # Just after session end (18:05)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(18, 5),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "SESSION_CLOSE"


def test_normal_hours_within_session(sample_short_put_position):
    """During trading hours before session_end (e.g. 17:59), no time cutoff triggers."""
    cfg = SilverConfig(session_end=time(18, 0), eod_cutoff=time(23, 15))
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(17, 59),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


# ==============================================================================
# Trend Reversal Tests
# ==============================================================================

def test_trend_reversal_trigger_mocked(sample_short_put_position):
    """When check_trend_reversal returns True, return (True, 'TREND_REVERSAL')."""
    cfg = SilverConfig()
    dummy_df = pd.DataFrame({"high": [100.0, 101.0], "low": [98.0, 97.0], "close": [99.0, 98.0]})

    with patch("strategy.silver_options.risk_manager.check_trend_reversal", return_value=True) as mock_rev:
        should_exit, reason = check_exit_conditions(
            sample_short_put_position,
            current_opt_price=800.0,
            current_time=time(14, 0),
            is_expiry_day=False,
            config=cfg,
            df_5m=dummy_df,
        )
        assert should_exit is True
        assert reason == "TREND_REVERSAL"
        mock_rev.assert_called_once_with(dummy_df, "PE", cfg)


def test_trend_reversal_short_call_mocked(sample_short_call_position):
    """Short call position passes 'CE' to check_trend_reversal."""
    cfg = SilverConfig()
    dummy_df = pd.DataFrame({"high": [100.0, 102.0], "low": [98.0, 99.0], "close": [99.0, 101.0]})

    with patch("strategy.silver_options.risk_manager.check_trend_reversal", return_value=True) as mock_rev:
        should_exit, reason = check_exit_conditions(
            sample_short_call_position,
            current_opt_price=600.0,
            current_time=time(14, 0),
            is_expiry_day=False,
            config=cfg,
            df_5m=dummy_df,
        )
        assert should_exit is True
        assert reason == "TREND_REVERSAL"
        mock_rev.assert_called_once_with(dummy_df, "CE", cfg)


def test_trend_reversal_with_real_dataframe(sample_short_put_position):
    """Integration test: 5m DataFrame with genuine 5 EMA / 50 EMA bearish cross triggers reversal."""
    cfg = SilverConfig(ema_fast=5, ema_slow=50)

    # 60 flat bars followed by sharp drop causing 5 EMA to cross below 50 EMA
    closes = [1000.0] * 60 + [1000.0 - i * 20.0 for i in range(1, 15)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    ema5 = compute_ema(df["close"], span=5)
    ema50 = compute_ema(df["close"], span=50)

    cross_idx = None
    for i in range(1, len(df)):
        if ema5.iloc[i] < ema50.iloc[i] and ema5.iloc[i - 1] >= ema50.iloc[i - 1]:
            cross_idx = i
            break

    assert cross_idx is not None
    df_cross = df.iloc[: cross_idx + 1].copy().reset_index(drop=True)
    df_cross.loc[len(df_cross) - 1, "low"] = df_cross.loc[len(df_cross) - 2, "low"] - 5.0

    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=800.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
        df_5m=df_cross,
    )
    assert should_exit is True
    assert reason == "TREND_REVERSAL"


def test_trend_reversal_no_exit_when_favorable(sample_short_put_position):
    """When market remains favorable, check_trend_reversal returns False -> no exit."""
    cfg = SilverConfig(ema_fast=5, ema_slow=50)
    # Continuous uptrend: 5 EMA remains well above 50 EMA
    closes = [1000.0 + i * 10.0 for i in range(60)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    df_up = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
        df_5m=df_up,
    )
    assert should_exit is False
    assert reason is None


def test_trend_reversal_insufficient_bars(sample_short_put_position):
    """Fewer than 2 bars or None dataframe does not trigger trend reversal."""
    cfg = SilverConfig()

    # None DataFrame
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=800.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
        df_5m=None,
    )
    assert should_exit is False
    assert reason is None

    # 1 bar DataFrame
    df_1 = pd.DataFrame({"high": [100.0], "low": [90.0], "close": [95.0]})
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=800.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
        df_5m=df_1,
    )
    assert should_exit is False
    assert reason is None


# ==============================================================================
# Evaluation Priority Tests
# ==============================================================================

def test_priority_expiry_over_stop_loss(sample_short_put_position):
    """Priority: Expiry devolvement cutoff takes precedence over Stop Loss."""
    cfg = SilverConfig(devolvement_cutoff=time(15, 30), sl_multiplier=1.5)
    # Both conditions met: is_expiry_day + time >= 15:30 AND opt_price (2000) >= SL (1500)
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=2000.0,
        current_time=time(15, 35),
        is_expiry_day=True,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"


def test_priority_stop_loss_over_session_close(sample_short_put_position):
    """Priority: Stop Loss takes precedence over Session Close."""
    cfg = SilverConfig(session_end=time(18, 0), sl_multiplier=1.5)
    # Both conditions met: opt_price (1600) >= SL (1500) AND time (18:10) >= session_end
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=1600.0,
        current_time=time(18, 10),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"


def test_priority_intraday_eod_over_session_close(sample_short_put_position):
    """Priority: Intraday EOD cutoff takes precedence over Session Close when time >= 23:15."""
    cfg = SilverConfig(session_end=time(18, 0), eod_cutoff=time(23, 15))
    # Time is 23:20, which is >= 18:00 and >= 23:15
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=600.0,
        current_time=time(23, 20),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "INTRADAY_EOD"


def test_normal_ongoing_position_holds(sample_short_put_position):
    """Normal ongoing position with no exit criteria met returns (False, None)."""
    cfg = SilverConfig(
        session_end=time(18, 0),
        eod_cutoff=time(23, 15),
        devolvement_cutoff=time(15, 30),
        sl_multiplier=1.5,
    )
    # 12:00 PM, option price decayed to 800.0, non-expiry, no reversal
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=800.0,
        current_time=time(12, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is False
    assert reason is None


def test_daily_max_loss_limit_triggers_exit(sample_short_put_position):
    """Priority: Daily max loss limit triggers immediate exit if cumulative day loss hits threshold."""
    # Config: 2 lots, daily_max_loss_per_lot = 15000 -> daily_max_loss = 30000
    cfg = SilverConfig(lots=2, lot_size=5, daily_max_loss_per_lot=15000.0)
    # Entry premium = 1000.0, current price = 2500.0 -> unrealized loss = (1000 - 2500) * 10 = -15,000
    # Prior today_realized_pnl = -16,000 -> total day loss = -31,000 <= -30,000
    should_exit, reason = check_exit_conditions(
        sample_short_put_position,
        current_opt_price=2500.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
        today_realized_pnl=-16000.0,
    )
    assert should_exit is True
    assert reason == "DAILY_MAX_LOSS_LIMIT"


def test_hybrid_sl_triggers_on_fixed_cap_when_premium_is_huge():
    """Hybrid SL: When premium is huge, fixed cap per lot is used instead of dynamic 1.5x."""
    # Lot size = 5, sl_per_lot = 10,000 -> max allowed pts = 2,000
    # Entry premium = 6,000. Dynamic 1.5x would be 9,000 (3,000 pts).
    # Hybrid cap restricts SL target to 6,000 + 2,000 = 8,000.
    cfg = SilverConfig(lots=2, lot_size=5, sl_multiplier=1.5, sl_per_lot=10000.0)
    leg = OptionLeg(strike=150000, option_type="PE", action="SELL", quantity=10, premium=6000.0)
    pos = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[leg],
        entry_net_credit=60000.0,
        sl_price=0.0,  # Unset -> computes hybrid fallback
        status="ACTIVE",
    )

    # Price at 8050.0 (below 9,000 dynamic, but above 8,000 hybrid cap)
    should_exit, reason = check_exit_conditions(
        pos,
        current_opt_price=8050.0,
        current_time=time(14, 0),
        is_expiry_day=False,
        config=cfg,
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"
