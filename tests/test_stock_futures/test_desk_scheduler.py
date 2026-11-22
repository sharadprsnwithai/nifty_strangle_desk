import pytest
from datetime import datetime, date
from unittest.mock import MagicMock, patch

from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.order_manager import StockFuturesOrderManager
from strategy.stock_futures.desk import StockFuturesDesk

def test_desk_run_loop_skips_on_holiday(tmp_path):
    cfg = StockFuturesConfig(universe_file=tmp_path / "empty.json")
    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=tmp_path / "state.json")
    desk = StockFuturesDesk(order_manager=om, config=cfg)

    with patch("strategy.holidays.is_trading_holiday", return_value=(True, "Weekend (Saturday)")):
        with patch("time.sleep") as mock_sleep:
            desk.run_loop(poll_seconds=15, max_iterations=1)
            mock_sleep.assert_called_with(30)
            assert desk._last_holiday_date == datetime.now().strftime("%Y-%m-%d")

def test_desk_run_loop_handles_ignore_holidays(tmp_path):
    cfg = StockFuturesConfig(universe_file=tmp_path / "empty.json")
    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=tmp_path / "state.json")
    desk = StockFuturesDesk(order_manager=om, config=cfg)

    with patch("strategy.holidays.is_trading_holiday", return_value=(True, "Weekend (Saturday)")):
        with patch("time.sleep") as mock_sleep:
            # Should ignore the holiday and proceed into market check
            desk.run_loop(poll_seconds=15, ignore_holidays=True, max_iterations=1)
            mock_sleep.assert_called()


def test_desk_scan_and_enter_respects_max_concurrent(tmp_path):
    cfg = StockFuturesConfig(
        max_concurrent_positions=2,
        universe_file=tmp_path / "empty.json",
        entry_start_time="00:00",
        entry_cutoff_time="23:59",
    )
    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=tmp_path / "state.json")
    desk = StockFuturesDesk(order_manager=om, config=cfg)

    # Pre-populate 2 open positions
    from strategy.stock_futures.models import StockPosition
    om.positions["ABB"] = StockPosition(
        symbol="ABB", tradingsymbol="ABB_FUT", side="LONG", quantity=250, lots=2,
        entry_time="10:30", entry_price=1000.0, sl_price=990.0, target_price=1022.5, current_price=1000.0,
        status="OPEN"
    )
    om.positions["MCX"] = StockPosition(
        symbol="MCX", tradingsymbol="MCX_FUT", side="LONG", quantity=500, lots=2,
        entry_time="10:30", entry_price=2000.0, sl_price=1980.0, target_price=2045.0, current_price=2000.0,
        status="OPEN"
    )

    # Calling scan_and_enter should immediately return [] without querying further
    entries = desk.scan_and_enter()
    assert entries == []

