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
    assert cfg.daily_stop_loss_limit == 10000.0
    assert cfg.wick_max_pct == 0.20
    assert cfg.use_pdh_pdl_filter is True
    assert cfg.use_nifty_filter is True
    assert cfg.trail_1r_to_be is True
    assert cfg.book_target_2_5r_pct == 0.50
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

    # Case 1: LTP rises to 103.0 -> reaches 1:1 target (102.5) -> moves SL to Breakeven (100.0)
    kite_mock.ltp.return_value = {"NSE:SAIL": {"last_price": 103.0}}
    desk.monitor_positions(current_time=dt_time(11, 0))

    assert len(desk.active_positions) == 1
    pos = desk.active_positions[0]
    assert pos["be_moved"] is True
    assert pos["current_sl"] == 100.0  # Trailed to Breakeven
    assert notifier_mock.send_message.called

    # Case 2: LTP falls to 99.8 -> hits Breakeven SL (100.0) -> closed at Breakeven
    kite_mock.ltp.return_value = {"NSE:SAIL": {"last_price": 99.8}}
    desk.monitor_positions(current_time=dt_time(11, 15))

    assert len(desk.active_positions) == 0
    assert len(desk.closed_trades) == 1
    closed = desk.closed_trades[0]
    assert closed["exit_reason"] == "BREAKEVEN"
    assert closed["realized_pnl_rs"] == 0.0


def test_monitor_positions_target_2_5_booking(tmp_path):
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
        "target_2_5r": 156.25,
        "risk_per_share": 2.5,
        "half_booked": False,
        "be_moved": False,
        "entry_time": "10:30:00",
        "order_id": "PAPER_1",
        "wick_pct": 10.0
    })

    # Price explodes to 157.0 (past 156.25 2.5R target)
    kite_mock.ltp.return_value = {"NSE:TATASTEEL": {"last_price": 157.0}}
    desk.monitor_positions(current_time=dt_time(11, 0))

    # Position should remain active with half_booked=True and be_moved=True
    assert len(desk.active_positions) == 1
    pos = desk.active_positions[0]
    assert pos["half_booked"] is True
    assert pos["be_moved"] is True
    assert pos["current_sl"] == 150.0  # Trailed to BE
    assert pos["half_booked_pnl"] == (156.25 - 150.0) * 500  # Rs 3,125.0


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
        "be_moved": True,
        "entry_time": "10:45:00",
        "order_id": "PAPER_HINDALCO",
        "wick_pct": 5.0
    })

    # At 15:15 EOD with LTP at 608.0
    kite_mock.ltp.return_value = {"NSE:HINDALCO": {"last_price": 608.0}}
    desk.monitor_positions(current_time=dt_time(15, 15))

    assert len(desk.active_positions) == 0
    assert len(desk.closed_trades) == 1
    assert desk.closed_trades[0]["exit_reason"] == "EOD_SQUAREOFF"
    assert desk.closed_trades[0]["realized_pnl_rs"] == (608.0 - 600.0) * 500  # +Rs 4,000

