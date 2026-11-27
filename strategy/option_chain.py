import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.chain")

NIFTY_INDEX_TOKEN = 256265  # NSE:NIFTY 50 instrument token


def fetch_daily_atr(
    kite: KiteConnect,
    instrument_token: int = NIFTY_INDEX_TOKEN,
    period: int = 14,
    default_atr: float = 150.0
) -> float:
    """
    Fetches daily candles via Kite Connect and computes standard 14-day Average True Range (ATR).
    Falls back gracefully to default_atr (150.0) upon any failure.
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(days=period * 3 + 10)
    try:
        candles = kite.historical_data(
            instrument_token,
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
            interval="day"
        )
        if not candles or len(candles) < 2:
            logger.warning("Insufficient daily candles (%d) to compute ATR. Using default: %.1f", len(candles) if candles else 0, default_atr)
            return default_atr

        true_ranges = []
        for i in range(1, len(candles)):
            high = float(candles[i]["high"])
            low = float(candles[i]["low"])
            prev_close = float(candles[i - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        if not true_ranges:
            return default_atr

        sample = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
        calculated_atr = round(sum(sample) / len(sample), 2)
        logger.info("Calculated %d-day Daily ATR: %.2f pts (sample size: %d days)", period, calculated_atr, len(sample))
        return calculated_atr
    except Exception as e:
        from kite.client import is_token_error
        if is_token_error(e):
            raise
        logger.warning("Failed to fetch daily ATR (%s). Falling back to default ATR %.1f pts.", e, default_atr)
        return default_atr


def fetch_daily_adr(
    kite: KiteConnect,
    instrument_token: int = NIFTY_INDEX_TOKEN,
    period: int = 14,
    default_adr: float = 150.0,
) -> float:
    """
    Fetches daily candles via Kite Connect and computes prior-day completed N-day Average Daily Range (High - Low).
    Excludes today's incomplete candle so the regime gate has zero lookahead bias.
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(days=period * 3 + 10)
    try:
        candles = kite.historical_data(
            instrument_token,
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
            interval="day",
        )
        if not candles:
            logger.warning("Insufficient daily candles (0) to compute ADR. Using default: %.1f", default_adr)
            return default_adr

        today_d = to_date.date()
        completed = [c for c in candles if (_normalize_date(c.get("date")) or today_d) < today_d]
        if not completed:
            completed = candles

        ranges = [float(c["high"]) - float(c["low"]) for c in completed[-period:]]
        if not ranges:
            return default_adr

        calculated_adr = round(sum(ranges) / len(ranges), 2)
        logger.info("Calculated %d-day prior Daily ADR: %.2f pts (sample size: %d days)", period, calculated_adr, len(ranges))
        return calculated_adr
    except Exception as e:
        from kite.client import is_token_error
        if is_token_error(e):
            raise
        logger.warning("Failed to fetch daily ADR (%s). Falling back to default ADR %.1f pts.", e, default_adr)
        return default_adr


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
            logger.debug("Today is Expiry Day (0 DTE). Selecting Next Week's expiry: %s", upcoming[1])
            return upcoming[1]
        return upcoming[0]

    logger.debug("Selecting Current Week's expiry: %s", upcoming[0])
    return upcoming[0]


