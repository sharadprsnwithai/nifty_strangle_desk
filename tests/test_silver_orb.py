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

    # Verify repeated calls for the same day return in-memory state without redundant disk loads
    with patch.object(desk, "load_state") as mock_load:
        state_again = desk.initialize_day(today_d=date(2026, 11, 7))
        mock_load.assert_not_called()
        assert state_again == state


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


def test_4lot_tranche_scaling(tmp_path):
    state_file = tmp_path / "silver_state.json"
    notifier_mock = MagicMock()
    om = SilverOrderManager(mode="PAPER")
    cfg = SilverORBConfig(state_file=str(state_file), lots=4, initial_sl_pts=500.0, target1_pts=800.0)
    desk = SilverORBDesk(config=cfg, kite=None, order_manager=om, notifier=notifier_mock)

    # Initialize state with active LONG trade entered with 4 lots at 185,000
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
            "lots_total": 4,
            "lots_open": 4,
            "lot1_closed": False,
            "lot1_exit_price": None,
            "lot1_pnl_rs": 0.0,
            "lot2_closed": False,
            "lot2_exit_price": None,
            "lot2_pnl_rs": 0.0,
            "be_moved": False,
            "status": "OPEN",
        },
    }
    desk.save_state(desk.state)

    # Step 1: Hits Target 1 (+800 pts) -> 2 lots exited at target, 2 lots runner trailed to BE
    desk.monitor_active_trade(current_ltp=185850.0, current_time=dt_time(18, 45))
    trade = desk.state["trade"]
    assert trade["lot1_closed"] is True
    # 2 lots * 800 pts * Rs 5 = Rs 8,000
    assert trade["lot1_pnl_rs"] == 8000.0
    assert trade["lots_open"] == 2
    assert trade["current_sl"] == 185000.0  # Trailed to Breakeven
    assert trade["be_moved"] is True
    assert desk.state["realized_pnl_rs"] == 8000.0

    # Step 2: Session close at 22:45 -> 2 lots runner squared off at 186,000 (+1,000 pts)
    desk.monitor_active_trade(current_ltp=186000.0, current_time=dt_time(22, 45))
    trade = desk.state["trade"]
    assert trade["lot2_closed"] is True
    # 2 lots * 1000 pts * Rs 5 = Rs 10,000
    assert trade["lot2_pnl_rs"] == 10000.0
    assert trade["lots_open"] == 0
    assert desk.state["realized_pnl_rs"] == 18000.0  # 8k + 10k = 18k!


def test_silver_order_manager_live_market_orders():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_MKT_1"
    mock_kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 185025.0}]

    om = SilverOrderManager(
        mode="LIVE",
        kite=mock_kite,
        order_type="MARKET",
    )

    # Test Long Entry with MARKET order
    res = om.place_entry("SILVERM26NOVFUT", "LONG", 2, 185000.0, 184500.0, 185800.0)
    assert res["order_id"] == "ORDER_MKT_1"
    assert res["status"] == "COMPLETE"
    assert res["average_price"] == 185025.0

    call_kwargs = mock_kite.place_order.call_args[1]
    assert call_kwargs["order_type"] == mock_kite.ORDER_TYPE_MARKET
    assert call_kwargs["price"] is None

    # Test Long Exit with MARKET order
    mock_kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 185810.0}]
    exit_res = om.place_exit("SILVERM26NOVFUT", "LONG", 1, 185800.0, "TARGET_1_HIT")
    assert exit_res["status"] == "COMPLETE"
    assert exit_res["average_price"] == 185810.0

    exit_kwargs = mock_kite.place_order.call_args[1]
    assert exit_kwargs["order_type"] == mock_kite.ORDER_TYPE_MARKET
    assert exit_kwargs["price"] is None


def test_silver_order_manager_entry_timeout_cancellation():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_PENDING"
    # Order stays OPEN throughout polling
    mock_kite.order_history.return_value = [{"status": "OPEN"}]

    om = SilverOrderManager(
        mode="LIVE",
        kite=mock_kite,
        order_type="LIMIT",
    )

    with pytest.raises(RuntimeError, match="failed to fill within timeout"):
        om.place_entry("SILVERM26NOVFUT", "SHORT", 2, 171600.0, 172100.0, 170800.0)

    # Verify cancel_order was called on Kite
    mock_kite.cancel_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        order_id="ORDER_PENDING",
    )


