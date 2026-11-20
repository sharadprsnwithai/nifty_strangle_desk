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
    assert cfg.symbol == "SILVERM"
    assert cfg.lot_size == 5
    assert cfg.strike_step == 1000
    assert cfg.market_open == time(9, 0)
    assert cfg.market_close == time(23, 30)
    assert cfg.devolvement_cutoff == time(15, 30)
    assert cfg.max_debit_width_ratio == 0.35
    assert cfg.take_profit_ratio == 0.75
    assert cfg.stop_loss_ratio == 0.50

def test_option_chain_profile_and_spread_position():
    profile = OptionChainProfile(
        spot_price=153800.0,
        pcr=0.77,
        max_pain=155000,
        call_wall=160000,
        put_base=148000,
        atm_strike=154000,
    )
    assert profile.pcr == 0.77
    assert profile.max_pain == 155000

    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0)
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0)
    pos = SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg1, leg2],
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
    )
    assert pos.spread_type == SpreadType.BULL_CALL_SPREAD
    assert pos.entry_net_debit == 800.0
    assert pos.status == "PENDING"
