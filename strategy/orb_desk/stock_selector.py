import time
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Set
from kiteconnect import KiteConnect

logger = logging.getLogger("orb.stock_selector")

INDEX_EXCLUSIONS: Set[str] = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50"
}


def get_fno_stock_symbols(kite: KiteConnect) -> List[str]:
    """
    Retrieves all unique stock underlying symbols in the NSE F&O segment,
    excluding indices.
    """
    try:
        nfo_instruments = kite.instruments("NFO")
        symbols = {
            item["name"]
            for item in nfo_instruments
            if item.get("segment") == "NFO-FUT"
            and item.get("instrument_type") == "FUT"
            and item.get("name") not in INDEX_EXCLUSIONS
        }
        return sorted(list(symbols))
    except Exception as e:
        logger.error("Failed to fetch F&O stock symbols from Kite: %s", e)
        return []


def get_fno_stock_metadata(kite: KiteConnect) -> Dict[str, Dict[str, Any]]:
    """
    Retrieves metadata (lot size, token) for all F&O stock underlyings.
    """
    metadata: Dict[str, Dict[str, Any]] = {}
    try:
        nfo_instruments = kite.instruments("NFO")
        for item in nfo_instruments:
            name = item.get("name")
            if (
                item.get("segment") == "NFO-FUT"
                and item.get("instrument_type") == "FUT"
                and name not in INDEX_EXCLUSIONS
            ):
                if name not in metadata:
                    metadata[name] = {
                        "symbol": name,
                        "lot_size": item.get("lot_size", 1),
                        "nfo_token": item.get("instrument_token")
                    }
    except Exception as e:
        logger.error("Failed to fetch F&O metadata from Kite: %s", e)
    return metadata


