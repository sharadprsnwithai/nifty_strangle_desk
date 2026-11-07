import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, date, time as dt_time
from strategy.orb_desk.config import ORBConfig, TARGET_STOCKS
from strategy.orb_desk.order_manager import ORBOrderManager, _round_to_nse_tick
from strategy.orb_desk.desk import ORBDesk


def test_orb_config_defaults():
    cfg = ORBConfig()
    assert cfg.fixed_risk_per_trade == 2500.0
    assert cfg.max_trades_per_day == 5
    assert cfg.daily_stop_loss_limit in (10000.0, 50000.0)
    assert cfg.lots == 2
    assert cfg.wick_max_pct == 0.20
    assert cfg.use_pdh_pdl_filter is True
    assert cfg.use_nifty_filter is True
    assert cfg.trail_1r_to_be is True
    assert cfg.target_1_r == 1.0
    assert cfg.book_target_1_pct == 0.50
    assert cfg.eod_squareoff_time == "15:00"
    assert cfg.use_armed_candle_trigger is True
    assert cfg.mode == "PAPER"


def test_round_to_nse_tick():
    assert _round_to_nse_tick(100.02) == 100.0
    assert _round_to_nse_tick(100.03) == 100.05
    assert _round_to_nse_tick(100.07) == 100.05
    assert _round_to_nse_tick(100.08) == 100.10


def test_paper_order_manager_entry_and_exit():
    om = ORBOrderManager(mode="PAPER")
    order = om.place_entry(
        symbol="TATASTEEL",
        direction="LONG",
        quantity=250,
        entry_price=180.0,
        initial_sl=170.0,
        target_2_5r=205.0
    )
    assert order["mode"] == "PAPER"
    assert order["symbol"] == "TATASTEEL"
    assert order["direction"] == "LONG"
    assert order["quantity"] == 250
    assert order["fill_price"] == 180.0
    assert order["status"] == "COMPLETE"
    assert "PAPER_ENTRY" in order["order_id"]

    exit_order = om.place_exit(
        symbol="TATASTEEL",
        direction="LONG",
        quantity=125,
        exit_price=205.0,
        reason="TARGET_2.5_BOOK_50%"
    )
    assert exit_order["mode"] == "PAPER"
    assert exit_order["transaction_type"] == "SELL"
    assert exit_order["quantity"] == 125
    assert exit_order["fill_price"] == 205.0
    assert exit_order["reason"] == "TARGET_2.5_BOOK_50%"


