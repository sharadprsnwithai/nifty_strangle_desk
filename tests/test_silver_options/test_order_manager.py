from unittest.mock import MagicMock
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadType, OptionLeg, SpreadPosition
from strategy.silver_options.contract_resolver import resolve_silver_symbols
from strategy.silver_options.order_manager import execute_spread_entry, execute_spread_exit

def test_resolve_silver_symbols():
    mock_instruments = [
        {"tradingsymbol": "SILVERM26NOVFUT", "instrument_type": "FUT", "segment": "MCX-FUT", "name": "SILVERM"},
        {"tradingsymbol": "SILVERM26NOV155000CE", "instrument_type": "CE", "strike": 155000, "segment": "MCX-OPT", "name": "SILVERM"},
        {"tradingsymbol": "SILVERM26NOV160000CE", "instrument_type": "CE", "strike": 160000, "segment": "MCX-OPT", "name": "SILVERM"},
        {"tradingsymbol": "GOLDM26NOVFUT", "instrument_type": "FUT", "segment": "MCX-FUT", "name": "GOLDM"},
    ]
    resolved = resolve_silver_symbols(mock_instruments, underlying="SILVERM")
    assert len(resolved) == 3
    assert all("SILVERM" in item["tradingsymbol"] for item in resolved)

def test_paper_execution_lifecycle():
    kite_mock = MagicMock()
    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0, trading_symbol="SILVERM26NOV155000CE")
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0, trading_symbol="SILVERM26NOV160000CE")
    spread = SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg1, leg2],
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
    )

    success_entry = execute_spread_entry(kite_mock, spread, is_paper=True)
    assert success_entry is True
    assert spread.status == "ACTIVE"
    # Kite API should NOT be called in paper mode
    assert kite_mock.place_order.call_count == 0

    success_exit = execute_spread_exit(kite_mock, spread, reason="TAKE_PROFIT", is_paper=True)
    assert success_exit is True
    assert spread.status == "CLOSED"
    assert spread.exit_reason == "TAKE_PROFIT"

def test_live_execution_lifecycle():
    kite_mock = MagicMock()
    kite_mock.place_order.side_effect = ["ORD_BUY_1", "ORD_SELL_1"]

    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0, trading_symbol="SILVERM26NOV155000CE")
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0, trading_symbol="SILVERM26NOV160000CE")
    # Pass legs in reverse order to ensure sequence_entry_legs places BUY first
    spread = SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg2, leg1],
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
    )

    success_entry = execute_spread_entry(kite_mock, spread, is_paper=False)
    assert success_entry is True
    assert spread.status == "ACTIVE"
    assert kite_mock.place_order.call_count == 2
    # Verify first call is BUY (hedge first)
    first_call = kite_mock.place_order.call_args_list[0].kwargs
    assert first_call["tradingsymbol"] == "SILVERM26NOV155000CE"
    assert first_call["transaction_type"] == "BUY"
    assert leg1.order_id == "ORD_BUY_1"

    # Verify second call is SELL
    second_call = kite_mock.place_order.call_args_list[1].kwargs
    assert second_call["tradingsymbol"] == "SILVERM26NOV160000CE"
    assert second_call["transaction_type"] == "SELL"
    assert leg2.order_id == "ORD_SELL_1"

    # Now test live exit: short leg (SELL leg) should be closed first via BUY
    kite_mock.reset_mock()
    kite_mock.place_order.side_effect = ["ORD_CLOSE_SHORT", "ORD_CLOSE_LONG"]
    success_exit = execute_spread_exit(kite_mock, spread, reason="TAKE_PROFIT", is_paper=False)
    assert success_exit is True
    assert spread.status == "CLOSED"
    assert spread.exit_reason == "TAKE_PROFIT"
    assert kite_mock.place_order.call_count == 2

    # First exit order should close the short leg with BUY
    exit_call_1 = kite_mock.place_order.call_args_list[0].kwargs
    assert exit_call_1["tradingsymbol"] == "SILVERM26NOV160000CE"
    assert exit_call_1["transaction_type"] == "BUY"

    # Second exit order should close the long leg with SELL
    exit_call_2 = kite_mock.place_order.call_args_list[1].kwargs
    assert exit_call_2["tradingsymbol"] == "SILVERM26NOV155000CE"
    assert exit_call_2["transaction_type"] == "SELL"

def test_live_execution_failure_handling():
    kite_mock = MagicMock()
    kite_mock.place_order.side_effect = RuntimeError("RMS Margin Error")

    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0, trading_symbol="SILVERM26NOV155000CE")
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0, trading_symbol="SILVERM26NOV160000CE")
    spread = SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg1, leg2],
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
    )

    success = execute_spread_entry(kite_mock, spread, is_paper=False)
    assert success is False
    assert spread.status == "FAILED"

    # Exit failure
    success_exit = execute_spread_exit(kite_mock, spread, reason="STOP_LOSS", is_paper=False)
    assert success_exit is False
