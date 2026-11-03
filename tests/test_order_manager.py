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
    # Default is LIMIT with 5% buffer: 32.0 * 0.95 = 30.40
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=650, price=32.0, order_type="LIMIT", buffer_pct=0.05)
    
    assert order["order_id"] == "ORDER_12345"
    assert order["mode"] == "LIVE"
    assert order["status"] == "SUBMITTED"
    assert order["order_type"] == "LIMIT"
    assert order["limit_price"] == 30.40
    mock_kite.place_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        exchange=mock_kite.EXCHANGE_NFO,
        tradingsymbol="NIFTY26NOV24200CE",
        transaction_type=mock_kite.TRANSACTION_TYPE_SELL,
        quantity=650,
        product=mock_kite.PRODUCT_MIS,
        order_type=mock_kite.ORDER_TYPE_LIMIT,
        price=30.40
    )


def test_live_order_execution_market_mode():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_MKT"

    om = OrderManager(mode="LIVE", kite=mock_kite)
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=650, price=32.0, order_type="MARKET")

    assert order["order_type"] == "MARKET"
    assert order["limit_price"] is None
    mock_kite.place_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        exchange=mock_kite.EXCHANGE_NFO,
        tradingsymbol="NIFTY26NOV24200CE",
        transaction_type=mock_kite.TRANSACTION_TYPE_SELL,
        quantity=650,
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
    # Default is LIMIT with 5% buffer: 6.20 * 1.05 = 6.51 -> rounded to tick 6.50
    order = om.buy_to_cover(symbol="NIFTY26NOV23700PE", quantity=650, price=6.2, reason="TARGET", order_type="LIMIT", buffer_pct=0.05)
    assert order["order_id"] == "ORDER_67890"
    assert order["mode"] == "LIVE"
    assert order["status"] == "SUBMITTED"
    assert order["reason"] == "TARGET"
    assert order["order_type"] == "LIMIT"
    assert order["limit_price"] == 6.50
    mock_kite.place_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        exchange=mock_kite.EXCHANGE_NFO,
        tradingsymbol="NIFTY26NOV23700PE",
        transaction_type=mock_kite.TRANSACTION_TYPE_BUY,
        quantity=650,
        product=mock_kite.PRODUCT_MIS,
        order_type=mock_kite.ORDER_TYPE_LIMIT,
        price=6.50
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


def test_order_manager_paper_get_order_status_and_cancel():
    om = OrderManager(mode="PAPER", kite=None)
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=650, price=32.0)
    oid = order["order_id"]

    status = om.get_order_status(oid)
    assert status["order_id"] == oid
    assert status["status"] == "COMPLETE"
    assert status["filled_quantity"] == 650
    assert status["pending_quantity"] == 0
    assert status["average_price"] == 32.0

    # Cancel paper order
    res = om.cancel_order(oid)
    assert res is True
    cancelled_status = om.get_order_status(oid)
    assert cancelled_status["status"] == "CANCELLED"
    assert cancelled_status["pending_quantity"] == 0


def test_order_manager_live_get_order_status_and_cancel():
    mock_kite = MagicMock()
    mock_kite.order_history.return_value = [
        {"order_id": "ORD_LIVE_1", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 650, "average_price": 0.0},
        {"order_id": "ORD_LIVE_1", "status": "COMPLETE", "filled_quantity": 650, "pending_quantity": 0, "average_price": 31.85, "status_message": None}
    ]

    om = OrderManager(mode="LIVE", kite=mock_kite)
    status = om.get_order_status("ORD_LIVE_1")

    assert status["order_id"] == "ORD_LIVE_1"
    assert status["status"] == "COMPLETE"
    assert status["filled_quantity"] == 650
    assert status["pending_quantity"] == 0
    assert status["average_price"] == 31.85
    mock_kite.order_history.assert_called_once_with("ORD_LIVE_1")

    # Test cancel
    mock_kite.cancel_order.return_value = {"order_id": "ORD_LIVE_1"}
    cancelled = om.cancel_order("ORD_LIVE_1")
    assert cancelled is True
    mock_kite.cancel_order.assert_called_once_with(variety="regular", order_id="ORD_LIVE_1")

    # Test error handling when Kite API raises
    mock_kite.order_history.side_effect = Exception("Network timeout")
    err_status = om.get_order_status("ORD_LIVE_1")
    assert err_status["status"] == "ERROR"
    assert "Network timeout" in err_status["status_message"]

    mock_kite.cancel_order.side_effect = Exception("Order already executed")
    err_cancel = om.cancel_order("ORD_LIVE_1")
    assert err_cancel is False


def test_order_manager_product_parameter():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_NRML_1"
    mock_kite.PRODUCT_NRML = "NRML"

    om = OrderManager(mode="LIVE", kite=mock_kite, product="NRML")
    assert om.product == "NRML"

    om.sell("NIFTY26NOV24000CE", 65, 50.0)
    assert mock_kite.place_order.call_args[1]["product"] == "NRML"

    om.buy_to_cover("NIFTY26NOV24000CE", 65, 50.0)
    assert mock_kite.place_order.call_args[1]["product"] == "NRML"


def test_order_manager_invalid_product():
    with pytest.raises(ValueError, match="Invalid product type"):
        OrderManager(mode="PAPER", product="INVALID")

