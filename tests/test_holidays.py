from datetime import date
from strategy.holidays import is_trading_holiday, NSE_HOLIDAYS_2026


def test_is_trading_holiday_weekend():
    # Saturday
    is_sat, reason_sat = is_trading_holiday(date(2026, 11, 7))
    assert is_sat is True
    assert "Saturday" in reason_sat

    # Sunday
    is_sun, reason_sun = is_trading_holiday(date(2026, 11, 8))
    assert is_sun is True
    # Nov 8 is also Diwali, but weekend check triggers or holiday triggers
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
