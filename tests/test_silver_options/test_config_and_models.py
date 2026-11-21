import pytest
from datetime import time
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import (
    SignalType,
    SpreadType,
    OptionLeg,
    OptionChainProfile,
    SpreadPosition,
)


def test_silver_config_defaults():
    cfg = SilverConfig()
    # Contract attributes
    assert cfg.symbol == "SILVERM"
    assert cfg.exchange == "MCX"
    assert cfg.lot_size == 5
    assert cfg.strike_step == 1000
    assert cfg.tick_size == 1.0
    assert cfg.market_open == time(9, 0)
    assert cfg.market_close == time(23, 30)
    assert cfg.devolvement_cutoff == time(15, 30)

    # Williams %R and Triple EMA parameters
    assert cfg.williams_r_period == 70
    assert cfg.ema_fast == 5
    assert cfg.ema_mid == 15
    assert cfg.ema_slow == 50
    assert cfg.target_delta == 0.35
    assert cfg.sl_multiplier == 1.5
    assert cfg.session_start == time(9, 15)
    assert cfg.session_end == time(18, 0)
    assert cfg.eod_cutoff == time(23, 15)
    assert cfg.slippage_buffer_ticks == 2

    # Verify legacy attributes are removed
    legacy_attrs = [
        "daily_rsi_period",
        "daily_rsi_oversold",
        "daily_rsi_overbought",
        "hourly_ema_period",
        "max_debit_width_ratio",
        "take_profit_ratio",
        "stop_loss_ratio",
    ]
    for attr in legacy_attrs:
        assert not hasattr(cfg, attr), f"Legacy attribute {attr} should be removed"


def test_signal_type_enum():
    assert SignalType.BULLISH_MOMENTUM == "BULLISH_MOMENTUM"
    assert SignalType.BEARISH_MOMENTUM == "BEARISH_MOMENTUM"
    assert SignalType.NO_SIGNAL == "NO_SIGNAL"

    # Backward compatibility aliases
    assert SignalType.BULLISH_REVERSAL == SignalType.BULLISH_MOMENTUM
    assert SignalType.BEARISH_BREAKDOWN == SignalType.BEARISH_MOMENTUM
    assert SignalType("BULLISH_MOMENTUM") == SignalType.BULLISH_MOMENTUM
    assert SignalType("BULLISH_REVERSAL") == SignalType.BULLISH_MOMENTUM


def test_spread_type_enum():
    assert SpreadType.SHORT_PUT == "SHORT_PUT"
    assert SpreadType.SHORT_CALL == "SHORT_CALL"
    assert SpreadType.BULL_PUT_SPREAD == "BULL_PUT_SPREAD"
    assert SpreadType.BEAR_CALL_SPREAD == "BEAR_CALL_SPREAD"

    # Backward compatibility aliases
    assert SpreadType.BULL_CALL_SPREAD == SpreadType.BULL_PUT_SPREAD
    assert SpreadType.BEAR_PUT_SPREAD == SpreadType.BEAR_CALL_SPREAD
    assert SpreadType("SHORT_PUT") == SpreadType.SHORT_PUT
    assert SpreadType("BULL_CALL_SPREAD") == SpreadType.BULL_PUT_SPREAD


def test_option_chain_profile_and_spread_position():
    profile = OptionChainProfile(
        spot_price=153800.0,
        pcr=0.77,
        max_pain=155000,
        call_wall=160000,
        put_base=148000,
        atm_strike=154000,
    )
    assert profile.spot_price == 153800.0
    assert profile.pcr == 0.77
    assert profile.max_pain == 155000
    assert profile.call_wall == 160000
    assert profile.put_base == 148000
    assert profile.atm_strike == 154000

    # Short Option selling position
    leg_sell = OptionLeg(
        strike=150000,
        option_type="PE",
        action="SELL",
        quantity=5,
        premium=1200.0,
        trading_symbol="SILVERM26FEB150000PE",
    )
    pos_sell = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[leg_sell],
        entry_net_credit=1200.0 * 5,
        max_profit=1200.0 * 5,
        target_delta=0.35,
        sl_price=1200.0 * 1.5,
    )
    assert pos_sell.spread_type == SpreadType.SHORT_PUT
    assert pos_sell.entry_net_credit == 6000.0
    assert pos_sell.entry_net_debit == 0.0
    assert pos_sell.max_profit == 6000.0
    assert pos_sell.target_delta == 0.35
    assert pos_sell.sl_price == 1800.0
    assert pos_sell.status == "PENDING"
    assert pos_sell.current_value == 0.0
    assert pos_sell.realized_pnl == 0.0
    assert pos_sell.exit_reason is None

    # Multi-leg spread position with credit
    leg_buy = OptionLeg(
        strike=148000,
        option_type="PE",
        action="BUY",
        quantity=5,
        premium=600.0,
        trading_symbol="SILVERM26FEB148000PE",
    )
    pos_spread = SpreadPosition(
        spread_type=SpreadType.BULL_PUT_SPREAD,
        legs=[leg_sell, leg_buy],
        entry_net_credit=3000.0,
        max_profit=3000.0,
        max_loss=7000.0,
    )
    assert pos_spread.spread_type == SpreadType.BULL_PUT_SPREAD
    assert len(pos_spread.legs) == 2
    assert pos_spread.entry_net_credit == 3000.0
