from datetime import datetime, timedelta
from unittest.mock import MagicMock
import numpy as np
import pandas as pd
import pytest

from strategy.positional_condor.indicator import compute_rsi, classify_regime, fetch_daily_nifty_rsi


def test_compute_rsi_known_series():
    # Test on a monotonic upward series (RSI should approach 100)
    prices = [100.0 + i * 2.0 for i in range(30)]
    s = pd.Series(prices)
    rsi = compute_rsi(s, period=14)
    assert not rsi.empty
    assert rsi.iloc[-1] > 95.0

    # Test on a monotonic downward series (RSI should approach 0)
    prices_down = [100.0 - i * 2.0 for i in range(30)]
    s_down = pd.Series(prices_down)
    rsi_down = compute_rsi(s_down, period=14)
    assert rsi_down.iloc[-1] < 5.0


def test_classify_regime_boundaries():
    assert classify_regime(50.0) == "SIDEWAYS"
    assert classify_regime(40.0) == "SIDEWAYS"
    assert classify_regime(60.0) == "SIDEWAYS"
    assert classify_regime(39.99) == "BEARISH"
    assert classify_regime(25.0) == "BEARISH"
    assert classify_regime(60.01) == "BULLISH"
    assert classify_regime(75.0) == "BULLISH"


def test_fetch_daily_nifty_rsi_mocked():
    mock_kite = MagicMock()
    # Mock instruments to find NIFTY 50 token
    mock_kite.instruments.return_value = [
        {"name": "NIFTY 50", "tradingsymbol": "NIFTY 50", "instrument_token": 256265, "exchange": "NSE"}
    ]
    # Generate 50 daily candles with sideways oscilation around 24000
    base_date = datetime(2026, 9, 1)
    candles = []
    for i in range(50):
        d = base_date + timedelta(days=i)
        close = 24000 + (50 if i % 2 == 0 else -50)
        candles.append({
            "date": d,
            "open": close - 10,
            "high": close + 20,
            "low": close - 20,
            "close": close,
            "volume": 100000
        })
    mock_kite.historical_data.return_value = candles

    rsi_val, regime, df = fetch_daily_nifty_rsi(mock_kite, days=50)
    assert 40.0 <= rsi_val <= 60.0
    assert regime == "SIDEWAYS"
    assert len(df) == 50
    assert "rsi" in df.columns


def test_get_monthly_expiry_selection():
    from strategy.positional_condor.strike_solver import get_monthly_expiry
    from datetime import date

    # Mock instruments across Sep, Oct, Nov 2026
    instruments = [
        {"name": "NIFTY", "expiry": "2026-09-17"},
        {"name": "NIFTY", "expiry": "2026-09-24"},  # Monthly Sep
        {"name": "NIFTY", "expiry": "2026-10-15"},
        {"name": "NIFTY", "expiry": "2026-10-29"},  # Monthly Oct
        {"name": "NIFTY", "expiry": "2026-11-26"},  # Monthly Nov
    ]

    # If today is 2026-09-01 (well before Sep expiry), select Sep monthly
    exp = get_monthly_expiry(instruments, current_date=date(2026, 9, 1))
    assert exp == date(2026, 9, 24)

    # If today is 2026-09-24 (expiry day or <= 2 days before), roll to Oct monthly
    exp_rolled = get_monthly_expiry(instruments, current_date=date(2026, 9, 24))
    assert exp_rolled == date(2026, 10, 29)


def test_get_directional_ratio_strikes_bearish():
    from strategy.positional_condor.strike_solver import get_directional_ratio_strikes
    from datetime import date

    expiry = date(2026, 11, 26)
    instruments = [
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23950, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23950PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23750, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23750PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 65},
    ]

    # Spot is 24010 -> ATM is 24000
    plan = get_directional_ratio_strikes(spot_price=24010.0, regime="BEARISH", expiry=expiry, instruments=instruments, lots=1)
    assert plan["atm_strike"] == 24000
    assert plan["regime"] == "BEARISH"
    assert plan["buy_leg_1"]["strike"] == 23950  # ATM - 50
    assert plan["buy_leg_1"]["quantity"] == 65   # 1 lot
    assert plan["sell_body"]["strike"] == 23750  # ATM - 250
    assert plan["sell_body"]["quantity"] == 130  # 2 lots
    assert plan["buy_leg_2"]["strike"] == 23700  # ATM - 300
    assert plan["buy_leg_2"]["quantity"] == 65   # 1 lot


