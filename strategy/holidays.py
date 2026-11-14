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

    return False, None


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
        return False, None

    # Morning session check (closed on all standard holidays)
    if check_date in NSE_HOLIDAYS_2026:
        return True, f"MCX Morning Holiday: {NSE_HOLIDAYS_2026[check_date]}"

    return False, None
