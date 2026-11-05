import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta, date, time as dt_time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

# Ensure stdout handles UTF-8 characters cleanly
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from kite import get_kite_client
from strategy.orb_desk.stock_selector import get_fno_stock_metadata, is_coiling_pattern
from strategy.orb_desk.config import TARGET_STOCKS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("orb.dynamic_backtest")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache_15m_fno"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NIFTY_TOKEN = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"


# ---------------------------------------------------------------------------
# Data Downloader & Cache Manager
# ---------------------------------------------------------------------------
def download_all_historical_data(kite, days: int = 90) -> None:
    """
    Downloads 15-minute historical candles from Kite Connect for NIFTY and all F&O stocks.
    Caches each instrument to disk to ensure repeatable offline backtesting.
    """
    logger.info("Discovering F&O stock universe metadata from Kite...")
    fno_meta = get_fno_stock_metadata(kite)
    logger.info("Found %d F&O stocks.", len(fno_meta))

    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)

    # 1. Download NIFTY 50 15m data
    nifty_cache = CACHE_DIR / "NIFTY_50_15m.csv"
    if not nifty_cache.exists():
        logger.info("Fetching %d days 15m candles for NIFTY 50...", days)
        try:
            res = kite.historical_data(NIFTY_TOKEN, from_date=from_d, to_date=to_d, interval="15minute")
            df = pd.DataFrame(res)
            df.to_csv(nifty_cache, index=False)
            logger.info("Saved NIFTY 50 (%d bars) to %s", len(df), nifty_cache.name)
        except Exception as e:
            logger.error("Failed to fetch NIFTY 50: %s", e)
        time.sleep(0.35)

    # 2. Download all F&O stocks
    # First, get NSE equity tokens from kite.instruments("NSE")
    logger.info("Fetching NSE instruments to map underlying symbols to equity tokens...")
    nse_instruments = kite.instruments("NSE")
    nse_token_map = {
        i["tradingsymbol"]: i["instrument_token"]
        for i in nse_instruments
        if i.get("segment") == "NSE" and i.get("instrument_type") == "EQ"
    }

    total_stocks = len(fno_meta)
    downloaded = 0
    skipped = 0

    for idx, (sym, meta) in enumerate(fno_meta.items(), 1):
        clean_sym = sym.replace("&", "_")
        cache_file = CACHE_DIR / f"{clean_sym}_15m.csv"

        if cache_file.exists():
            skipped += 1
            continue

        token = nse_token_map.get(sym) or meta.get("nfo_token")
        if not token:
            logger.warning("[%d/%d] No token found for %s", idx, total_stocks, sym)
            continue

        try:
            res = kite.historical_data(token, from_date=from_d, to_date=to_d, interval="15minute")
            if res:
                df = pd.DataFrame(res)
                df["lot_size"] = meta.get("lot_size", 1)
                df.to_csv(cache_file, index=False)
                downloaded += 1
                if downloaded % 20 == 0 or idx == total_stocks:
                    logger.info("[%d/%d] Downloaded %d stocks so far (latest: %s, %d bars)", idx, total_stocks, downloaded, sym, len(df))
            else:
                logger.warning("[%d/%d] No candles returned for %s", idx, total_stocks, sym)
        except Exception as e:
            logger.warning("[%d/%d] Failed to fetch %s: %s", idx, total_stocks, sym, e)

        time.sleep(0.35)  # Respect Kite rate limit

    logger.info("Data download complete. Downloaded: %d, Cached/Skipped: %d, Total: %d", downloaded, skipped, total_stocks)


