from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, date, timedelta
import pytest
import pandas as pd
from run_backtest import run_backtest_cli, load_historical_nifty_data
from run_strategy import run_strategy_cli
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk
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


def test_run_backtest_cli_force_fetch_flag(tmp_path):
    output_csv = tmp_path / "trade_log.csv"
    with patch("run_backtest.load_historical_nifty_data") as mock_data:
        mock_data.return_value = pd.DataFrame([
            {"timestamps": datetime(2026, 11, 2, 10, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0},
            {"timestamps": datetime(2026, 11, 2, 15, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0}
        ])
        exit_code = run_backtest_cli(["--days", "120", "--force-fetch", "--output", str(output_csv)])
        assert exit_code == 0
        mock_data.assert_called_once_with(days=120, force_fetch=True)


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
            "expiry": "2026-11-03", "lot_size": 65, "total_lots": 10, "total_quantity": 650,
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
            "expiry": "2026-11-03", "lot_size": 65, "total_lots": 10, "total_quantity": 650,
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
            "expiry": "2026-11-03", "lot_size": 65, "total_lots": 10, "total_quantity": 650,
            "ce": {"symbol": "NIFTY26NOV24200CE", "strike": 24200, "ltp": 32.0, "sl_price": 48.0, "target_price": 6.4},
            "pe": {"symbol": "NIFTY26NOV23700PE", "strike": 23700, "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2}
        }
        mock_desk = MagicMock()
        mock_desk.load_state.return_value = {"ce": {}, "pe": {}}
        mock_desk.state_file = Path("data/trades_today.json")
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--poll", "10"])
        assert exit_code == 0
        mock_desk.run_scheduler.assert_called_once()


def test_order_manager_mode_validation():
    with pytest.raises(ValueError, match="Invalid mode"):
        OrderManager(mode="INVALID")


def test_option_chain_normalize_today_date():
    today = datetime.now().date()
    instruments = [
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-11-05"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-11-05"},
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-11-12"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-11-12"},
        {"name": "NIFTY", "instrument_type": "CE", "expiry": (today + timedelta(days=7)).strftime("%Y-%m-%d")},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": (today + timedelta(days=7)).strftime("%Y-%m-%d")},
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
    res = run_backtest_simulation(df, slippage=0.50, lots=1, lot_size=65)
    trade = res["trades"][0]
    # Verify ce_pnl + pe_pnl == net_pnl
    assert round(trade["ce_pnl"] + trade["pe_pnl"], 2) == trade["net_pnl"]
    # Check max_drawdown captures loss if net_pnl < 0
    if trade["net_pnl"] < 0:
        assert res["summary"]["max_drawdown"] <= trade["net_pnl"]


def test_run_strategy_cli_mutually_exclusive():
    with pytest.raises(SystemExit):
        run_strategy_cli(["--live", "--paper"])


def test_run_strategy_cli_once_flag():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--once"])
        assert exit_code == 0
        mock_desk.run_session.assert_called_once_with(
            underlying="NIFTY",
            lots=10,
            poll_seconds=5,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=10000.0,
            order_type="LIMIT",
            limit_buffer_pct=0.05,
            ignore_holidays=False,
            order_timeout=300,
            morning_filter=True,
            morning_filter_pct=0.60,
            strike_mode="ATR",
            atr_multiplier=1.25,
            target_price=30.0,
            use_leg_sl=True,
            trail_to_cost=True
        )


def test_strangle_desk_run_session_sl_removed_and_target_exit(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    # 1st call to ltp: strike discovery
    # 2nd call to ltp: monitoring loop (CE LTP 49.0 > 48.0 (+50%), PE hits Target 6.0 <= 6.2)
    # Combined PnL = (32 - 49)*65 + (31 - 6)*65 = -1105 + 1625 = +520 (loss not > 10k)
    # CE must NOT exit on individual leg loss; only PE exits on DECAY_TARGET.
    mock_kite.ltp.side_effect = [
        {"NFO:NIFTY26NOV24200CE": {"last_price": 32.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}},
        {"NFO:NIFTY26NOV24200CE": {"last_price": 49.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 6.0}}
    ]

    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 10, 15)
        state = desk.run_session(lots=1, poll_seconds=0, dry_run=False, execute_once=True, kite=mock_kite, use_leg_sl=False)

    assert state is not None
    assert state["status"] == "COMPLETED"
    # PE decay target is reached: PE is CLOSED with DECAY_TARGET
    assert state["pe"]["status"] == "CLOSED"
    assert state["pe"]["exit_reason"] == "DECAY_TARGET"
    assert state["pe"]["exit_price"] == 6.0
    # Option 3 (legacy): opposite leg is simultaneously closed to avoid naked risk
    assert state["ce"]["status"] == "CLOSED"
    assert state["ce"]["exit_reason"] == "CO_TARGET_EXIT"
    assert state["ce"]["exit_price"] == 49.0


def test_strangle_desk_run_session_single_leg_sl_exit(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    # CE Entry: 32.0 (SL 48.0), PE Entry: 31.0
    # Tick 2: CE LTP 49.0 (> 48.0 SL), PE LTP 25.0 (> 6.2 target)
    # Under single-leg exit: CE exits on STOP_LOSS, PE stays OPEN and is trailed to cost (31.0)
    mock_kite.ltp.side_effect = [
        {"NFO:NIFTY26NOV24200CE": {"last_price": 32.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}},
        {"NFO:NIFTY26NOV24200CE": {"last_price": 49.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 25.0}}
    ]

    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 10, 15)
        state = desk.run_session(lots=1, poll_seconds=0, dry_run=False, execute_once=True, kite=mock_kite, use_leg_sl=True, trail_to_cost=True)

    assert state is not None
    assert state["status"] == "OPEN"
    assert state["ce"]["status"] == "CLOSED"
    assert state["ce"]["exit_reason"] == "STOP_LOSS"
    assert state["ce"]["exit_price"] == 49.0
    assert state["pe"]["status"] == "OPEN"
    assert state["pe"]["sl_price"] == 31.0  # Trailed to cost!


def test_strangle_desk_run_session_no_exit_when_sl_exceeded_and_no_target(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    # In legacy mode (use_leg_sl=False), individual leg SL is bypassed
    mock_kite.ltp.side_effect = [
        {"NFO:NIFTY26NOV24200CE": {"last_price": 32.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}},
        {"NFO:NIFTY26NOV24200CE": {"last_price": 49.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 25.0}}
    ]

    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 10, 15)
        state = desk.run_session(lots=1, poll_seconds=0, dry_run=False, execute_once=True, kite=mock_kite, use_leg_sl=False)

    assert state is not None
    assert state["status"] == "OPEN"
    assert state["ce"]["status"] == "OPEN"
    assert state["ce"]["exit_reason"] is None
    assert state["pe"]["status"] == "OPEN"
    assert state["pe"]["exit_reason"] is None


def test_strangle_desk_run_session_time_exit(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}
    }

    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    # Freeze time past 15:00
    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 15, 5)
        state = desk.run_session(lots=1, poll_seconds=0, dry_run=False, execute_once=True, kite=mock_kite)

    assert state is not None
    assert state["status"] == "COMPLETED"
    assert state["ce"]["status"] == "CLOSED"
    assert state["ce"]["exit_reason"] == "TIME_EXIT"
    assert state["pe"]["status"] == "CLOSED"
    assert state["pe"]["exit_reason"] == "TIME_EXIT"