def test_risk_parity_quantity_sizing(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    cfg = ORBConfig(fixed_risk_per_trade=2500.0, state_file=str(tmp_path / "test_state.json"))
    desk = ORBDesk(kite=kite_mock, order_manager=om, config=cfg)

    # Suppose entry = 500, SL = 490 -> risk per share = 10
    # Qty = 2500 / 10 = 250 shares
    risk_per_share = 10.0
    qty = max(1, int(desk.config.fixed_risk_per_trade / risk_per_share))
    assert qty == 250

    # Suppose entry = 2500, SL = 2450 -> risk = 50
    # Qty = 2500 / 50 = 50 shares
    risk_per_share = 50.0
    qty = max(1, int(desk.config.fixed_risk_per_trade / risk_per_share))
    assert qty == 50


def test_monitor_positions_stop_loss_and_breakeven(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    notifier_mock = MagicMock()
    cfg = ORBConfig(fixed_risk_per_trade=2500.0, state_file=str(tmp_path / "test_state.json"))
    desk = ORBDesk(kite=kite_mock, order_manager=om, notifier=notifier_mock, config=cfg)

    # Add active long position
    desk.active_positions.append({
        "symbol": "SAIL",
        "direction": "LONG",
        "quantity": 1000,
        "half_quantity": 500,
        "entry_price": 100.0,
        "initial_sl": 97.5,
        "current_sl": 97.5,
        "target_1r": 102.5,
        "target_2_5r": 106.25,
        "risk_per_share": 2.5,
        "half_booked": False,
        "be_moved": False,
        "entry_time": "10:30:00",
        "order_id": "PAPER_1",
        "wick_pct": 12.0
    })

    # Case 1: LTP rises to 103.0 -> reaches Target 1 (102.5) -> books 50% and moves SL to BE (100.0)
    kite_mock.ltp.return_value = {"NSE:SAIL": {"last_price": 103.0}}
    desk.monitor_positions(current_time=dt_time(11, 0))

    assert len(desk.active_positions) == 1
    pos = desk.active_positions[0]
    assert pos["half_booked"] is True
    assert pos["be_moved"] is True
    assert pos["current_sl"] == 100.0  # Trailed to Breakeven
    assert pos["half_booked_pnl"] == (102.5 - 100.0) * 500  # Rs 1,250.0
    assert notifier_mock.send_message.called

    # Case 2: LTP falls to 99.8 -> hits Breakeven SL (100.0) -> closed with 50% profit retained
    kite_mock.ltp.return_value = {"NSE:SAIL": {"last_price": 99.8}}
    desk.monitor_positions(current_time=dt_time(11, 15))

    assert len(desk.active_positions) == 0
    assert len(desk.closed_trades) == 1
    closed = desk.closed_trades[0]
    assert closed["exit_reason"] == "TRAIL_TO_COST_EXIT"
    assert closed["realized_pnl_rs"] == 1250.0  # Kept +Rs 1,250 from Target 1!


def test_monitor_positions_3pm_runner_exit(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    notifier_mock = MagicMock()
    cfg = ORBConfig(fixed_risk_per_trade=2500.0, state_file=str(tmp_path / "test_state.json"))
    desk = ORBDesk(kite=kite_mock, order_manager=om, notifier=notifier_mock, config=cfg)

    desk.active_positions.append({
        "symbol": "TATASTEEL",
        "direction": "LONG",
        "quantity": 1000,
        "half_quantity": 500,
        "entry_price": 150.0,
        "initial_sl": 147.5,
        "current_sl": 147.5,
        "target_1r": 152.5,
        "target_2_5r": 0.0,
        "risk_per_share": 2.5,
        "half_booked": False,
        "be_moved": False,
        "entry_time": "10:30:00",
        "order_id": "PAPER_1",
        "wick_pct": 10.0
    })

    # Step 1: Price hits Target 1 (152.5) -> books 50% and trails SL to cost
    kite_mock.ltp.return_value = {"NSE:TATASTEEL": {"last_price": 152.6}}
    desk.monitor_positions(current_time=dt_time(11, 0))
    assert len(desk.active_positions) == 1
    assert desk.active_positions[0]["half_booked"] is True
    assert desk.active_positions[0]["current_sl"] == 150.0

    # Step 2: At 3:00 PM IST (15:00) with LTP at 155.0 -> exits remaining 50% runner
    kite_mock.ltp.return_value = {"NSE:TATASTEEL": {"last_price": 155.0}}
    desk.monitor_positions(current_time=dt_time(15, 0))

    assert len(desk.active_positions) == 0
    assert len(desk.closed_trades) == 1
    closed = desk.closed_trades[0]
    assert closed["exit_reason"] == "3PM_EXIT_AFTER_T1"
    assert closed["realized_pnl_rs"] == (152.5 - 150.0) * 500 + (155.0 - 150.0) * 500  # Rs 3,750.0


def test_circuit_breaker_emergency_squareoff(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    notifier_mock = MagicMock()
    cfg = ORBConfig(
        fixed_risk_per_trade=2500.0,
        daily_stop_loss_limit=10000.0,
        state_file=str(tmp_path / "test_state.json")
    )
    desk = ORBDesk(kite=kite_mock, order_manager=om, notifier=notifier_mock, config=cfg)

    # 3 trades stopped out earlier today -> -Rs 7,500 realized loss
    desk.closed_trades = [
        {"realized_pnl_rs": -2500.0},
        {"realized_pnl_rs": -2500.0},
        {"realized_pnl_rs": -2500.0}
    ]

    # 1 active position currently down -Rs 3,000 MTM (Total MTM = -Rs 10,500 <= -Rs 10,000 limit)
    desk.active_positions.append({
        "symbol": "ABB",
        "direction": "LONG",
        "quantity": 100,
        "half_quantity": 50,
        "entry_price": 5000.0,
        "initial_sl": 4900.0,
        "current_sl": 4900.0,
        "target_1r": 5100.0,
        "target_2_5r": 5250.0,
        "risk_per_share": 100.0,
        "half_booked": False,
        "be_moved": False,
        "entry_time": "11:00:00",
        "order_id": "PAPER_ABB",
        "wick_pct": 8.0
    })

    # ABB drops to 4970.0 (-30/share x 100 = -Rs 3,000 MTM)
    kite_mock.ltp.return_value = {"NSE:ABB": {"last_price": 4970.0}}
    desk.monitor_positions(current_time=dt_time(11, 0))

    # Circuit breaker should trip and close active positions
    assert desk.circuit_tripped is True
    assert len(desk.active_positions) == 0
    assert any(t.get("exit_reason") == "CIRCUIT_BREAKER_HALT" for t in desk.closed_trades)
    assert notifier_mock.send_message.called


def test_monitor_positions_eod_squareoff(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    notifier_mock = MagicMock()
    cfg = ORBConfig(fixed_risk_per_trade=2500.0, state_file=str(tmp_path / "test_state.json"))
    desk = ORBDesk(kite=kite_mock, order_manager=om, notifier=notifier_mock, config=cfg)

    desk.active_positions.append({
        "symbol": "HINDALCO",
        "direction": "LONG",
        "quantity": 500,
        "half_quantity": 250,
        "entry_price": 600.0,
        "initial_sl": 595.0,
        "current_sl": 600.0,  # Trailed to BE
        "target_1r": 605.0,
        "target_2_5r": 612.5,
        "risk_per_share": 5.0,
        "half_booked": False,
        "be_moved": False,
        "entry_time": "10:45:00",
        "order_id": "PAPER_HINDALCO",
        "wick_pct": 5.0
    })

    # At 15:00 with LTP at 603.0 (has not hit Target 1)
    kite_mock.ltp.return_value = {"NSE:HINDALCO": {"last_price": 603.0}}
    desk.monitor_positions(current_time=dt_time(15, 0))

    assert len(desk.active_positions) == 0
    assert len(desk.closed_trades) == 1
    assert desk.closed_trades[0]["exit_reason"] == "3PM_EXIT_NO_T1"
    assert desk.closed_trades[0]["realized_pnl_rs"] == (603.0 - 600.0) * 500  # +Rs 1,500


def test_armed_candle_trigger_and_invalidation(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    notifier_mock = MagicMock()
    cfg = ORBConfig(fixed_risk_per_trade=2500.0, state_file=str(tmp_path / "test_state.json"))
    desk = ORBDesk(kite=kite_mock, order_manager=om, notifier=notifier_mock, config=cfg)

    # 1. Arm a Long setup
    desk.armed_positions.append({
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_trigger": 2500.05,
        "trigger_price": 2500.05,
        "initial_sl": 2480.0,
        "current_sl": 2480.0,
        "risk_per_share": 20.05,
        "qty": 124,
        "half_quantity": 62,
        "target_1r": 2520.10,
        "target_2r": 2530.10,
        "target_2_5r": 2530.10,
        "wick_ratio": 0.10,
        "armed_time": "10:30:00"
    })

    # Case A: LTP below trigger_price but above initial_sl -> Remains armed
    kite_mock.ltp.return_value = {"NSE:RELIANCE": {"last_price": 2495.0}}
    desk.monitor_positions(current_time=dt_time(10, 35))
    assert len(desk.armed_positions) == 1
    assert len(desk.active_positions) == 0

    # Case B: LTP reaches 2501.0 -> Breaks trigger_price -> Triggered into Active Position!
    kite_mock.ltp.return_value = {"NSE:RELIANCE": {"last_price": 2501.0}}
    desk.monitor_positions(current_time=dt_time(10, 36))
    assert len(desk.armed_positions) == 0
    assert len(desk.active_positions) == 1
    assert desk.active_positions[0]["symbol"] == "RELIANCE"
    assert desk.active_positions[0]["entry_price"] == 2500.05

    # 2. Arm a Short setup that gets Invalidated
    desk.armed_positions.append({
        "symbol": "INFY",
        "direction": "SHORT",
        "entry_trigger": 1800.0,
        "trigger_price": 1800.0,
        "initial_sl": 1815.0,
        "current_sl": 1815.0,
        "risk_per_share": 15.0,
        "qty": 166,
        "half_quantity": 83,
        "target_1r": 1785.0,
        "target_2r": 1777.5,
        "target_2_5r": 1777.5,
        "wick_ratio": 0.08,
        "armed_time": "11:00:00"
    })

    # Case C: LTP rises to 1816.0 (breaches 1815.0 SL of armed candle before triggering) -> Invalidated!
    kite_mock.ltp.return_value = {
        "NSE:RELIANCE": {"last_price": 2505.0},
        "NSE:INFY": {"last_price": 1816.0}
    }
    desk.monitor_positions(current_time=dt_time(11, 5))
    assert len(desk.armed_positions) == 0  # INFY removed
    assert not any(p["symbol"] == "INFY" for p in desk.active_positions)
    assert not any(t["symbol"] == "INFY" for t in desk.closed_trades)  # No loss incurred!


def test_orb_order_manager_limit_and_market_modes():
    # 1. Test PAPER mode with default LIMIT order type
    om_paper_limit = ORBOrderManager(mode="PAPER", order_type="LIMIT", limit_buffer_pct=0.002)
    entry_res = om_paper_limit.place_entry(
        symbol="RELIANCE",
        direction="LONG",
        quantity=250,
        entry_price=2500.0,
        initial_sl=2475.0,
        target_2_5r=0.0
    )
    assert entry_res["order_type"] == "LIMIT"
    # LONG entry limit price = 2500 * (1 + 0.002) = 2505.00
    assert entry_res["limit_price"] == 2505.00

    # Short entry limit price = 2500 * (1 - 0.002) = 2495.00
    short_entry = om_paper_limit.place_entry(
        symbol="TCS",
        direction="SHORT",
        quantity=175,
        entry_price=3000.0,
        initial_sl=3030.0,
        target_2_5r=0.0
    )
    assert short_entry["limit_price"] == 2994.00

    # Exit LONG (SELL) limit price = 2525 * (1 - 0.002) = 2519.95
    exit_res = om_paper_limit.place_exit(
        symbol="RELIANCE",
        direction="LONG",
        quantity=125,
        exit_price=2525.0,
        reason="TARGET_1R_BOOK_50%"
    )
    assert exit_res["order_type"] == "LIMIT"
    assert exit_res["limit_price"] == 2519.95

    # 2. Test LIVE mode with LIMIT order passing correct Kite params
    kite_mock = MagicMock()
    kite_mock.VARIETY_REGULAR = "regular"
    kite_mock.ORDER_TYPE_LIMIT = "LIMIT"
    kite_mock.ORDER_TYPE_MARKET = "MARKET"
    kite_mock.TRANSACTION_TYPE_BUY = "BUY"
    kite_mock.TRANSACTION_TYPE_SELL = "SELL"
    kite_mock.PRODUCT_MIS = "MIS"
    kite_mock.VALIDITY_DAY = "DAY"
    kite_mock.place_order.return_value = "LIVE_ORDER_999"

    om_live_limit = ORBOrderManager(mode="LIVE", kite=kite_mock, order_type="LIMIT", limit_buffer_pct=0.002)
    live_entry = om_live_limit.place_entry(
        symbol="INFY",
        direction="LONG",
        quantity=400,
        entry_price=1500.0,
        initial_sl=1485.0,
        target_2_5r=0.0
    )
    assert live_entry["order_id"] == "LIVE_ORDER_999"
    assert live_entry["limit_price"] == 1503.00
    assert kite_mock.place_order.call_args[1]["order_type"] == "LIMIT"
    assert kite_mock.place_order.call_args[1]["price"] == 1503.00
    assert kite_mock.place_order.call_args[1]["tradingsymbol"] == "INFY"

    # 3. Test MARKET mode fallback
    om_paper_mkt = ORBOrderManager(mode="PAPER", order_type="MARKET")
    mkt_res = om_paper_mkt.place_entry(
        symbol="SBIN",
        direction="LONG",
        quantity=750,
        entry_price=800.0,
        initial_sl=790.0,
        target_2_5r=0.0
    )
    assert mkt_res["order_type"] == "MARKET"
    assert mkt_res["limit_price"] is None



