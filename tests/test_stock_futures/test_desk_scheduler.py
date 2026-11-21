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
