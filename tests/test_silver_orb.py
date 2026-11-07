import json
from datetime import date, datetime, time as dt_time, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from strategy.silver_orb.config import SilverORBConfig
from strategy.silver_orb.contract_resolver import resolve_active_silverm_contract
from strategy.silver_orb.order_manager import SilverOrderManager
from strategy.silver_orb.desk import SilverORBDesk


def test_silver_orb_config_defaults():
    cfg = SilverORBConfig()
    assert cfg.lots == 2
    assert cfg.lot_size_kg == 5.0
    assert cfg.point_value_per_lot == 5.0
    assert cfg.initial_sl_pts == 500.0
    assert cfg.target1_pts == 800.0
    assert cfg.ref_candle_time == dt_time(17, 45)
    assert cfg.trade_window_start == dt_time(18, 0)
    assert cfg.trade_window_end == dt_time(19, 30)
    assert cfg.session_cutoff_time == dt_time(22, 45)
    assert cfg.mode == "PAPER"
    assert cfg.product == "MIS"
    assert cfg.exchange == "MCX"


def test_resolve_active_silverm_contract_fallback():
    contract = resolve_active_silverm_contract(kite=None)
    assert "SILVERM" in contract["tradingsymbol"]
    assert contract["instrument_token"] == 123668487


def test_resolve_active_silverm_contract_mock():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"tradingsymbol": "SILVERM26NOVFUT", "instrument_type": "FUT", "instrument_token": 111, "expiry": date.today() + timedelta(days=2)},  # Expiring soon (< 3 days)
        {"tradingsymbol": "SILVERM26DECFUT", "instrument_type": "FUT", "instrument_token": 222, "expiry": date.today() + timedelta(days=25)}, # Valid near month
        {"tradingsymbol": "SILVERM27JANFUT", "instrument_type": "FUT", "instrument_token": 333, "expiry": date.today() + timedelta(days=55)}, # Next month
        {"tradingsymbol": "CRUDEOIL26NOVFUT", "instrument_type": "FUT", "instrument_token": 444, "expiry": date.today() + timedelta(days=20)},
    ]
    contract = resolve_active_silverm_contract(mock_kite, min_days_to_expiry=3)
    assert contract["tradingsymbol"] == "SILVERM26DECFUT"
    assert contract["instrument_token"] == 222


def test_silver_order_manager_paper():
    om = SilverOrderManager(mode="PAPER")
    entry = om.place_entry("SILVERM26NOVFUT", "LONG", 2, 185000.0, 184500.0, 185800.0)
    assert entry["status"] == "COMPLETE"
    assert entry["quantity"] == 2
    assert entry["transaction_type"] == "BUY"

    exit_res = om.place_exit("SILVERM26NOVFUT", "LONG", 1, 185800.0, "TARGET_1_HIT")
    assert exit_res["status"] == "COMPLETE"
    assert exit_res["quantity"] == 1
    assert exit_res["transaction_type"] == "SELL"


def test_silver_desk_state_persistence(tmp_path):
    state_file = tmp_path / "silver_state.json"
    cfg = SilverORBConfig(state_file=str(state_file))
    desk = SilverORBDesk(config=cfg, kite=None)

    state = desk.initialize_day(today_d=date(2026, 11, 7))
    assert state_file.exists()
    assert state["contract"]["tradingsymbol"] == "SILVERM26NOVFUT"
    assert state["status"] == "IDLE"

    loaded = desk.load_state()
    assert loaded["date"] == "2026-11-07"
    assert loaded["realized_pnl_rs"] == 0.0


def test_evaluate_breakout_signal():
    cfg = SilverORBConfig(use_trend_filter=True)
    desk = SilverORBDesk(config=cfg, kite=None)

    # Build 60 synthetic 15m candles
    dates = pd.date_range("2026-11-07 09:15", periods=60, freq="15min")
    # Upward trend above 50 EMA
    prices = [180000.0 + (i * 50.0) for i in range(60)]
    df = pd.DataFrame({
        "date": dates,
        "time": [d.time() for d in dates],
        "open": prices,
        "high": [p + 20.0 for p in prices],
        "low": [p - 20.0 for p in prices],
        "close": [p + 10.0 for p in prices],
        "volume": [100.0] * 59 + [300.0],  # Volume spike on last bar
    })

    ref_h = 182500.0
    ref_l = 182000.0
    # Last price is around 183000 > ref_h and > 50 EMA
    sig = desk.evaluate_breakout_signal(df, ref_h, ref_l, dt_time(18, 15))
    assert sig is not None
    assert sig["direction"] == "LONG"
    assert sig["trigger_price"] == ref_h + 5.0


