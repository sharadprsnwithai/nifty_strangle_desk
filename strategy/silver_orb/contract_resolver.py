import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("silver_orb.resolver")


def resolve_active_silverm_contract(kite: Any, min_days_to_expiry: int = 7) -> Dict[str, Any]:
    """
    Dynamically identifies the near-month active MCX Silver Mini (SILVERM) futures contract.
    Filters out contracts expiring within min_days_to_expiry (default: 7 days)
    to safely avoid MCX 5-day physical delivery tender periods.
    """
    if kite is None:
        # Fallback for offline tests or mock environments
        return {
            "tradingsymbol": "SILVERM26NOVFUT",
            "instrument_token": 123668487,
            "expiry": date(2026, 11, 30),
            "lot_size": 1,
        }

    try:
        instruments = kite.instruments("MCX")
    except Exception as e:
        logger.error("Failed to fetch MCX instruments from Kite: %s", e)
        raise

    today = date.today()
    cutoff_date = today + timedelta(days=min_days_to_expiry)

    candidates = []
    for inst in instruments:
        sym = inst.get("tradingsymbol", "")
        inst_type = inst.get("instrument_type", "")
        expiry = inst.get("expiry")

        if sym.startswith("SILVERM") and inst_type == "FUT" and expiry:
            if isinstance(expiry, str):
                expiry_date = date.fromisoformat(expiry)
            elif isinstance(expiry, date):
                expiry_date = expiry
            else:
                continue

            if expiry_date > cutoff_date:
                candidates.append({
                    "tradingsymbol": sym,
                    "instrument_token": inst.get("instrument_token"),
                    "expiry": expiry_date,
                    "lot_size": inst.get("lot_size", 1),
                })

    if not candidates:
        raise ValueError("No active MCX SILVERM futures contract found with sufficient expiry runway.")

    # Sort by expiry ascending (near-month first)
    candidates.sort(key=lambda x: x["expiry"])
    active_contract = candidates[0]
    logger.info(
        "Resolved active MCX Silver Mini contract: %s (Token: %s, Expiry: %s)",
        active_contract["tradingsymbol"],
        active_contract["instrument_token"],
        active_contract["expiry"],
    )
    return active_contract
