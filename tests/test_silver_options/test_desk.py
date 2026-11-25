import pytest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock

from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.desk import SilverStrangleDesk
from strategy.silver_options.order_manager import SilverOrderManager
from strategy.silver_options.models import SilverStrangleLeg, SilverStranglePosition


@pytest.fixture
def temp_state_file(tmp_path):
    return tmp_path / "silver_trades_test.json"


@pytest.fixture
def mock_chain_data():
    today = date(2026, 11, 23)
    expiry = date(2026, 11, 27)
    return [
        {"name": "SILVERM", "instrument_type": "FUT", "tradingsymbol": "SILVERM26NOVFUT", "expiry": expiry, "instrument_token": 1001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM26NOV157000CE", "strike": 157000.0, "expiry": expiry, "instrument_token": 2001},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM26NOV153000PE", "strike": 153000.0, "expiry": expiry, "instrument_token": 3001},
    ]


def test_desk_enter_strangle(temp_state_file, mock_chain_data):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = mock_chain_data
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": 155000.0},
        "MCX:SILVERM26NOV157000CE": {"last_price": 400.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 400.0},
    }

    config = SilverOptionsConfig(lots=2, sl_per_lot=3000.0, sl_pct=0.40)
    om = SilverOrderManager(mode="PAPER", config=config)
    desk = SilverStrangleDesk(
        order_manager=om,
        config=config,
        state_file=temp_state_file,
        kite_client=mock_kite,
    )

    now = datetime(2026, 11, 23, 20, 0, 0)
    success = desk.enter_strangle(now)
    assert success is True
    assert desk.position is not None
    assert desk.position.status == "OPEN"
    assert desk.position.lots == 2
    assert desk.position.multiplier == 10.0
    # Stop loss calculated from number of lots: 2 * 3000 = 6000
    assert desk.position.max_loss_cap == 6000.0
    assert desk.position.ce_leg.entry_price == 400.0
    # 400 * 1.4 = 560
    assert desk.position.ce_leg.stop_loss_price == 560.0
    assert desk.position.pe_leg.entry_price == 400.0
    assert desk.position.pe_leg.stop_loss_price == 560.0

    # Verify state was saved to file
    assert temp_state_file.exists()


def test_desk_single_leg_sl_and_trail_cost(temp_state_file):
    mock_kite = MagicMock()
    config = SilverOptionsConfig(lots=2, sl_per_lot=3000.0, sl_pct=0.40, trail_to_cost=True)
    om = SilverOrderManager(mode="PAPER", config=config)
    desk = SilverStrangleDesk(
        order_manager=om,
        config=config,
        state_file=temp_state_file,
        kite_client=mock_kite,
    )

    # Setup active position
    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE",
        strike=157000.0,
        option_type="CE",
        entry_price=400.0,
        entry_time="20:00:00",
        stop_loss_price=560.0,
        current_price=400.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE",
        strike=153000.0,
        option_type="PE",
        entry_price=400.0,
        entry_time="20:00:00",
        stop_loss_price=560.0,
        current_price=400.0,
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-23",
        underlying_spot=155000.0,
        lots=2,
        multiplier=10.0,
        max_loss_cap=6000.0,
        ce_leg=ce,
        pe_leg=pe,
        status="OPEN",
    )

    # Simulate Call leg spiking to 580 (breaching 560 SL) while Put decays to 300
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 580.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 300.0},
    }

    now = datetime(2026, 11, 23, 21, 0, 0)
    is_active = desk.monitor_cycle(now)

    assert is_active is True
    # CE stopped out
    assert desk.position.ce_leg.status == "SL"
    assert desk.position.ce_leg.exit_price == 580.0
    # PE remained open, but its SL was trailed to cost (entry price 400.0)!
    assert desk.position.pe_leg.status == "OPEN"
    assert desk.position.pe_leg.stop_loss_price == 400.0