def test_get_directional_ratio_strikes_bullish():
    from strategy.positional_condor.strike_solver import get_directional_ratio_strikes
    from datetime import date

    expiry = date(2026, 11, 26)
    instruments = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24050, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV24050CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24250, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV24250CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24300, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV24300CE", "lot_size": 65},
    ]

    # Spot is 23990 -> ATM is 24000
    plan = get_directional_ratio_strikes(spot_price=23990.0, regime="BULLISH", expiry=expiry, instruments=instruments, lots=2)
    assert plan["atm_strike"] == 24000
    assert plan["regime"] == "BULLISH"
    assert plan["buy_leg_1"]["strike"] == 24050  # ATM + 50
    assert plan["buy_leg_1"]["quantity"] == 130  # 2 lots
    assert plan["sell_body"]["strike"] == 24250  # ATM + 250
    assert plan["sell_body"]["quantity"] == 260  # 4 lots (2 * 2 lots)
    assert plan["buy_leg_2"]["strike"] == 24300  # ATM + 300
    assert plan["buy_leg_2"]["quantity"] == 130  # 2 lots


def test_solve_1_to_1_iron_condor():
    from strategy.positional_condor.strike_solver import solve_1_to_1_iron_condor
    from datetime import date

    expiry = date(2026, 11, 26)
    instruments = [
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23400, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23400PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24300, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV24300CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24600, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV24600CE", "lot_size": 65},
    ]

    # Put spread: Sell 23700 (90), Buy 23400 (30) -> 60 credit, width 300 -> Put Max Loss = 240
    # Call spread: Sell 24300 (100), Buy 24600 (35) -> 65 credit, width 300 -> Call Max Loss = 235
    # Total credit = 125, Width = 300 -> Max loss = 300 - 125 = 175
    # Ratio = 125 / 175 ≈ 0.71
    quotes = {
        "NFO:NIFTY26NOV23400PE": 30.0,
        "NFO:NIFTY26NOV23700PE": 90.0,
        "NFO:NIFTY26NOV24300CE": 100.0,
        "NFO:NIFTY26NOV24600CE": 35.0,
    }

    condor = solve_1_to_1_iron_condor(spot_price=24000.0, expiry=expiry, instruments=instruments, quotes=quotes, lots=1)
    assert condor["pe_buy"]["strike"] == 23400
    assert condor["pe_sell"]["strike"] == 23700
    assert condor["ce_sell"]["strike"] == 24300
    assert condor["ce_buy"]["strike"] == 24600
    assert condor["net_credit"] == 125.0
    assert condor["max_loss"] == 175.0
    assert condor["lower_breakeven"] == 23700 - 125.0
    assert condor["upper_breakeven"] == 24300 + 125.0


def test_positional_desk_state_persistence(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = PositionalDesk(order_manager=om, state_file=state_file)

    test_state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "regime": "SIDEWAYS",
        "monthly_realized_pnl": 1200.0
    }
    desk.save_state(test_state)
    assert state_file.exists()

    desk_recovered = PositionalDesk(order_manager=om, state_file=state_file)
    recovered = desk_recovered.load_state()
    assert recovered["month"] == "2026-11"
    assert recovered["monthly_realized_pnl"] == 1200.0