# ---------------------------------------------------------------------------
# Data Preloading & Structure Indexing
# ---------------------------------------------------------------------------
def preload_indexed_data() -> Tuple[Dict[date, Dict[str, Any]], Dict[str, Dict[str, Any]], List[date]]:
    """
    Preloads all 15m CSVs into memory, indexes by day and bar time,
    and computes daily OHLC metrics for PDH/PDL and contraction pattern scans.
    """
    logger.info("Preloading historical data into memory...")
    nifty_cache = CACHE_DIR / "NIFTY_50_15m.csv"
    if not nifty_cache.exists():
        # Fallback to cache_15m if present
        fallback = DATA_DIR / "cache_15m" / "NIFTY_50_15m.csv"
        if fallback.exists():
            nifty_cache = fallback

    df_nifty = pd.read_csv(nifty_cache, parse_dates=["date"])
    df_nifty["d_only"] = df_nifty["date"].dt.date
    df_nifty["time_only"] = df_nifty["date"].dt.time

    nifty_day_map: Dict[date, Dict[str, Any]] = {}
    for d, g in df_nifty.groupby("d_only"):
        g_sorted = g.sort_values("date").reset_index(drop=True)
        if len(g_sorted) < 4:
            continue
        day_open = float(g_sorted.loc[0, "open"])
        bar_map = {row["time_only"]: float(row["close"]) for _, row in g_sorted.iterrows()}
        nifty_day_map[d] = {
            "day_open": day_open,
            "bars": bar_map
        }

    # Load all F&O stocks
    stock_data: Dict[str, Dict[str, Any]] = {}
    all_days_set: Set[date] = set()

    csv_files = list(CACHE_DIR.glob("*_15m.csv"))
    for file in csv_files:
        sym = file.stem.replace("_15m", "").replace("_", "&")
        if sym in ("NIFTY&50", "NIFTY_50", "NIFTY 50"):
            continue

        try:
            df = pd.read_csv(file, parse_dates=["date"])
            if df.empty or len(df) < 50:
                continue
            df["d_only"] = df["date"].dt.date
            df["time_only"] = df["date"].dt.time
            lot = int(df["lot_size"].iloc[0]) if "lot_size" in df.columns else 1

            # Index by day for rapid lookup
            day_groups = {}
            for d, g in df.groupby("d_only"):
                g_sorted = g.sort_values("date").reset_index(drop=True)
                if len(g_sorted) >= 15:  # Valid active day
                    day_groups[d] = g_sorted
                    all_days_set.add(d)

            stock_data[sym] = {
                "day_groups": day_groups,
                "lot": lot
            }
        except Exception as e:
            logger.warning("Error reading %s: %s", file.name, e)

    trading_days = sorted(list(all_days_set))
    logger.info("Preloaded %d stocks across %d trading days (%s to %s).", len(stock_data), len(trading_days), trading_days[0], trading_days[-1])

    return nifty_day_map, stock_data, trading_days


# ---------------------------------------------------------------------------
# Daily Dynamic Screener Simulation
# ---------------------------------------------------------------------------
def screen_day_stocks_at_1015(
    stock_data: Dict[str, Dict[str, Any]],
    current_day: date,
    trading_days: List[date],
    day_idx: int,
    top_n_gainers: int = 3,
    top_n_losers: int = 3,
    enable_coiling: bool = True
) -> List[Dict[str, Any]]:
    """
    Simulates real-time 10:15 AM stock selection on historical day D:
      1. Top N Gainers & Top N Losers at 10:15 AM:
         % Change = (10:15 Close - PrevDay Close) / PrevDay Close * 100
      2. 3-day contraction / coiling pattern:
         H(D-3) > H(D-2) > H(D-1) and L(D-3) < L(D-2) < L(D-1)
    """
    if day_idx < 3:
        return []

    prev_day = trading_days[day_idx - 1]
    prev_day_2 = trading_days[day_idx - 2]
    prev_day_3 = trading_days[day_idx - 3]

    stock_pct_changes = []
    coiling_candidates = []

    for sym, s_info in stock_data.items():
        days_map = s_info["day_groups"]
        if current_day not in days_map or prev_day not in days_map:
            continue

        today_bars = days_map[current_day]
        prev_bars = days_map[prev_day]

        if len(today_bars) < 4:  # Needs at least 4 bars for 09:15-10:15
            continue

        prev_close = float(prev_bars.iloc[-1]["close"])
        bar_1015_close = float(today_bars.iloc[3]["close"])  # Candle 4 close at 10:15

        if prev_close > 0:
            pct_change = ((bar_1015_close - prev_close) / prev_close) * 100.0
            stock_pct_changes.append({
                "symbol": sym,
                "pct_change": pct_change,
                "lot": s_info["lot"]
            })

        # Check 3-day coiling pattern if previous 3 days exist
        if enable_coiling and prev_day_2 in days_map and prev_day_3 in days_map:
            d3_bars = days_map[prev_day_3]
            d2_bars = days_map[prev_day_2]
            d1_bars = prev_bars

            c3 = {"high": float(d3_bars["high"].max()), "low": float(d3_bars["low"].min())}
            c2 = {"high": float(d2_bars["high"].max()), "low": float(d2_bars["low"].min())}
            c1 = {"high": float(d1_bars["high"].max()), "low": float(d1_bars["low"].min())}

            if (c3["high"] > c2["high"] > c1["high"]) and (c3["low"] < c2["low"] < c1["low"]):
                coiling_candidates.append({
                    "symbol": sym,
                    "lot": s_info["lot"],
                    "reasons": ["COILING"]
                })

    if not stock_pct_changes:
        return []

    # Sort for Top Gainers & Losers
    sorted_by_pct = sorted(stock_pct_changes, key=lambda x: x["pct_change"], reverse=True)
    top_gainers = sorted_by_pct[:top_n_gainers]
    top_losers = sorted_by_pct[-top_n_losers:]

    # Deduplicate into combined watchlist map
    watchlist_map: Dict[str, Dict[str, Any]] = {}

    for g in top_gainers:
        sym = g["symbol"]
        watchlist_map[sym] = {
            "symbol": sym,
            "lot": g["lot"],
            "pct_change": g["pct_change"],
            "reasons": ["TOP_GAINER"]
        }

    for l in top_losers:
        sym = l["symbol"]
        if sym in watchlist_map:
            watchlist_map[sym]["reasons"].append("TOP_LOSER")
        else:
            watchlist_map[sym] = {
                "symbol": sym,
                "lot": l["lot"],
                "pct_change": l["pct_change"],
                "reasons": ["TOP_LOSER"]
            }

    for c in coiling_candidates:
        sym = c["symbol"]
        if sym in watchlist_map:
            if "COILING" not in watchlist_map[sym]["reasons"]:
                watchlist_map[sym]["reasons"].append("COILING")
        else:
            watchlist_map[sym] = {
                "symbol": sym,
                "lot": c["lot"],
                "pct_change": None,
                "reasons": ["COILING"]
            }

    return list(watchlist_map.values())