def test_desk_target_decay_exit(temp_state_file):
    mock_kite = MagicMock()
    config = SilverOptionsConfig(lots=2, target_decay_pct=0.70)
    om = SilverOrderManager(mode="PAPER", config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-23", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN"
    )

    # 400 + 400 = 800 entry premium. 70% decay means combined premium <= 240 (e.g. 100 CE + 100 PE = 200)
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 100.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 100.0},
    }

    now = datetime(2026, 11, 23, 22, 30, 0)
    is_active = desk.monitor_cycle(now)

    assert is_active is False
    assert desk.position.status == "CLOSED"
    assert desk.position.ce_leg.status == "TARGET_DECAY"
    assert desk.position.pe_leg.status == "TARGET_DECAY"
    # Points captured: (400 - 100) + (400 - 100) = +600 points -> +Rs 6,000 for 10 kg
    assert desk.position.total_points == 600.0
    assert desk.position.total_pnl == 6000.0


def test_desk_eod_square_off(temp_state_file):
    mock_kite = MagicMock()
    config = SilverOptionsConfig(lots=2, exit_time="23:15")
    om = SilverOrderManager(mode="PAPER", config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=250.0
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=220.0
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-23", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN"
    )

    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 250.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 220.0},
    }

    # At 23:15 IST, mandatory square off
    now = datetime(2026, 11, 23, 23, 15, 0)
    is_active = desk.monitor_cycle(now)

    assert is_active is False
    assert desk.position.status == "CLOSED"
    assert desk.position.ce_leg.status == "EOD"
    assert desk.position.pe_leg.status == "EOD"
    # Points captured: (400 - 250) + (400 - 220) = 150 + 180 = 330 pts -> Rs 3,300
    assert desk.position.total_points == 330.0
    assert desk.position.total_pnl == 3300.0


def test_desk_clears_stale_position_from_previous_day(temp_state_file, mock_chain_data):
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = mock_chain_data
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": 155000.0},
        "MCX:SILVERM26NOV157000CE": {"last_price": 400.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 400.0},
    }

    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="PAPER", config=config)

    # Pre-populate state file with a CLOSED position from yesterday (2026-11-23)
    yesterday_pos = {
        "trade_date": "2026-11-23",
        "underlying_spot": 155000.0,
        "lots": 2,
        "multiplier": 10.0,
        "max_loss_cap": 6000.0,
        "status": "CLOSED",
        "created_at": "2026-11-23T20:00:00",
        "closed_at": "2026-11-23T23:15:00",
        "ce_leg": {
            "symbol": "SILVERM26NOV157000CE", "strike": 157000.0, "option_type": "CE",
            "entry_price": 400.0, "entry_time": "20:00:00", "stop_loss_price": 560.0,
            "current_price": 200.0, "exit_price": 200.0, "status": "CLOSED"
        },
        "pe_leg": {
            "symbol": "SILVERM26NOV153000PE", "strike": 153000.0, "option_type": "PE",
            "entry_price": 400.0, "entry_time": "20:00:00", "stop_loss_price": 560.0,
            "current_price": 200.0, "exit_price": 200.0, "status": "CLOSED"
        }
    }
    import json
    with open(temp_state_file, "w") as f:
        json.dump(yesterday_pos, f)

    # Initialize desk on a new day (2026-11-24)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    # Stale position from yesterday should NOT be active for today
    assert desk.load_state(today_str="2026-11-24") is None
    assert desk.position is None

    # Now run session entry on new day (2026-11-24 20:00:00)
    now = datetime(2026, 11, 24, 20, 0, 0)
    desk.run_session(poll_interval=1, execute_once=True, now=now)

    # Position should successfully be entered for 2026-11-24!
    assert desk.position is not None
    assert desk.position.trade_date == "2026-11-24"
    assert desk.position.status == "OPEN"


def _make_live_mock_kite(mock_chain_data, ce_sell_id="CE_ORD_1", pe_fail_exc=None):
    mock_kite = MagicMock()
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.EXCHANGE_MCX = "MCX"
    mock_kite.TRANSACTION_TYPE_SELL = "SELL"
    mock_kite.TRANSACTION_TYPE_BUY = "BUY"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.ORDER_TYPE_MARKET = "MARKET"
    mock_kite.PRODUCT_MIS = "MIS"
    mock_kite.VALIDITY_DAY = "DAY"
    mock_kite.instruments.return_value = mock_chain_data
    mock_kite.positions.return_value = {"net": []}
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": 155000.0},
        "MCX:SILVERM26NOV157000CE": {"last_price": 400.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 400.0},
    }

    def _place_order(**kwargs):
        if kwargs["transaction_type"] == "SELL" and kwargs["tradingsymbol"].endswith("PE") and pe_fail_exc:
            raise pe_fail_exc
        if kwargs["transaction_type"] == "SELL":
            return ce_sell_id
        return "ROLLBACK_BUY_1"

    mock_kite.place_order.side_effect = _place_order
    mock_kite.order_history.return_value = [
        {"status": "COMPLETE", "filled_quantity": 2, "pending_quantity": 0, "average_price": 400.0}
    ]
    return mock_kite