def test_positional_desk_target_circuit_breaker(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=state_file, notifier=mock_notifier, capital_per_lot=100000.0)

    # Active state with +4,200 Rs profit (+4.2% on 100k capital)
    state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "regime": "SIDEWAYS",
        "monthly_realized_pnl": 4200.0,
        "current_trade": None
    }
    desk.save_state(state)

    res = desk.check_circuit_breakers(unrealized_pnl=0.0)
    assert res == "TARGET_LOCKED"
    assert desk.state["status"] == "TARGET_LOCKED"
    mock_notifier.send_message.assert_called_once()
    assert "Target" in mock_notifier.send_message.call_args[0][0]


def test_positional_desk_stop_loss_circuit_breaker(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    mock_notifier = MagicMock()
    # Default monthly_stop_pct is 0.03 (3.0% = Rs 3,000 on 100k capital)
    desk = PositionalDesk(order_manager=om, state_file=state_file, notifier=mock_notifier, capital_per_lot=100000.0)

    # Active state with -2,500 Rs loss (-2.5% on 100k capital) -> should remain ACTIVE (no circuit breaker)
    state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "regime": "SIDEWAYS",
        "monthly_realized_pnl": -2500.0,
        "current_trade": None
    }
    desk.save_state(state)
    assert desk.check_circuit_breakers(unrealized_pnl=0.0) is None
    assert desk.state["status"] == "ACTIVE"

    # Active state with -3,100 Rs loss (-3.1% on 100k capital) -> should trigger STOPPED_OUT
    state["monthly_realized_pnl"] = -3100.0
    desk.save_state(state)
    res = desk.check_circuit_breakers(unrealized_pnl=0.0)
    assert res == "STOPPED_OUT"
    assert desk.state["status"] == "STOPPED_OUT"
    mock_notifier.send_message.assert_called_once()
    assert "Max Loss" in mock_notifier.send_message.call_args[0][0]


def test_positional_desk_evaluate_daily_close_bearish_deploy(tmp_path):
    from unittest.mock import patch
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager
    from datetime import date

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = PositionalDesk(order_manager=om, state_file=state_file)

    mock_kite = MagicMock()
    # Mock NFO instruments
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23950, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23950PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23750, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23750PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 65},
    ]
    # Mock spot LTP
    mock_kite.ltp.return_value = {
        "NSE:NIFTY 50": {"last_price": 24000.0}
    }

    # Patch fetch_daily_nifty_rsi to return RSI = 35.0 (BEARISH)
    with patch("strategy.positional_condor.condor_desk.fetch_daily_nifty_rsi", return_value=(35.0, "BEARISH", pd.DataFrame())):
        state = desk.evaluate_daily_close(kite=mock_kite, force_date=date(2026, 11, 3), ignore_holidays=True)

    assert state is not None
    assert state["status"] == "ACTIVE"
    assert state["regime"] == "BEARISH"
    assert state["current_trade"] is not None
    assert state["current_trade"]["type"] == "DIRECTIONAL_RATIO"
    assert len(state["current_trade"]["legs"]) == 3


def test_positional_desk_evaluate_daily_close_holiday_skip(tmp_path):
    from unittest.mock import patch
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = PositionalDesk(order_manager=om, state_file=state_file)

    mock_kite = MagicMock()

    with patch("strategy.positional_condor.condor_desk.is_trading_holiday", return_value=(True, "Diwali Laxmi Pujan")):
        res = desk.evaluate_daily_close(kite=mock_kite, ignore_holidays=False)

    assert res["status"] == "HOLIDAY"
    assert res["reason"] == "Diwali Laxmi Pujan"
    assert desk.state is None


def test_run_positional_monthly_backtest_simulation():
    from strategy.positional_condor.backtest import run_positional_monthly_backtest

    # Generate 60 days of synthetic daily candles spanning two months
    dates = pd.date_range("2026-08-01", periods=60, freq="B")
    prices = [24000.0 + (i * 10.0 if i < 30 else - (i - 30) * 15.0) for i in range(len(dates))]
    df = pd.DataFrame({
        "date": dates,
        "open": prices,
        "high": [p + 50.0 for p in prices],
        "low": [p - 50.0 for p in prices],
        "close": prices,
        "volume": 100000
    })

    summary = run_positional_monthly_backtest(df, initial_capital=100000.0, lots=1)
    assert summary["total_months"] >= 1
    assert "profitable_months" in summary
    assert "total_net_pnl" in summary
    assert len(summary["monthly_records"]) >= 1