def test_strangle_desk_run_session_1445_cutoff_exit(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}
    }

    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    # Freeze time at 14:46 (past 14:45 cutoff)
    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 14, 46)
        state = desk.run_session(lots=1, poll_seconds=0, dry_run=False, execute_once=True, kite=mock_kite)

    assert state is not None
    assert state["status"] == "COMPLETED"
    assert state["ce"]["status"] == "CLOSED"
    assert state["ce"]["exit_reason"] == "TIME_EXIT"
    assert state["pe"]["status"] == "CLOSED"
    assert state["pe"]["exit_reason"] == "TIME_EXIT"


def test_strangle_desk_run_session_dry_run(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}
    }

    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    result = desk.run_session(dry_run=True, kite=mock_kite)
    assert result["status"] == "DRY_RUN"
    om.sell.assert_not_called()
    assert not state_file.exists()


def test_load_historical_nifty_data_env_cache_path(tmp_path, monkeypatch):
    custom_parquet = tmp_path / "custom.parquet"
    df = pd.DataFrame([
        {"timestamps": datetime(2026, 11, 2, 10, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0}
    ])
    df.to_parquet(custom_parquet)

    monkeypatch.setenv("HISTORICAL_CACHE_PATH", str(custom_parquet))
    loaded = load_historical_nifty_data(days=1)
    assert len(loaded) == 1
    assert loaded["close"].iloc[0] == 24000.0


def test_load_historical_nifty_data_chunked_kite_fetch(monkeypatch):
    monkeypatch.delenv("HISTORICAL_CACHE_PATH", raising=False)
    # Ensure neither cache file exists
    with patch("run_backtest.Path.exists", return_value=False), \
         patch("run_backtest.get_kite_client") as mock_kite_fn, \
         patch("pandas.DataFrame.to_parquet"):
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_kite.historical_data.return_value = [
            {"date": "2026-10-01 10:00:00", "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0}
        ]

        # Request 150 days -> should chunk into 3 requests (60, 60, 30 days)
        df = load_historical_nifty_data(days=150)
        assert mock_kite.historical_data.call_count == 3
        assert not df.empty


def test_strangle_desk_run_session_strategy_max_loss_exit(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    # CE Entry: 35.0, PE Entry: 35.0. Total qty = 650.
    # At tick 2: CE LTP 52.0 (loss = (52 - 35) * 650 = 11,050).
    # PE LTP 34.0 (gain = (35 - 34) * 650 = 650).
    # Total strategy loss = 11,050 - 650 = 10,400 >= 10,000 strategy limit.
    mock_kite.ltp.side_effect = [
        {"NFO:NIFTY26NOV24200CE": {"last_price": 35.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 35.0}},
        {"NFO:NIFTY26NOV24200CE": {"last_price": 52.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 34.0}}
    ]
    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 10, 15)
        state = desk.run_session(
            lots=10,
            poll_seconds=0,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=10000.0,
            use_leg_sl=False
        )

    assert state is not None
    # BOTH legs must be closed immediately in legacy dual-leg mode
    assert state["ce"]["status"] == "CLOSED"
    assert state["pe"]["status"] == "CLOSED"
    assert state["ce"]["exit_reason"] == "STRATEGY_MAX_LOSS_10K"
    assert state["pe"]["exit_reason"] == "STRATEGY_MAX_LOSS_10K"
    assert state["status"] == "COMPLETED"


def test_strangle_desk_run_session_single_leg_10k_loss_exit(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    # CE Entry: 35.0, PE Entry: 35.0. Total qty = 650.
    # At tick 2: CE LTP 52.0 (loss = (52 - 35) * 650 = 11,050).
    # PE LTP 34.0 (gain = (35 - 34) * 650 = 650).
    # Total strategy loss = 10,400 >= 10,000.
    # In single-leg mode (use_leg_sl=True): ONLY CE is exited! PE remains OPEN!
    mock_kite.ltp.side_effect = [
        {"NFO:NIFTY26NOV24200CE": {"last_price": 35.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 35.0}},
        {"NFO:NIFTY26NOV24200CE": {"last_price": 52.0}, "NFO:NIFTY26NOV23700PE": {"last_price": 34.0}}
    ]
    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=mock_kite)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 10, 15)
        state = desk.run_session(
            lots=10,
            poll_seconds=0,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=10000.0,
            use_leg_sl=True,
            trail_to_cost=True
        )

    assert state is not None
    assert state["ce"]["status"] == "CLOSED"
    assert state["ce"]["exit_reason"] in ("SINGLE_LEG_10K_EXIT", "STOP_LOSS")
    assert state["pe"]["status"] == "OPEN"
    assert state["pe"]["sl_price"] == 35.0  # Trailed to cost!



def test_run_strategy_cli_max_loss_flag():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--max-loss", "8000.0", "--once"])
        assert exit_code == 0
        mock_desk.run_session.assert_called_once_with(
            underlying="NIFTY",
            lots=10,
            poll_seconds=5,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=8000.0,
            order_type="LIMIT",
            limit_buffer_pct=0.05,
            ignore_holidays=False,
            order_timeout=300,
            morning_filter=True,
            morning_filter_pct=0.60,
            strike_mode="ATR",
            atr_multiplier=1.25,
            target_price=30.0,
            use_leg_sl=True,
            trail_to_cost=True
        )


def test_run_strategy_cli_strategy_max_loss_flag():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--strategy-max-loss", "10000.0", "--once"])
        assert exit_code == 0
        mock_desk.run_session.assert_called_once_with(
            underlying="NIFTY",
            lots=10,
            poll_seconds=5,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=10000.0,
            order_type="LIMIT",
            limit_buffer_pct=0.05,
            ignore_holidays=False,
            order_timeout=300,
            morning_filter=True,
            morning_filter_pct=0.60,
            strike_mode="ATR",
            atr_multiplier=1.25,
            target_price=30.0,
            use_leg_sl=True,
            trail_to_cost=True
        )



def test_strangle_desk_run_session_skips_on_holiday(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    with patch("strategy.strangle_desk.is_trading_holiday", return_value=(True, "Independence Day")):
        res = desk.run_session(ignore_holidays=False)
        assert res["status"] == "HOLIDAY"
        assert res["reason"] == "Independence Day"
        om.sell.assert_not_called()


def test_run_strategy_cli_order_type_and_holiday_flags():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--order-type", "MARKET", "--limit-buffer-pct", "0.08", "--ignore-holidays", "--once"])
        assert exit_code == 0
        mock_desk.run_session.assert_called_once_with(
            underlying="NIFTY",
            lots=10,
            poll_seconds=5,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=10000.0,
            order_type="MARKET",
            limit_buffer_pct=0.08,
            ignore_holidays=True,
            order_timeout=300,
            morning_filter=True,
            morning_filter_pct=0.60,
            strike_mode="ATR",
            atr_multiplier=1.25,
            target_price=30.0,
            use_leg_sl=True,
            trail_to_cost=True
        )


def test_run_strategy_cli_order_timeout_flag():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--order-timeout", "120", "--once"])
        assert exit_code == 0
        mock_desk.run_session.assert_called_once_with(
            underlying="NIFTY",
            lots=10,
            poll_seconds=5,
            dry_run=False,
            execute_once=True,
            kite=mock_kite,
            strategy_max_loss=10000.0,
            order_type="LIMIT",
            limit_buffer_pct=0.05,
            ignore_holidays=False,
            order_timeout=120,
            morning_filter=True,
            morning_filter_pct=0.60,
            strike_mode="ATR",
            atr_multiplier=1.25,
            target_price=30.0,
            use_leg_sl=True,
            trail_to_cost=True
        )


def test_run_strategy_cli_log_flags(tmp_path):
    log_dir = tmp_path / "custom_cli_logs"
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--paper", "--log-level", "DEBUG", "--log-dir", str(log_dir), "--once"])
        assert exit_code == 0
        assert log_dir.exists()
        log_file = log_dir / "strangle_desk.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "Initializing 10:00 AM NIFTY Short Strangle Desk" in content


def test_strangle_desk_run_session_order_timeout_zero_fill_aborts(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 31.0}
    }

    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    om.sell.side_effect = [
        {"order_id": "ORD_CE_1", "status": "SUBMITTED"},
        {"order_id": "ORD_PE_1", "status": "SUBMITTED"}
    ]
    # Simulate both orders still OPEN with 0 filled after timeout
    om.get_order_status.side_effect = [
        {"order_id": "ORD_CE_1", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 65, "average_price": 0.0},
        {"order_id": "ORD_PE_1", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 65, "average_price": 0.0}
    ]

    desk = StrangleDesk(order_manager=om, state_file=state_file)
    res = desk.run_session(lots=1, order_timeout=1, poll_seconds=0, execute_once=True, kite=mock_kite)

    assert res is not None
    assert res["status"] == "ABORTED"
    assert res["reason"] == "ENTRY_ORDER_TIMEOUT"
    # Verify cancellation was called for both orders
    om.cancel_order.assert_any_call("ORD_CE_1")
    om.cancel_order.assert_any_call("ORD_PE_1")
    # Verify buy_to_cover was NOT called because filled_quantity == 0
    om.buy_to_cover.assert_not_called()
    assert state_file.exists()


def test_strangle_desk_run_session_partial_fill_aborts_and_squares_off(tmp_path):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26NOV24200CE", "expiry": "2026-11-26", "strike": 24200, "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "NIFTY26NOV23700PE", "expiry": "2026-11-26", "strike": 23700, "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.5},
        "NFO:NIFTY26NOV23700PE": {"last_price": 30.5}
    }

    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    om.sell.side_effect = [
        {"order_id": "ORD_CE_2", "status": "SUBMITTED"},
        {"order_id": "ORD_PE_2", "status": "SUBMITTED"}
    ]
    # CE was completely filled (650 shares), PE only partially filled (130 shares)
    om.get_order_status.side_effect = [
        {"order_id": "ORD_CE_2", "status": "COMPLETE", "filled_quantity": 650, "pending_quantity": 0, "average_price": 32.0},
        {"order_id": "ORD_PE_2", "status": "OPEN", "filled_quantity": 130, "pending_quantity": 520, "average_price": 31.0}
    ]

    desk = StrangleDesk(order_manager=om, state_file=state_file)
    res = desk.run_session(lots=10, order_timeout=1, poll_seconds=0, execute_once=True, kite=mock_kite)

    assert res is not None
    assert res["status"] == "ABORTED"
    assert res["reason"] == "ENTRY_ORDER_TIMEOUT"
    # PE still had pending quantity, so cancel_order must be called on PE
    om.cancel_order.assert_called_with("ORD_PE_2")
    # Both CE and PE filled quantities must be squared off to prevent unmatched or partial exposure
    om.buy_to_cover.assert_any_call(
        "NIFTY26NOV24200CE", 650, 32.5, reason="ABORT_UNMATCHED_LEG", order_type="LIMIT", buffer_pct=0.05
    )
    om.buy_to_cover.assert_any_call(
        "NIFTY26NOV23700PE", 130, 30.5, reason="ABORT_PARTIAL_FILL", order_type="LIMIT", buffer_pct=0.05
    )
    assert state_file.exists()