def test_live_partial_pe_rejection_rolls_back_ce_and_prevents_restart_loop(temp_state_file, mock_chain_data):
    oi_err = Exception("MIS LIMIT orders are blocked for this contract due to its open interest (OI) being less than 100 lots.")
    mock_kite = _make_live_mock_kite(mock_chain_data, pe_fail_exc=oi_err)

    config = SilverOptionsConfig(lots=2, sl_per_lot=3000.0, sl_pct=0.40)
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    from datetime import time as dt_time
    today = datetime.now().date()
    now = datetime.combine(today, dt_time(20, 14, 0))
    entered = desk.enter_strangle(now)
    assert entered is False
    assert desk.position is not None
    assert desk.position.status == "ABORTED"
    assert temp_state_file.exists()

    # Verify CE leg was rolled back with a BUY order of 2 lots
    buy_calls = [c.kwargs for c in mock_kite.place_order.call_args_list if c.kwargs["transaction_type"] == "BUY"]
    assert len(buy_calls) == 1
    assert buy_calls[0]["tradingsymbol"] == "SILVERM26NOV157000CE"
    assert buy_calls[0]["quantity"] == 2

    # Simulate container restart reloading the same state file on the same day -> must NOT place any orders!
    calls_before_restart = mock_kite.place_order.call_count
    desk2 = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    assert desk2.enter_strangle(datetime.combine(today, dt_time(20, 15, 0))) is False
    assert mock_kite.place_order.call_count == calls_before_restart


def test_live_aborts_if_broker_already_has_open_silverm_position(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    mock_kite.positions.return_value = {
        "net": [
            {"exchange": "MCX", "tradingsymbol": "SILVERM26DEC140000CE", "quantity": -4},
        ]
    }
    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    assert desk.enter_strangle(datetime(2026, 11, 25, 20, 16, 0)) is False
    assert desk.position is not None
    assert desk.position.status == "ABORTED"
    mock_kite.place_order.assert_not_called()


def test_live_aborts_if_live_quotes_unavailable_no_fake_155000_fallback(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    mock_kite.quote.side_effect = Exception("Network error fetching quote")

    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    assert desk.enter_strangle(datetime(2026, 11, 25, 20, 0, 22)) is False
    assert desk.position is not None
    assert desk.position.status == "ABORTED"
    mock_kite.place_order.assert_not_called()


def test_stale_state_cleared_from_memory_and_file_next_day(temp_state_file, mock_chain_data):
    import json
    with open(temp_state_file, "w") as f:
        json.dump({
            "trade_date": "2026-11-24",
            "underlying_spot": 140000.0,
            "lots": 2,
            "multiplier": 10.0,
            "max_loss_cap": 6000.0,
            "status": "ABORTED",
            "abort_reason": "OI < 100",
        }, f)

    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="PAPER", config=config)
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = mock_chain_data
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": 155000.0},
        "MCX:SILVERM26NOV157000CE": {"last_price": 400.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 400.0},
    }

    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    # Loading on a new date clears both memory AND deletes the stale file on disk
    assert desk.load_state(today_str="2026-11-25") is None
    assert desk.position is None
    assert not temp_state_file.exists()

    # Fresh position can now be entered on the new day
    assert desk.enter_strangle(datetime(2026, 11, 25, 20, 0, 0)) is True
    assert desk.position is not None
    assert desk.position.trade_date == "2026-11-25"
    assert desk.position.status == "OPEN"
    assert temp_state_file.exists()