def test_run_positional_cli_flags():
    from unittest.mock import patch
    from run_positional_condor import run_positional_cli

    with patch("run_positional_condor.load_config"), \
         patch("run_positional_condor.get_kite_client") as mock_kite_fn, \
         patch("run_positional_condor.PositionalDesk") as mock_desk_cls:

        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk.evaluate_daily_close.return_value = {
            "status": "DRY_RUN",
            "regime": "SIDEWAYS",
            "month": "2026-11",
            "expiry": "2026-11-26",
            "monthly_realized_pnl": 0.0
        }
        mock_desk_cls.return_value = mock_desk

        exit_code = run_positional_cli(["--paper", "--dry-run", "--lots", "2"])
        assert exit_code == 0
        mock_desk.evaluate_daily_close.assert_called_once_with(
            kite=mock_kite,
            execute_once=True,
            dry_run=True,
            ignore_holidays=False
        )


def test_run_positional_cli_daemon_mode():
    from unittest.mock import patch
    from run_positional_condor import run_positional_cli

    with patch("run_positional_condor.load_config"), \
         patch("run_positional_condor.get_kite_client") as mock_kite_fn, \
         patch("run_positional_condor.PositionalDesk") as mock_desk_cls:

        mock_kite = MagicMock()
        mock_kite_fn.return_value = mock_kite
        mock_desk = MagicMock()
        mock_desk_cls.return_value = mock_desk

        # Default mode (no --dry-run and no --once) should call run_scheduler
        exit_code = run_positional_cli(["--paper", "--lots", "1", "--schedule-time", "15:20"])
        assert exit_code == 0
        mock_desk.run_scheduler.assert_called_once_with(
            kite=mock_kite,
            schedule_time="15:20",
            ignore_holidays=False
        )


def test_positional_desk_run_scheduler(tmp_path):
    from unittest.mock import patch, MagicMock
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    mock_kite = MagicMock()

    # Mock datetime so current time is 15:22 on a weekday (Monday)
    fake_now = datetime(2026, 11, 2, 15, 22, 0)
    with patch("strategy.positional_condor.condor_desk.datetime") as mock_dt, \
         patch.object(desk, "evaluate_daily_close") as mock_eval, \
         patch("strategy.positional_condor.condor_desk.time.sleep") as mock_sleep:

        mock_dt.now.return_value = fake_now
        desk.run_scheduler(kite=mock_kite, schedule_time="15:20", poll_interval=1, max_iterations=1)

        mock_eval.assert_called_once_with(kite=mock_kite, ignore_holidays=False)
        mock_sleep.assert_called_once_with(1)


def test_positional_desk_close_current_trade_sends_telegram_alert(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "regime": "BEARISH",
        "monthly_realized_pnl": 0.0,
        "current_trade": {
            "type": "DIRECTIONAL_RATIO",
            "regime": "BEARISH",
            "legs": [
                {"symbol": "NIFTY26NOV23950PE", "action": "BUY", "qty": 65, "entry_price": 100.0},
                {"symbol": "NIFTY26NOV23750PE", "action": "SELL", "qty": 130, "entry_price": 50.0},
                {"symbol": "NIFTY26NOV23700PE", "action": "BUY", "qty": 65, "entry_price": 40.0},
            ]
        }
    }
    desk.save_state(state)

    mock_kite = MagicMock()
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV23950PE": {"last_price": 120.0},
        "NFO:NIFTY26NOV23750PE": {"last_price": 30.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 25.0},
    }

    trade_pnl = desk.close_current_trade(mock_kite, reason="REGIME_TRANSITION")
    assert trade_pnl != 0.0
    mock_notifier.send_message.assert_called_once()
    msg = mock_notifier.send_message.call_args[0][0]
    assert "Trade Exited" in msg
    assert "REGIME_TRANSITION" in msg
    assert "Trade Realized P&L" in msg


