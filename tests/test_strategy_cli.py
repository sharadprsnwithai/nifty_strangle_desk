from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, date, timedelta
import pytest
import pandas as pd
from run_backtest import run_backtest_cli, load_historical_nifty_data
from run_strategy import run_strategy_cli
from strategy.order_manager import OrderManager
from strategy.option_chain import resolve_target_expiry
from strategy.backtest import run_backtest_simulation


def test_run_backtest_cli(tmp_path):
    output_csv = tmp_path / "trade_log.csv"
    with patch("run_backtest.load_historical_nifty_data") as mock_data:
        mock_data.return_value = pd.DataFrame([
            {"timestamps": datetime(2026, 11, 2, 10, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0},
            {"timestamps": datetime(2026, 11, 2, 15, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0}
        ])
        
        exit_code = run_backtest_cli(["--days", "5", "--output", str(output_csv)])
        assert exit_code == 0
        assert output_csv.exists()


def test_run_backtest_cli_handles_error():
    with patch("run_backtest.load_historical_nifty_data", side_effect=RuntimeError("Data load error")):
        exit_code = run_backtest_cli(["--days", "5"])
        assert exit_code == 1


def test_load_historical_nifty_data_filter_days():
    # Create 30 days of dummy data
    base = datetime(2026, 11, 2, 10, 0)
    dates = [base - timedelta(days=i) for i in range(30)]
    df = pd.DataFrame([{"timestamps": d, "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0} for d in dates])

    with patch("run_backtest.pd.read_parquet", return_value=df):
        loaded_df = load_historical_nifty_data(days=5)
        # Should be filtered to recent 5 days + cutoff boundary
        assert len(loaded_df) <= 6
        assert len(loaded_df) < len(df)


def test_run_strategy_cli_paper_mode():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client"), \
         patch("run_strategy.find_strangle_strikes") as mock_find, \
         patch("run_strategy.StrangleDesk"):
        
        mock_find.return_value = {
            "expiry": "2026-11-05", "lot_size": 75, "total_lots": 10, "total_quantity": 750,
            "ce": {"symbol": "NIFTY26NOV24200CE", "strike": 24200, "ltp": 32.0, "sl_price": 48.0, "target_price": 6.4},
            "pe": {"symbol": "NIFTY26NOV23700PE", "strike": 23700, "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2}
        }
        
        # Test dry-run flag
        exit_code = run_strategy_cli(["--paper", "--dry-run"])
        assert exit_code == 0


def test_run_strategy_cli_live_mode():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.find_strangle_strikes") as mock_find, \
         patch("run_strategy.StrangleDesk"):
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_find.return_value = {
            "expiry": "2026-11-05", "lot_size": 75, "total_lots": 10, "total_quantity": 750,
            "ce": {"symbol": "NIFTY26NOV24200CE", "strike": 24200, "ltp": 32.0, "sl_price": 48.0, "target_price": 6.4},
            "pe": {"symbol": "NIFTY26NOV23700PE", "strike": 23700, "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2}
        }
        
        exit_code = run_strategy_cli(["--live", "--dry-run"])
        assert exit_code == 0


def test_run_strategy_cli_execution_armed():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client"), \
         patch("run_strategy.find_strangle_strikes") as mock_find, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_find.return_value = {
            "expiry": "2026-11-05", "lot_size": 75, "total_lots": 10, "total_quantity": 750,
            "ce": {"symbol": "NIFTY26NOV24200CE", "strike": 24200, "ltp": 32.0, "sl_price": 48.0, "target_price": 6.4},
            "pe": {"symbol": "NIFTY26NOV23700PE", "strike": 23700, "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2}
        }
        mock_desk = MagicMock()
        mock_desk.load_state.return_value = {"ce": {}, "pe": {}}
        mock_desk.state_file = Path("data/trades_today.json")
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--poll", "10"])
        assert exit_code == 0


def test_order_manager_mode_validation():
    with pytest.raises(ValueError, match="Invalid mode"):
        OrderManager(mode="INVALID")


def test_option_chain_normalize_today_date():
    instruments = [
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-11-05"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-11-05"},
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-11-12"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-11-12"},
    ]
    # Pass datetime
    exp = resolve_target_expiry(instruments, today_date=datetime(2026, 11, 2, 10, 0))
    assert exp == date(2026, 11, 5)

    # Pass string
    exp_str = resolve_target_expiry(instruments, today_date="2026-11-02")
    assert exp_str == date(2026, 11, 5)

    # Pass invalid string -> falls back to today
    exp_fallback = resolve_target_expiry(instruments, today_date="not-a-date")
    assert isinstance(exp_fallback, date)


def test_backtest_drawdown_baseline_and_no_double_slippage():
    # Construct a 1-day bar dataframe where the trade loses or wins
    df = pd.DataFrame([
        {"timestamps": datetime(2026, 11, 2, 10, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0},
        {"timestamps": datetime(2026, 11, 2, 15, 0), "open": 24000.0, "high": 24500.0, "low": 23500.0, "close": 24450.0}
    ])
    res = run_backtest_simulation(df, slippage=0.50, lots=1, lot_size=75)
    trade = res["trades"][0]
    # Verify ce_pnl + pe_pnl == net_pnl
    assert round(trade["ce_pnl"] + trade["pe_pnl"], 2) == trade["net_pnl"]
    # Check max_drawdown captures loss if net_pnl < 0
    if trade["net_pnl"] < 0:
        assert res["summary"]["max_drawdown"] <= trade["net_pnl"]