def test_live_square_off_escalates_unfilled_limit_to_market(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    # Buy limit stays OPEN -> cancel -> Market order completes at actual avg price 585.0
    mock_kite.place_order.side_effect = ["BUY_LIMIT_1", "BUY_MKT_2"]
    mock_kite.order_history.side_effect = [
        [{"status": "OPEN", "filled_quantity": 0, "pending_quantity": 2, "average_price": 0.0}],
        [{"status": "CANCELLED", "filled_quantity": 0, "pending_quantity": 0, "average_price": 0.0}],
        [{"status": "COMPLETE", "filled_quantity": 2, "pending_quantity": 0, "average_price": 585.0}],
    ]
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 580.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 300.0},
    }

    config = SilverOptionsConfig(lots=2, sl_pct=0.40, order_type="LIMIT")
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    desk._wait_for_orders = lambda ids, timeout_seconds=10, poll_seconds=1: {
        oid: om.get_order_status(oid) for oid in ids if oid
    }

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-25", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN",
    )

    active = desk.monitor_cycle(datetime(2026, 11, 25, 21, 0, 0))
    assert active is True
    assert desk.position.ce_leg.status == "SL"
    assert desk.position.ce_leg.exit_price == 585.0
    assert desk.position.ce_leg.exit_order_id == "BUY_MKT_2"
    # Verify escalation placed a MARKET order for remaining 2 lots
    assert mock_kite.place_order.call_count == 2
    assert mock_kite.place_order.call_args_list[1].kwargs["order_type"] == "MARKET"


def test_live_unfilled_eod_keeps_position_open_for_retry(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    mock_kite.place_order.side_effect = ["BUY_1", "BUY_MKT_2"]
    mock_kite.order_history.return_value = [
        {"status": "REJECTED", "filled_quantity": 0, "pending_quantity": 0, "average_price": 0.0, "status_message": "Margin shortfall"}
    ]
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 300.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 300.0},
    }

    config = SilverOptionsConfig(lots=2, exit_time="23:15", order_type="LIMIT")
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    desk._wait_for_orders = lambda ids, timeout_seconds=10, poll_seconds=1: {
        oid: om.get_order_status(oid) for oid in ids if oid
    }

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=300.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=300.0,
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-25", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN",
    )

    active = desk.monitor_cycle(datetime(2026, 11, 25, 23, 15, 0))
    # Must NOT mark CLOSED if broker buy-back failed/rejected!
    assert active is True
    assert desk.position.status == "OPEN"
    assert desk.position.ce_leg.status == "OPEN"
    assert desk.position.pe_leg.status == "OPEN"


def test_trail_to_cost_waits_if_surviving_leg_is_underwater_then_locks_and_takes_single_decay(temp_state_file):
    mock_kite = MagicMock()
    config = SilverOptionsConfig(lots=2, sl_pct=0.40, trail_to_cost=True, target_decay_pct=0.70)
    om = SilverOrderManager(mode="PAPER", config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-25", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN",
    )

    # Tick 1: IV spike -> CE stops at 570 while PE is slightly underwater at 430 (> 400 entry)
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 570.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 430.0},
    }
    assert desk.monitor_cycle(datetime(2026, 11, 25, 20, 30, 0)) is True
    assert desk.position.ce_leg.status == "SL"
    assert desk.position.pe_leg.status == "OPEN"
    assert desk.position.pe_leg.cost_trail_pending is True
    # PE SL must NOT jump down to 400 while PE is at 430 (would cause instant double stop-out!)
    assert desk.position.pe_leg.stop_loss_price == 560.0

    # Tick 2: PE stays at 435 -> survives without premature breakeven stop
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 570.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 435.0},
    }
    assert desk.monitor_cycle(datetime(2026, 11, 25, 20, 30, 5)) is True
    assert desk.position.pe_leg.status == "OPEN"

    # Tick 3: PE drops into profit (380 < 400) -> cost SL locks at 400!
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 570.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 380.0},
    }
    assert desk.monitor_cycle(datetime(2026, 11, 25, 21, 0, 0)) is True
    assert desk.position.pe_leg.stop_loss_price == 400.0

    # Tick 4: Surviving PE decays 70%+ (110 <= 400 * 0.30) -> closes on TARGET_DECAY
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 570.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 110.0},
    }
    assert desk.monitor_cycle(datetime(2026, 11, 25, 22, 0, 0)) is False
    assert desk.position.status == "CLOSED"
    assert desk.position.pe_leg.status == "TARGET_DECAY"


