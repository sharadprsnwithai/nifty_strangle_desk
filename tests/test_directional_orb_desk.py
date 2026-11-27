"""Unit tests for 30m ORB Directional NIFTY Option Selling Desk."""
from datetime import date, datetime, time
from unittest.mock import MagicMock

import pandas as pd

from strategy.directional_orb_desk import DirectionalOrbDesk
from strategy.order_manager import OrderManager


def _make_bars(closes, start_hour=9, start_min=15):
    rows = []
    base_dt = datetime(2026, 11, 27, start_hour, start_min)
    for i, c in enumerate(closes):
        dt = base_dt + pd.Timedelta(minutes=5 * i)
        rows.append({
            "date": dt,
            "open": c - 2.0,
            "high": c + 5.0,
            "low": c - 5.0,
            "close": float(c),
            "volume": 0,
        })
    return pd.DataFrame(rows)


def _mock_instruments():
    return [
        {
            "name": "NIFTY",
            "instrument_type": "PE",
            "strike": 25750.0,
            "expiry": date(2026, 12, 1),
            "tradingsymbol": "NIFTY26DEC0125750PE",
            "lot_size": 65,
        },
        {
            "name": "NIFTY",
            "instrument_type": "CE",
            "strike": 25850.0,
            "expiry": date(2026, 12, 1),
            "tradingsymbol": "NIFTY26DEC0125850CE",
            "lot_size": 65,
        },
    ]


def test_evaluate_signal_bull_and_bear():
    closes = [25800] * 6 + [25840, 25860]
    sig = DirectionalOrbDesk.evaluate_signal(
        _make_bars(closes), orb_end_time=time(9, 45), min_adx=0.0
    )
    assert sig is not None
    assert sig["direction"] == "BULL"
    assert sig["opt_type"] == "PE"

    closes_bear = [25800] * 6 + [25760, 25740]
    sig_bear = DirectionalOrbDesk.evaluate_signal(
        _make_bars(closes_bear), orb_end_time=time(9, 45), min_adx=0.0
    )
    assert sig_bear is not None
    assert sig_bear["direction"] == "BEAR"
    assert sig_bear["opt_type"] == "CE"


def test_completed_bar_guard_and_orb_width_filter():
    # Forming 09:45 bar at now_dt=09:47 must be ignored
    closes = [25800] * 6 + [25860]
    sig_early = DirectionalOrbDesk.evaluate_signal(
        _make_bars(closes),
        orb_end_time=time(9, 45),
        min_adx=0.0,
        now_dt=datetime(2026, 11, 27, 9, 47),
    )
    assert sig_early is None

    # Once 09:50 starts, 09:45 bar is completed and triggers
    sig_closed = DirectionalOrbDesk.evaluate_signal(
        _make_bars(closes),
        orb_end_time=time(9, 45),
        min_adx=0.0,
        now_dt=datetime(2026, 11, 27, 9, 50),
    )
    assert sig_closed is not None and sig_closed["direction"] == "BULL"

    # Wide ORB (> 0.65% of open) skips entry
    wide_closes = [25800, 26000, 25800, 25800, 25800, 25800, 26050]
    sig_wide = DirectionalOrbDesk.evaluate_signal(
        _make_bars(wide_closes),
        orb_end_time=time(9, 45),
        min_adx=0.0,
        max_orb_width_pct=0.65,
    )
    assert sig_wide is None


def test_adx_filter_blocks_chop_allows_trend():
    # Trending series builds ADX >= 20
    trend_closes = [25800 + i * 2 for i in range(6)] + [25830 + i * 15 for i in range(15)]
    sig = DirectionalOrbDesk.evaluate_signal(
        _make_bars(trend_closes), orb_end_time=time(9, 45), min_adx=20.0, max_orb_width_pct=0.65
    )
    assert sig is not None and sig["adx14"] >= 20.0

    # Alternating choppy series has low ADX and is blocked by min_adx=20.0
    chop_closes = [25800, 25810, 25795, 25810, 25795, 25805] * 6 + [25816]
    sig_blocked = DirectionalOrbDesk.evaluate_signal(
        _make_bars(chop_closes), orb_end_time=time(9, 45), min_adx=20.0
    )
    assert sig_blocked is None


def test_desk_entry_and_50pct_sl_exit(tmp_path):
    om = OrderManager(mode="PAPER")
    desk = DirectionalOrbDesk(order_manager=om, state_file=tmp_path / "paper.json")

    kite = MagicMock()
    kite.historical_data.return_value = _make_bars([25800] * 6 + [25860]).to_dict("records")
    kite.ltp.return_value = {"NFO:NIFTY26DEC0125750PE": {"last_price": 120.0}}

    instruments = _mock_instruments()
    status = desk.step(
        kite=kite,
        instruments=instruments,
        lots=2,
        otm_offset=100,
        leg_sl_pct=0.50,
        min_adx=0.0,
        now_dt=datetime(2026, 11, 27, 9, 50),
    )
    assert status == "OPEN"
    assert desk.state["symbol"] == "NFO:NIFTY26DEC0125750PE"
    assert desk.state["quantity"] == 130

    kite.ltp.return_value = {"NFO:NIFTY26DEC0125750PE": {"last_price": 180.0}}
    status = desk.step(
        kite=kite,
        instruments=instruments,
        lots=2,
        otm_offset=100,
        leg_sl_pct=0.50,
        min_adx=0.0,
        now_dt=datetime(2026, 11, 27, 11, 0),
    )
    assert status == "CLOSED"
    assert desk.state["exit_reason"] == "LEG_SL"
    assert desk.state["realized_pnl"] < 0


def test_desk_cost_trail_and_eod_exit(tmp_path):
    om = OrderManager(mode="PAPER")
    desk = DirectionalOrbDesk(order_manager=om, state_file=tmp_path / "paper2.json")
    kite = MagicMock()
    kite.historical_data.return_value = _make_bars([25800] * 6 + [25740]).to_dict("records")
    kite.ltp.return_value = {"NFO:NIFTY26DEC0125850CE": {"last_price": 100.0}}

    instruments = _mock_instruments()
    desk.step(kite=kite, instruments=instruments, lots=1, min_adx=0.0, now_dt=datetime(2026, 11, 27, 9, 50))
    assert desk.state["symbol"] == "NFO:NIFTY26DEC0125850CE"

    kite.ltp.return_value = {"NFO:NIFTY26DEC0125850CE": {"last_price": 65.0}}
    desk.step(kite=kite, instruments=instruments, lots=1, min_adx=0.0, now_dt=datetime(2026, 11, 27, 12, 0))
    assert desk.state["trailed_to_cost"] is True
    assert desk.state["status"] == "OPEN"

    kite.ltp.return_value = {"NFO:NIFTY26DEC0125850CE": {"last_price": 40.0}}
    status = desk.step(kite=kite, instruments=instruments, lots=1, min_adx=0.0, now_dt=datetime(2026, 11, 27, 15, 15))
    assert status == "CLOSED"
    assert desk.state["exit_reason"] == "EOD_CUTOFF"
    assert desk.state["realized_pnl"] > 0
