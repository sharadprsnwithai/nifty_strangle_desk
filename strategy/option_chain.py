import logging
from datetime import date, datetime
from typing import List, Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.chain")


def _normalize_date(d: Any) -> Optional[date]:
    """Normalize date representation from datetime, date, or ISO string."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def resolve_target_expiry(
    instruments: List[Dict[str, Any]],
    today_date: Optional[Any] = None,
    underlying: str = "NIFTY"
) -> date:
    """
    Finds target weekly expiry:
    If today is expiry day (0 DTE), selects the Next Week expiry.
    Otherwise, selects the Current Week nearest expiry.
    """
    today = _normalize_date(today_date) or datetime.now().date()

    raw_expiries = {
        _normalize_date(i["expiry"])
        for i in instruments
        if i.get("name") == underlying
        and i.get("instrument_type") in ("CE", "PE")
        and i.get("expiry")
    }
    expiries = sorted([e for e in raw_expiries if e is not None])

    upcoming = [e for e in expiries if e >= today]
    if not upcoming:
        raise ValueError(f"No upcoming {underlying} option expiries found in instruments list.")

    # 0 DTE Rule: If today is expiry day, take next week's expiry
    if upcoming[0] == today:
        if len(upcoming) > 1:
            logger.info("Today is Expiry Day (0 DTE). Selecting Next Week's expiry: %s", upcoming[1])
            return upcoming[1]
        return upcoming[0]

    logger.info("Selecting Current Week's expiry: %s", upcoming[0])
    return upcoming[0]


def find_strangle_strikes(
    kite: KiteConnect,
    underlying: str = "NIFTY",
    target_price: float = 30.0,
    target_date: Optional[date] = None,
    lots: int = 10
) -> Dict[str, Any]:
    """
    Discovers 1 CE and 1 PE with LTP >= target_price (closest to target_price from above).
    """
    instruments = kite.instruments("NFO")
    target_expiry = resolve_target_expiry(instruments, today_date=target_date, underlying=underlying)

    # Filter contracts matching underlying and expiry
    ce_contracts = [
        i for i in instruments
        if i.get("name") == underlying
        and i.get("instrument_type") == "CE"
        and _normalize_date(i.get("expiry")) == target_expiry
    ]
    pe_contracts = [
        i for i in instruments
        if i.get("name") == underlying
        and i.get("instrument_type") == "PE"
        and _normalize_date(i.get("expiry")) == target_expiry
    ]

    if not ce_contracts or not pe_contracts:
        raise ValueError(f"No CE/PE option contracts found for {underlying} on {target_expiry}")

    lot_size = int(ce_contracts[0].get("lot_size") or 65)
    total_quantity = lots * lot_size

    # Batch query prices
    symbols_to_query = [f"NFO:{c['tradingsymbol']}" for c in ce_contracts + pe_contracts]
    quotes = kite.ltp(symbols_to_query)

    # Find CE >= target_price with lowest LTP
    valid_ce = []
    for c in ce_contracts:
        sym = c["tradingsymbol"]
        ltp_data = quotes.get(f"NFO:{sym}", {})
        ltp = float(ltp_data.get("last_price") or 0.0)
        if ltp >= target_price:
            valid_ce.append((ltp, c))

    if not valid_ce:
        raise ValueError(f"No CE strike found with LTP >= {target_price}")
    valid_ce.sort(key=lambda x: x[0])
    chosen_ce_ltp, chosen_ce = valid_ce[0]

    # Find PE >= target_price with lowest LTP
    valid_pe = []
    for p in pe_contracts:
        sym = p["tradingsymbol"]
        ltp_data = quotes.get(f"NFO:{sym}", {})
        ltp = float(ltp_data.get("last_price") or 0.0)
        if ltp >= target_price:
            valid_pe.append((ltp, p))

    if not valid_pe:
        raise ValueError(f"No PE strike found with LTP >= {target_price}")
    valid_pe.sort(key=lambda x: x[0])
    chosen_pe_ltp, chosen_pe = valid_pe[0]

    return {
        "expiry": target_expiry,
        "lot_size": lot_size,
        "total_lots": lots,
        "total_quantity": total_quantity,
        "ce": {
            "symbol": chosen_ce["tradingsymbol"],
            "strike": float(chosen_ce["strike"]),
            "ltp": chosen_ce_ltp,
            "sl_price": round(chosen_ce_ltp * 1.50, 2),
            "target_price": round(chosen_ce_ltp * 0.20, 2)
        },
        "pe": {
            "symbol": chosen_pe["tradingsymbol"],
            "strike": float(chosen_pe["strike"]),
            "ltp": chosen_pe_ltp,
            "sl_price": round(chosen_pe_ltp * 1.50, 2),
            "target_price": round(chosen_pe_ltp * 0.20, 2)
        }
    }