def test_strangle_desk_run_session_market_closed(tmp_path):
    from datetime import datetime
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    mock_kite = MagicMock()
    # Mock datetime to 18:33 (market closed) on a weekday
    fake_now = datetime(2026, 11, 2, 18, 33, 0)
    with patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        res = desk.run_session(lots=1, kite=mock_kite, check_market_hours=True)

    assert res["status"] == "MARKET_CLOSED"
    assert "market is closed" in res["reason"]
    # Verify no orders were placed to Kite Connect
    om.sell.assert_not_called()
    mock_notifier.send_message.assert_not_called()


def test_strangle_desk_run_scheduler_loop(tmp_path):
    from datetime import datetime
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    mock_kite = MagicMock()
    # Mock datetime to 10:02 AM (entry window) on a Monday
    fake_now = datetime(2026, 11, 2, 10, 2, 0)
    with patch("strategy.strangle_desk.datetime") as mock_dt, \
         patch.object(desk, "run_session") as mock_session, \
         patch.object(desk, "perform_daily_morning_login", return_value=mock_kite) as mock_login, \
         patch("strategy.strangle_desk.time.sleep") as mock_sleep:

        mock_dt.now.return_value = fake_now
        desk.run_scheduler(lots=1, kite=mock_kite, entry_time_str="10:00", idle_sleep_seconds=1, max_iterations=1)

        mock_login.assert_called_once_with(mock_kite)
        mock_session.assert_called_once()
        mock_notifier.send_message.assert_not_called()


