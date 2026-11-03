from pathlib import Path
from unittest.mock import MagicMock
import pytest
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk

def test_paper_order_execution():
    om = OrderManager(mode="PAPER", kite=None)
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=750, price=32.0)
    assert order["status"] == "COMPLETE"
    assert order["mode"] == "PAPER"
    assert order["symbol"] == "NIFTY26NOV24200CE"
    assert order["fill_price"] == 32.0
    assert order["transaction_type"] == "SELL"
    assert order["quantity"] == 750

def test_live_order_execution():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_12345"
    
    om = OrderManager(mode="LIVE", kite=mock_kite)
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=750, price=32.0)
    
    assert order["order_id"] == "ORDER_12345"
    assert order["mode"] == "LIVE"
    assert order["status"] == "SUBMITTED"
    mock_kite.place_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        exchange=mock_kite.EXCHANGE_NFO,
        tradingsymbol="NIFTY26NOV24200CE",
        transaction_type=mock_kite.TRANSACTION_TYPE_SELL,
        quantity=750,
        product=mock_kite.PRODUCT_MIS,
        order_type=mock_kite.ORDER_TYPE_MARKET
    )

def test_order_manager_live_without_kite_raises():
    with pytest.raises(ValueError, match="KiteConnect instance required for LIVE mode"):
        OrderManager(mode="LIVE", kite=None)

def test_paper_buy_to_cover():
    om = OrderManager(mode="PAPER", kite=None)
    order = om.buy_to_cover(symbol="NIFTY26NOV24200CE", quantity=750, price=48.0, reason="STOP_LOSS")
    assert order["status"] == "COMPLETE"
    assert order["mode"] == "PAPER"
    assert order["symbol"] == "NIFTY26NOV24200CE"
    assert order["fill_price"] == 48.0
    assert order["reason"] == "STOP_LOSS"
    assert order["transaction_type"] == "BUY"

def test_live_buy_to_cover():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_67890"

    om = OrderManager(mode="LIVE", kite=mock_kite)
    order = om.buy_to_cover(symbol="NIFTY26NOV23700PE", quantity=750, price=6.2, reason="TARGET")
    assert order["order_id"] == "ORDER_67890"
    assert order["mode"] == "LIVE"
    assert order["status"] == "SUBMITTED"
    assert order["reason"] == "TARGET"
    mock_kite.place_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        exchange=mock_kite.EXCHANGE_NFO,
        tradingsymbol="NIFTY26NOV23700PE",
        transaction_type=mock_kite.TRANSACTION_TYPE_BUY,
        quantity=750,
        product=mock_kite.PRODUCT_MIS,
        order_type=mock_kite.ORDER_TYPE_MARKET
    )

def test_desk_state_persistence_and_recovery(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    trade_setup = {
        "date": "2026-11-03",
        "status": "OPEN",
        "ce": {"symbol": "NIFTY26NOV24200CE", "entry": 32.0, "sl": 48.0, "target": 6.4, "status": "OPEN", "qty": 750},
        "pe": {"symbol": "NIFTY26NOV23700PE", "entry": 31.0, "sl": 46.5, "target": 6.2, "status": "OPEN", "qty": 750}
    }
    desk.save_state(trade_setup)
    assert state_file.exists()

    # Load recovered state in a fresh instance
    desk2 = StrangleDesk(order_manager=om, state_file=state_file)
    recovered = desk2.load_state()
    assert recovered["ce"]["entry"] == 32.0
    assert recovered["status"] == "OPEN"

def test_desk_load_state_nonexistent(tmp_path):
    state_file = tmp_path / "nonexistent.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = StrangleDesk(order_manager=om, state_file=state_file)
    assert desk.load_state() is None

def test_desk_load_state_corrupt(tmp_path):
    state_file = tmp_path / "corrupt.json"
    state_file.write_text("{invalid json", encoding="utf-8")
    om = OrderManager(mode="PAPER", kite=None)
    desk = StrangleDesk(order_manager=om, state_file=state_file)
    assert desk.load_state() is None

def test_desk_clear_state(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = StrangleDesk(order_manager=om, state_file=state_file)
    desk.save_state({"test": 123})
    assert state_file.exists()
    assert desk.state == {"test": 123}

    desk.clear_state()
    assert not state_file.exists()
    assert desk.state is None

    # Clearing again when file does not exist should not raise
    desk.clear_state()