def test_monitor_cycle_refreshes_expired_token_and_syncs_order_manager(temp_state_file, monkeypatch):
    mock_kite = MagicMock()
    refreshed_kite = MagicMock()
    refreshed_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 350.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 350.0},
    }
    mock_kite.quote.side_effect = Exception("Incorrect `api_key` or `access_token`.")

    import kite.client as kite_client_mod
    monkeypatch.setattr(kite_client_mod, "ensure_kite_session", lambda k, force_login=False: refreshed_kite)

    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="PAPER", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-25", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN",
    )

    assert desk.monitor_cycle(datetime(2026, 11, 25, 21, 0, 0)) is True
    assert desk.kite is refreshed_kite
    assert desk.order_manager.kite is refreshed_kite
    assert desk.position.ce_leg.current_price == 350.0
    assert desk.position.pe_leg.current_price == 350.0


def test_mcx_fixed_holidays_work_in_2027():
    from strategy.holidays import is_mcx_trading_holiday
    is_hol, reason = is_mcx_trading_holiday(date(2027, 1, 26), session="EVENING")
    assert is_hol is True
    assert "Republic Day" in reason


def test_partial_exit_escalation_failure_retries_only_remaining_lots(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    # Cycle 1: LIMIT fills 1/2 @ 580.0, cancel leaves 1, MARKET fallback rejected -> Cycle 2: retries ONLY 1 lot @ 590.0
    mock_kite.place_order.side_effect = ["BUY_L1", "BUY_M1", "BUY_L2"]
    mock_kite.order_history.side_effect = [
        [{"status": "OPEN", "filled_quantity": 1, "pending_quantity": 1, "average_price": 580.0}],
        [{"status": "CANCELLED", "filled_quantity": 1, "pending_quantity": 0, "average_price": 580.0}],
        [{"status": "REJECTED", "filled_quantity": 0, "pending_quantity": 0, "average_price": 0.0}],
        [{"status": "COMPLETE", "filled_quantity": 1, "pending_quantity": 0, "average_price": 590.0}],
    ]
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 585.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 300.0},
    }

    config = SilverOptionsConfig(lots=2, sl_pct=0.40, order_type="LIMIT")
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    desk._wait_for_orders = lambda ids, timeout_seconds=10, poll_seconds=1: {
        oid: om.get_order_status(oid) for oid in ids if oid
    }

    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV157000CE", strike=157000.0, option_type="CE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV153000PE", strike=153000.0, option_type="PE",
        entry_price=400.0, entry_time="20:00:00", stop_loss_price=560.0, current_price=400.0,
    )
    desk.position = SilverStranglePosition(
        trade_date="2026-11-25", underlying_spot=155000.0, lots=2, multiplier=10.0,
        max_loss_cap=6000.0, ce_leg=ce, pe_leg=pe, status="OPEN",
    )

    # Poll 1: 1 lot filled, 1 remaining -> stays OPEN with exit_filled_lots=1
    assert desk.monitor_cycle(datetime(2026, 11, 25, 21, 0, 0)) is True
    assert desk.position.ce_leg.status == "OPEN"
    assert desk.position.ce_leg.exit_filled_lots == 1

    # Poll 2: retries ONLY remaining 1 lot (never 2!)
    assert desk.monitor_cycle(datetime(2026, 11, 25, 21, 0, 5)) is True
    assert mock_kite.place_order.call_args_list[2].kwargs["quantity"] == 1
    assert desk.position.ce_leg.status == "SL"
    assert desk.position.ce_leg.exit_filled_lots == 2
    assert desk.position.ce_leg.exit_price == 585.0  # weighted average (580 + 590) / 2


