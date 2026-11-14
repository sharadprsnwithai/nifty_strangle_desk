import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta, date, time as dt_time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

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
from strategy.orb_desk.config import TARGET_STOCKS
from scripts.backtest_orb import run_orb_simulation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("orb.last_2weeks")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache_2weeks_orb"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NIFTY_TOKEN = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"


def fetch_and_cache_fresh_data(kite, from_date: datetime, to_date: datetime):
    """Fetches fresh 15m historical candles from Kite API for NIFTY and 30 stocks."""
    logger.info("Fetching fresh 15m candles from Kite API (%s to %s)...", from_date.date(), to_date.date())
    
    # 1. NIFTY 50
    nifty_cache = CACHE_DIR / "NIFTY_50_15m.csv"
    try:
        res = kite.historical_data(NIFTY_TOKEN, from_date=from_date, to_date=to_date, interval="15minute")
        if res:
            df = pd.DataFrame(res)
            df.to_csv(nifty_cache, index=False)
            logger.info("Saved fresh NIFTY 50 (%d bars) to %s", len(df), nifty_cache.name)
    except Exception as e:
        logger.error("Failed to fetch fresh NIFTY 50: %s", e)
    time.sleep(0.35)

    # 2. Target Stocks
    total = len(TARGET_STOCKS)
    for idx, item in enumerate(TARGET_STOCKS, 1):
        sym = item["symbol"]
        token = item.get("token")
        clean_sym = sym.replace("&", "_")
        cache_file = CACHE_DIR / f"{clean_sym}_15m.csv"
        try:
            res = kite.historical_data(token, from_date=from_date, to_date=to_date, interval="15minute")
            if res:
                df = pd.DataFrame(res)
                df["lot_size"] = item.get("lot", 1)
                df.to_csv(cache_file, index=False)
                logger.info("[%d/%d] Fresh data saved for %s (%d bars)", idx, total, sym, len(df))
            else:
                logger.warning("[%d/%d] No bars returned for %s", idx, total, sym)
        except Exception as e:
            logger.error("[%d/%d] Failed to fetch %s: %s", idx, total, sym, e)
        time.sleep(0.35)


def preload_data() -> Tuple[Dict[date, Dict[str, Any]], Dict[str, Dict[str, Any]], List[date]]:
    nifty_cache = CACHE_DIR / "NIFTY_50_15m.csv"
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

    stock_data: Dict[str, Dict[str, Any]] = {}
    all_days_set: Set[date] = set()

    for file in CACHE_DIR.glob("*_15m.csv"):
        sym = file.stem.replace("_15m", "").replace("_", "&")
        if "NIFTY" in sym:
            continue
        try:
            df = pd.read_csv(file, parse_dates=["date"])
            if df.empty or len(df) < 15:
                continue
            df["d_only"] = df["date"].dt.date
            df["time_only"] = df["date"].dt.time
            lot = int(df["lot_size"].iloc[0]) if "lot_size" in df.columns else 1

            day_groups = {}
            for d, g in df.groupby("d_only"):
                g_sorted = g.sort_values("date").reset_index(drop=True)
                if len(g_sorted) >= 15:
                    day_groups[d] = g_sorted
                    all_days_set.add(d)

            stock_data[sym] = {
                "day_groups": day_groups,
                "lot": lot
            }
        except Exception as e:
            logger.warning("Error preloading %s: %s", file.name, e)

    trading_days = sorted(list(all_days_set))
    return nifty_day_map, stock_data, trading_days