def test_2lot_tranche_target1_and_breakeven_runner(tmp_path):
    state_file = tmp_path / "silver_state.json"
    notifier_mock = MagicMock()
    om = SilverOrderManager(mode="PAPER")
    cfg = SilverORBConfig(state_file=str(state_file), lots=2, initial_sl_pts=500.0, target1_pts=800.0)
    desk = SilverORBDesk(config=cfg, kite=None, order_manager=om, notifier=notifier_mock)

    # Initialize state with active LONG trade entered at 185,000
    desk.state = {
        "date": "2026-11-07",
        "status": "ACTIVE_TRADE",
        "contract": {"tradingsymbol": "SILVERM26NOVFUT", "instrument_token": 123},
        "realized_pnl_rs": 0.0,
        "trade": {
            "symbol": "SILVERM26NOVFUT",
            "direction": "LONG",
            "entry_price": 185000.0,
            "initial_sl": 184500.0,
            "current_sl": 184500.0,
            "target1_price": 185800.0,
            "lots_total": 2,
            "lots_open": 2,
            "lot1_closed": False,
            "lot1_exit_price": None,
            "lot1_pnl_rs": 0.0,
            "lot2_closed": False,
            "lot2_exit_price": None,
            "lot2_pnl_rs": 0.0,
            "be_moved": False,
            "status": "OPEN"
        }
    }
    desk.save_state(desk.state)

    # Step 1: Price rallies to 185,850 -> Hits Target 1 (+800 pts)
    desk.monitor_active_trade(current_ltp=185850.0, current_time=dt_time(18, 45))
    trade = desk.state["trade"]
    assert trade["lot1_closed"] is True
    assert trade["lot1_pnl_rs"] == 4000.0  # 800 pts * Rs 5 = Rs 4,000
    assert trade["lots_open"] == 1
    assert trade["current_sl"] == 185000.0  # Trailed to Breakeven
    assert trade["be_moved"] is True
    assert desk.state["realized_pnl_rs"] == 4000.0
    notifier_mock.send_message.assert_called_once()
    assert "TARGET 1 HIT" in notifier_mock.send_message.call_args[0][0]
    assert "Total Day Realized P&L" in notifier_mock.send_message.call_args[0][0]

    notifier_mock.reset_mock()

    # Step 2: Price drops back to 184,950 -> Hits Breakeven SL on Lot 2
    desk.monitor_active_trade(current_ltp=184950.0, current_time=dt_time(19, 30))
    trade = desk.state["trade"]
    assert trade["lot2_closed"] is True
    assert trade["lot2_pnl_rs"] == 0.0  # Flat at cost
    assert trade["lots_open"] == 0
    assert trade["status"] == "CLOSED"
    assert desk.state["status"] == "COMPLETED"
    assert desk.state["realized_pnl_rs"] == 4000.0  # Kept +Rs 4,000 profit!
    notifier_mock.send_message.assert_called_once()
    assert "RUNNER EXITED" in notifier_mock.send_message.call_args[0][0]
    assert "TRAILED_BREAKEVEN_EXIT" in notifier_mock.send_message.call_args[0][0]


def test_initial_stop_loss_hit(tmp_path):
    state_file = tmp_path / "silver_state.json"
    notifier_mock = MagicMock()
    om = SilverOrderManager(mode="PAPER")
    cfg = SilverORBConfig(state_file=str(state_file), lots=2, initial_sl_pts=500.0)
    desk = SilverORBDesk(config=cfg, kite=None, order_manager=om, notifier=notifier_mock)

    desk.state = {
        "date": "2026-11-07",
        "status": "ACTIVE_TRADE",
        "contract": {"tradingsymbol": "SILVERM26NOVFUT", "instrument_token": 123},
        "realized_pnl_rs": 0.0,
        "trade": {
            "symbol": "SILVERM26NOVFUT",
            "direction": "LONG",
            "entry_price": 185000.0,
            "initial_sl": 184500.0,
            "current_sl": 184500.0,
            "target1_price": 185800.0,
            "lots_total": 2,
            "lots_open": 2,
            "lot1_closed": False,
            "lot1_exit_price": None,
            "lot1_pnl_rs": 0.0,
            "lot2_closed": False,
            "lot2_exit_price": None,
            "lot2_pnl_rs": 0.0,
            "be_moved": False,
            "status": "OPEN"
        }
    }
    desk.save_state(desk.state)

    # Price drops directly to 184,400 -> Hits initial SL before Target 1
    desk.monitor_active_trade(current_ltp=184400.0, current_time=dt_time(18, 20))
    trade = desk.state["trade"]
    assert trade["status"] == "CLOSED"
    assert trade["lots_open"] == 0
    assert desk.state["status"] == "COMPLETED"
    assert desk.state["realized_pnl_rs"] == -5000.0  # 500 pts * Rs 10 = -Rs 5,000
    notifier_mock.send_message.assert_called_once()
    assert "STOP LOSS HIT" in notifier_mock.send_message.call_args[0][0]
    assert "₹-5,000.00" in notifier_mock.send_message.call_args[0][0]