def test_positional_desk_reconcile_with_broker_closed_legs(tmp_path):
    from datetime import date
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="LIVE", kite=MagicMock())
    desk = PositionalDesk(order_manager=om, state_file=state_file)

    # State records an open trade
    state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "regime": "BEARISH",
        "monthly_realized_pnl": 0.0,
        "current_trade": {
            "type": "DIRECTIONAL_RATIO",
            "legs": [
                {"symbol": "NIFTY26NOV23950PE", "action": "BUY", "qty": 65},
                {"symbol": "NIFTY26NOV23750PE", "action": "SELL", "qty": 130},
                {"symbol": "NIFTY26NOV23700PE", "action": "BUY", "qty": 65},
            ]
        }
    }
    desk.save_state(state)

    # Kite returns 0 quantity for these legs (e.g. manually closed on mobile)
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {"tradingsymbol": "NIFTY26NOV23950PE", "exchange": "NFO", "quantity": 0},
            {"tradingsymbol": "NIFTY26NOV23750PE", "exchange": "NFO", "quantity": 0},
            {"tradingsymbol": "NIFTY26NOV23700PE", "exchange": "NFO", "quantity": 0},
        ]
    }

    desk.reconcile_with_broker(mock_kite, state, expiry_date=date(2026, 11, 26))
    assert state["current_trade"] is None
    loaded = desk.load_state()
    assert loaded["current_trade"] is None


def test_positional_desk_reconcile_with_broker_reconstruct_open_legs(tmp_path):
    from datetime import date
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="LIVE", kite=MagicMock())
    desk = PositionalDesk(order_manager=om, state_file=state_file)

    # State has no active trade (e.g. lost file or new container)
    state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "regime": "SIDEWAYS",
        "monthly_realized_pnl": 0.0,
        "current_trade": None
    }
    desk.save_state(state)

    # Kite has 4 open legs for an Iron Condor
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {"tradingsymbol": "NIFTY26NOV23400PE", "exchange": "NFO", "quantity": 65, "buy_price": 30.0},
            {"tradingsymbol": "NIFTY26NOV23700PE", "exchange": "NFO", "quantity": -65, "sell_price": 90.0},
            {"tradingsymbol": "NIFTY26NOV24300CE", "exchange": "NFO", "quantity": -65, "sell_price": 100.0},
            {"tradingsymbol": "NIFTY26NOV24600CE", "exchange": "NFO", "quantity": 65, "buy_price": 35.0},
        ]
    }

    desk.reconcile_with_broker(mock_kite, state, expiry_date=date(2026, 11, 26))
    assert state["current_trade"] is not None
    assert state["current_trade"]["type"] == "IRON_CONDOR"
    assert len(state["current_trade"]["legs"]) == 4
    loaded = desk.load_state()
    assert loaded["current_trade"] is not None
    assert loaded["current_trade"]["type"] == "IRON_CONDOR"


