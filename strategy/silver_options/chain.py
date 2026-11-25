import logging
from datetime import date, datetime
from typing import Dict, Any, Optional, List
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.silver.chain")


def get_strike_step(spot_price: float) -> float:
    """MCX Silver strikes are structured in 500 or 1000 increments."""
    if spot_price > 120000:
        return 1000.0
    return 500.0


def round_to_strike_step(price: float, step: float = 500.0) -> float:
    return round(round(price / step) * step, 1)


def _extract_quote_metrics(q: Dict[str, Any]) -> Dict[str, Any]:
    """Extracts LTP, OI, best bid/ask, and bid-ask spread pct from a Kite quote entry."""
    ltp = float(q.get("last_price") or 0.0)
    oi = int(q["oi"]) if ("oi" in q and q["oi"] is not None) else None
    depth = q.get("depth") if isinstance(q.get("depth"), dict) else {}
    buy_levels = depth.get("buy") or []
    sell_levels = depth.get("sell") or []
    best_bid = float(buy_levels[0].get("price") or 0.0) if buy_levels else None
    best_ask = float(sell_levels[0].get("price") or 0.0) if sell_levels else None
    if best_bid is not None and best_bid <= 0:
        best_bid = None
    if best_ask is not None and best_ask <= 0:
        best_ask = None
    spread_pct = None
    if best_bid and best_ask and best_ask >= best_bid:
        mid = 0.5 * (best_bid + best_ask)
        if mid > 0:
            spread_pct = (best_ask - best_bid) / mid
    return {
        "ltp": ltp,
        "oi": oi,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": spread_pct,
    }


def _select_liquid_candidate(
    candidates: List[Dict[str, Any]],
    quotes: Dict[str, Any],
    exchange: str,
    min_oi: int,
    max_spread_pct: float,
):
    """Selects the first candidate (closest to target) passing OI and bid-ask spread filters."""
    rejected = []
    for opt in candidates:
        sym = opt["tradingsymbol"]
        q = quotes.get(f"{exchange}:{sym}", {})
        if not q:
            rejected.append(f"{sym}: no quote")
            continue
        metrics = _extract_quote_metrics(q)
        if metrics["ltp"] <= 0.0:
            rejected.append(f"{sym}: LTP=0")
            continue
        if min_oi > 0 and metrics["oi"] is not None and metrics["oi"] < min_oi:
            rejected.append(f"{sym}: OI={metrics['oi']}<{min_oi}")
            continue
        if max_spread_pct > 0 and metrics["spread_pct"] is not None and metrics["spread_pct"] > max_spread_pct:
            rejected.append(f"{sym}: spread={metrics['spread_pct']*100:.1f}%>{max_spread_pct*100:.1f}%")
            continue
        return opt, metrics, rejected
    return None, {}, rejected