def main():
    kite = get_kite_client()
    to_d = datetime.now()
    from_d = to_d - timedelta(days=22)  # Go back ~3 weeks to have ample T-1 history for Oct 30 & Nov 2

    fetch_and_cache_fresh_data(kite, from_d, to_d)
    nifty_day_map, stock_data, trading_days = preload_data()

    logger.info("Available trading days in dataset: %s", [str(d) for d in trading_days])

    # Filter strictly to the last 2 weeks (e.g. from 2026-10-30 onwards so simulation runs for 2026-11-02 to 2026-11-13)
    # The backtester starts from index 1 (using index 0 as prev_day for PDH/PDL)
    two_weeks_cutoff = date(2026, 10, 30)
    filtered_days = [d for d in trading_days if d >= two_weeks_cutoff]

    logger.info("Backtesting across last 2 trading weeks: %s", [str(d) for d in filtered_days[1:]])

    res = run_orb_simulation(
        nifty_day_map=nifty_day_map,
        stock_data=stock_data,
        trading_days=filtered_days,
        sizing_mode="FIXED_RISK",
        fixed_risk_rs=2500.0,
        max_trades_per_day=5,
        daily_stop_loss_limit=10000.0,
    )

    df_trades = res.get("df_trades")
    df_daily = res.get("df_daily")

    print("\n" + "=" * 90)
    print("      STOCK ORB STRATEGY BACKTEST (ACTUAL KITE API DATA - LAST 2 WEEKS)")
    print("=" * 90)
    print(f"Period: {filtered_days[1]} to {filtered_days[-1]} ({len(filtered_days)-1} Trading Days)")
    print(f"Universe: Static 30 High-Momentum F&O Stocks | Risk Parity: Rs 2,500/trade | Max 5 Trades/Day")
    print(f"Rules: 20% Wick Filter | PDH/PDL | NIFTY Day Open | 1:1 R (+50% booked, SL to BE) | 15:00 Exit")
    print("-" * 90)

    # Detailed trade log
    if df_trades is not None and not df_trades.empty:
        print("\n--- INDIVIDUAL TRADE BREAKDOWN ---")
        for idx, (_, t) in enumerate(df_trades.iterrows(), 1):
            pnl_val = float(t.get("pnl", 0.0))
            pnl_str = f"₹{pnl_val:+,.2f}"
            print(f"[{idx:02d}] {t['date']} | {t['symbol']:<11} ({t['direction']:<5}) | Entry: {t['entry_time']} @ ₹{float(t['entry_price']):<7.2f} -> Exit: {t['exit_time']} @ ₹{float(t['exit_price']):<7.2f} | Qty: {int(t['quantity']):>4} | P&L: {pnl_str:>12} | Exit: {t['exit_reason']}")

    if df_daily is not None and not df_daily.empty:
        print("\n--- DAILY PERFORMANCE BREAKDOWN ---")
        print(f"{'Date':<12} | {'Trades':<8} | {'Daily P&L (₹)':<16} | {'Cumulative P&L (₹)':<20} | {'Drawdown (₹)':<15}")
        print("-" * 80)
        for _, row in df_daily.iterrows():
            d_pnl = float(row['pnl'])
            cum = float(row.get('cum_pnl', 0.0))
            dd_val = float(row.get('drawdown', 0.0))
            print(f"{row['date']:<12} | {int(row['trades_count']):<8} | ₹{d_pnl:>+13,.2f} | ₹{cum:>+17,.2f} | ₹{abs(dd_val):>12,.2f}")

    print("\n" + "-" * 90)
    print("--- PERFORMANCE SUMMARY ---")
    pnl = res.get("total_pnl", 0.0)
    trades_count = res.get("total_trades", 0)
    wr = res.get("win_rate_pct", 0.0)
    pf = res.get("profit_factor", 0.0)
    dd = res.get("max_drawdown_rs", 0.0)
    ret_dd = res.get("return_to_dd", 0.0)
    cb = res.get("circuit_breaker_trips", 0)
    armed = res.get("armed_total", 0)
    inval = res.get("invalidated_total", 0)

    print(f"Total Realized P&L          : ₹{pnl:+,.2f}")
    print(f"Total Trades Executed       : {trades_count}")
    print(f"Win Rate                    : {wr:.1f}%")
    print(f"Profit Factor               : {pf:.2f}")
    print(f"Avg P&L / Trade             : ₹{(pnl / trades_count if trades_count else 0):+,.2f}")
    print(f"Max Portfolio Drawdown      : ₹{dd:,.2f}")
    print(f"Return / Max Drawdown Ratio : {ret_dd:.2f}x")
    print(f"Total Setups Armed          : {armed}")
    print(f"Setups Invalidated (Traps)  : {inval}")
    print(f"Daily Circuit Breakers Hit  : {cb}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