# ---------------------------------------------------------------------------
# Core Portfolio Backtest Engine
# ---------------------------------------------------------------------------
def run_orb_simulation(
    nifty_day_map: Dict[date, Dict[str, Any]],
    stock_data: Dict[str, Dict[str, Any]],
    trading_days: List[date],
    use_dynamic_selection: bool = True,
    static_stock_symbols: Optional[List[str]] = None,
    fixed_risk_rs: float = 2500.0,
    sizing_mode: str = "FIXED_RISK",
    lots: int = 2,
    max_trades_per_day: int = 5,
    daily_stop_loss_limit: float = 10000.0,
    wick_max_pct: float = 0.20,
    max_sl_pct: float = 0.025,
    use_pdh_pdl_filter: bool = True,
    use_nifty_filter: bool = True,
    target_1_r: float = 1.0,
    eod_squareoff_time: dt_time = dt_time(15, 0)
) -> Dict[str, Any]:
    """
    Executes the multi-stock intraday ORB simulation (Option 3 Canonical Desk):
      - Trigger: Armed Candle Confirmation Break (15m close outside IB arms candle; next candle breaks high/low)
      - Target 1: Book 50% profit at 1:1 R
      - Stop-Loss: Move to Cost / Breakeven at 1:1 R
      - Final Exit: Square off remaining 50% runner at 3:00 PM IST (15:00)
      - Sizing: Fixed-Risk Risk Parity (Rs 2,500) or Futures Lots (2 lots)
    """
    all_trades: List[Dict[str, Any]] = []
    daily_summaries: List[Dict[str, Any]] = []
    circuit_breaker_trips = 0
    armed_total = 0
    invalidated_total = 0

    # Start from index 3 to have T-3, T-2, T-1 history
    sim_days = trading_days[3:]

    for day_idx, d in enumerate(sim_days, 3):
        prev_day = trading_days[day_idx - 1]
        n_info = nifty_day_map.get(d)
        if not n_info:
            continue

        n_day_open = n_info["day_open"]
        n_bars = n_info["bars"]

        # 1. Determine Watchlist for Today
        if use_dynamic_selection:
            candidates_meta = screen_day_stocks_at_1015(
                stock_data=stock_data,
                current_day=d,
                trading_days=trading_days,
                day_idx=day_idx,
                top_n_gainers=3,
                top_n_losers=3,
                enable_coiling=True
            )
        else:
            # Static universe baseline from TARGET_STOCKS config
            sym_list = static_stock_symbols or [s["symbol"] for s in TARGET_STOCKS]
            candidates_meta = [{"symbol": s, "lot": stock_data[s]["lot"], "reasons": ["STATIC"]} for s in sym_list if s in stock_data]

        if not candidates_meta:
            continue

        # Prepare day bars, IB, PDH, PDL
        day_pool = {}
        for c_item in candidates_meta:
            sym = c_item["symbol"]
            s_info = stock_data.get(sym)
            if not s_info:
                continue
            days_map = s_info["day_groups"]
            if d not in days_map or prev_day not in days_map:
                continue

            today_bars = days_map[d]
            prev_bars = days_map[prev_day]
            if len(today_bars) < 20:  # Need full active session
                continue

            ib_bars = today_bars.iloc[:4]
            ib_high = float(ib_bars["high"].max())
            ib_low = float(ib_bars["low"].min())
            pdh = float(prev_bars["high"].max())
            pdl = float(prev_bars["low"].min())

            day_pool[sym] = {
                "bars": today_bars,
                "ib_high": ib_high,
                "ib_low": ib_low,
                "pdh": pdh,
                "pdl": pdl,
                "lot": c_item["lot"],
                "reasons": c_item.get("reasons", [])
            }

        # 2. Simulate Bar-by-Bar Trading Desk (10:15 to 15:15)
        day_closed_trades = []
        active_positions = []
        armed_orders = []
        traded_today = set()
        circuit_tripped = False

        # Build rapid lookup map by timestamp for each stock
        for sym, s_data in day_pool.items():
            s_data["time_map"] = {row["time_only"]: row for _, row in s_data["bars"].iterrows()}

        # Canonical day timeline from 10:15 onwards based on NIFTY bars
        n_timeline = sorted([t for t in n_bars.keys() if t >= dt_time(10, 15)])
        if not n_timeline:
            continue

        for t_idx, curr_time in enumerate(n_timeline):
            if circuit_tripped:
                break

            # Step A: Update Active Positions on Current Candle Bar
            for pos in list(active_positions):
                sym = pos["symbol"]
                stock_bar = day_pool[sym]["time_map"].get(curr_time)
                if stock_bar is None:
                    continue
                bar_h = float(stock_bar["high"])
                bar_l = float(stock_bar["low"])
                bar_c = float(stock_bar["close"])

                direction = pos["direction"]
                qty = pos["quantity"]
                rem_qty = pos["rem_qty"]
                entry_p = pos["entry_price"]
                curr_sl = pos["current_sl"]

                closed_now = False

                if direction == "LONG":
                    # 1. Stop Loss check (Cost or Initial SL)
                    if bar_l <= curr_sl:
                        loss_pnl = (curr_sl - entry_p) * rem_qty
                        total_pnl = pos["booked_pnl"] + loss_pnl
                        day_closed_trades.append({
                            "symbol": sym,
                            "date": str(d),
                            "direction": direction,
                            "entry_time": str(pos["entry_time"]),
                            "exit_time": str(curr_time),
                            "entry_price": entry_p,
                            "exit_price": curr_sl,
                            "quantity": qty,
                            "pnl": total_pnl,
                            "exit_reason": "TRAIL_TO_COST_EXIT" if pos["t1_booked"] else "STOP_LOSS",
                            "reasons": pos["reasons"]
                        })
                        active_positions.remove(pos)
                        closed_now = True

                    # 2. Target 1 (1:1 R): Book 50% & Trail SL to Cost
                    if not closed_now:
                        if bar_h >= pos["target_1r"] and not pos["t1_booked"]:
                            pos["t1_booked"] = True
                            pos["current_sl"] = entry_p  # Trail SL to Cost
                            t1_gain = (pos["target_1r"] - entry_p) * pos["t1_qty"]
                            pos["booked_pnl"] += t1_gain
                            pos["rem_qty"] -= pos["t1_qty"]

                elif direction == "SHORT":
                    # 1. Stop Loss check (Cost or Initial SL)
                    if bar_h >= curr_sl:
                        loss_pnl = (entry_p - curr_sl) * rem_qty
                        total_pnl = pos["booked_pnl"] + loss_pnl
                        day_closed_trades.append({
                            "symbol": sym,
                            "date": str(d),
                            "direction": direction,
                            "entry_time": str(pos["entry_time"]),
                            "exit_time": str(curr_time),
                            "entry_price": entry_p,
                            "exit_price": curr_sl,
                            "quantity": qty,
                            "pnl": total_pnl,
                            "exit_reason": "TRAIL_TO_COST_EXIT" if pos["t1_booked"] else "STOP_LOSS",
                            "reasons": pos["reasons"]
                        })
                        active_positions.remove(pos)
                        closed_now = True

                    # 2. Target 1 (1:1 R): Book 50% & Trail SL to Cost
                    if not closed_now:
                        if bar_l <= pos["target_1r"] and not pos["t1_booked"]:
                            pos["t1_booked"] = True
                            pos["current_sl"] = entry_p  # Trail SL to Cost
                            t1_gain = (entry_p - pos["target_1r"]) * pos["t1_qty"]
                            pos["booked_pnl"] += t1_gain
                            pos["rem_qty"] -= pos["t1_qty"]

                # 3. 3:00 PM IST Square-off (Option 3 Canonical Runner Exit)
                if not closed_now and (curr_time >= eod_squareoff_time or t_idx == len(n_timeline) - 1):
                    eod_pnl = ((bar_c - entry_p) if direction == "LONG" else (entry_p - bar_c)) * pos["rem_qty"]
                    total_pnl = pos["booked_pnl"] + eod_pnl
                    day_closed_trades.append({
                        "symbol": sym,
                        "date": str(d),
                        "direction": direction,
                        "entry_time": str(pos["entry_time"]),
                        "exit_time": str(curr_time),
                        "entry_price": entry_p,
                        "exit_price": bar_c,
                        "quantity": qty,
                        "pnl": total_pnl,
                        "exit_reason": "3PM_EXIT_AFTER_T1" if pos["t1_booked"] else "3PM_EXIT_NO_T1",
                        "reasons": pos["reasons"]
                    })
                    active_positions.remove(pos)

            # Step B: Check Daily Portfolio Stop Loss Circuit Breaker
            realized_loss = sum(t["pnl"] for t in day_closed_trades)
            unrealized_loss = 0.0
            for pos in active_positions:
                sym = pos["symbol"]
                bar_row = day_pool[sym]["time_map"].get(curr_time)
                bar_c = float(bar_row["close"]) if bar_row is not None else pos["entry_price"]
                rem_qty = pos["rem_qty"]
                if pos["direction"] == "LONG":
                    mtm = (bar_c - pos["entry_price"]) * rem_qty
                else:
                    mtm = (pos["entry_price"] - bar_c) * rem_qty
                unrealized_loss += pos["booked_pnl"] + mtm

            if (realized_loss + unrealized_loss) <= -daily_stop_loss_limit:
                # Trip circuit breaker: Emergency square off all positions
                circuit_tripped = True
                circuit_breaker_trips += 1
                for pos in list(active_positions):
                    sym = pos["symbol"]
                    bar_row = day_pool[sym]["time_map"].get(curr_time)
                    bar_c = float(bar_row["close"]) if bar_row is not None else pos["entry_price"]
                    rem_qty = pos["rem_qty"]
                    if pos["direction"] == "LONG":
                        pnl = (bar_c - pos["entry_price"]) * rem_qty
                    else:
                        pnl = (pos["entry_price"] - bar_c) * rem_qty
                    day_closed_trades.append({
                        "symbol": sym,
                        "date": str(d),
                        "direction": pos["direction"],
                        "entry_time": str(pos["entry_time"]),
                        "exit_time": str(curr_time),
                        "entry_price": pos["entry_price"],
                        "exit_price": bar_c,
                        "quantity": pos["quantity"],
                        "pnl": pos["booked_pnl"] + pnl,
                        "exit_reason": "CIRCUIT_BREAKER_HALT",
                        "reasons": pos["reasons"]
                    })
                active_positions = []
                break

            # Step C: Check Pending Armed Setups for Confirmation Break or Invalidation
            for pending in list(armed_orders):
                sym = pending["symbol"]
                stock_bar = day_pool[sym]["time_map"].get(curr_time)
                if stock_bar is None:
                    continue
                bar_h = float(stock_bar["high"])
                bar_l = float(stock_bar["low"])

                if pending["direction"] == "LONG":
                    if bar_h >= pending["trigger_price"]:
                        # TRIGGERED!
                        armed_orders.remove(pending)
                        pending["entry_time"] = curr_time
                        active_positions.append(pending)
                        traded_today.add(sym)
                    elif bar_l <= pending["initial_sl"]:
                        # Invalidated before triggering (broke low of armed candle)
                        invalidated_total += 1
                        armed_orders.remove(pending)
                elif pending["direction"] == "SHORT":
                    if bar_l <= pending["trigger_price"]:
                        # TRIGGERED!
                        armed_orders.remove(pending)
                        pending["entry_time"] = curr_time
                        active_positions.append(pending)
                        traded_today.add(sym)
                    elif bar_h >= pending["initial_sl"]:
                        # Invalidated before triggering (broke high of armed candle)
                        invalidated_total += 1
                        armed_orders.remove(pending)

            # Step D: Scan for NEW Armed Setups (only during 10:15 - 13:15)
            available_slots = max_trades_per_day - (len(day_closed_trades) + len(active_positions) + len(armed_orders))
            if available_slots <= 0 or curr_time > dt_time(13, 15):
                continue

            # Prior candle timestamp (the candle that just completed)
            if t_idx == 0:
                eval_time = dt_time(10, 0)
            else:
                eval_time = n_timeline[t_idx - 1]

            n_close_eval = n_bars.get(eval_time)
            nifty_bias = None
            if n_close_eval is not None:
                nifty_bias = "BULLISH" if n_close_eval >= n_day_open else "BEARISH"

            new_candidates = []

            for sym, s_data in day_pool.items():
                if sym in traded_today or any(p["symbol"] == sym for p in active_positions) or any(p["symbol"] == sym for p in armed_orders):
                    continue

                c_bar = s_data["time_map"].get(eval_time)
                if c_bar is None:
                    continue
                o = float(c_bar["open"])
                h = float(c_bar["high"])
                l = float(c_bar["low"])
                c = float(c_bar["close"])
                c_range = h - l

                if c_range <= 0 or (c_range / c) > max_sl_pct:
                    continue

                direction = None
                wick_ratio = 0.0

                if c > s_data["ib_high"]:
                    upper_wick = h - c
                    wick_ratio = upper_wick / c_range
                    if wick_ratio <= wick_max_pct:
                        direction = "LONG"
                elif c < s_data["ib_low"]:
                    lower_wick = c - l
                    wick_ratio = lower_wick / c_range
                    if wick_ratio <= wick_max_pct:
                        direction = "SHORT"

                if not direction:
                    continue

                # PDH / PDL filter
                if use_pdh_pdl_filter:
                    if direction == "LONG" and c < s_data["pdh"]:
                        continue
                    elif direction == "SHORT" and c > s_data["pdl"]:
                        continue

                # NIFTY trend bias filter
                if use_nifty_filter and nifty_bias is not None:
                    if direction == "LONG" and nifty_bias != "BULLISH":
                        continue
                    elif direction == "SHORT" and nifty_bias != "BEARISH":
                        continue

                # Armed Trade Setup
                entry_trigger = h + 0.05 if direction == "LONG" else l - 0.05
                initial_sl = l - 0.05 if direction == "LONG" else h + 0.05
                risk_per_share = abs(entry_trigger - initial_sl)
                if risk_per_share <= 0:
                    continue

                # Sizing
                if sizing_mode == "LOTS":
                    lot_size = s_data["lot"]
                    qty = lots * lot_size
                    t1_qty = (lots // 2) * lot_size
                else:
                    qty = max(2, int(fixed_risk_rs / risk_per_share))
                    t1_qty = qty // 2

                target_1r = entry_trigger + (target_1_r * risk_per_share) if direction == "LONG" else entry_trigger - (target_1_r * risk_per_share)

                new_candidates.append({
                    "symbol": sym,
                    "direction": direction,
                    "entry_price": entry_trigger,
                    "trigger_price": entry_trigger,
                    "initial_sl": initial_sl,
                    "current_sl": initial_sl,
                    "risk_per_share": risk_per_share,
                    "quantity": qty,
                    "rem_qty": qty,
                    "t1_qty": t1_qty,
                    "target_1r": target_1r,
                    "t1_booked": False,
                    "booked_pnl": 0.0,
                    "wick_ratio": wick_ratio,
                    "entry_time": curr_time,
                    "reasons": s_data["reasons"]
                })

            if new_candidates:
                # Rank by cleanest rejection wick
                new_candidates.sort(key=lambda x: x["wick_ratio"])
                to_arm = new_candidates[:available_slots]
                for pos in to_arm:
                    armed_orders.append(pos)
                    armed_total += 1

        day_pnl = sum(t["pnl"] for t in day_closed_trades)
        daily_summaries.append({
            "date": str(d),
            "trades_count": len(day_closed_trades),
            "pnl": day_pnl,
            "circuit_tripped": circuit_tripped
        })
        all_trades.extend(day_closed_trades)

    # Compute Aggregate Metrics
    df_trades = pd.DataFrame(all_trades)
    df_daily = pd.DataFrame(daily_summaries)

    if df_trades.empty:
        return {
            "total_trades": 0,
            "total_pnl": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_rs": 0.0,
            "armed_total": armed_total,
            "invalidated_total": invalidated_total,
            "trades": [],
            "daily_summaries": []
        }

    total_trades = len(df_trades)
    wins = df_trades[df_trades["pnl"] > 0]
    losses = df_trades[df_trades["pnl"] < 0]
    be_trades = df_trades[df_trades["pnl"] == 0]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / total_trades) * 100.0

    gross_profit = float(wins["pnl"].sum())
    gross_loss = abs(float(losses["pnl"].sum()))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan
    total_pnl = float(df_trades["pnl"].sum())

    # Drawdown Calculation
    df_daily["cum_pnl"] = df_daily["pnl"].cumsum()
    df_daily["peak"] = df_daily["cum_pnl"].cummax()
    df_daily["drawdown"] = df_daily["cum_pnl"] - df_daily["peak"]
    max_dd = abs(float(df_daily["drawdown"].min()))

    # Group metrics by setup reason
    reasons_breakdown = {}
    for r_type in ["TOP_GAINER", "TOP_LOSER", "COILING", "STATIC"]:
        subset = df_trades[df_trades["reasons"].apply(lambda r: r_type in r)]
        if not subset.empty:
            sub_wins = len(subset[subset["pnl"] > 0])
            sub_pnl = float(subset["pnl"].sum())
            reasons_breakdown[r_type] = {
                "trades": len(subset),
                "win_rate": (sub_wins / len(subset)) * 100.0,
                "pnl": sub_pnl,
                "avg_pnl": sub_pnl / len(subset)
            }

    # Exit reason breakdown
    exit_breakdown = {}
    for reason, grp in df_trades.groupby("exit_reason"):
        exit_breakdown[reason] = {
            "count": int(len(grp)),
            "sum_pnl": float(grp["pnl"].sum()),
            "avg_pnl": float(grp["pnl"].mean())
        }

    return {
        "total_trades": total_trades,
        "win_count": win_count,
        "loss_count": loss_count,
        "be_count": len(be_trades),
        "win_rate_pct": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
        "avg_trade_pnl": total_pnl / total_trades,
        "max_drawdown_rs": max_dd,
        "return_to_dd": (total_pnl / max_dd) if max_dd > 0 else np.nan,
        "circuit_breaker_trips": circuit_breaker_trips,
        "armed_total": armed_total,
        "invalidated_total": invalidated_total,
        "reasons_breakdown": reasons_breakdown,
        "exit_breakdown": exit_breakdown,
        "df_daily": df_daily,
        "df_trades": df_trades
    }


