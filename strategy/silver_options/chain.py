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


def find_silver_strangle_strikes(
    kite: Optional[KiteConnect],
    underlying: str = "SILVERM",
    strike_otm_pct: float = 0.015,
    exchange: str = "MCX",
    mock_spot: Optional[float] = None,
    instruments: Optional[List[Dict[str, Any]]] = None,
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
        try:
            q_key = f"{exchange}:{fut_symbol}"
            quotes = kite.quote([q_key])
            if q_key in quotes and quotes[q_key].get("last_price"):
                spot_price = float(quotes[q_key]["last_price"])
                logger.info("Live %s underlying spot from %s: Rs %.2f", underlying, fut_symbol, spot_price)
        except Exception as e:
            logger.warning("Could not fetch live spot quote for %s: %s", fut_symbol, e)

    # Fallback spot price if Kite quote unavailable
    if spot_price is None or spot_price <= 0:
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

    # 5. Match closest available strikes in the active expiry
    ce_opt = None
    pe_opt = None

    if opts and active_expiry:
        expiry_opts = [o for o in opts if o.get("expiry") == active_expiry]
        ce_candidates = [o for o in expiry_opts if o["instrument_type"] == "CE"]
        pe_candidates = [o for o in expiry_opts if o["instrument_type"] == "PE"]

        if ce_candidates:
            ce_opt = min(ce_candidates, key=lambda x: abs(x["strike"] - target_ce_strike))
        if pe_candidates:
            pe_opt = min(pe_candidates, key=lambda x: abs(x["strike"] - target_pe_strike))

    # Construct symbols and tokens
    ce_symbol = ce_opt["tradingsymbol"] if ce_opt else f"{underlying}{today.strftime('%y%b').upper()}{int(target_ce_strike)}CE"
    pe_symbol = pe_opt["tradingsymbol"] if pe_opt else f"{underlying}{today.strftime('%y%b').upper()}{int(target_pe_strike)}PE"
    ce_token = ce_opt.get("instrument_token") if ce_opt else None
    pe_token = pe_opt.get("instrument_token") if pe_opt else None
    ce_strike = ce_opt["strike"] if ce_opt else target_ce_strike
    pe_strike = pe_opt["strike"] if pe_opt else target_pe_strike

    # 6. Fetch live option quotes if available
    ce_ltp = 0.0
    pe_ltp = 0.0
    if kite is not None:
        try:
            q_keys = [f"{exchange}:{ce_symbol}", f"{exchange}:{pe_symbol}"]
            quotes = kite.quote(q_keys)
            ce_key = f"{exchange}:{ce_symbol}"
            pe_key = f"{exchange}:{pe_symbol}"
            if ce_key in quotes and quotes[ce_key].get("last_price"):
                ce_ltp = float(quotes[ce_key]["last_price"])
            if pe_key in quotes and quotes[pe_key].get("last_price"):
                pe_ltp = float(quotes[pe_key]["last_price"])
        except Exception as e:
            logger.warning("Could not fetch option quotes for %s and %s: %s", ce_symbol, pe_symbol, e)

    # If test mode / no quotes available, compute approximate Black-76 estimate
    if ce_ltp <= 0.0 or pe_ltp <= 0.0:
        from strategy.options_model import black_76_price
        cal_days = max(1, (active_expiry - today).days) if isinstance(active_expiry, date) else 5
        dte = max(1.0, float(cal_days) * 5.0 / 7.0)
        ce_ltp = black_76_price(spot_price, ce_strike, dte, iv=0.30, opt_type="CE")
        pe_ltp = black_76_price(spot_price, pe_strike, dte, iv=0.30, opt_type="PE")

    logger.info(
        "Resolved Strangle: Spot: %.1f | CE: %s @ Rs %.2f (Strike: %.1f) | PE: %s @ Rs %.2f (Strike: %.1f)",
        spot_price, ce_symbol, ce_ltp, ce_strike, pe_symbol, pe_ltp, pe_strike
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
        "pe_symbol": pe_symbol,
        "pe_token": pe_token,
        "pe_strike": pe_strike,
        "pe_ltp": pe_ltp,
    }
