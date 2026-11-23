import pytest
from unittest.mock import MagicMock
from strategy.silver_options.order_manager import SilverOrderManager, _round_to_mcx_tick
from strategy.silver_options.config import SilverOptionsConfig


def test_round_to_mcx_tick():
    assert _round_to_mcx_tick(150.23, tick=0.5) == 150.0
    assert _round_to_mcx_tick(150.35, tick=0.5) == 150.5
    assert _round_to_mcx_tick(150.78, tick=0.5) == 151.0


def test_order_manager_paper_sell_and_buy():
    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="PAPER", config=config)

    # Test Sell
    sell_order = om.sell(
        symbol="SILVERM26NOV160000CE",
        quantity=2,
        price=1500.0,
        order_type="LIMIT",
        buffer_pct=0.05,
    )
    assert sell_order["status"] == "COMPLETE"
    assert sell_order["transaction_type"] == "SELL"
    assert sell_order["quantity"] == 2
    assert sell_order["fill_price"] == 1500.0
    # Limit price placed below LTP (1500 - 75 = 1425)
    assert sell_order["limit_price"] == 1425.0

    # Test Buy (Square-off)
    buy_order = om.buy(
        symbol="SILVERM26NOV160000CE",
        quantity=2,
        price=1100.0,
        order_type="LIMIT",
        buffer_pct=0.05,
    )
    assert buy_order["status"] == "COMPLETE"
    assert buy_order["transaction_type"] == "BUY"
    assert buy_order["quantity"] == 2
    assert buy_order["fill_price"] == 1100.0
    # Limit price placed above LTP (1100 + 55 = 1155)
    assert buy_order["limit_price"] == 1155.0


def test_order_manager_live_mcx_execution():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_MCX_12345"
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.EXCHANGE_MCX = "MCX"
    mock_kite.TRANSACTION_TYPE_SELL = "SELL"
    mock_kite.TRANSACTION_TYPE_BUY = "BUY"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.PRODUCT_MIS = "MIS"
    mock_kite.VALIDITY_DAY = "DAY"

    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)

    order = om.sell(
        symbol="SILVERM26NOV160000CE",
        quantity=2,
        price=1500.0,
        order_type="LIMIT",
    )
    assert order["status"] == "SUBMITTED"
    assert order["order_id"] == "ORDER_MCX_12345"
    mock_kite.place_order.assert_called_once()
    call_kwargs = mock_kite.place_order.call_args.kwargs
    assert call_kwargs["exchange"] == "MCX"
    assert call_kwargs["tradingsymbol"] == "SILVERM26NOV160000CE"
    assert call_kwargs["quantity"] == 2
