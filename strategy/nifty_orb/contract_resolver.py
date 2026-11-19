import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("nifty_orb.resolver")


def resolve_active_nifty_fut_contract(
    kite: Any,
    min_days_to_expiry: int = 0,
    today_d: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Dynamically identifies the near-month active NIFTY 50 index futures contract on NFO.
    Selects the earliest expiring valid monthly FUT contract on or after today + min_days_to_expiry.
    """
    if kite is None:
        # Offline fallback for testing or mock environments
        return {
            "tradingsymbol": "NIFTY26NOVFUT",
            "instrument_token": 256265,
            "expiry": date(2026, 11, 26),
            "lot_size": 65,
        }

    try:
        instruments = kite.instruments("NFO")
    except Exception as e:
        logger.error("Failed to fetch NFO instruments from Kite: %s", e)
        raise

    today = today_d or date.today()
    cutoff_date = today + timedelta(days=min_days_to_expiry)

    candidates = []
    for inst in instruments:
        sym = inst.get("tradingsymbol", "")
        name = inst.get("name", "")
        inst_type = inst.get("instrument_type", "")
        expiry = inst.get("expiry")

        is_nifty = (name == "NIFTY") if name else (sym.startswith("NIFTY") and not any(sym.startswith(x) for x in ("NIFTYIT", "NIFTYMID", "NIFTYNXT")))
        if is_nifty and inst_type == "FUT" and expiry:
            if isinstance(expiry, str):
                try:
                    expiry_date = date.fromisoformat(expiry)
                except ValueError:
                    continue
            elif hasattr(expiry, "date") and callable(expiry.date):  # Handles datetime.datetime and pd.Timestamp
                expiry_date = expiry.date()
            elif isinstance(expiry, date):
                expiry_date = expiry
            else:
                continue

            if expiry_date >= cutoff_date:
                candidates.append({
                    "tradingsymbol": sym,
                    "instrument_token": inst.get("instrument_token"),
                    "expiry": expiry_date,
                    "lot_size": int(inst.get("lot_size", 65)),
                })

    if not candidates:
        raise ValueError(f"No active NIFTY FUT contracts found on NFO on or after {cutoff_date}")

    candidates.sort(key=lambda c: c["expiry"])
    chosen = candidates[0]
    logger.info(
        "Resolved active NIFTY Futures contract: %s (Token: %s, Expiry: %s, Lot Size: %d)",
        chosen["tradingsymbol"],
        chosen["instrument_token"],
        chosen["expiry"],
        chosen["lot_size"],
    )
    return chosen
