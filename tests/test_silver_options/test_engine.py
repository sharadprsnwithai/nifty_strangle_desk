from datetime import time
from unittest.mock import MagicMock
import pandas as pd
import pytest

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.engine import SilverOptionsEngine
from strategy.silver_options.models import OptionLeg, SpreadPosition, SpreadType, SignalType
from strategy.silver_options.technical_signals import compute_ema


def _create_uptrend_df(n_bars: int = 80, base: float = 75000.0) -> pd.DataFrame:
    """Helper to generate a sustained 5m uptrend (wr > -20, 5 > 15 > 50 EMA, high breakout)."""
    closes = [base + i * 15.0 for i in range(n_bars)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _create_downtrend_df(n_bars: int = 80, base: float = 75000.0) -> pd.DataFrame:
    """Helper to generate a sustained 5m downtrend (wr < -80, 5 < 15 < 50 EMA, low breakdown)."""
    closes = [base - i * 15.0 for i in range(n_bars)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _create_flat_df(n_bars: int = 80, price: float = 75000.0) -> pd.DataFrame:
    """Helper to generate flat prices with no momentum."""
    return pd.DataFrame({
        "high": [price + 2.0] * n_bars,
        "low": [price - 2.0] * n_bars,
        "close": [price] * n_bars,
    })


@pytest.fixture
def sample_chain():
    """Option chain with strikes around spot 75000."""
    return [
        {
            "strike": 73000,
            "pe_oi": 1500,
            "pe_ltp": 700.0,
            "pe_delta": -0.25,
            "pe_tradingsymbol": "SILVERM26NOV73000PE",
            "ce_oi": 100,
            "ce_ltp": 3500.0,
            "ce_delta": 0.75,
            "ce_tradingsymbol": "SILVERM26NOV73000CE",
        },
        {
            "strike": 74000,
            "pe_oi": 2200,
            "pe_ltp": 1000.0,
            "pe_delta": -0.35,
            "pe_tradingsymbol": "SILVERM26NOV74000PE",
            "ce_oi": 250,
            "ce_ltp": 2500.0,
            "ce_delta": 0.65,
            "ce_tradingsymbol": "SILVERM26NOV74000CE",
        },
        {
            "strike": 75000,
            "pe_oi": 3000,
            "pe_ltp": 1500.0,
            "pe_delta": -0.50,
            "pe_tradingsymbol": "SILVERM26NOV75000PE",
            "ce_oi": 3000,
            "ce_ltp": 1500.0,
            "ce_delta": 0.50,
            "ce_tradingsymbol": "SILVERM26NOV75000CE",
        },
        {
            "strike": 76000,
            "pe_oi": 250,
            "pe_ltp": 2500.0,
            "pe_delta": -0.65,
            "pe_tradingsymbol": "SILVERM26NOV76000PE",
            "ce_oi": 2200,
            "ce_ltp": 1000.0,
            "ce_delta": 0.35,
            "ce_tradingsymbol": "SILVERM26NOV76000CE",
        },
        {
            "strike": 77000,
            "pe_oi": 100,
            "pe_ltp": 3500.0,
            "pe_delta": -0.75,
            "pe_tradingsymbol": "SILVERM26NOV77000PE",
            "ce_oi": 1500,
            "ce_ltp": 700.0,
            "ce_delta": 0.25,
            "ce_tradingsymbol": "SILVERM26NOV77000CE",
        },
    ]


def test_engine_idle_when_no_signal(sample_chain):
    """Engine stays idle when technical indicators produce NO_SIGNAL."""
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_flat = _create_flat_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_flat,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 0


def test_engine_entry_bullish_momentum(sample_chain):
    """Bullish momentum signal triggers selling OTM Put (~0.35 delta)."""
    cfg = SilverConfig(target_delta=0.35, lot_size=5)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is not None
    assert engine.active_position.spread_type == SpreadType.SHORT_PUT
    assert engine.active_position.status == "ACTIVE"
    assert len(engine.active_position.legs) == 1

    leg = engine.active_position.legs[0]
    assert leg.strike == 74000
    assert leg.option_type == "PE"
    assert leg.action == "SELL"
    assert leg.premium == 1000.0
    assert leg.quantity == 10
    assert engine.active_position.entry_net_credit == 10000.0
    assert engine.active_position.sl_price == 1500.0


def test_engine_entry_bearish_momentum(sample_chain):
    """Bearish momentum signal triggers selling OTM Call (~0.35 delta)."""
    cfg = SilverConfig(target_delta=0.35, lot_size=5)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_down = _create_downtrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_down,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is not None
    assert engine.active_position.spread_type == SpreadType.SHORT_CALL
    assert engine.active_position.status == "ACTIVE"
    assert len(engine.active_position.legs) == 1

    leg = engine.active_position.legs[0]
    assert leg.strike == 76000
    assert leg.option_type == "CE"
    assert leg.action == "SELL"
    assert leg.premium == 1000.0
    assert leg.quantity == 10
    assert engine.active_position.entry_net_credit == 10000.0
    assert engine.active_position.sl_price == 1500.0


def test_engine_position_holding_during_price_decay(sample_chain):
    """Holding position as option premium decays (normal theta decay)."""
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # 1. Enter on bullish momentum
    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None

    # 2. Next tick: option price decays from 1000 to 700 (profitable theta decay)
    decayed_chain = [
        {"strike": 74000, "pe_ltp": 700.0, "ce_ltp": 2700.0},
    ]
    engine.on_market_tick(
        spot_price=75200.0,
        df_5m=df_up,
        chain_data=decayed_chain,
        current_time=time(11, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is not None
    assert engine.active_position.current_value == 700.0
    assert len(engine.closed_positions) == 0


def test_engine_exit_on_stop_loss(sample_chain):
    """Option price surges to 1.5x SL target -> exit triggered with realized loss."""
    cfg = SilverConfig(lot_size=5, sl_multiplier=1.5)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # 1. Entry at 1000.0 -> SL target = 1500.0
    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None

    # 2. Adverse surge: PE rises to 1550.0
    surge_chain = [
        {"strike": 74000, "pe_ltp": 1550.0},
    ]
    engine.on_market_tick(
        spot_price=73500.0,
        df_5m=df_up,
        chain_data=surge_chain,
        current_time=time(11, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    closed = engine.closed_positions[0]
    assert closed.status == "CLOSED"
    assert closed.exit_reason == "STOP_LOSS"
    # Realized PnL: (1000.0 - 1550.0) * 10 = -5500.0 (2 lots of 5kg = 10kg)
    assert closed.realized_pnl == -5500.0


def test_engine_exit_on_trend_reversal():
    """5 EMA crosses below 50 EMA with candle breakdown -> exits short put position."""
    cfg = SilverConfig(lot_size=5, ema_fast=5, ema_slow=50)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # Setup active short put position directly
    leg = OptionLeg(strike=74000, option_type="PE", action="SELL", quantity=5, premium=1000.0)
    engine.active_position = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[leg],
        entry_net_credit=5000.0,
        sl_price=1500.0,
        status="ACTIVE",
    )

    # Generate trend reversal dataset (bearish cross + lower low)
    closes = [1000.0] * 60 + [1000.0 - i * 20.0 for i in range(1, 16)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    df_rev = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    ema5 = compute_ema(df_rev["close"], span=5)
    ema50 = compute_ema(df_rev["close"], span=50)
    cross_idx = None
    for i in range(1, len(df_rev)):
        if ema5.iloc[i] < ema50.iloc[i] and ema5.iloc[i - 1] >= ema50.iloc[i - 1]:
            cross_idx = i
            break
    assert cross_idx is not None

    df_cross = df_rev.iloc[: cross_idx + 1].copy().reset_index(drop=True)
    df_cross.loc[len(df_cross) - 1, "low"] = df_cross.loc[len(df_cross) - 2, "low"] - 5.0

    # Option price is 1100.0 (below 1500.0 SL target, so exit is purely due to TREND_REVERSAL)
    chain_tick = [{"strike": 74000, "pe_ltp": 1100.0}]
    engine.on_market_tick(
        spot_price=74000.0,
        df_5m=df_cross,
        chain_data=chain_tick,
        current_time=time(12, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    closed = engine.closed_positions[0]
    assert closed.status == "CLOSED"
    assert closed.exit_reason == "TREND_REVERSAL"
    # Realized PnL: (1000.0 - 1100.0) * 5 = -500.0
    assert closed.realized_pnl == -500.0


def test_engine_exit_on_session_end(sample_chain):
    """Session end cutoff at 18:00 closes position to avoid high overnight/US session volatility."""
    cfg = SilverConfig(session_end=time(18, 0), lot_size=5)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # 1. Entry at 1000.0
    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None

    # 2. Market tick at 18:00
    chain_tick = [{"strike": 74000, "pe_ltp": 400.0}]
    engine.on_market_tick(
        spot_price=75500.0,
        df_5m=df_up,
        chain_data=chain_tick,
        current_time=time(18, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    closed = engine.closed_positions[0]
    assert closed.status == "CLOSED"
    assert closed.exit_reason == "SESSION_CLOSE"
    # Realized PnL: (1000.0 - 400.0) * 10 = +6000.0 (2 lots of 5kg = 10kg)
    assert closed.realized_pnl == 6000.0


def test_engine_exit_on_intraday_eod(sample_chain):
    """Intraday EOD cutoff at 23:15 forces exit if position remains open."""
    cfg = SilverConfig(eod_cutoff=time(23, 15), session_end=time(23, 30), lot_size=5)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None

    chain_tick = [{"strike": 74000, "pe_ltp": 200.0}]
    engine.on_market_tick(
        spot_price=75500.0,
        df_5m=df_up,
        chain_data=chain_tick,
        current_time=time(23, 15),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].exit_reason == "INTRADAY_EOD"


def test_engine_exit_on_mandatory_devolvement_cutoff(sample_chain):
    """Expiry day devolvement cutoff at 15:30 triggers exit."""
    cfg = SilverConfig(devolvement_cutoff=time(15, 30))
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=True,
    )
    assert engine.active_position is not None

    chain_tick = [{"strike": 74000, "pe_ltp": 300.0}]
    engine.on_market_tick(
        spot_price=75500.0,
        df_5m=df_up,
        chain_data=chain_tick,
        current_time=time(15, 35),
        is_expiry_day=True,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].exit_reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"


def test_engine_exit_execution_failure_leaves_exit_failed(sample_chain):
    """If exit order execution fails, active position status becomes EXIT_FAILED."""
    cfg = SilverConfig(session_end=time(18, 0))
    mock_kite = MagicMock()
    # Live mode: entry succeeds, but exit fails
    mock_kite.place_order.side_effect = ["ORD_ENTRY_1", Exception("Order rejected by RMS")]
    engine = SilverOptionsEngine(kite_client=mock_kite, config=cfg, is_paper=False)

    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None
    assert engine.active_position.status == "ACTIVE"

    # Trigger exit condition
    chain_tick = [{"strike": 74000, "pe_ltp": 400.0}]
    engine.on_market_tick(
        spot_price=75500.0,
        df_5m=df_up,
        chain_data=chain_tick,
        current_time=time(18, 0),
        is_expiry_day=False,
    )

    # Position is retained for retry
    assert engine.active_position is not None
    assert engine.active_position.status == "EXIT_FAILED"
    assert len(engine.closed_positions) == 0


def test_engine_live_execution_full_cycle(sample_chain):
    """Live mode execution places orders with Kite Connect on entry and exit."""
    cfg = SilverConfig(session_end=time(18, 0), lot_size=5)
    mock_kite = MagicMock()
    mock_kite.place_order.side_effect = ["ORD_ENTRY_1", "ORD_EXIT_1"]
    engine = SilverOptionsEngine(kite_client=mock_kite, config=cfg, is_paper=False)

    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None
    assert mock_kite.place_order.call_count == 1

    # Exit at session end
    chain_tick = [{"strike": 74000, "pe_ltp": 500.0}]
    engine.on_market_tick(
        spot_price=75500.0,
        df_5m=df_up,
        chain_data=chain_tick,
        current_time=time(18, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is None
    assert mock_kite.place_order.call_count == 2
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].realized_pnl == (1000.0 - 500.0) * 10


def test_engine_backward_compatibility_legacy_kwargs(sample_chain):
    """Verify backward compatibility when callers supply daily_df via kwargs, or omit optional args."""
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_up = _create_uptrend_df(80)
    # Call using daily_df kwarg, omit chain_data and current_time
    engine.on_market_tick(
        spot_price=75000.0,
        daily_df=df_up,
        chain_data=sample_chain,
    )
    assert engine.active_position is not None
    assert engine.active_position.status == "ACTIVE"


def test_engine_option_price_fallback_when_not_in_chain():
    """If strike is absent from chain_data, fallback to leg.premium."""
    cfg = SilverConfig(sl_multiplier=1.5)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    leg = OptionLeg(strike=74000, option_type="PE", action="SELL", quantity=5, premium=1000.0)
    engine.active_position = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[leg],
        entry_net_credit=5000.0,
        sl_price=1500.0,
        status="ACTIVE",
    )

    # Empty chain: price falls back to leg.premium (1000.0), no exit triggered
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=None,
        chain_data=[],
        current_time=time(11, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is not None
    assert engine.active_position.current_value == 1000.0


def test_engine_daily_max_loss_circuit_breaker(sample_chain):
    """When cumulative daily loss reaches daily_max_loss (e.g. 30,000 for 2 lots), trading is halted."""
    cfg = SilverConfig(lots=2, lot_size=5, daily_max_loss_per_lot=15000.0)
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    df_up = _create_uptrend_df(80)
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(10, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None

    # Catastrophic surge: PE spikes to 4500.0 -> loss of (1000 - 4500) * 10 = -35000.0
    surge_chain = [{"strike": 74000, "pe_ltp": 4500.0}]
    engine.on_market_tick(
        spot_price=70000.0,
        df_5m=df_up,
        chain_data=surge_chain,
        current_time=time(11, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].realized_pnl == -35000.0
    assert engine.trading_halted_for_day is True

    # Subsequent tick with strong signal should NOT enter because trading is halted
    engine.on_market_tick(
        spot_price=75000.0,
        df_5m=df_up,
        chain_data=sample_chain,
        current_time=time(12, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is None
