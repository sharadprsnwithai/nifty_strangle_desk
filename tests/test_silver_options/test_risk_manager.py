from datetime import time
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadType, OptionLeg, SpreadPosition
from strategy.silver_options.risk_manager import (
    sequence_entry_legs,
    sequence_exit_legs,
    check_exit_conditions,
)

@pytest.fixture
def sample_position():
    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0)
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0)
    return SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg2, leg1],  # Intentionally unordered
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
        status="ACTIVE",
    )

def test_hedge_first_entry_sequencing(sample_position):
    ordered = sequence_entry_legs(sample_position.legs)
    assert ordered[0].action == "BUY"
    assert ordered[1].action == "SELL"

def test_hedge_first_exit_sequencing(sample_position):
    # On exit, short leg must be closed first (BUY to cover), then long leg sold
    ordered = sequence_exit_legs(sample_position.legs)
    assert ordered[0].strike == 160000  # The short leg
    assert ordered[1].strike == 155000  # The long leg

def test_take_profit_trigger(sample_position):
    cfg = SilverConfig(take_profit_ratio=0.75)
    # Entry debit 800, max profit 4200. 75% target = 800 + (0.75 * 4200) = 3950
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=4000.0, current_time=time(14, 0), is_expiry_day=False, config=cfg
    )
    assert should_exit is True
    assert reason == "TAKE_PROFIT"

def test_stop_loss_trigger(sample_position):
    cfg = SilverConfig(stop_loss_ratio=0.50)
    # 50% loss on 800 debit = value falls to 400
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=350.0, current_time=time(14, 0), is_expiry_day=False, config=cfg
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"

def test_mandatory_devolvement_cutoff(sample_position):
    cfg = SilverConfig(devolvement_cutoff=time(15, 30))
    # On expiry day at 15:35, mandatory exit regardless of PnL
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=1200.0, current_time=time(15, 35), is_expiry_day=True, config=cfg
    )
    assert should_exit is True
    assert reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"

def test_no_exit_condition_met(sample_position):
    cfg = SilverConfig(take_profit_ratio=0.75, stop_loss_ratio=0.50, devolvement_cutoff=time(15, 30))
    # Spread value 1200 is between stop loss (400) and take profit (3950)
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=1200.0, current_time=time(14, 0), is_expiry_day=False, config=cfg
    )
    assert should_exit is False
    assert reason is None

def test_expiry_day_before_cutoff(sample_position):
    cfg = SilverConfig(take_profit_ratio=0.75, stop_loss_ratio=0.50, devolvement_cutoff=time(15, 30))
    # On expiry day at 15:20 (before 15:30 cutoff), no exit if PnL between bounds
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=1200.0, current_time=time(15, 20), is_expiry_day=True, config=cfg
    )
    assert should_exit is False
    assert reason is None
