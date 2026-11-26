import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.silver.chain")


def _normalize_date(d: Any) -> Optional[date]:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


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
        if isinstance(q.get("depth"), dict) and (metrics["best_bid"] is None or metrics["best_ask"] is None):
            rejected.append(f"{sym}: empty bid/ask depth")
            continue
        if min_oi > 0 and metrics["oi"] is not None and metrics["oi"] < min_oi:
            rejected.append(f"{sym}: OI={metrics['oi']}<{min_oi}")
            continue
        if max_spread_pct > 0 and metrics["spread_pct"] is not None and metrics["spread_pct"] > max_spread_pct:
            rejected.append(f"{sym}: spread={metrics['spread_pct']*100:.1f}%>{max_spread_pct*100:.1f}%")
            continue
        return opt, metrics, rejected
    return None, {}, rejected


def _compute_silver_atr_metrics(
    kite: Optional[KiteConnect],
    fut_token: Optional[int],
    period: int = 14,
    as_of_date: Optional[date] = None,
) -> Tuple[Optional[float], Optional[float], Optional[KiteConnect]]:
    """
    Computes `(daily_atr, prev_completed_close, kite)` from completed historical daily candles
    prior to `as_of_date`, excluding today's incomplete bar and retrying once on expired Kite tokens.
    """
    if kite is None or not fut_token or period <= 0:
        return None, None, kite
    ref_date = as_of_date or datetime.now().date()
    from_dt = ref_date - timedelta(days=max(30, period * 3))
    to_dt = ref_date
    try:
        try:
            candles = kite.historical_data(
                instrument_token=fut_token,
                from_date=from_dt,
                to_date=to_dt,
                interval="day",
            )
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("Token error fetching daily candles for Silver ATR (token=%s). Refreshing session...", fut_token)
                kite = ensure_kite_session(kite, force_login=False)
                candles = kite.historical_data(
                    instrument_token=fut_token,
                    from_date=from_dt,
                    to_date=to_dt,
                    interval="day",
                )
            else:
                raise
    except Exception as e:
        logger.warning("Failed to fetch daily candles for Silver ATR (token=%s): %s", fut_token, e)
        return None, None, kite

    if not candles:
        return None, None, kite

    completed = []
    for c in candles:
        c_date = _normalize_date(c.get("date"))
        if c_date is not None and c_date < ref_date:
            completed.append(c)
    if not completed:
        return None, None, kite

    trs: List[float] = []
    prev_close: Optional[float] = None
    for bar in completed:
        h = float(bar.get("high") or 0.0)
        l = float(bar.get("low") or 0.0)
        c = float(bar.get("close") or 0.0)
        if h <= 0 or l <= 0 or c <= 0:
            continue
        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c

    if not trs:
        return None, None, kite
    window = trs[-period:]
    return round(sum(window) / len(window), 2), prev_close, kite


def compute_silver_daily_atr(
    kite: Optional[KiteConnect],
    fut_token: Optional[int],
    period: int = 14,
    as_of_date: Optional[date] = None,
) -> Optional[float]:
    """
    Computes prior-completed-days True Range average (Daily ATR) for MCX Silver futures
    using Kite historical daily candles, excluding today's incomplete bar to avoid lookahead.
    """
    atr_val, _, _ = _compute_silver_atr_metrics(
        kite=kite,
        fut_token=fut_token,
        period=period,
        as_of_date=as_of_date,
    )
    return atr_val