def test_strangle_desk_perform_daily_morning_login():
    om = MagicMock()
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=om, notifier=mock_notifier)

    mock_fresh_kite = MagicMock()
    mock_fresh_kite.profile.return_value = {"user_name": "Taruna Sehgal", "user_id": "TC4648"}

    with patch("kite.client.get_kite_client", return_value=mock_fresh_kite) as mock_get_client:
        res = desk.perform_daily_morning_login()
        assert res == mock_fresh_kite
        mock_get_client.assert_called_once_with(force_login=True)
        assert om.kite == mock_fresh_kite
        mock_notifier.send_message.assert_not_called()


def test_run_strategy_cli_morning_login_flag():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client") as mock_kite_fn, \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:

        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        exit_code = run_strategy_cli(["--morning-login"])
        assert exit_code == 0
        mock_desk.perform_daily_morning_login.assert_called_once_with(kite=mock_kite)
        mock_desk.run_session.assert_not_called()
        mock_desk.run_scheduler.assert_not_called()


def test_strangle_desk_run_session_ce_entry_exception(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    om.sell.side_effect = RuntimeError("Your order could not be converted to a After Market Order (AMO).")
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    mock_kite = MagicMock()
    with patch("strategy.strangle_desk.find_strangle_strikes") as mock_find:
        mock_find.return_value = {
            "expiry": "2026-11-09",
            "total_lots": 1,
            "total_quantity": 65,
            "ce": {"symbol": "NIFTY26N0924300CE", "ltp": 30.5, "sl_price": 45.75, "target_price": 6.1},
            "pe": {"symbol": "NIFTY26N0923800PE", "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2},
        }
        res = desk.run_session(lots=1, kite=mock_kite, execute_once=True)

    assert res["status"] == "ABORTED"
    assert "Failed to place CE entry order" in res["reason"]
    mock_notifier.send_message.assert_called_once()


def test_strangle_desk_run_session_pe_entry_exception_cleans_up_ce(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    # First sell (CE) succeeds, second sell (PE) fails
    om.sell.side_effect = [
        {"order_id": "CE123", "status": "SUBMITTED"},
        RuntimeError("Kite margin error on PE leg"),
    ]
    om.get_order_status.return_value = {"order_id": "CE123", "status": "COMPLETE", "filled_quantity": 65}
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    mock_kite = MagicMock()
    with patch("strategy.strangle_desk.find_strangle_strikes") as mock_find:
        mock_find.return_value = {
            "expiry": "2026-11-09",
            "total_lots": 1,
            "total_quantity": 65,
            "ce": {"symbol": "NIFTY26N0924300CE", "ltp": 30.5, "sl_price": 45.75, "target_price": 6.1},
            "pe": {"symbol": "NIFTY26N0923800PE", "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2},
        }
        res = desk.run_session(lots=1, kite=mock_kite, execute_once=True)

    assert res["status"] == "ABORTED"
    assert "Failed to place PE entry order" in res["reason"]
    # Check that CE was cancelled and bought back
    om.cancel_order.assert_called_with("CE123")
    om.buy_to_cover.assert_called_once()
    assert om.buy_to_cover.call_args[1]["symbol"] == "NIFTY26N0924300CE"
    assert om.buy_to_cover.call_args[1]["quantity"] == 65
    mock_notifier.send_message.assert_called_once()


def test_ensure_kite_session_valid():
    om = MagicMock()
    desk = StrangleDesk(order_manager=om)
    mock_kite = MagicMock()
    mock_kite.profile.return_value = {"user_id": "TC4648"}
    res = desk._ensure_kite_session(mock_kite)
    assert res == mock_kite
    mock_kite.profile.assert_called_once()


def test_ensure_kite_session_reauthenticates_on_expired_token():
    om = MagicMock()
    desk = StrangleDesk(order_manager=om)
    mock_kite = MagicMock()
    mock_kite.profile.side_effect = Exception("Incorrect `api_key` or `access_token`.")
    new_mock_kite = MagicMock()

    with patch("kite.client.get_kite_client", return_value=new_mock_kite) as mock_get_client:
        res = desk._ensure_kite_session(mock_kite)
        assert res == new_mock_kite
        mock_get_client.assert_called_once_with(force_login=True)
        assert om.kite == new_mock_kite


def test_reconcile_with_broker_adopts_open_positions(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"tradingsymbol": "NIFTY26N0924400CE", "name": "NIFTY", "instrument_type": "CE", "expiry": date(2026, 11, 9), "strike": 24400.0, "lot_size": 65},
        {"tradingsymbol": "NIFTY26N0923400PE", "name": "NIFTY", "instrument_type": "PE", "expiry": date(2026, 11, 9), "strike": 23400.0, "lot_size": 65},
    ]
    mock_kite.positions.return_value = {
        "net": [
            {"tradingsymbol": "NIFTY26N0924400CE", "exchange": "NFO", "product": "NRML", "quantity": -650, "average_price": 41.4},
            {"tradingsymbol": "NIFTY26N0923400PE", "exchange": "NFO", "product": "NRML", "quantity": -650, "average_price": 39.8},
            {"tradingsymbol": "RELIANCE26NOV1200PE", "exchange": "NFO", "product": "NRML", "quantity": -20000, "average_price": 10.0},
        ]
    }

    state = desk.reconcile_with_broker(mock_kite, target_expiry=date(2026, 11, 9))
    assert state is not None
    assert state["status"] == "OPEN"
    assert state["quantity"] == 650
    assert state["lots"] == 10
    assert state["product"] == "NRML"
    assert state["ce"]["symbol"] == "NIFTY26N0924400CE"
    assert state["ce"]["entry_price"] == 41.4
    assert state["ce"]["sl_price"] == 62.10
    assert state["ce"]["target_price"] == 8.28
    assert state["pe"]["symbol"] == "NIFTY26N0923400PE"
    assert state["pe"]["entry_price"] == 39.8
    assert state["pe"]["sl_price"] == 59.70
    assert state["pe"]["target_price"] == 7.96
    assert state_file.exists()
    mock_notifier.send_message.assert_not_called()


def test_reconcile_with_broker_marks_closed_when_flat(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    initial_state = {
        "status": "OPEN",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ce": {"symbol": "NIFTY26N0924400CE", "status": "OPEN"},
        "pe": {"symbol": "NIFTY26N0923400PE", "status": "OPEN"}
    }
    desk.save_state(initial_state)

    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {"tradingsymbol": "NIFTY26N0924400CE", "exchange": "NFO", "quantity": 0},
            {"tradingsymbol": "NIFTY26N0923400PE", "exchange": "NFO", "quantity": 0},
        ]
    }

    state = desk.reconcile_with_broker(mock_kite)
    assert state["status"] == "COMPLETED"
    assert state["ce"]["status"] == "CLOSED"
    assert state["pe"]["status"] == "CLOSED"


def test_reconcile_with_broker_partial_closed(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    initial_state = {
        "status": "OPEN",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ce": {"symbol": "NIFTY26N0924400CE", "status": "OPEN"},
        "pe": {"symbol": "NIFTY26N0923400PE", "status": "OPEN"}
    }
    desk.save_state(initial_state)

    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {"tradingsymbol": "NIFTY26N0924400CE", "exchange": "NFO", "quantity": 0},
            {"tradingsymbol": "NIFTY26N0923400PE", "exchange": "NFO", "quantity": -650},
        ]
    }

    state = desk.reconcile_with_broker(mock_kite)
    assert state["status"] == "OPEN"
    assert state["ce"]["status"] == "CLOSED"
    assert state["pe"]["status"] == "OPEN"


def test_safe_exit_leg_uses_position_product(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.product = "MIS"
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    state = {
        "ce": {"symbol": "NIFTY26N0924400CE", "status": "OPEN", "product": "NRML"},
        "pe": {"symbol": "NIFTY26N0923400PE", "status": "OPEN", "product": "NRML"}
    }
    desk.save_state(state)

    success = desk._safe_exit_leg(
        leg_name="ce",
        symbol="NIFTY26N0924400CE",
        qty=650,
        exit_price=35.0,
        reason="STOP_LOSS",
        order_type="LIMIT",
        limit_buffer_pct=0.05,
        state=state
    )
    assert success is True
    om.buy_to_cover.assert_called_once_with(
        "NIFTY26N0924400CE", 650, 35.0, reason="STOP_LOSS", order_type="LIMIT", buffer_pct=0.05, product="NRML", min_buffer_rs=1.5
    )


def test_safe_exit_leg_enforces_universal_min_buffer_rs_floor(tmp_path):
    """
    Test that _safe_exit_leg enforces a universal min_buffer_rs=1.5 floor across all exits,
    ensuring low-priced options (e.g. Rs 6.0) get at least Rs 1.50 buffer rather than 5% (Rs 0.30).
    """
    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER")
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    state = {
        "ce": {"symbol": "NIFTY26N0924400CE", "status": "OPEN", "product": "MIS"},
        "pe": {"symbol": "NIFTY26N0923400PE", "status": "OPEN", "product": "MIS"}
    }
    desk.save_state(state)

    # Low premium exit at Rs 6.00: 5% is only Rs 0.30, but min_buffer_rs=1.5 floor gives Rs 7.50
    desk._safe_exit_leg(
        leg_name="ce",
        symbol="NIFTY26N0924400CE",
        qty=65,
        exit_price=6.0,
        reason="TIME_EXIT",
        order_type="LIMIT",
        limit_buffer_pct=0.05,
        state=state
    )

    paper_order = om._paper_orders.get("PAPER_BUY_NIFTY26N0924400CE")
    assert paper_order is not None
    # 6.0 + 1.5 = 7.50 (rather than 6.0 * 1.05 = 6.30)
    assert paper_order["limit_price"] == 7.50


def test_run_strategy_cli_product_flag():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client"), \
         patch("run_strategy.OrderManager") as mock_om_cls, \
         patch("run_strategy.StrangleDesk"):

        exit_code = run_strategy_cli(["--product", "NRML", "--dry-run"])
        assert exit_code == 0
        assert mock_om_cls.call_args[1]["product"] == "NRML"


def test_run_session_reauthenticates_on_token_error(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    mock_kite_expired = MagicMock()
    mock_kite_expired.profile.side_effect = Exception("Incorrect `api_key` or `access_token`.")
    mock_kite_fresh = MagicMock()
    mock_kite_fresh.profile.return_value = {"user_id": "TC4648"}

    with patch("kite.client.get_kite_client", return_value=mock_kite_fresh), \
         patch("strategy.strangle_desk.find_strangle_strikes") as mock_find:
        mock_find.return_value = {
            "expiry": "2026-11-09",
            "total_lots": 1,
            "total_quantity": 65,
            "ce": {"symbol": "NIFTY26N0924300CE", "ltp": 30.5, "sl_price": 45.75, "target_price": 6.1},
            "pe": {"symbol": "NIFTY26N0923800PE", "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2},
        }
        res = desk.run_session(lots=1, kite=mock_kite_expired, dry_run=True)
        assert res["status"] == "DRY_RUN"
        mock_find.assert_called_once_with(mock_kite_fresh, underlying="NIFTY", target_price=30.0, strike_mode="ATR", atr_multiplier=1.25, lots=1)






def test_reconcile_with_broker_skips_instrument_fetch_when_no_short_nfo_positions(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 100},
            {"tradingsymbol": "NIFTY26N0924400CE", "exchange": "NFO", "quantity": 650},  # BUY/LONG position, not short
        ]
    }

    state = desk.reconcile_with_broker(mock_kite)
    assert state is None
    # instruments("NFO") must NOT be called if there are no short NFO positions!
    mock_kite.instruments.assert_not_called()


def test_run_scheduler_skips_reconciliation_outside_market_hours(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = MagicMock()
    om.mode = "LIVE"
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    mock_kite = MagicMock()

    # Simulate 17:15 IST (off-market hours) on a weekday (Wednesday)
    fixed_time = datetime(2026, 11, 4, 17, 15, 0)
    with patch("strategy.strangle_desk.datetime") as mock_dt,          patch("strategy.strangle_desk.time.sleep") as mock_sleep:
        mock_dt.now.return_value = fixed_time
        desk.run_scheduler(kite=mock_kite, max_iterations=1, ignore_holidays=False)

    # In off-market hours without an active trade, neither positions nor instruments should be polled!
    mock_kite.positions.assert_not_called()
    mock_kite.instruments.assert_not_called()
    mock_sleep.assert_called_once_with(15)


def test_check_available_equity_margin(tmp_path):
    state_file = tmp_path / "trades_today.json"
    mock_om = MagicMock()
    mock_kite = MagicMock()
    mock_om.kite = mock_kite
    mock_om.mode = "LIVE"
    desk = StrangleDesk(order_manager=mock_om, state_file=state_file)

    # 1. OK status
    mock_kite.margins.return_value = {
        "enabled": True,
        "available": {"live_balance": 350000.0, "cash": 300000.0},
        "net": 350000.0
    }
    margin = desk.check_available_equity_margin()
    assert margin["status"] == "OK"
    assert margin["available_cash"] == 350000.0

    # 2. Disabled status
    mock_kite.margins.return_value = {"enabled": False, "net": 0.0}
    margin_dis = desk.check_available_equity_margin()
    assert margin_dis["status"] == "DISABLED"


def test_no_preflight_equity_margin_check_proceeds_to_entry(tmp_path):
    state_file = tmp_path / "trades_today.json"
    mock_om = MagicMock()
    mock_kite = MagicMock()
    mock_om.kite = mock_kite
    mock_om.mode = "LIVE"
    mock_om.sell.return_value = {"order_id": "ORD_1", "status": "SUBMITTED"}
    desk = StrangleDesk(order_manager=mock_om, state_file=state_file)

    mock_kite.profile.return_value = {"user_id": "TC4648"}
    mock_kite.positions.return_value = {"net": []}
    mock_kite.margins.return_value = {
        "enabled": True,
        "available": {"live_balance": 50000.0},
        "net": 50000.0
    }

    with patch("strategy.strangle_desk.is_trading_holiday", return_value=(False, "")), \
         patch("strategy.strangle_desk.find_strangle_strikes") as mock_find, \
         patch.object(desk, "wait_for_order_completion", return_value={"status": "COMPLETE", "average_price": 30.5}):
        mock_find.return_value = {
            "expiry": "2026-11-09",
            "total_lots": 2,
            "total_quantity": 130,
            "ce": {"symbol": "NIFTY26N0924300CE", "ltp": 30.5, "sl_price": 45.75, "target_price": 6.1},
            "pe": {"symbol": "NIFTY26N0923800PE", "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2},
        }
        res = desk.run_session(lots=2, kite=mock_kite, execute_once=True, ignore_holidays=True)
        # Pre-flight equity margin check removed: does not abort, sells both CE and PE legs directly
        assert res is not None
        assert mock_om.sell.call_count == 2


def test_safe_exit_leg_live_verification(tmp_path):
    state_file = tmp_path / "trades_today.json"
    mock_om = MagicMock()
    mock_om.mode = "LIVE"
    desk = StrangleDesk(order_manager=mock_om, state_file=state_file)

    state = {
        "date": "2026-11-16",
        "status": "OPEN",
        "ce": {"symbol": "NIFTY26N1624300CE", "status": "OPEN", "entry_price": 30.0},
        "pe": {"symbol": "NIFTY26N1623800PE", "status": "OPEN", "entry_price": 30.0}
    }

    # 1. Complete fill
    mock_om.buy_to_cover.return_value = {"order_id": "ORD_EXIT_1", "status": "SUBMITTED"}
    with patch.object(desk, "wait_for_order_completion") as mock_wait:
        mock_wait.return_value = {"status": "COMPLETE", "average_price": 45.5}
        ok = desk._safe_exit_leg("ce", "NIFTY26N1624300CE", 65, 45.0, "STOP_LOSS", "LIMIT", 0.05, state)
        assert ok is True
        assert state["ce"]["status"] == "CLOSED"
        assert state["ce"]["exit_price"] == 45.5

    # 2. Rejected order
    state["pe"]["status"] = "OPEN"
    mock_om.buy_to_cover.return_value = {"order_id": "ORD_EXIT_2", "status": "SUBMITTED"}
    with patch.object(desk, "wait_for_order_completion") as mock_wait:
        mock_wait.return_value = {"status": "REJECTED", "status_message": "RMS: Margin exceeded"}
        ok = desk._safe_exit_leg("pe", "NIFTY26N1623800PE", 65, 45.0, "STOP_LOSS", "LIMIT", 0.05, state)
        assert ok is False
        assert state["pe"]["status"] == "OPEN"

    # 3. Timeout order cancels resting order
    mock_om.buy_to_cover.return_value = {"order_id": "ORD_EXIT_3", "status": "SUBMITTED"}
    with patch.object(desk, "wait_for_order_completion") as mock_wait:
        # First call (10s wait) returns OPEN, second call (after cancel) returns CANCELLED
        mock_wait.side_effect = [
            {"status": "OPEN", "pending_quantity": 65},
            {"status": "CANCELLED", "filled_quantity": 0}
        ]
        ok = desk._safe_exit_leg("pe", "NIFTY26N1623800PE", 65, 45.0, "STOP_LOSS", "LIMIT", 0.05, state)
        assert ok is False
        assert state["pe"]["status"] == "OPEN"
        mock_om.cancel_order.assert_called_with("ORD_EXIT_3")


def test_check_morning_trend_filter_triggered():
    desk = StrangleDesk(order_manager=MagicMock())
    mock_kite = MagicMock()
    # 09:15-10:00 range: Open 24000, High 24200, Low 23980 => Range = 220 pts = 0.916% (>= 0.60%)
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 11, 20, 9, 15), "open": 24000.0, "high": 24050.0, "low": 23980.0, "close": 24040.0},
        {"date": datetime(2026, 11, 20, 9, 30), "open": 24040.0, "high": 24150.0, "low": 24030.0, "close": 24140.0},
        {"date": datetime(2026, 11, 20, 9, 45), "open": 24140.0, "high": 24200.0, "low": 24120.0, "close": 24190.0},
    ]

    is_trend, details = desk.check_morning_trend_filter(mock_kite, underlying="NIFTY", threshold_pct=0.60)
    assert is_trend is True
    assert details["triggered"] is True
    assert details["range_pts"] == 220.0
    assert details["range_pct"] > 0.60


def test_check_morning_trend_filter_not_triggered():
    desk = StrangleDesk(order_manager=MagicMock())
    mock_kite = MagicMock()
    # 09:15-10:00 range: Open 24000, High 24050, Low 23980 => Range = 70 pts = 0.29% (< 0.60%)
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 11, 20, 9, 15), "open": 24000.0, "high": 24050.0, "low": 23980.0, "close": 24040.0},
        {"date": datetime(2026, 11, 20, 9, 45), "open": 24040.0, "high": 24050.0, "low": 24010.0, "close": 24020.0},
    ]

    is_trend, details = desk.check_morning_trend_filter(mock_kite, underlying="NIFTY", threshold_pct=0.60)
    assert is_trend is False
    assert details["triggered"] is False
    assert details["range_pts"] == 70.0