def test_live_pure_limit_timeout_allows_single_retry_before_abort(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    mock_kite.order_history.return_value = [
        {"status": "OPEN", "filled_quantity": 0, "pending_quantity": 2, "average_price": 0.0}
    ]
    config = SilverOptionsConfig(lots=2, order_type="LIMIT")
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    desk._wait_for_orders = lambda ids, timeout_seconds=10, poll_seconds=1: {
        oid: om.get_order_status(oid) for oid in ids if oid
    }
    # Force rollback status check to report clean CANCELLED so no rollback buy is needed
    om.get_order_status = lambda oid: {"order_id": oid, "status": "CANCELLED", "filled_quantity": 0, "pending_quantity": 0, "average_price": 0.0}

    now = datetime(2026, 11, 25, 20, 0, 0)
    # 1st timeout -> clean rollback, NOT aborted yet (allows 1 retry)
    assert desk.enter_strangle(now) is False
    assert desk.position is None

    # 2nd timeout -> persists ABORTED to stop infinite loop
    assert desk.enter_strangle(datetime(2026, 11, 25, 20, 1, 0)) is False
    assert desk.position is not None
    assert desk.position.status == "ABORTED"


def test_target_decay_does_not_escalate_to_market_in_live(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    mock_kite.place_order.side_effect = ["BUY_LIM_1", "BUY_LIM_2"]
    mock_kite.order_history.return_value = [
        {"status": "OPEN", "filled_quantity": 0, "pending_quantity": 2, "average_price": 0.0}
    ]
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOV157000CE": {"last_price": 100.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 100.0},
    }
    config = SilverOptionsConfig(lots=2, target_decay_pct=0.70, order_type="LIMIT")
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    desk._wait_for_orders = lambda ids, timeout_seconds=10, poll_seconds=1: {
        oid: om.get_order_status(oid) for oid in ids if oid
    }
    desk.position = SilverStranglePosition(
        trade_date="2026-11-26", underlying_spot=155000.0, lots=2, multiplier=10.0, max_loss_cap=6000.0,
        ce_leg=SilverStrangleLeg("SILVERM26NOV157000CE", 157000.0, "CE", 400.0, "20:00:00", 560.0, 400.0),
        pe_leg=SilverStrangleLeg("SILVERM26NOV153000PE", 153000.0, "PE", 400.0, "20:00:00", 560.0, 400.0),
        status="OPEN",
    )
    active = desk.monitor_cycle(datetime(2026, 11, 26, 22, 0, 0))
    assert active is True
    # Only the 2 initial LIMIT orders placed; ZERO MARKET orders for TARGET_DECAY
    assert mock_kite.place_order.call_count == 2
    assert all(c.kwargs["order_type"] == "LIMIT" for c in mock_kite.place_order.call_args_list)


def test_reconcile_with_broker_marks_manual_flat_leg_closed(temp_state_file, mock_chain_data):
    mock_kite = _make_live_mock_kite(mock_chain_data)
    mock_kite.positions.return_value = {
        "net": [
            {"exchange": "MCX", "tradingsymbol": "SILVERM26NOV157000CE", "quantity": 0, "buy_price": 250.0},
            {"exchange": "MCX", "tradingsymbol": "SILVERM26NOV153000PE", "quantity": -2, "sell_price": 400.0},
        ]
    }
    config = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config, state_file=temp_state_file, kite_client=mock_kite)
    desk.position = SilverStranglePosition(
        trade_date="2026-11-26", underlying_spot=155000.0, lots=2, multiplier=10.0, max_loss_cap=6000.0,
        ce_leg=SilverStrangleLeg("SILVERM26NOV157000CE", 157000.0, "CE", 400.0, "20:00:00", 560.0, 300.0),
        pe_leg=SilverStrangleLeg("SILVERM26NOV153000PE", 153000.0, "PE", 400.0, "20:00:00", 560.0, 300.0),
        status="OPEN",
    )
    desk.reconcile_with_broker(now=datetime(2026, 11, 26, 21, 0, 0))
    assert desk.position.ce_leg.status == "BROKER_RECONCILED"
    assert desk.position.ce_leg.exit_price == 250.0
    assert desk.position.pe_leg.status == "OPEN"


def test_run_session_refuses_entry_at_or_after_exit_time_and_isolates_paper_state(monkeypatch):
    monkeypatch.delenv("SILVER_STATE_FILE", raising=False)
    config = SilverOptionsConfig(lots=2, entry_time="20:00", exit_time="23:10")
    om = SilverOrderManager(mode="PAPER", config=config)
    desk = SilverStrangleDesk(order_manager=om, config=config)
    assert desk.state_file.name == "silver_paper_trades_today.json"

    desk.enter_strangle = MagicMock()
    desk.run_session(execute_once=True, now=datetime(2026, 11, 26, 23, 10, 0))
    desk.enter_strangle.assert_not_called()