def _fetch_live_fut_spot(
    kite: Optional[KiteConnect],
    fut_symbol: Optional[str],
    exchange: str,
    underlying: str,
) -> Tuple[Optional[float], Optional[KiteConnect]]:
    """Fetches live futures last_price with one TokenException refresh retry."""
    if not fut_symbol or kite is None:
        return None, kite
    q_key = f"{exchange}:{fut_symbol}"
    spot_price: Optional[float] = None
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
    return spot_price, kite


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
    skip_0dte: bool = True,
    strike_mode: str = "PERCENT",
    atr_multiplier: float = 0.75,
    atr_period: int = 14,
    atr_min_otm_pct: float = 0.01,
    daily_atr: Optional[float] = None,
    as_of_date: Optional[date] = None,
    context: Optional[Dict[str, Any]] = None,
    exclude_symbols: Optional[set] = None,
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
        strike_mode: 'PERCENT' (fixed OTM %) or 'ATR' (daily ATR * atr_multiplier)
    """
    logger.info("Resolving %s option strikes (%s, mode=%s, %.1f%% OTM)...", underlying, exchange, strike_mode, strike_otm_pct * 100)

    today = as_of_date or datetime.now().date()
    want_atr = str(strike_mode).upper() == "ATR" and daily_atr is None
    if context is None:
        ctx = resolve_silver_context(
            kite=kite,
            underlying=underlying,
            exchange=exchange,
            mock_spot=mock_spot,
            instruments=instruments,
            allow_fallback=allow_fallback,
            skip_0dte=skip_0dte,
            atr_period=atr_period,
            as_of_date=today,
            include_atr=want_atr,
        )
        kite = ctx.get("kite")
    else:
        ctx = context
        kite = kite or ctx.get("kite")
    opts = ctx.get("opts") or []
    active_expiry = ctx.get("active_expiry") or today
    fut_symbol = ctx.get("fut_symbol")
    fut_token = ctx.get("fut_token")
    spot_price = float(mock_spot) if mock_spot is not None else float(ctx.get("spot_price") or 0.0)
    if spot_price <= 0:
        if not allow_fallback:
            raise RuntimeError(f"Could not fetch live spot price for {underlying} ({fut_symbol}); refusing fallback benchmark in LIVE mode.")
        spot_price = 155000.0
        logger.warning("Using fallback spot price for %s: Rs %.2f", underlying, spot_price)

    # 4. Compute target strikes (PERCENT or ATR-scaled)
    step = get_strike_step(spot_price)
    resolved_atr: Optional[float] = None
    if str(strike_mode).upper() == "ATR":
        resolved_atr = (
            daily_atr if (daily_atr is not None and daily_atr > 0)
            else (ctx.get("daily_atr") or compute_silver_daily_atr(kite=kite, fut_token=fut_token, period=atr_period, as_of_date=today))
        )
        if resolved_atr is not None and resolved_atr > 0:
            offset = max(resolved_atr * atr_multiplier, spot_price * atr_min_otm_pct)
            target_ce_strike = round_to_strike_step(spot_price + offset, step)
            target_pe_strike = round_to_strike_step(spot_price - offset, step)
            logger.info(
                "ATR Strike Scaling: ATR(%d)=Rs %.1f * %.2f -> Offset=Rs %.1f (%.2f%%) -> CE %.0f / PE %.0f",
                atr_period, resolved_atr, atr_multiplier, offset, (offset / spot_price) * 100, target_ce_strike, target_pe_strike,
            )
        else:
            logger.warning("ATR mode requested but daily ATR unavailable; falling back to %.1f%% OTM.", strike_otm_pct * 100)
            target_ce_strike = round_to_strike_step(spot_price * (1.0 + strike_otm_pct), step)
            target_pe_strike = round_to_strike_step(spot_price * (1.0 - strike_otm_pct), step)
    else:
        target_ce_strike = round_to_strike_step(spot_price * (1.0 + strike_otm_pct), step)
        target_pe_strike = round_to_strike_step(spot_price * (1.0 - strike_otm_pct), step)

    dte_days = (active_expiry - today).days if isinstance(active_expiry, date) else int(ctx.get("dte_days") or 0)

    # 5. Match closest available strikes in the active expiry (with OI & spread filtering when quotes available)
    ce_opt = None
    pe_opt = None
    ce_candidates: List[Dict[str, Any]] = []
    pe_candidates: List[Dict[str, Any]] = []

    if opts and active_expiry:
        expiry_opts = [o for o in opts if _normalize_date(o.get("expiry")) == active_expiry]
        if exclude_symbols:
            expiry_opts = [o for o in expiry_opts if o.get("tradingsymbol") not in exclude_symbols]
        all_ce = [o for o in expiry_opts if o["instrument_type"] == "CE"]
        all_pe = [o for o in expiry_opts if o["instrument_type"] == "PE"]

        otm_ce = [o for o in all_ce if o["strike"] > spot_price] or all_ce
        otm_pe = [o for o in all_pe if o["strike"] < spot_price] or all_pe

        max_cands = max(1, scan_steps * 2 + 1)
        # Prefer further-OTM strikes over stepping inward toward ATM if liquidity scan skips the primary strike
        ce_candidates = sorted(
            otm_ce,
            key=lambda x: (0 if x["strike"] >= target_ce_strike else 1, abs(x["strike"] - target_ce_strike)),
        )[:max_cands]
        pe_candidates = sorted(
            otm_pe,
            key=lambda x: (0 if x["strike"] <= target_pe_strike else 1, abs(x["strike"] - target_pe_strike)),
        )[:max_cands]

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
        "dte_days": dte_days,
        "strike_mode": str(strike_mode).upper(),
        "daily_atr": resolved_atr,
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


def resolve_silver_context(
    kite: Optional[KiteConnect],
    underlying: str = "SILVERM",
    exchange: str = "MCX",
    mock_spot: Optional[float] = None,
    instruments: Optional[List[Dict[str, Any]]] = None,
    allow_fallback: bool = True,
    skip_0dte: bool = True,
    atr_period: int = 14,
    as_of_date: Optional[date] = None,
    include_atr: bool = True,
) -> Dict[str, Any]:
    """
    Resolves active option expiry, aligned futures contract/token, live futures spot,
    calendar DTE, and prior-day Daily ATR(period) for MCX Silver.
    """
    if instruments is None and kite is not None:
        try:
            instruments = kite.instruments(exchange)
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                kite = ensure_kite_session(kite, force_login=False)
                instruments = kite.instruments(exchange)
            else:
                logger.error("Failed to fetch %s instruments from Kite: %s", exchange, e)
                instruments = []
    instruments = instruments or []

    opts = [
        i for i in instruments
        if i.get("name") == underlying and i.get("instrument_type") in ("CE", "PE")
    ]
    futs = [
        i for i in instruments
        if i.get("name") == underlying and i.get("instrument_type") == "FUT"
    ]

    today = as_of_date or datetime.now().date()
    active_expiry = None
    if opts:
        future_expiries = sorted({
            d for d in (_normalize_date(o.get("expiry")) for o in opts)
            if d is not None and d >= today
        })
        if future_expiries:
            if skip_0dte and future_expiries[0] == today and len(future_expiries) > 1:
                logger.info("Today (%s) is %s option expiry (0-DTE). Rolling to next expiry: %s", today, underlying, future_expiries[1])
                active_expiry = future_expiries[1]
            else:
                active_expiry = future_expiries[0]
    if active_expiry is None:
        active_expiry = today

    fut_symbol = None
    fut_token = None
    spot_price = mock_spot

    if futs:
        future_futs = sorted(
            [
                (exp_d, f)
                for f in futs
                for exp_d in [_normalize_date(f.get("expiry"))]
                if exp_d is not None and exp_d >= today
            ],
            key=lambda x: x[0],
        )
        aligned_futs = [f for exp_d, f in future_futs if exp_d >= active_expiry]
        selected_pool = aligned_futs or [f for _, f in future_futs]
        if selected_pool:
            active_fut = selected_pool[0]
            fut_symbol = active_fut["tradingsymbol"]
            fut_token = active_fut.get("instrument_token")

    if spot_price is None:
        spot_price, kite = _fetch_live_fut_spot(kite, fut_symbol, exchange, underlying)

    if spot_price is None or spot_price <= 0:
        if not allow_fallback:
            raise RuntimeError(f"Could not fetch live spot price for {underlying} ({fut_symbol}); refusing fallback benchmark in LIVE mode.")
        spot_price = 155000.0
        logger.warning("Using fallback spot price for %s: Rs %.2f", underlying, spot_price)

    dte_days = (active_expiry - today).days if isinstance(active_expiry, date) else 0
    daily_atr: Optional[float] = None
    prev_close: Optional[float] = None
    if include_atr:
        daily_atr, prev_close, kite = _compute_silver_atr_metrics(
            kite=kite, fut_token=fut_token, period=atr_period, as_of_date=today
        )
    atr_denom = prev_close if (prev_close is not None and prev_close > 0) else spot_price
    atr_pct = round((daily_atr / atr_denom) * 100.0, 2) if (daily_atr and atr_denom > 0) else None

    return {
        "kite": kite,
        "opts": opts,
        "active_expiry": active_expiry,
        "dte_days": dte_days,
        "fut_symbol": fut_symbol,
        "fut_token": fut_token,
        "spot_price": spot_price,
        "daily_atr": daily_atr,
        "prev_close": prev_close,
        "atr_pct": atr_pct,
    }


def find_silver_atm_option(
    kite: Optional[KiteConnect],
    option_type: str,
    underlying: str = "SILVERM",
    exchange: str = "MCX",
    mock_spot: Optional[float] = None,
    instruments: Optional[List[Dict[str, Any]]] = None,
    allow_fallback: bool = True,
    min_oi: int = 100,
    max_spread_pct: float = 0.03,
    scan_steps: int = 5,
    skip_0dte: bool = True,
    atr_period: int = 14,
    as_of_date: Optional[date] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolves the liquid ATM option contract (`CE` or `PE`) for the Directional Buyer.
    """
    opt_upper = option_type.upper()
    if opt_upper not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type}")

    today = as_of_date or datetime.now().date()
    if context is None:
        ctx = resolve_silver_context(
            kite=kite,
            underlying=underlying,
            exchange=exchange,
            mock_spot=mock_spot,
            instruments=instruments,
            allow_fallback=allow_fallback,
            skip_0dte=skip_0dte,
            atr_period=atr_period,
            as_of_date=today,
        )
        kite = ctx.get("kite")
        spot_price = ctx["spot_price"]
    else:
        ctx = context
        kite = kite or ctx.get("kite")
        spot_price = mock_spot
        if spot_price is None and kite is not None and ctx.get("fut_symbol"):
            live_spot, kite = _fetch_live_fut_spot(kite, ctx.get("fut_symbol"), exchange, underlying)
            if kite is not None:
                ctx["kite"] = kite
            spot_price = live_spot if (live_spot and live_spot > 0) else ctx.get("spot_price")
        elif spot_price is None:
            spot_price = ctx.get("spot_price")
        if spot_price is None or spot_price <= 0:
            if not allow_fallback:
                raise RuntimeError(f"Could not fetch live spot price for {underlying} ({ctx.get('fut_symbol')}); refusing fallback benchmark in LIVE mode.")
            spot_price = 155000.0

    opts = ctx.get("opts") or []
    active_expiry = ctx.get("active_expiry") or today

    step = get_strike_step(spot_price)
    target_strike = round_to_strike_step(spot_price, step)

    candidates: List[Dict[str, Any]] = []
    if opts and active_expiry:
        expiry_opts = [
            o for o in opts
            if _normalize_date(o.get("expiry")) == active_expiry and o.get("instrument_type") == opt_upper
        ]
        max_cands = max(1, scan_steps * 2 + 1)
        candidates = sorted(expiry_opts, key=lambda x: abs(x["strike"] - target_strike))[:max_cands]

    opt = candidates[0] if candidates else None
    symbol = opt["tradingsymbol"] if opt else f"{underlying}{today.strftime('%y%b').upper()}{int(target_strike)}{opt_upper}"
    token = opt.get("instrument_token") if opt else None
    strike = opt["strike"] if opt else target_strike
    ltp = 0.0
    metrics: Dict[str, Any] = {}

    if kite is not None:
        pool = candidates if candidates else [{"tradingsymbol": symbol, "strike": strike, "instrument_token": token}]
        q_keys = list(dict.fromkeys([f"{exchange}:{o['tradingsymbol']}" for o in pool]))
        quotes: Dict[str, Any] = {}
        try:
            quotes = kite.quote(q_keys) or {}
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                try:
                    kite = ensure_kite_session(kite, force_login=False)
                    quotes = kite.quote(q_keys) or {}
                except Exception as retry_e:
                    logger.warning("Retry failed fetching ATM option quotes: %s", retry_e)
            else:
                logger.warning("Could not fetch ATM option quotes: %s", e)

        if quotes:
            sel, m, rej = _select_liquid_candidate(pool, quotes, exchange, min_oi, max_spread_pct)
            if sel is not None:
                opt = sel
                symbol = sel["tradingsymbol"]
                token = sel.get("instrument_token")
                strike = sel["strike"]
                ltp = m["ltp"]
                metrics = m
            elif not allow_fallback:
                raise RuntimeError(f"No liquid ATM {opt_upper} strike met OI>={min_oi} & spread<={max_spread_pct*100:.1f}%: {'; '.join(rej[:5])}")

    if ltp <= 0.0:
        if not allow_fallback:
            raise RuntimeError(f"Live option quote unavailable for ATM {symbol}; aborting LIVE buy entry.")
        from strategy.options_model import black_76_price
        cal_days = max(1, (active_expiry - today).days) if isinstance(active_expiry, date) else 5
        dte = max(1.0, float(cal_days) * 5.0 / 7.0)
        ltp = black_76_price(spot_price, strike, dte, iv=0.30, opt_type=opt_upper)

    return {
        "underlying": underlying,
        "underlying_futures": ctx["fut_symbol"],
        "fut_token": ctx["fut_token"],
        "spot_price": spot_price,
        "expiry": str(active_expiry),
        "dte_days": ctx["dte_days"],
        "daily_atr": ctx["daily_atr"],
        "atr_pct": ctx["atr_pct"],
        "option_type": opt_upper,
        "symbol": symbol,
        "token": token,
        "strike": strike,
        "ltp": ltp,
        "bid": metrics.get("best_bid"),
        "ask": metrics.get("best_ask"),
        "oi": metrics.get("oi"),
    }
