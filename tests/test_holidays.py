from datetime import date
from strategy.holidays import is_trading_holiday, is_mcx_trading_holiday, NSE_HOLIDAYS_2026, MCX_FULL_HOLIDAYS_2026


def test_is_trading_holiday_weekend():
    # Saturday
    is_sat, reason_sat = is_trading_holiday(date(2026, 11, 7))
    assert is_sat is True
    assert "Saturday" in reason_sat

    # Sunday
    is_sun, reason_sun = is_trading_holiday(date(2026, 11, 8))
    assert is_sun is True
    assert is_sun is True


def test_is_trading_holiday_regular_trading_day():
    # Tuesday Nov 3, 2026
    is_hol, reason = is_trading_holiday(date(2026, 11, 3))
    assert is_hol is False
    assert reason is None


def test_is_trading_holiday_official_nse_holiday():
    # Republic day Monday Jan 26, 2026
    is_hol, reason = is_trading_holiday(date(2026, 1, 26))
    assert is_hol is True
    assert "Republic Day" in reason

    # Good Friday Apr 3, 2026
    is_gf, reason_gf = is_trading_holiday(date(2026, 4, 3))
    assert is_gf is True
    assert "Good Friday" in reason_gf


def test_is_mcx_trading_holiday_sessions():
    # 1. Full Holiday (Republic Day Jan 26, 2026) -> Both sessions closed
    is_hol_eve, reason_eve = is_mcx_trading_holiday(date(2026, 1, 26), session="EVENING")
    assert is_hol_eve is True
    assert "Republic Day" in reason_eve

    is_hol_morn, reason_morn = is_mcx_trading_holiday(date(2026, 1, 26), session="MORNING")
    assert is_hol_morn is True

    # 2. NSE Holiday but MCX Evening Session OPEN (Maharashtra Day May 1, 2026)
    # Morning is closed, Evening is OPEN for trading!
    is_morn_closed, _ = is_mcx_trading_holiday(date(2026, 5, 1), session="MORNING")
    assert is_morn_closed is True

    is_eve_closed, reason_eve = is_mcx_trading_holiday(date(2026, 5, 1), session="EVENING")
    assert is_eve_closed is False
    assert reason_eve is None

    # 3. Dussehra (Oct 20, 2026) -> MCX Evening is OPEN
    is_dussehra_eve, _ = is_mcx_trading_holiday(date(2026, 10, 20), session="EVENING")
    assert is_dussehra_eve is False

    # 4. Weekend check
    is_sat, reason_sat = is_mcx_trading_holiday(date(2026, 11, 7), session="EVENING")
    assert is_sat is True
    assert "Saturday" in reason_sat