def find_strangle_strikes(
    kite: KiteConnect,
    underlying: str = "NIFTY",
    target_price: float = 30.0,
    strike_mode: str = "ATR",
    atr_multiplier: float = 1.25,
    target_date: Optional[date] = None,
    lots: int = 10
) -> Dict[str, Any]:
    """
    Discovers 1 CE and 1 PE contract:
    - If strike_mode == 'ATR': Computes target strike distance = max(ATR * atr_multiplier, 150 pts),
      discovers strikes closest to (Spot + distance) and (Spot - distance).
      Falls back to PRICE mode upon failure.
    - If strike_mode == 'PRICE': Discovers strikes with LTP >= target_price (closest from above).
    """
    try:
        instruments = kite.instruments("NFO")
    except Exception as e:
        logger.error("Failed to fetch NFO instruments from Kite: %s", e)
        raise RuntimeError(f"Failed to fetch NFO instruments from Kite: {e}") from e

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

    # Batch query prices in chunks of 100 to avoid payload / URL length limits
    if underlying == "NIFTY":
        spot_sym = "NSE:NIFTY 50"
        index_token: Optional[int] = NIFTY_INDEX_TOKEN
    elif underlying == "BANKNIFTY":
        spot_sym = "NSE:NIFTY BANK"
        index_token = 260105
    else:
        spot_sym = f"NSE:{underlying}"
        index_token = None
    symbols_to_query = [spot_sym] + [f"NFO:{c['tradingsymbol']}" for c in ce_contracts + pe_contracts]
    quotes: Dict[str, Any] = {}
    for i in range(0, len(symbols_to_query), 100):
        batch = symbols_to_query[i:i + 100]
        try:
            batch_quotes = kite.ltp(batch)
            quotes.update(batch_quotes)
        except Exception as e:
            from kite.client import is_token_error
            if is_token_error(e):
                logger.error("Authentication failed querying batch LTP quotes for %d instruments: %s", len(batch), e)
                raise
            logger.error("Failed to query batch LTP quotes for %d instruments: %s", len(batch), e)

    spot = float(quotes.get(spot_sym, {}).get("last_price") or 0.0)

    # Try ATR-based strike discovery if requested and spot price was returned
    if strike_mode.upper() == "ATR":
        try:
            if spot > 0.0:
                atr = fetch_daily_atr(kite, instrument_token=index_token) if index_token else 150.0
                target_distance = max(atr * atr_multiplier, 150.0)
                strike_step = 100.0 if underlying == "BANKNIFTY" else 50.0
                target_ce_strike = round((spot + target_distance) / strike_step) * strike_step
                target_pe_strike = round((spot - target_distance) / strike_step) * strike_step

                # Prefer OTM strikes with non-zero LTP closest to the ATR target strike
                ce_sorted = sorted(ce_contracts, key=lambda c: abs(float(c["strike"]) - target_ce_strike))
                pe_sorted = sorted(pe_contracts, key=lambda p: abs(float(p["strike"]) - target_pe_strike))
                liquid_ce = [c for c in ce_sorted if float(quotes.get(f"NFO:{c['tradingsymbol']}", {}).get("last_price") or 0.0) > 0.0]
                liquid_pe = [p for p in pe_sorted if float(quotes.get(f"NFO:{p['tradingsymbol']}", {}).get("last_price") or 0.0) > 0.0]
                chosen_ce = liquid_ce[0] if liquid_ce else ce_sorted[0]
                chosen_pe = liquid_pe[0] if liquid_pe else pe_sorted[0]

                ce_sym_key = f"NFO:{chosen_ce['tradingsymbol']}"
                pe_sym_key = f"NFO:{chosen_pe['tradingsymbol']}"
                chosen_ce_ltp = float(quotes.get(ce_sym_key, {}).get("last_price") or 0.0)
                chosen_pe_ltp = float(quotes.get(pe_sym_key, {}).get("last_price") or 0.0)

                if chosen_ce_ltp > 0.0 and chosen_pe_ltp > 0.0:
                    logger.info(
                        "ATR Strike Discovery: Spot=%.1f | ATR=%.1f (%.2fx -> Offset=%.1f pts) | CE Strike=%.0f (LTP=%.2f) | PE Strike=%.0f (LTP=%.2f)",
                        spot, atr, atr_multiplier, target_distance, chosen_ce["strike"], chosen_ce_ltp, chosen_pe["strike"], chosen_pe_ltp
                    )

                    return {
                        "expiry": target_expiry,
                        "lot_size": lot_size,
                        "total_lots": lots,
                        "total_quantity": total_quantity,
                        "strike_mode": "ATR",
                        "atr": atr,
                        "spot": spot,
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
        except Exception as e:
            from kite.client import is_token_error
            if is_token_error(e):
                raise
            logger.warning("ATR strike selection failed (%s). Gracefully falling back to PRICE mode (target_price=%.1f)...", e, target_price)

    # Find CE >= target_price with lowest LTP (prefer OTM when spot is known)
    ce_pool = [c for c in ce_contracts if float(c.get("strike", 0.0)) >= spot] if spot > 0.0 else ce_contracts
    if not ce_pool:
        ce_pool = ce_contracts
    valid_ce = []
    for c in ce_pool:
        sym = c["tradingsymbol"]
        ltp_data = quotes.get(f"NFO:{sym}", {})
        ltp = float(ltp_data.get("last_price") or 0.0)
        if ltp >= target_price:
            valid_ce.append((ltp, c))

    if not valid_ce:
        raise ValueError(f"No CE strike found with LTP >= {target_price}")
    valid_ce.sort(key=lambda x: x[0])
    chosen_ce_ltp, chosen_ce = valid_ce[0]

    # Find PE >= target_price with lowest LTP (prefer OTM when spot is known)
    pe_pool = [p for p in pe_contracts if float(p.get("strike", 0.0)) <= spot] if spot > 0.0 else pe_contracts
    if not pe_pool:
        pe_pool = pe_contracts
    valid_pe = []
    for p in pe_pool:
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
        "strike_mode": "PRICE",
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