def test_positional_desk_regime_transition_exit_and_deploy(tmp_path):
    from unittest.mock import patch
    from datetime import date
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    state_file = tmp_path / "condor_state.json"
    om = OrderManager(mode="PAPER", kite=None)
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    # Yesterday: RSI was 42.0 and state had an active Iron Condor in SIDEWAYS regime
    state = {
        "month": "2026-11",
        "expiry": "2026-11-26",
        "status": "ACTIVE",
        "regime": "SIDEWAYS",
        "last_rsi": 45.0,
        "current_rsi": 42.0,
        "monthly_realized_pnl": 0.0,
        "current_trade": {
            "type": "IRON_CONDOR",
            "regime": "SIDEWAYS",
            "expiry": "2026-11-26",
            "legs": [
                {"symbol": "NIFTY26NOV23400PE", "action": "BUY", "qty": 65, "entry_price": 30.0},
                {"symbol": "NIFTY26NOV23700PE", "action": "SELL", "qty": 65, "entry_price": 90.0},
                {"symbol": "NIFTY26NOV24300CE", "action": "SELL", "qty": 65, "entry_price": 100.0},
                {"symbol": "NIFTY26NOV24600CE", "action": "BUY", "qty": 65, "entry_price": 35.0},
            ]
        }
    }
    desk.save_state(state)

    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23750, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23750PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23550, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23550PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23500, "expiry": "2026-11-26", "tradingsymbol": "NIFTY26NOV23500PE", "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NSE:NIFTY 50": {"last_price": 23800.0},
        "NFO:NIFTY26NOV23400PE": {"last_price": 25.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 75.0},
        "NFO:NIFTY26NOV24300CE": {"last_price": 80.0},
        "NFO:NIFTY26NOV24600CE": {"last_price": 20.0},
    }

    # Today: RSI dropped to 39.0 (BEARISH)
    df_rsi = pd.DataFrame({"rsi": [42.0, 39.0]})
    with patch("strategy.positional_condor.condor_desk.fetch_daily_nifty_rsi", return_value=(39.0, "BEARISH", df_rsi)):
        res = desk.evaluate_daily_close(kite=mock_kite, force_date=date(2026, 11, 4), ignore_holidays=True)

    # 1. State must show transition from 42.0 to 39.0
    assert res["last_rsi"] == 42.0
    assert res["current_rsi"] == 39.0
    assert res["regime"] == "BEARISH"

    # 2. Previous Iron Condor was exited and new 1:2:1 Ratio deployed
    assert res["current_trade"] is not None
    assert res["current_trade"]["type"] == "DIRECTIONAL_RATIO"
    assert res["current_trade"]["regime"] == "BEARISH"
    assert len(res["current_trade"]["legs"]) == 3

    # 3. History log recorded transition
    assert len(res["history"]) >= 1
    assert res["history"][-1]["action"] == "TRANSITION_SIDEWAYS_TO_BEARISH"
    assert res["history"][-1]["prev_rsi"] == 42.0
    assert res["history"][-1]["current_rsi"] == 39.0

    # 4. Telegram alert was sent for transition
    sent_texts = [call[0][0] for call in mock_notifier.send_message.call_args_list]
    assert any("Regime Transition" in t for t in sent_texts)


def test_positional_desk_close_current_trade_handles_partial_failure(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk

    state_file = tmp_path / "condor_state.json"
    om = MagicMock()
    om.mode = "LIVE"
    # First leg (BUY) succeeds with sell, second leg (SELL) fails with buy_to_cover, third leg (BUY) succeeds with sell
    om.sell.side_effect = [{"order_id": "SELL1"}, {"order_id": "SELL3"}]
    om.buy_to_cover.side_effect = [RuntimeError("Kite connection dropped")]
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=state_file, notifier=mock_notifier)

    state = {
        "month": "2026-11",
        "status": "ACTIVE",
        "monthly_realized_pnl": 0.0,
        "current_trade": {
            "type": "DIRECTIONAL_RATIO",
            "legs": [
                {"symbol": "LEG1", "action": "BUY", "qty": 65, "entry_price": 100.0},
                {"symbol": "LEG2", "action": "SELL", "qty": 130, "entry_price": 50.0},
                {"symbol": "LEG3", "action": "BUY", "qty": 65, "entry_price": 40.0},
            ]
        }
    }
    desk.save_state(state)

    mock_kite = MagicMock()
    mock_kite.ltp.return_value = {
        "NFO:LEG1": {"last_price": 110.0},
        "NFO:LEG2": {"last_price": 45.0},
        "NFO:LEG3": {"last_price": 35.0},
    }

    # Should not throw exception, should keep failed leg in state, and alert Telegram
    trade_pnl = desk.close_current_trade(mock_kite, reason="EXIT_TEST")
    saved = desk.load_state()
    assert saved["current_trade"] is not None
    assert len(saved["current_trade"]["legs"]) == 1
    assert saved["current_trade"]["legs"][0]["symbol"] == "LEG2"
    mock_notifier.send_message.assert_called_once()
    assert "Positional Trade Exit Warning" in mock_notifier.send_message.call_args[0][0]


