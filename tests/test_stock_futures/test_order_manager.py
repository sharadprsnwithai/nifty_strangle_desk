import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.models import TradeSignal, SignalType
from strategy.stock_futures.order_manager import StockFuturesOrderManager, round_to_nse_tick


def test_round_to_nse_tick():
    assert round_to_nse_tick(100.02) == 100.00
    assert round_to_nse_tick(100.03) == 100.05
    assert round_to_nse_tick(100.08) == 100.10


def test_order_manager_live_marketable_limit(tmp_path):
    state_file = tmp_path / "live_test_trades.json"
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "241121000123"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.PRODUCT_MIS = "MIS"
    mock_kite.TRANSACTION_TYPE_BUY = "BUY"
    mock_kite.TRANSACTION_TYPE_SELL = "SELL"
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.EXCHANGE_NFO = "NFO"
    mock_kite.VALIDITY_DAY = "DAY"

    cfg = StockFuturesConfig(
        lots_per_trade=2,
        order_type="LIMIT",
        limit_buffer_pct=0.05,
        state_file=state_file
    )
    om = StockFuturesOrderManager(mode="LIVE", kite=mock_kite, config=cfg, state_file=state_file)

    signal = TradeSignal(
        symbol="ABB",
        side=SignalType.LONG,
        trigger_time="10:15",
        entry_price=1000.0,
        sl_price=990.0,
        target_price=1015.0,
        volume_ratio=2.0,
        c1_high=1000.0,
        c1_low=980.0,
    )

    pos = om.enter_position(signal)
    assert pos is not None
    assert mock_kite.place_order.called
    kwargs = mock_kite.place_order.call_args.kwargs
    assert kwargs["order_type"] == "LIMIT"
    # Marketable buy limit buffer is +5%: 1000 * 1.05 = 1050.0
    assert kwargs["price"] == 1050.0
    assert kwargs["quantity"] == 250

    # Test exit order
    om.exit_position("ABB", exit_price=1015.0, exit_reason="TARGET_HIT", exit_time="11:30")
    exit_kwargs = mock_kite.place_order.call_args.kwargs
    assert exit_kwargs["order_type"] == "LIMIT"
    # Marketable sell limit buffer is -5%: 1015 * 0.95 = 964.25
    assert exit_kwargs["price"] == 964.25
    assert exit_kwargs["transaction_type"] == "SELL"


def test_order_manager_paper_trade_lifecycle(tmp_path):
    state_file = tmp_path / "test_trades.json"
    universe_file = tmp_path / "empty_universe.json"
    cfg = StockFuturesConfig(lots_per_trade=2, state_file=state_file, universe_file=universe_file)
    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=state_file)

    signal = TradeSignal(
        symbol="MCX",
        side=SignalType.LONG,
        trigger_time="10:15",
        entry_price=2000.0,
        sl_price=1980.0,
        target_price=2030.0,
        volume_ratio=2.5,
        c1_high=2000.0,
        c1_low=1950.0,
    )

    pos = om.enter_position(signal)
    assert pos is not None
    assert pos.symbol == "MCX"
    assert pos.quantity == cfg.get_unit_quantity("MCX")
    assert pos.status == "OPEN"
    assert state_file.exists()

    # Close position
    closed_pos = om.exit_position("MCX", exit_price=2030.0, exit_reason="TARGET_HIT", exit_time="12:00")
    assert closed_pos is not None
    assert closed_pos.status == "CLOSED"
    assert closed_pos.gross_pnl == (2030.0 - 2000.0) * cfg.get_unit_quantity("MCX")
    assert closed_pos.net_pnl > 0

    # Test state reload
    om_new = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=state_file)
    assert len(om_new.trades_history) == 1
    assert om_new.trades_history[0]["symbol"] == "MCX"


def test_order_manager_hard_concurrency_limit(tmp_path):
    state_file = tmp_path / "concurrency_test_trades.json"
    cfg = StockFuturesConfig(
        lots_per_trade=2,
        max_concurrent_positions=2,
        state_file=state_file,
    )
    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=state_file)

    def create_signal(sym, price):
        return TradeSignal(
            symbol=sym,
            side=SignalType.LONG,
            trigger_time="10:30",
            entry_price=price,
            sl_price=price * 0.99,
            target_price=price * 1.0225,
            volume_ratio=2.0,
            c1_high=price,
            c1_low=price * 0.98,
        )

    # 1st position: Should succeed
    pos1 = om.enter_position(create_signal("ABB", 5000.0))
    assert pos1 is not None
    assert pos1.status == "OPEN"
    assert len([p for p in om.positions.values() if p.status == "OPEN"]) == 1

    # 2nd position: Should succeed (limit is 2)
    pos2 = om.enter_position(create_signal("MCX", 2500.0))
    assert pos2 is not None
    assert pos2.status == "OPEN"
    assert len([p for p in om.positions.values() if p.status == "OPEN"]) == 2

    # 3rd position: Must be rejected by hard concurrency guard!
    pos3 = om.enter_position(create_signal("BSE", 3000.0))
    assert pos3 is None
    assert len([p for p in om.positions.values() if p.status == "OPEN"]) == 2
    assert "BSE" not in om.positions

    # Close one position (ABB)
    closed = om.exit_position("ABB", exit_price=5112.5, exit_reason="TARGET_HIT", exit_time="11:45")
    assert closed is not None
    assert len([p for p in om.positions.values() if p.status == "OPEN"]) == 1

    # 3rd position (BSE) can now enter successfully!
    pos3_retry = om.enter_position(create_signal("BSE", 3000.0))
    assert pos3_retry is not None
    assert pos3_retry.status == "OPEN"
    assert len([p for p in om.positions.values() if p.status == "OPEN"]) == 2

    # 4th position (IDEA): Must be rejected again as open positions = 2
    pos4 = om.enter_position(create_signal("IDEA", 10.0))
    assert pos4 is None
    assert len([p for p in om.positions.values() if p.status == "OPEN"]) == 2