# ---------------------------------------------------------------------------
# Runner & Comparison Presentation
# ---------------------------------------------------------------------------
def run_backtest_suite(days: int = 90, sizing_mode: str = "FIXED_RISK", fixed_risk_rs: float = 2500.0, lots: int = 2):
    config_kite = get_kite_client()
    download_all_historical_data(config_kite, days=days)

    nifty_day_map, stock_data, trading_days = preload_indexed_data()

    logger.info("=" * 80)
    logger.info("  RUNNING BACKTEST: DYNAMIC F&O SELECTION (Option 3 Canonical Desk)")
    logger.info("=" * 80)

    res_dynamic = run_orb_simulation(
        nifty_day_map=nifty_day_map,
        stock_data=stock_data,
        trading_days=trading_days,
        use_dynamic_selection=True,
        sizing_mode=sizing_mode,
        fixed_risk_rs=fixed_risk_rs,
        lots=lots
    )

    logger.info("=" * 80)
    logger.info("  RUNNING BACKTEST: BASELINE (Static 30 Stocks Universe)")
    logger.info("=" * 80)

    res_static = run_orb_simulation(
        nifty_day_map=nifty_day_map,
        stock_data=stock_data,
        trading_days=trading_days,
        use_dynamic_selection=False,
        sizing_mode=sizing_mode,
        fixed_risk_rs=fixed_risk_rs,
        lots=lots
    )

    return res_dynamic, res_static, trading_days


