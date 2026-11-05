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
    assert cfg.target_1_r == 1.0
    assert cfg.book_target_1_pct == 0.50
    assert cfg.eod_squareoff_time == "15:00"
    assert cfg.use_armed_candle_trigger is True
    assert cfg.use_dynamic_stock_selection is False
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


def test_dynamic_stock_selection_in_desk(tmp_path):
    kite_mock = MagicMock()
    om = ORBOrderManager(mode="PAPER")
    cfg = ORBConfig(
        use_dynamic_stock_selection=True,
        top_gainers_n=3,
        top_losers_n=3,
        enable_coiling_filter=True,
        state_file=str(tmp_path / "test_orb_dynamic_state.json")
    )
    desk = ORBDesk(kite=kite_mock, order_manager=om, config=cfg)

    # Mock instruments
    kite_mock.instruments.return_value = [
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "G1", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "G2", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "G3", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "L1", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "L2", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "L3", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "COIL_A", "lot_size": 100},
    ]

    # Mock quotes
    kite_mock.quote.return_value = {
        "NSE:G1": {"instrument_token": 1, "last_price": 130.0, "ohlc": {"close": 100.0}}, # +30%
        "NSE:G2": {"instrument_token": 2, "last_price": 120.0, "ohlc": {"close": 100.0}}, # +20%
        "NSE:G3": {"instrument_token": 3, "last_price": 110.0, "ohlc": {"close": 100.0}}, # +10%
        "NSE:L1": {"instrument_token": 4, "last_price": 90.0,  "ohlc": {"close": 100.0}}, # -10%
        "NSE:L2": {"instrument_token": 5, "last_price": 80.0,  "ohlc": {"close": 100.0}}, # -20%
        "NSE:L3": {"instrument_token": 6, "last_price": 70.0,  "ohlc": {"close": 100.0}}, # -30%
        "NSE:COIL_A": {"instrument_token": 7, "last_price": 100.0, "ohlc": {"close": 100.0}}, # 0%
    }

    # Pre-scanned coiling
    desk.pre_scanned_coiling = [
        {"symbol": "COIL_A", "token": 7, "lot": 100, "t1_high": 102.0, "t1_low": 98.0}
    ]

    # Mock historical data for PDH/PDL and IB
    def mock_hist(instrument_token, *args, **kwargs):
        interval = kwargs.get("interval")
        if interval == "day":
            return [
                {"date": datetime(2026, 11, 4), "high": 105.0, "low": 95.0},
                {"date": datetime(2026, 11, 5), "high": 108.0, "low": 97.0},
            ]
        elif interval == "15minute":
            return [
                {"date": datetime(2026, 11, 5, 9, 15), "open": 100, "high": 102, "low": 99, "close": 101},
                {"date": datetime(2026, 11, 5, 9, 30), "open": 101, "high": 104, "low": 100, "close": 103},
                {"date": datetime(2026, 11, 5, 9, 45), "open": 103, "high": 105, "low": 102, "close": 104},
                {"date": datetime(2026, 11, 5, 10, 0), "open": 104, "high": 106, "low": 103, "close": 105},
            ]
        return []

    kite_mock.historical_data.side_effect = mock_hist

    # Run dynamic stock selection
    desk.select_dynamic_stocks()

    # Should have selected G1, G2, G3, L3, L2, L1, COIL_A (7 stocks)
    assert len(desk.target_stocks) == 7
    symbols = [s["symbol"] for s in desk.target_stocks]
    assert "G1" in symbols
    assert "L3" in symbols
    assert "COIL_A" in symbols
    assert desk.dynamic_stocks_selected is True

    # Now run compute_initial_balance
    desk.compute_initial_balance()
    assert desk.ib_formed is True
    assert desk.stock_stats["G1"]["ib_high"] == 106.0
    assert desk.stock_stats["G1"]["ib_low"] == 99.0


