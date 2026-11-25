import logging
from datetime import date, datetime
from typing import Tuple, Optional, Dict

logger = logging.getLogger("strategy.holidays")

# NSE Trading Holidays for 2026 (Equity and Equity Derivatives Segment)
NSE_HOLIDAYS_2026: Dict[date, str] = {
    date(2026, 1, 26): "Republic Day",
    date(2026, 2, 17): "Mahashivratri",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 20): "Id-Ul-Fitr (Ramzan Id)",
    date(2026, 3, 27): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 27): "Bakri Id (Id-Ul-Zuha)",
    date(2026, 6, 26): "Muharram",
    date(2026, 8, 15): "Independence Day",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 8): "Diwali Laxmi Pujan",
    date(2026, 11, 10): "Diwali Balipratipada",
    date(2026, 11, 24): "Guru Nanak Jayanti",
    date(2026, 12, 25): "Christmas",
}

# MCX Full Trading Holidays for 2026 (Both Morning and Evening Sessions Closed)
MCX_FULL_HOLIDAYS_2026: Dict[date, str] = {
    date(2026, 1, 26): "Republic Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 8, 15): "Independence Day",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 12, 25): "Christmas",
}

# Fixed-date MCX full-day national closures valid across years
MCX_FIXED_FULL_HOLIDAYS: Dict[Tuple[int, int], str] = {
    (1, 26): "Republic Day",
    (8, 15): "Independence Day",
    (10, 2): "Mahatma Gandhi Jayanti",
    (12, 25): "Christmas",
}

# Fixed-date NSE national closures valid across years
NSE_FIXED_HOLIDAYS: Dict[Tuple[int, int], str] = {
    (1, 26): "Republic Day",
    (5, 1): "Maharashtra Day",
    (8, 15): "Independence Day",
    (10, 2): "Mahatma Gandhi Jayanti",
    (12, 25): "Christmas",
}

_WARNED_YEARS: set = set()
_NSE_WARNED_YEARS: set = set()


def is_trading_holiday(check_date: Optional[date] = None) -> Tuple[bool, Optional[str]]:
    """
    Checks if a given date is a non-trading day on NSE (weekend or official exchange holiday).
    
    Returns:
        (is_holiday: bool, reason: Optional[str])
    """
    if check_date is None:
        check_date = datetime.now().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    # Weekend check
    weekday = check_date.weekday()
    if weekday == 5:
        return True, "Weekend (Saturday)"
    if weekday == 6:
        return True, "Weekend (Sunday)"

    # Official NSE holiday check
    if check_date in NSE_HOLIDAYS_2026:
        return True, f"NSE Holiday: {NSE_HOLIDAYS_2026[check_date]}"
    fixed_nse = NSE_FIXED_HOLIDAYS.get((check_date.month, check_date.day))
    if fixed_nse:
        return True, f"NSE Holiday: {fixed_nse}"
    if check_date.year > 2026 and check_date.year not in _NSE_WARNED_YEARS:
        _NSE_WARNED_YEARS.add(check_date.year)
        logger.warning(
            "NSE variable holiday calendar is not populated for %d; update strategy/holidays.py.",
            check_date.year,
        )

    return False, None


def is_nse_holiday(check_date: Optional[date] = None) -> bool:
    """
    Returns True if check_date is a non-trading day on NSE (weekend or holiday).
    """
    is_holiday, _ = is_trading_holiday(check_date)
    return is_holiday


def is_mcx_trading_holiday(check_date: Optional[date] = None, session: str = "EVENING") -> Tuple[bool, Optional[str]]:
    """
    Checks if a given date is a non-trading day on MCX for the specified session (MORNING or EVENING).
    MCX Evening Session (17:00 - 23:30/23:55 IST) is OPEN on most national holidays,
    closing ONLY on weekends and official full holidays (Republic Day, Good Friday,
    Independence Day, Gandhi Jayanti, Christmas).
    
    Returns:
        (is_holiday: bool, reason: Optional[str])
    """
    if check_date is None:
        check_date = datetime.now().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    # Weekend check (MCX is closed on Saturday and Sunday)
    weekday = check_date.weekday()
    if weekday == 5:
        return True, "Weekend (Saturday)"
    if weekday == 6:
        return True, "Weekend (Sunday)"

    if session.upper() == "EVENING":
        if check_date in MCX_FULL_HOLIDAYS_2026:
            return True, f"MCX Full Holiday: {MCX_FULL_HOLIDAYS_2026[check_date]}"
        fixed_name = MCX_FIXED_FULL_HOLIDAYS.get((check_date.month, check_date.day))
        if fixed_name:
            return True, f"MCX Full Holiday: {fixed_name}"
        if check_date.year > 2026 and check_date.year not in _WARNED_YEARS:
            _WARNED_YEARS.add(check_date.year)
            logger.warning(
                "MCX variable holiday calendar (e.g. Good Friday) is not populated for %d; update strategy/holidays.py.",
                check_date.year,
            )
        return False, None

    # Morning session check (closed on all standard holidays)
    if check_date in NSE_HOLIDAYS_2026:
        return True, f"MCX Morning Holiday: {NSE_HOLIDAYS_2026[check_date]}"

    return False, None


def is_market_open(
    now: Optional[datetime] = None,
    market_open_time: str = "09:15",
    market_close_time: str = "15:30",
    ignore_holidays: bool = False,
) -> Tuple[bool, str]:
    """
    Checks if NSE Equity / F&O market is currently open for trading.
    Returns: (is_open: bool, reason: str)
    """
    if ignore_holidays:
        return True, "Market OPEN (Bypassed)"

    if now is None:
        now = datetime.now()

    is_holiday, reason = is_trading_holiday(now.date())
    if is_holiday:
        return False, f"Market closed today ({reason})"

    time_str = now.strftime("%H:%M")
    if time_str < market_open_time:
        return False, f"Pre-market (Current: {time_str} IST, opens at {market_open_time} IST)"
    if time_str >= market_close_time:
        return False, f"Post-market (Current: {time_str} IST, closed at {market_close_time} IST)"

    return True, "Market OPEN"


def is_premarket_window(
    now: Optional[datetime] = None,
    premarket_start: str = "08:00",
    market_open_time: str = "09:15",
    ignore_holidays: bool = False,
) -> bool:
    """
    Returns True if today is a trading day and time is in pre-market preparation window (08:00 to 09:15 IST).
    """
    if ignore_holidays:
        return True
    if now is None:
        now = datetime.now()
    is_holiday, _ = is_trading_holiday(now.date())
    if is_holiday:
        return False
    time_str = now.strftime("%H:%M")
    return premarket_start <= time_str < market_open_time