def test_runner_eod_exit_at_2245(tmp_path):
    state_file = tmp_path / "silver_state.json"
    notifier_mock = MagicMock()
    om = SilverOrderManager(mode="PAPER")
    cfg = SilverORBConfig(state_file=str(state_file), lots=2)
    desk = SilverORBDesk(config=cfg, kite=None, order_manager=om, notifier=notifier_mock)

    desk.state = {
        "date": "2026-11-07",
        "status": "ACTIVE_TRADE",
        "contract": {"tradingsymbol": "SILVERM26NOVFUT", "instrument_token": 123},
        "realized_pnl_rs": 4000.0,  # Lot 1 already banked
        "trade": {
            "symbol": "SILVERM26NOVFUT",
            "direction": "LONG",
            "entry_price": 185000.0,
            "initial_sl": 184500.0,
            "current_sl": 185000.0,  # At breakeven
            "target1_price": 185800.0,
            "lots_total": 2,
            "lots_open": 1,
            "lot1_closed": True,
            "lot1_exit_price": 185800.0,
            "lot1_pnl_rs": 4000.0,
            "lot2_closed": False,
            "lot2_exit_price": None,
            "lot2_pnl_rs": 0.0,
            "be_moved": True,
            "status": "OPEN"
        }
    }
    desk.save_state(desk.state)

    # 22:45 IST session cutoff arrives with LTP at 186,500 (+1,500 pts runner gain!)
    desk.monitor_active_trade(current_ltp=186500.0, current_time=dt_time(22, 45))
    trade = desk.state["trade"]
    assert trade["lot2_closed"] is True
    assert trade["lot2_exit_price"] == 186500.0
    assert trade["lot2_pnl_rs"] == 7500.0  # 1500 pts * Rs 5 = Rs 7,500
    assert desk.state["realized_pnl_rs"] == 11500.0  # 4000 + 7500 = Rs 11,500
    notifier_mock.send_message.assert_called_once()
    assert "RUNNER EXITED" in notifier_mock.send_message.call_args[0][0]
    assert "SESSION_CUTOFF_EXIT" in notifier_mock.send_message.call_args[0][0]
    assert "₹+11,500.00" in notifier_mock.send_message.call_args[0][0]


def test_silver_order_manager_live_limit_orders():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_123"
    mock_kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 185012.0}]

    om = SilverOrderManager(
        mode="LIVE",
        kite=mock_kite,
        order_type="LIMIT",
        limit_buffer_pts=15.0,
    )

    # Test Long Entry
    res = om.place_entry("SILVERM26NOVFUT", "LONG", 2, 185000.0, 184500.0, 185800.0)
    assert res["order_id"] == "ORDER_123"
    assert res["status"] == "COMPLETE"
    assert res["average_price"] == 185012.0

    call_kwargs = mock_kite.place_order.call_args[1]
    assert call_kwargs["order_type"] == mock_kite.ORDER_TYPE_LIMIT
    assert call_kwargs["price"] == 185015.0  # 185000 + 15 buffer

    # Test Long Exit
    mock_kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 185785.0}]
    exit_res = om.place_exit("SILVERM26NOVFUT", "LONG", 1, 185800.0, "TARGET_1_HIT")
    assert exit_res["status"] == "COMPLETE"
    exit_kwargs = mock_kite.place_order.call_args[1]
    assert exit_kwargs["order_type"] == mock_kite.ORDER_TYPE_LIMIT
    assert exit_kwargs["price"] == 185785.0  # 185800 - 15 buffer