def find_silver_strangle_strikes(
    kite: Optional[KiteConnect],
    underlying: str = "SILVERM",
    strike_otm_pct: float = 0.015,
    exchange: str = "MCX",
    mock_spot: Optional[float] = None,
    instruments: Optional[List[Dict[str, Any]]] = None,
    allow_fallback: bool = True,
    min_oi: int = 100,
    max_spread_pct: float = 0.03,
    scan_steps: int = 5,
) -> Dict[str, Any]:
    """
    Identifies the active near-month monthly expiry and OTM Strangle strikes on MCX.
    
    Parameters:
        kite: KiteConnect instance (optional for mock/offline testing)
        underlying: 'SILVERM' (Silver Mini) or 'SILVER'
        strike_otm_pct: Percentage distance from spot for OTM strike (default: 0.015 / 1.5%)
        exchange: Exchange code ('MCX')
        mock_spot: Override spot price for testing
        instruments: Pre-fetched instruments cache
    """
    logger.info("Resolving %s option strikes (%s, %.1f%% OTM)...", underlying, exchange, strike_otm_pct * 100)

    # 1. Fetch Instruments if not provided
    if instruments is None and kite is not None:
        try:
            instruments = kite.instruments(exchange)
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("Token error fetching %s instruments (%s). Refreshing session...", exchange, e)
                kite = ensure_kite_session(kite, force_login=False)
                instruments = kite.instruments(exchange)
            else:
                logger.error("Failed to fetch %s instruments from Kite: %s", exchange, e)
                instruments = []
    instruments = instruments or []

    # 2. Filter Silver options and futures
    opts = [
        i for i in instruments
        if i.get("name") == underlying and i.get("instrument_type") in ("CE", "PE")
    ]
    futs = [
        i for i in instruments
        if i.get("name") == underlying and i.get("instrument_type") == "FUT"
    ]

    # Resolve active near-month futures
    fut_symbol = None
    fut_token = None
    spot_price = mock_spot

    today = datetime.now().date()
    if futs:
        # Sort futures by expiry
        future_futs = [f for f in futs if f.get("expiry") and f["expiry"] >= today]
        future_futs.sort(key=lambda x: x["expiry"])
        if future_futs:
            active_fut = future_futs[0]
            fut_symbol = active_fut["tradingsymbol"]
            fut_token = active_fut["instrument_token"]

    # If spot price not mocked, fetch live quote from Kite
    if spot_price is None and fut_symbol and kite is not None:
        q_key = f"{exchange}:{fut_symbol}"
        try:
            quotes = kite.quote([q_key])
            if q_key in quotes and quotes[q_key].get("last_price"):
                spot_price = float(quotes[q_key]["last_price"])
                logger.info("Live %s underlying spot from %s: Rs %.2f", underlying, fut_symbol, spot_price)
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("Token error fetching spot quote for %s (%s). Refreshing session...", fut_symbol, e)
                try:
                    kite = ensure_kite_session(kite, force_login=False)
                    quotes = kite.quote([q_key])
                    if q_key in quotes and quotes[q_key].get("last_price"):
                        spot_price = float(quotes[q_key]["last_price"])
                        logger.info("Live %s underlying spot from %s: Rs %.2f", underlying, fut_symbol, spot_price)
                except Exception as retry_e:
                    logger.warning("Retry failed for live spot quote %s: %s", fut_symbol, retry_e)
            else:
                logger.warning("Could not fetch live spot quote for %s: %s", fut_symbol, e)

    # Fallback spot price if Kite quote unavailable
    if spot_price is None or spot_price <= 0:
        if not allow_fallback:
            raise RuntimeError(f"Could not fetch live spot price for {underlying} ({fut_symbol}); refusing fallback benchmark in LIVE mode.")
        spot_price = 155000.0  # Conservative typical price benchmark for Silver
        logger.warning("Using fallback spot price for %s: Rs %.2f", underlying, spot_price)

    # 3. Resolve active options expiry
    active_expiry = None
    if opts:
        future_expiries = sorted(list({o["expiry"] for o in opts if o.get("expiry") and o["expiry"] >= today}))
        if future_expiries:
            active_expiry = future_expiries[0]

    if active_expiry is None:
        active_expiry = today

    # 4. Compute target strikes
    step = get_strike_step(spot_price)
    target_ce_strike = round_to_strike_step(spot_price * (1.0 + strike_otm_pct), step)
    target_pe_strike = round_to_strike_step(spot_price * (1.0 - strike_otm_pct), step)

    # 5. Match closest available strikes in the active expiry (with OI & spread filtering when quotes available)
    ce_opt = None
    pe_opt = None
    ce_candidates: List[Dict[str, Any]] = []
    pe_candidates: List[Dict[str, Any]] = []

    if opts and active_expiry:
        expiry_opts = [o for o in opts if o.get("expiry") == active_expiry]
        all_ce = [o for o in expiry_opts if o["instrument_type"] == "CE"]
        all_pe = [o for o in expiry_opts if o["instrument_type"] == "PE"]

        otm_ce = [o for o in all_ce if o["strike"] > spot_price] or all_ce
        otm_pe = [o for o in all_pe if o["strike"] < spot_price] or all_pe

        max_cands = max(1, scan_steps * 2 + 1)
        ce_candidates = sorted(otm_ce, key=lambda x: abs(x["strike"] - target_ce_strike))[:max_cands]
        pe_candidates = sorted(otm_pe, key=lambda x: abs(x["strike"] - target_pe_strike))[:max_cands]

        if ce_candidates:
            ce_opt = ce_candidates[0]
        if pe_candidates:
            pe_opt = pe_candidates[0]

    # Construct default fallback symbols and tokens
    ce_symbol = ce_opt["tradingsymbol"] if ce_opt else f"{underlying}{today.strftime('%y%b').upper()}{int(target_ce_strike)}CE"
    pe_symbol = pe_opt["tradingsymbol"] if pe_opt else f"{underlying}{today.strftime('%y%b').upper()}{int(target_pe_strike)}PE"
    ce_token = ce_opt.get("instrument_token") if ce_opt else None
    pe_token = pe_opt.get("instrument_token") if pe_opt else None
    ce_strike = ce_opt["strike"] if ce_opt else target_ce_strike
    pe_strike = pe_opt["strike"] if pe_opt else target_pe_strike

    # 6. Fetch live option quotes across candidate strikes in one batch call
    ce_ltp = 0.0
    pe_ltp = 0.0
    ce_metrics: Dict[str, Any] = {}
    pe_metrics: Dict[str, Any] = {}

    if kite is not None:
        ce_pool = ce_candidates if ce_candidates else [{"tradingsymbol": ce_symbol, "strike": ce_strike, "instrument_token": ce_token}]
        pe_pool = pe_candidates if pe_candidates else [{"tradingsymbol": pe_symbol, "strike": pe_strike, "instrument_token": pe_token}]
        q_keys = list(dict.fromkeys([f"{exchange}:{o['tradingsymbol']}" for o in ce_pool + pe_pool]))
        quotes: Dict[str, Any] = {}
        try:
            quotes = kite.quote(q_keys) or {}
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("Token error fetching option quotes (%s). Refreshing session...", e)
                try:
                    kite = ensure_kite_session(kite, force_login=False)
                    quotes = kite.quote(q_keys) or {}
                except Exception as retry_e:
                    logger.warning("Retry failed fetching option quotes: %s", retry_e)
            else:
                logger.warning("Could not fetch option quotes: %s", e)

        if quotes:
            sel_ce, ce_m, ce_rej = _select_liquid_candidate(ce_pool, quotes, exchange, min_oi, max_spread_pct)
            sel_pe, pe_m, pe_rej = _select_liquid_candidate(pe_pool, quotes, exchange, min_oi, max_spread_pct)

            if sel_ce is not None:
                ce_opt = sel_ce
                ce_symbol = sel_ce["tradingsymbol"]
                ce_token = sel_ce.get("instrument_token")
                ce_strike = sel_ce["strike"]
                ce_ltp = ce_m["ltp"]
                ce_metrics = ce_m
            elif not allow_fallback:
                raise RuntimeError(f"No liquid CE strike met OI>={min_oi} & spread<={max_spread_pct*100:.1f}%: {'; '.join(ce_rej[:5])}")

            if sel_pe is not None:
                pe_opt = sel_pe
                pe_symbol = sel_pe["tradingsymbol"]
                pe_token = sel_pe.get("instrument_token")
                pe_strike = sel_pe["strike"]
                pe_ltp = pe_m["ltp"]
                pe_metrics = pe_m
            elif not allow_fallback:
                raise RuntimeError(f"No liquid PE strike met OI>={min_oi} & spread<={max_spread_pct*100:.1f}%: {'; '.join(pe_rej[:5])}")

            if ce_rej:
                logger.info("Skipped illiquid CE candidate(s): %s", "; ".join(ce_rej))
            if pe_rej:
                logger.info("Skipped illiquid PE candidate(s): %s", "; ".join(pe_rej))

    # If test mode / no quotes available, compute approximate Black-76 estimate
    if ce_ltp <= 0.0 or pe_ltp <= 0.0:
        if not allow_fallback:
            raise RuntimeError(
                f"Live option quotes unavailable for {ce_symbol} (LTP={ce_ltp}) / {pe_symbol} (LTP={pe_ltp}); aborting LIVE entry."
            )
        from strategy.options_model import black_76_price
        cal_days = max(1, (active_expiry - today).days) if isinstance(active_expiry, date) else 5
        dte = max(1.0, float(cal_days) * 5.0 / 7.0)
        ce_ltp = black_76_price(spot_price, ce_strike, dte, iv=0.30, opt_type="CE")
        pe_ltp = black_76_price(spot_price, pe_strike, dte, iv=0.30, opt_type="PE")

    logger.info(
        "Resolved Strangle: Spot: %.1f | CE: %s @ Rs %.2f (Strike: %.1f, OI: %s, Bid: %s) | PE: %s @ Rs %.2f (Strike: %.1f, OI: %s, Bid: %s)",
        spot_price, ce_symbol, ce_ltp, ce_strike, ce_metrics.get("oi"), ce_metrics.get("best_bid"),
        pe_symbol, pe_ltp, pe_strike, pe_metrics.get("oi"), pe_metrics.get("best_bid")
    )

    return {
        "underlying": underlying,
        "underlying_futures": fut_symbol,
        "spot_price": spot_price,
        "expiry": str(active_expiry),
        "ce_symbol": ce_symbol,
        "ce_token": ce_token,
        "ce_strike": ce_strike,
        "ce_ltp": ce_ltp,
        "ce_bid": ce_metrics.get("best_bid"),
        "ce_ask": ce_metrics.get("best_ask"),
        "ce_oi": ce_metrics.get("oi"),
        "pe_symbol": pe_symbol,
        "pe_token": pe_token,
        "pe_strike": pe_strike,
        "pe_ltp": pe_ltp,
        "pe_bid": pe_metrics.get("best_bid"),
        "pe_ask": pe_metrics.get("best_ask"),
        "pe_oi": pe_metrics.get("oi"),
    }