def test_positional_desk_deploy_iron_condor_rollback_on_failure(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk

    state_file = tmp_path / "condor_state.json"
    om = MagicMock()
    om.mode = "LIVE"
    # Wings succeed, selling body leg 1 fails
    om.buy_to_cover.return_value = {"order_id": "WING"}
    om.sell.side_effect = RuntimeError("Margin limit exceeded")
    desk = PositionalDesk(order_manager=om, state_file=state_file)

    condor_setup = {
        "total_quantity": 65,
        "expiry": "2026-11-26",
        "net_credit": 100.0,
        "max_loss": 100.0,
        "lower_breakeven": 23000,
        "upper_breakeven": 25000,
        "pe_buy": {"tradingsymbol": "PE_WING", "strike": 23500, "ltp": 10.0},
        "pe_sell": {"tradingsymbol": "PE_BODY", "strike": 23800, "ltp": 30.0},
        "ce_sell": {"tradingsymbol": "CE_BODY", "strike": 24200, "ltp": 30.0},
        "ce_buy": {"tradingsymbol": "CE_WING", "strike": 24500, "ltp": 10.0},
    }

    with pytest.raises(RuntimeError, match="Margin limit exceeded"):
        desk.deploy_iron_condor(condor_setup)

    # Rollback should sell the bought wings back
    assert om.sell.call_count >= 2  # Attempted sell + rollbacks











def test_positional_desk_save_state_fallback_on_os_error(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    # Simulating the exact error: [Errno 2] No such file or directory: '/app/data/positional_condor_state.tmp'
    primary_state_file = Path('/app/data/positional_condor_state.json')
    om = OrderManager(mode='PAPER', kite=None)
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=primary_state_file, notifier=mock_notifier)

    # Custom temp dir for fallback isolation
    custom_temp = tmp_path / 'custom_temp'
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(custom_temp))

    test_state = {
        'month': '2026-11',
        'status': 'ACTIVE',
        'regime': 'SIDEWAYS',
        'monthly_realized_pnl': 500.0
    }

    original_open = open
    def mock_open_fn(file, mode='r', *args, **kwargs):
        s = str(file)
        if 'positional_condor_state' in s and 'custom_temp' not in s:
            raise FileNotFoundError(2, "No such file or directory: '/app/data/positional_condor_state.tmp'")
        return original_open(file, mode, *args, **kwargs)

    with patch('builtins.open', side_effect=mock_open_fn):
        # Should not raise FileNotFoundError / OSError
        desk.save_state(test_state)

    # In-memory state is preserved
    assert desk.state == test_state

    # Fallback file exists in fallback location
    fallback_file = custom_temp / 'trading_state' / 'positional_condor_state.json'
    assert fallback_file.exists()

    # Telegram warning was delivered
    mock_notifier.send_message.assert_called_once()
    assert "State Warning" in mock_notifier.send_message.call_args[0][0]

    # load_state recovers state from fallback when primary is missing
    desk_new = PositionalDesk(order_manager=om, state_file=primary_state_file)
    loaded = desk_new.load_state()
    assert loaded is not None
    assert loaded['monthly_realized_pnl'] == 500.0


def test_run_positional_cli_state_file_and_data_dir_args():
    from unittest.mock import patch
    from pathlib import Path
    from run_positional_condor import run_positional_cli

    with patch('run_positional_condor.load_config'), \
         patch('run_positional_condor.get_kite_client'), \
         patch('run_positional_condor.PositionalDesk') as mock_desk_cls:
        instance = mock_desk_cls.return_value
        instance.evaluate_daily_close.return_value = {'status': 'TEST'}

        exit_code = run_positional_cli(['--paper', '--dry-run', '--state-file', '/custom/state.json'])
        assert exit_code == 0
        _, kwargs = mock_desk_cls.call_args
        assert str(kwargs.get('state_file')) == str(Path('/custom/state.json'))