def test_silver_desk_desync_safeguard(tmp_path):
    state_file = tmp_path / "silver_state.json"
    notifier_mock = MagicMock()
    mock_kite = MagicMock()
    # Broker positions report FLAT (0 quantity), but desk thinks it's SHORT
    mock_kite.positions.return_value = {"net": [{"tradingsymbol": "SILVERM26NOVFUT", "quantity": 0}]}

    mock_om = MagicMock()
    cfg = SilverORBConfig(state_file=str(state_file), mode="LIVE", lots=2, initial_sl_pts=500.0, target1_pts=800.0)
    desk = SilverORBDesk(config=cfg, kite=mock_kite, order_manager=mock_om, notifier=notifier_mock)

    desk.state = {
        "date": "2026-11-16",
        "status": "ACTIVE_TRADE",
        "contract": {"tradingsymbol": "SILVERM26NOVFUT", "instrument_token": 123},
        "realized_pnl_rs": 0.0,
        "trade": {
            "symbol": "SILVERM26NOVFUT",
            "direction": "SHORT",
            "entry_price": 171600.0,
            "initial_sl": 172100.0,
            "current_sl": 172100.0,
            "target1_price": 170800.0,
            "lots_total": 2,
            "lots_open": 2,
            "lot1_closed": False,
            "lot1_exit_price": None,
            "lot1_pnl_rs": 0.0,
            "lot2_closed": False,
            "lot2_exit_price": None,
            "lot2_pnl_rs": 0.0,
            "be_moved": False,
            "status": "OPEN",
        },
    }
    desk.save_state(desk.state)

    # Target hit price reached
    desk.monitor_active_trade(current_ltp=170700.0, current_time=dt_time(19, 15))

    # Exit order must NOT have been called due to desync
    mock_om.place_exit.assert_not_called()
    assert desk.state["trade"]["status"] == "DESYNC_ABORTED"
    assert desk.state["status"] == "COMPLETED"
    notifier_mock.send_message.assert_called_once()
    assert "CRITICAL DESYNC ABORT" in notifier_mock.send_message.call_args[0][0]


def test_broker_side_stop_loss_lifecycle():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "SL_ORDER_999"

    om = SilverOrderManager(
        mode="LIVE",
        kite=mock_kite,
        order_type="MARKET",
    )

    # 1. Place broker SL order for SHORT position (covering BUY SL)
    sl_id = om.place_broker_sl(
        symbol="SILVERM26NOVFUT",
        direction="SHORT",
        quantity_lots=2,
        trigger_price=172100.0,
        buffer_pts=50.0,
    )
    assert sl_id == "SL_ORDER_999"
    place_kwargs = mock_kite.place_order.call_args[1]
    assert place_kwargs["order_type"] == mock_kite.ORDER_TYPE_SL
    assert place_kwargs["transaction_type"] == mock_kite.TRANSACTION_TYPE_BUY
    assert place_kwargs["trigger_price"] == 172100.0
    assert place_kwargs["price"] == 172150.0  # 172100 + 50 buffer
    assert place_kwargs["quantity"] == 2

    # 2. Modify broker SL when Target 1 hits (reduce to 1 lot, move trigger to cost)
    mock_kite.order_history.return_value = [{"transaction_type": "BUY"}]
    mod_ok = om.modify_broker_sl(
        order_id="SL_ORDER_999",
        quantity_lots=1,
        trigger_price=171600.0,
        buffer_pts=50.0,
    )
    assert mod_ok is True
    mod_kwargs = mock_kite.modify_order.call_args[1]
    assert mod_kwargs["quantity"] == 1
    assert mod_kwargs["trigger_price"] == 171600.0
    assert mod_kwargs["price"] == 171650.0

    # 3. Cancel broker SL on trade completion
    cancel_ok = om.cancel_broker_sl("SL_ORDER_999")
    assert cancel_ok is True
    mock_kite.cancel_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        order_id="SL_ORDER_999",
    )


