from datetime import date, time
from unittest.mock import MagicMock
import pytest
from strategy.nifty_orb.config import NiftyORBConfig
from strategy.nifty_orb.contract_resolver import resolve_active_nifty_fut_contract
from strategy.nifty_orb.order_manager import NiftyOrderManager


def test_nifty_orb_config_defaults():
    cfg = NiftyORBConfig()
    assert cfg.lots == 2
    assert cfg.initial_sl_pts == 50.0
    assert cfg.target1_pts == 75.0
    assert cfg.ref_start_time == time(9, 15)
    assert cfg.ref_end_time == time(9, 45)
    assert cfg.trade_window_end == time(13, 30)
    assert cfg.session_cutoff_time == time(15, 15)
    assert cfg.vol_multiplier == 1.2
    assert cfg.use_trend_filter is True
    assert cfg.exchange == "NFO"
    assert cfg.product == "MIS"
    assert cfg.order_type == "LIMIT"
    assert cfg.limit_buffer_pts == 5.0
    assert cfg.state_file == "data/nifty_orb_trades_today.json"


def test_resolve_active_nifty_fut_contract_mock():
    # Test offline fallback when kite is None
    contract = resolve_active_nifty_fut_contract(None)
    assert "NIFTY" in contract["tradingsymbol"]
    assert contract["instrument_token"] > 0
    assert contract["lot_size"] in (25, 50, 65, 75)
    assert isinstance(contract["expiry"], date)


def test_resolve_active_nifty_fut_contract_live_list():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {
            "tradingsymbol": "BANKNIFTY26NOVFUT",
            "name": "BANKNIFTY",
            "instrument_type": "FUT",
            "expiry": date(2026, 11, 26),
            "instrument_token": 1001,
            "lot_size": 15,
        },
        {
            "tradingsymbol": "NIFTY26NOVFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": date(2026, 11, 26),
            "instrument_token": 1002,
            "lot_size": 65,
        },
        {
            "tradingsymbol": "NIFTY26DECFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": date(2026, 12, 31),
            "instrument_token": 1003,
            "lot_size": 65,
        },
        {
            "tradingsymbol": "NIFTY26N2624000CE",
            "name": "NIFTY",
            "instrument_type": "CE",
            "expiry": date(2026, 11, 26),
            "instrument_token": 1004,
            "lot_size": 65,
        },
    ]

    contract = resolve_active_nifty_fut_contract(mock_kite, today_d=date(2026, 11, 19))
    assert contract["tradingsymbol"] == "NIFTY26NOVFUT"
    assert contract["instrument_token"] == 1002
    assert contract["lot_size"] == 65
    assert contract["expiry"] == date(2026, 11, 26)


def test_resolve_active_nifty_fut_contract_string_expiry_and_cutoff():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {
            "tradingsymbol": "NIFTY26NOVFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": "2026-11-26",
            "instrument_token": 1002,
            "lot_size": 65,
        },
        {
            "tradingsymbol": "NIFTY26DECFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": "2026-12-31",
            "instrument_token": 1003,
            "lot_size": 65,
        },
    ]

    # If min_days_to_expiry is 10 and today is 2026-11-20, cutoff is 2026-11-30, so it should pick DEC contract
    contract = resolve_active_nifty_fut_contract(mock_kite, min_days_to_expiry=10, today_d=date(2026, 11, 20))
    assert contract["tradingsymbol"] == "NIFTY26DECFUT"
    assert contract["expiry"] == date(2026, 12, 31)


def test_resolve_active_nifty_fut_contract_no_candidates():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = []
    with pytest.raises(ValueError, match="No active NIFTY FUT contracts found"):
        resolve_active_nifty_fut_contract(mock_kite, today_d=date(2026, 11, 19))


def test_order_manager_paper_execution():
    om = NiftyOrderManager(mode="PAPER", limit_buffer_pts=5.0)

    # Calculate limit price with buffer
    buy_limit = om.calculate_limit_price("BUY", 24000.0)
    assert buy_limit == 24005.0

    sell_limit = om.calculate_limit_price("SELL", 24000.0)
    assert sell_limit == 23995.0

    # Place paper buy order
    res = om.place_order(
        symbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=130,
        trigger_price=24000.0,
    )
    assert res["status"] == "COMPLETE"
    assert res["order_id"].startswith("PAPER_")
    assert res["price"] == 24000.0
    assert res["quantity"] == 130


def test_order_manager_live_execution():
    mock_kite = MagicMock()
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.ORDER_TYPE_MARKET = "MARKET"
    mock_kite.place_order.return_value = "241119000123"

    om = NiftyOrderManager(mode="LIVE", kite=mock_kite, limit_buffer_pts=5.0)
    res = om.place_order(
        symbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=130,
        trigger_price=24000.0,
    )
    assert res["status"] == "COMPLETE"
    assert res["order_id"] == "241119000123"
    assert res["price"] == 24000.0
    assert res["quantity"] == 130
    mock_kite.place_order.assert_called_once_with(
        variety="regular",
        exchange="NFO",
        tradingsymbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=130,
        product="MIS",
        order_type="LIMIT",
        price=24005.0,
        tag="NIFTY_ORB",
    )


def test_order_manager_live_no_kite():
    om = NiftyOrderManager(mode="LIVE", kite=None)
    with pytest.raises(RuntimeError, match="Live mode enabled but KiteConnect client is None"):
        om.place_order(symbol="NIFTY26NOVFUT", transaction_type="BUY", quantity=130)