def test_positional_desk_evaluate_daily_close_survives_state_save_error(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    from datetime import date
    import pandas as pd
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    primary_state_file = Path('/app/data/positional_condor_state.json')
    om = OrderManager(mode='PAPER', kite=None)
    mock_notifier = MagicMock()
    desk = PositionalDesk(order_manager=om, state_file=primary_state_file, notifier=mock_notifier)

    custom_temp = tmp_path / 'custom_temp'
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(custom_temp))

    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"tradingsymbol": "NIFTY26NOV24000CE", "name": "NIFTY", "expiry": "2026-11-26", "instrument_type": "CE", "strike": 24000},
        {"tradingsymbol": "NIFTY26NOV24000PE", "name": "NIFTY", "expiry": "2026-11-26", "instrument_type": "PE", "strike": 24000},
    ]

    dates = pd.date_range(end="2026-11-04", periods=30)
    df_daily = pd.DataFrame({"close": [24000.0] * 30, "rsi": [50.0] * 30}, index=dates)

    original_open = open
    def mock_open_fn(file, mode='r', *args, **kwargs):
        s = str(file)
        if "positional_condor_state" in s and "custom_temp" not in s:
            raise FileNotFoundError(2, "No such file or directory: '/app/data/positional_condor_state.tmp'")
        return original_open(file, mode, *args, **kwargs)

    with patch('builtins.open', side_effect=mock_open_fn), \
         patch('strategy.positional_condor.condor_desk.fetch_daily_nifty_rsi', return_value=(50.0, 'SIDEWAYS', df_daily)), \
         patch('strategy.positional_condor.condor_desk.solve_1_to_1_iron_condor', return_value={
             "total_quantity": 65, "expiry": "2026-11-26", "net_credit": 50.0, "max_loss": 50.0,
             "pe_buy": {"tradingsymbol": "PE_BUY", "strike": 23500, "ltp": 10.0},
             "pe_sell": {"tradingsymbol": "PE_SELL", "strike": 23800, "ltp": 30.0},
             "ce_sell": {"tradingsymbol": "CE_SELL", "strike": 24200, "ltp": 30.0},
             "ce_buy": {"tradingsymbol": "CE_BUY", "strike": 24500, "ltp": 10.0},
         }):
        res = desk.evaluate_daily_close(kite=mock_kite, force_date=date(2026, 11, 4), dry_run=True, ignore_holidays=True)
        assert res.get("status") == "DRY_RUN"
        assert desk.state is not None
        assert desk.state.get("month") == "2026-11"


def test_create_fresh_positional_state():
    from strategy.positional_condor.condor_desk import create_fresh_positional_state
    from datetime import date
    import re

    state = create_fresh_positional_state()
    assert state["status"] == "IDLE"
    assert state["regime"] == "UNKNOWN"
    assert state["monthly_realized_pnl"] == 0.0
    assert state["current_trade"] is None
    assert state["history"] == []
    assert re.match(r"^\d{4}-\d{2}$", state["month"])
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", state["expiry"])


def test_positional_desk_creates_fresh_state_if_not_present(tmp_path):
    from strategy.positional_condor.condor_desk import PositionalDesk
    from strategy.order_manager import OrderManager

    fresh_dir = tmp_path / "sub_data"
    state_file = fresh_dir / "condor_state.json"
    assert not state_file.exists()

    om = OrderManager(mode="PAPER", kite=None)
    desk = PositionalDesk(order_manager=om, state_file=state_file, auto_init_state=False)
    assert not state_file.exists()

    # Call ensure_state_file
    state = desk.ensure_state_file()
    assert state_file.exists()
    assert state["status"] == "IDLE"
    assert state["monthly_realized_pnl"] == 0.0

    # Verify reloading from disk
    loaded = desk.load_state()
    assert loaded is not None
    assert loaded["month"] == state["month"]
    assert loaded["expiry"] == state["expiry"]