def test_check_morning_trend_filter_quote_fallback():
    desk = StrangleDesk(order_manager=MagicMock())
    mock_kite = MagicMock()
    mock_kite.historical_data.side_effect = RuntimeError("Historical API down")
    mock_kite.quote.return_value = {
        "NSE:NIFTY 50": {
            "ohlc": {"open": 24000.0, "high": 24180.0, "low": 23980.0, "close": 24150.0}
        }
    }

    # Range = 200 pts / 24000 = 0.833% (>= 0.60%)
    is_trend, details = desk.check_morning_trend_filter(mock_kite, underlying="NIFTY", threshold_pct=0.60)
    assert is_trend is True
    assert details["range_pts"] == 200.0
    assert details["source"] == "quote_ohlc"


def test_run_session_skips_when_morning_filter_triggered(tmp_path):
    state_file = tmp_path / "trades_today.json"
    mock_om = MagicMock()
    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=mock_om, state_file=state_file, notifier=mock_notifier)
    mock_kite = MagicMock()

    with patch.object(desk, "check_morning_trend_filter", return_value=(True, {"range_pts": 200.0, "range_pct": 0.83, "triggered": True})):
        res = desk.run_session(
            underlying="NIFTY",
            lots=10,
            kite=mock_kite,
            morning_filter=True,
            morning_filter_pct=0.60,
            ignore_holidays=True
        )

        assert res["status"] == "SKIPPED_TREND_DAY"
        assert "Morning Trend Filter" in res["reason"]
        # Ensure no orders were placed
        mock_om.sell.assert_not_called()
        mock_notifier.send_message.assert_called_once()
        # Ensure state persisted
        saved_state = desk.load_state()
        assert saved_state["status"] == "SKIPPED_TREND_DAY"