def print_comparison_report(res_dyn: Dict[str, Any], res_stat: Dict[str, Any], days: List[date], sizing_mode: str = "FIXED_RISK"):
    print("\n" + "=" * 92)
    print("      15-MINUTE ORB BACKTEST PERFORMANCE REPORT (OPTION 3 CANONICAL DESK)")
    print("=" * 92)
    print(f"Period: {days[0]} to {days[-1]} ({len(days)} trading days across 219 F&O Stocks)")
    print(f"Strategy: Armed Candle Confirmation | 50% Booked at 1:1 R | SL Trailed to Cost | 3:00 PM IST Exit")
    print(f"Sizing Mode: {sizing_mode} | Max 5 Trades/Day | Rs 10,000 Portfolio Circuit Breaker")
    print("-" * 92)
    print(f"{'Performance Metric':<35} | {'Dynamic F&O Selection':<24} | {'Static 30 Stocks':<24}")
    print("-" * 92)

    pnl_dyn = res_dyn.get("total_pnl", 0.0)
    pnl_stat = res_stat.get("total_pnl", 0.0)
    trades_dyn = res_dyn.get("total_trades", 0)
    trades_stat = res_stat.get("total_trades", 0)
    wr_dyn = res_dyn.get("win_rate_pct", 0.0)
    wr_stat = res_stat.get("win_rate_pct", 0.0)
    pf_dyn = res_dyn.get("profit_factor", 0.0)
    pf_stat = res_stat.get("profit_factor", 0.0)
    dd_dyn = res_dyn.get("max_drawdown_rs", 0.0)
    dd_stat = res_stat.get("max_drawdown_rs", 0.0)
    ret_dd_dyn = res_dyn.get("return_to_dd", 0.0)
    ret_dd_stat = res_stat.get("return_to_dd", 0.0)
    cb_dyn = res_dyn.get("circuit_breaker_trips", 0)
    cb_stat = res_stat.get("circuit_breaker_trips", 0)
    armed_dyn = res_dyn.get("armed_total", 0)
    armed_stat = res_stat.get("armed_total", 0)
    inval_dyn = res_dyn.get("invalidated_total", 0)
    inval_stat = res_stat.get("invalidated_total", 0)

    print(f"{'Total Realized P&L (Rs)':<35} | Rs {pnl_dyn:>17,.2f} | Rs {pnl_stat:>17,.2f}")
    print(f"{'Total Trades Executed':<35} | {trades_dyn:>22} | {trades_stat:>22}")
    print(f"{'Win Rate (%)':<35} | {wr_dyn:>21.1f}% | {wr_stat:>21.1f}%")
    print(f"{'Profit Factor':<35} | {pf_dyn:>22.2f} | {pf_stat:>22.2f}")
    print(f"{'Average P&L / Trade (Rs)':<35} | Rs {(pnl_dyn/trades_dyn if trades_dyn else 0):>17,.2f} | Rs {(pnl_stat/trades_stat if trades_stat else 0):>17,.2f}")
    print(f"{'Max Portfolio Drawdown (Rs)':<35} | Rs {dd_dyn:>17,.2f} | Rs {dd_stat:>17,.2f}")
    print(f"{'Return / Max Drawdown Ratio':<35} | {ret_dd_dyn:>21.2f}x | {ret_dd_stat:>21.2f}x")
    print(f"{'Total Armed Setups':<35} | {armed_dyn:>22} | {armed_stat:>22}")
    print(f"{'Invalidated (Avoided Traps)':<35} | {inval_dyn:>22} | {inval_stat:>22}")
    print(f"{'Circuit Breaker Halts (Days)':<35} | {cb_dyn:>22} | {cb_stat:>22}")
    print("-" * 92)

    # Exit reason breakdown
    print("\n" + "-" * 92)
    print("   EXIT REASON BREAKDOWN:")
    print("-" * 92)
    print(f"{'Exit Reason':<25} | {'Dynamic Trades':<15} | {'Dynamic P&L (Rs)':<18} | {'Static Trades':<15} | {'Static P&L (Rs)':<18}")
    print("-" * 92)
    all_reasons = set(res_dyn.get("exit_breakdown", {}).keys()) | set(res_stat.get("exit_breakdown", {}).keys())
    for r in sorted(list(all_reasons)):
        d_info = res_dyn.get("exit_breakdown", {}).get(r, {"count": 0, "sum_pnl": 0.0})
        s_info = res_stat.get("exit_breakdown", {}).get(r, {"count": 0, "sum_pnl": 0.0})
        print(f"{r:<25} | {d_info['count']:>15} | Rs {d_info['sum_pnl']:>14,.2f} | {s_info['count']:>15} | Rs {s_info['sum_pnl']:>14,.2f}")
    print("-" * 92)

    # Category breakdown for dynamic
    reasons = res_dyn.get("reasons_breakdown", {})
    if reasons:
        print("\n" + "-" * 92)
        print("   DYNAMIC SELECTION CATEGORY BREAKDOWN:")
        print("-" * 92)
        print(f"{'Category':<25} | {'Trades':<10} | {'Win Rate':<12} | {'Total P&L (Rs)':<18} | {'Avg P&L (Rs)':<15}")
        print("-" * 92)
        for cat, stat in reasons.items():
            print(f"{cat:<25} | {stat['trades']:<10} | {stat['win_rate']:>9.1f}% | Rs {stat['pnl']:>12,.2f} | Rs {stat['avg_pnl']:>9,.2f}")
        print("-" * 92)

    # Save summary report
    report_data = {
        "strategy": "OPTION_3_CANONICAL_ORB",
        "sizing_mode": sizing_mode,
        "dynamic": {
            "total_pnl": pnl_dyn,
            "total_trades": trades_dyn,
            "win_rate": wr_dyn,
            "profit_factor": pf_dyn,
            "max_drawdown": dd_dyn,
            "return_to_dd": ret_dd_dyn,
            "armed_total": armed_dyn,
            "invalidated_total": inval_dyn,
            "circuit_breaker_trips": cb_dyn,
            "exit_breakdown": res_dyn.get("exit_breakdown", {}),
            "reasons_breakdown": reasons
        },
        "static": {
            "total_pnl": pnl_stat,
            "total_trades": trades_stat,
            "win_rate": wr_stat,
            "profit_factor": pf_stat,
            "max_drawdown": dd_stat,
            "return_to_dd": ret_dd_stat,
            "armed_total": armed_stat,
            "invalidated_total": inval_stat,
            "circuit_breaker_trips": cb_stat,
            "exit_breakdown": res_stat.get("exit_breakdown", {})
        }
    }
    report_file = DATA_DIR / "dynamic_orb_backtest_report.json"
    with open(report_file, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"\nSaved full structured report to: {report_file}")
    print("=" * 92 + "\n")


if __name__ == "__main__":
    days_to_test = 90
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        days_to_test = int(sys.argv[1])
    res_dyn, res_stat, days = run_backtest_suite(days=days_to_test)
    print_comparison_report(res_dyn, res_stat, days)


if __name__ == "__main__":
    days_to_test = 90
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        days_to_test = int(sys.argv[1])
    res_dyn, res_stat, days = run_backtest_suite(days=days_to_test)
    print_comparison_report(res_dyn, res_stat, days)