def test_exchange_stop_loss_trigger_detection(tmp_path):
    state_file = tmp_path / "silver_state.json"
    notifier_mock = MagicMock()
    mock_kite = MagicMock()
    mock_om = MagicMock()

    # Simulate broker SL executed on Kite directly
    mock_om.get_order_status.return_value = {
        "status": "COMPLETE",
        "average_price": 172105.0,
        "filled_quantity": 2,
    }

    cfg = SilverORBConfig(state_file=str(state_file), mode="LIVE", lots=2, initial_sl_pts=500.0)
    desk = SilverORBDesk(config=cfg, kite=mock_kite, order_manager=mock_om, notifier=notifier_mock)

    desk.state = {
        "date": "2026-11-16",
        "status": "ACTIVE_TRADE",
        "contract": {"tradingsymbol": "SILVERM26NOVFUT", "instrument_token": 123},
        "realized_pnl_rs": 0.0,
        "trade": {
            "symbol": "SILVERM26NOVFUT",
            "direction": "SHORT",
            "entry_price": 171600.0,
            "initial_sl": 172100.0,
            "current_sl": 172100.0,
            "target1_price": 170800.0,
            "lots_total": 2,
            "lots_open": 2,
            "lot1_closed": False,
            "lot1_exit_price": None,
            "lot1_pnl_rs": 0.0,
            "lot2_closed": False,
            "lot2_exit_price": None,
            "lot2_pnl_rs": 0.0,
            "be_moved": False,
            "status": "OPEN",
            "sl_order_id": "SL_ORDER_999",
        },
    }
    desk.save_state(desk.state)

    desk.monitor_active_trade(current_ltp=172150.0, current_time=dt_time(19, 10))

    trade = desk.state["trade"]
    assert trade["status"] == "CLOSED"
    assert trade["lots_open"] == 0
    assert desk.state["status"] == "COMPLETED"
    # Loss: (172105 - 171600) * 5 * 2 = 505 * 10 = Rs -5,050
    assert desk.state["realized_pnl_rs"] == -5050.0
    notifier_mock.send_message.assert_called_once()
    assert "STOP LOSS HIT" in notifier_mock.send_message.call_args[0][0]


def test_partial_fill_handling():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_PARTIAL"
    # 2 out of 4 lots filled before timeout
    mock_kite.order_history.return_value = [
        {"status": "OPEN", "filled_quantity": 2, "average_price": 171605.0}
    ]

    om = SilverOrderManager(
        mode="LIVE",
        kite=mock_kite,
        order_type="MARKET",
    )

    res = om.place_entry("SILVERM26NOVFUT", "SHORT", 4, 171600.0, 172100.0, 170800.0)
    assert res["status"] == "PARTIAL"
    assert res["quantity"] == 2
    assert res["average_price"] == 171605.0
    # Verify remaining 2 lots were cancelled
    mock_kite.cancel_order.assert_called_once_with(
        variety=mock_kite.VARIETY_REGULAR,
        order_id="ORDER_PARTIAL",
    )


def test_closed_candle_filtering():
    cfg = SilverORBConfig(use_trend_filter=True)
    desk = SilverORBDesk(config=cfg, kite=None)

    # 60 closed candles up to 18:30, plus 1 currently forming candle at 18:45 with 0 volume
    dates = pd.date_range("2026-11-16 03:45", periods=61, freq="15min")
    prices = [180000.0 + (i * 30.0) for i in range(61)]
    volumes = [100.0] * 59 + [300.0, 0.0]  # Bar 59 (18:30) volume spike, Bar 60 (18:45) is 0

    df = pd.DataFrame({
        "date": dates,
        "time": [d.time() for d in dates],
        "open": prices,
        "high": [p + 20.0 for p in prices],
        "low": [p - 20.0 for p in prices],
        "close": [p + 10.0 for p in prices],
        "volume": volumes,
    })

    ref_h = 181500.0
    ref_l = 181000.0
    # Current time is 18:45:05 (Bar 60 is only 5 seconds old and not closed)
    eval_time = datetime(2026, 11, 16, 18, 45, 5)
    sig = desk.evaluate_breakout_signal(df, ref_h, ref_l, dt_time(18, 45), current_dt=eval_time)

    # It must evaluate the completed 18:30 bar (which had volume 300 > 1.2x average)
    assert sig is not None
    assert sig["direction"] == "LONG"