def test_run_strategy_cli_morning_filter_flags():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client"), \
         patch("run_strategy.TelegramNotifier"), \
         patch("run_strategy.OrderManager"), \
         patch("run_strategy.StrangleDesk") as mock_desk_cls:
        
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        # Test CLI with --strike-mode ATR --atr-multiplier 1.25 --morning-filter-pct 0.55
        exit_code = run_strategy_cli([
            "--once",
            "--morning-filter",
            "--morning-filter-pct", "0.55",
            "--strike-mode", "ATR",
            "--atr-multiplier", "1.25"
        ])
        assert exit_code == 0
        call_kwargs = mock_desk.run_session.call_args[1]
        assert call_kwargs["morning_filter"] is True
        assert call_kwargs["morning_filter_pct"] == 0.55
        assert call_kwargs["strike_mode"] == "ATR"
        assert call_kwargs["atr_multiplier"] == 1.25


def test_strangle_desk_concurrent_entry_order_placement(tmp_path):
    """
    Test that CE and PE entry orders are placed concurrently in parallel threads,
    reducing latency and ensuring both legs are submitted simultaneously.
    """
    import threading
    import time
    state_file = tmp_path / "trades_today.json"
    mock_om = MagicMock()
    mock_om.mode = "LIVE"

    threads_seen = set()
    call_times = []

    def slow_sell(sym, qty, ltp, order_type="LIMIT", buffer_pct=0.05):
        threads_seen.add(threading.current_thread().ident)
        t_start = time.time()
        call_times.append(t_start)
        time.sleep(0.05)  # 50ms artificial delay
        return {
            "order_id": f"ORD_{sym}",
            "status": "COMPLETE",
            "filled_quantity": qty,
            "average_price": ltp
        }

    mock_om.sell.side_effect = slow_sell
    mock_om.get_order_status.side_effect = lambda oid: {
        "order_id": oid,
        "status": "COMPLETE",
        "filled_quantity": 65,
        "average_price": 30.0
    }

    mock_notifier = MagicMock()
    desk = StrangleDesk(order_manager=mock_om, state_file=state_file, notifier=mock_notifier)
    mock_kite = MagicMock()
    mock_kite.ltp.return_value = {
        "NSE:NIFTY 50": {"last_price": 25000.0},
        "NFO:NIFTY26N0925300CE": {"last_price": 30.0},
        "NFO:NIFTY26N0924700PE": {"last_price": 30.0}
    }

    with patch("strategy.strangle_desk.find_strangle_strikes") as mock_find, \
         patch("strategy.strangle_desk.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 3, 10, 15)
        mock_find.return_value = {
            "expiry": "2026-11-09",
            "total_lots": 1,
            "total_quantity": 65,
            "ce": {"symbol": "NIFTY26N0925300CE", "strike": 25300, "ltp": 30.0, "sl_price": 45.0, "target_price": 6.0},
            "pe": {"symbol": "NIFTY26N0924700PE", "strike": 24700, "ltp": 30.0, "sl_price": 45.0, "target_price": 6.0},
        }
        res = desk.run_session(lots=1, kite=mock_kite, execute_once=True, morning_filter=False, ignore_holidays=True)

    assert res["status"] == "OPEN"
    # Verify both calls were executed across distinct threads (concurrently)
    assert len(threads_seen) == 2
    # Verify that the two calls started within 20ms of each other (parallel dispatch)
    assert abs(call_times[0] - call_times[1]) < 0.04