def screen_top_gainers_losers(
    kite: KiteConnect,
    symbols: List[str],
    n: int = 3
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Fetches real-time quotes for F&O stocks and returns top N gainers and
    top N losers calculated as % change from previous day's close.
    """
    if not symbols:
        return [], []

    # Batch symbols in chunks of 400 (Kite quote supports up to 500)
    batch_size = 400
    quotes: Dict[str, Any] = {}
    
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i : i + batch_size]
        keys = [f"NSE:{s}" for s in chunk]
        try:
            res = kite.quote(keys)
            quotes.update(res)
        except Exception as e:
            logger.error("Failed to quote batch starting with %s: %s", chunk[0], e)

    items: List[Dict[str, Any]] = []
    for sym in symbols:
        key = f"NSE:{sym}"
        if key not in quotes:
            continue
        q = quotes[key]
        last_price = float(q.get("last_price") or 0.0)
        close = float((q.get("ohlc") or {}).get("close") or 0.0)
        token = q.get("instrument_token")

        if close > 0 and last_price > 0:
            pct_change = ((last_price - close) / close) * 100.0
            items.append({
                "symbol": sym,
                "token": token,
                "last_price": last_price,
                "prev_close": close,
                "pct_change": pct_change,
            })

    if not items:
        return [], []

    # Sort descending by % change
    items_by_gain = sorted(items, key=lambda x: x["pct_change"], reverse=True)
    top_gainers = items_by_gain[:n]

    # Sort ascending by % change for losers
    items_by_loss = sorted(items, key=lambda x: x["pct_change"])
    top_losers = items_by_loss[:n]

    logger.info(
        "Screened Top %d Gainers: %s",
        n,
        [f"{g['symbol']} ({g['pct_change']:+.2f}%)" for g in top_gainers]
    )
    logger.info(
        "Screened Top %d Losers: %s",
        n,
        [f"{l['symbol']} ({l['pct_change']:+.2f}%)" for l in top_losers]
    )

    return top_gainers, top_losers


def is_coiling_pattern(daily_candles: List[Dict[str, Any]]) -> bool:
    """
    Evaluates whether the last 3 completed daily candles satisfy the coiling compression:
      High(T-3) > High(T-2) > High(T-1)  (Lower Highs)
      Low(T-3)  < Low(T-2)  < Low(T-1)   (Higher Lows)
    """
    if len(daily_candles) < 3:
        return False

    c3, c2, c1 = daily_candles[-3], daily_candles[-2], daily_candles[-1]
    h3, h2, h1 = float(c3["high"]), float(c2["high"]), float(c1["high"])
    l3, l2, l1 = float(c3["low"]), float(c2["low"]), float(c1["low"])

    return (h3 > h2 > h1) and (l3 < l2 < l1)


def screen_coiling_pattern_stocks(
    kite: KiteConnect,
    symbols_data: List[Dict[str, Any]],
    today_date: Optional[date] = None,
    rate_limit_delay: float = 0.35,
    cache_dir: Optional[Path] = None,
    force_rescan: bool = False
) -> List[Dict[str, Any]]:
    """
    Screens stocks for the 3-day contraction / coiling pattern from completed daily candles.
    Caches results daily so subsequent runs load instantly without re-querying Kite historical data.
    """
    today = today_date or date.today()
    c_dir = cache_dir or (Path(__file__).resolve().parent.parent.parent / "data")
    cache_file = c_dir / "fno_coiling_cache.json"

    if not force_rescan and cache_file.exists():
        try:
            cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
            if cache_data.get("date") == str(today):
                cached_list = cache_data.get("coiling_stocks", [])
                logger.info("Loaded %d coiling pattern stocks from daily cache (%s).", len(cached_list), cache_file)
                return cached_list
        except Exception as e:
            logger.warning("Could not read coiling cache: %s", e)

    start_dt = today - timedelta(days=12)
    coiling_stocks: List[Dict[str, Any]] = []

    logger.info("Screening %d stocks for 3-day coiling pattern...", len(symbols_data))

    for item in symbols_data:
        sym = item["symbol"]
        token = item.get("token")
        if not token:
            continue

        try:
            hist = kite.historical_data(
                instrument_token=token,
                from_date=start_dt,
                to_date=today,
                interval="day"
            )
            # Filter to completed daily candles prior to today
            completed = [
                c for c in hist
                if (c["date"].date() if isinstance(c["date"], datetime) else c["date"]) < today
            ]

            if is_coiling_pattern(completed):
                c3, c2, c1 = completed[-3], completed[-2], completed[-1]
                coiling_stocks.append({
                    "symbol": sym,
                    "token": token,
                    "lot": item.get("lot", 1),
                    "t1_high": float(c1["high"]),
                    "t1_low": float(c1["low"]),
                    "t2_high": float(c2["high"]),
                    "t2_low": float(c2["low"]),
                    "t3_high": float(c3["high"]),
                    "t3_low": float(c3["low"]),
                })
                logger.info(
                    "Detected Coiling Pattern on %s: Highs (%.2f > %.2f > %.2f), Lows (%.2f < %.2f < %.2f)",
                    sym, c3["high"], c2["high"], c1["high"], c3["low"], c2["low"], c1["low"]
                )
        except Exception as e:
            logger.warning("Could not fetch daily candles for %s: %s", sym, e)

        if rate_limit_delay > 0:
            time.sleep(rate_limit_delay)

    logger.info("Found %d stocks satisfying 3-day coiling pattern.", len(coiling_stocks))

    try:
        c_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"date": str(today), "coiling_stocks": coiling_stocks}, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning("Failed to save coiling cache: %s", e)

    return coiling_stocks


def build_dynamic_watchlist(
    kite: KiteConnect,
    pre_scanned_coiling: Optional[List[Dict[str, Any]]] = None,
    top_gainers_n: int = 3,
    top_losers_n: int = 3,
    today_date: Optional[date] = None
) -> List[Dict[str, Any]]:
    """
    Builds the dynamic 10:15 AM watchlist by combining:
      1. Top N Gainers
      2. Top N Losers
      3. Pre-scanned (or freshly scanned) Coiling pattern stocks
    Deduplicates entries and attaches selection metadata ('reasons').
    """
    # 1. Fetch metadata for F&O universe
    metadata = get_fno_stock_metadata(kite)
    symbols = sorted(list(metadata.keys()))

    if not symbols:
        # Fallback to direct symbol extraction
        symbols = get_fno_stock_symbols(kite)

    # 2. Screen real-time Top Gainers & Losers
    gainers, losers = screen_top_gainers_losers(kite, symbols, n=top_gainers_n)

    # 3. Use pre-scanned coiling stocks or empty list
    coiling_list = pre_scanned_coiling or []

    # 4. Merge into deduplicated map
    watchlist_map: Dict[str, Dict[str, Any]] = {}

    for g in gainers:
        sym = g["symbol"]
        lot = metadata.get(sym, {}).get("lot_size", 1)
        watchlist_map[sym] = {
            "symbol": sym,
            "token": g["token"],
            "lot": lot,
            "pct_change": g["pct_change"],
            "last_price": g["last_price"],
            "prev_close": g["prev_close"],
            "reasons": ["TOP_GAINER"]
        }

    for l in losers:
        sym = l["symbol"]
        lot = metadata.get(sym, {}).get("lot_size", 1)
        if sym in watchlist_map:
            watchlist_map[sym]["reasons"].append("TOP_LOSER")
        else:
            watchlist_map[sym] = {
                "symbol": sym,
                "token": l["token"],
                "lot": lot,
                "pct_change": l["pct_change"],
                "last_price": l["last_price"],
                "prev_close": l["prev_close"],
                "reasons": ["TOP_LOSER"]
            }

    for c in coiling_list:
        sym = c["symbol"]
        lot = c.get("lot") or metadata.get(sym, {}).get("lot_size", 1)
        if sym in watchlist_map:
            if "COILING" not in watchlist_map[sym]["reasons"]:
                watchlist_map[sym]["reasons"].append("COILING")
        else:
            watchlist_map[sym] = {
                "symbol": sym,
                "token": c["token"],
                "lot": lot,
                "pct_change": None,
                "last_price": None,
                "prev_close": None,
                "reasons": ["COILING"]
            }

    final_watchlist = list(watchlist_map.values())
    logger.info(
        "Dynamic Watchlist built with %d stocks: %s",
        len(final_watchlist),
        [f"{x['symbol']} ({'+'.join(x['reasons'])})" for x in final_watchlist]
    )
    return final_watchlist
