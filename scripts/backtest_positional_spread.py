"""Backtest for NIFTY 50 Positional Defined-Risk Credit Spread (2-5 Day Swing)."""
import argparse
import logging
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.stats import norm
from tabulate import tabulate

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from strategy.backtest import get_weekly_expiry_for_date
from strategy.costs import calculate_trade_costs
from strategy.options_model import ANNUAL_TRADING_DAYS, black_76_price, compute_fractional_dte

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _black_76_abs_delta(spot: float, strike: float, dte_days: float, iv: float, r: float, opt_type: str) -> float:
    t = max(dte_days / ANNUAL_TRADING_DAYS, 1e-5)
    sigma = max(iv, 0.01)
    d = math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / (sigma * sqrt_t)
    if opt_type.upper() == "CE":
        return float(d * norm.cdf(d1))
    return float(d * norm.cdf(-d1))


def select_strike_by_delta(
    spot: float, dte_days: float, iv: float, opt_type: str, target_delta: float = 0.30, r: float = 0.065
) -> float:
    """Pick nearest 50-step strike matching target absolute delta."""
    atm = round(spot / 50.0) * 50.0
    step = 50 if opt_type.upper() == "CE" else -50
    best_strike = atm
    best_diff = 999.0
    for i in range(0, 40):
        s = atm + i * step
        if s <= 0:
            break
        delta = _black_76_abs_delta(spot, s, dte_days, iv, r, opt_type)
        diff = abs(delta - target_delta)
        if diff < best_diff:
            best_diff = diff
            best_strike = s
    return float(best_strike)


def get_swing_expiry(d: date, expiry_weekday: int = 1, min_cal_days: int = 4) -> date:
    """Select weekly expiry with at least min_cal_days remaining to avoid 0/1-DTE gamma."""
    exp, _ = get_weekly_expiry_for_date(d, expiry_weekday=expiry_weekday)
    while (exp - d).days < min_cal_days:
        exp = exp + timedelta(days=7)
    return exp


def fetch_kite_data(days: int = 90, include_vix: bool = True) -> pd.DataFrame:
    from kite import get_kite_client

    kite = get_kite_client()
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)

    chunks = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(days=55), end_dt)
        recs = kite.historical_data(256265, cur, nxt, "5minute")
        if recs:
            chunks.extend(recs)
        cur = nxt
    df = pd.DataFrame(chunks)

    if include_vix and not df.empty:
        v_chunks = []
        cur = start_dt
        while cur < end_dt:
            nxt = min(cur + timedelta(days=55), end_dt)
            vrecs = kite.historical_data(264969, cur, nxt, "5minute")
            if vrecs:
                v_chunks.extend(vrecs)
            cur = nxt
        vdf = pd.DataFrame(v_chunks)
        if not vdf.empty:
            df["date"] = pd.to_datetime(df["date"])
            vdf["date"] = pd.to_datetime(vdf["date"])
            df = df.sort_values("date")
            vdf = vdf[["date", "close"]].rename(columns={"close": "vix"}).sort_values("date")
            df = pd.merge_asof(df, vdf, on="date", direction="backward")
            df["vix"] = df["vix"].bfill().fillna(13.0)
    return df


def run_positional_spread_backtest(
    bars_df: pd.DataFrame,
    short_delta: float = 0.30,
    long_delta: float = 0.12,
    target_profit_pct: float = 0.55,
    sl_credit_mult: float = 1.50,
    max_hold_days: int = 5,
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
) -> Dict[str, Any]:
    """Simulate 2-5 day swing defined-risk credit spreads on 1H bars."""
    df = bars_df.copy()
    ts_col = "date" if "date" in df.columns else ("timestamps" if "timestamps" in df.columns else df.columns[0])
    df["dt"] = pd.to_datetime(df[ts_col])
    if "vix" not in df.columns:
        df["vix"] = iv * 100.0
    df = df.set_index("dt").sort_index()

    # Resample to 1-Hour bars for swing signals
    h1 = df.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vix": "last",
    }).dropna(subset=["close"]).reset_index()

    h1["ema9"] = h1["close"].ewm(span=9, adjust=False).mean()
    h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()

    qty = lots * lot_size
    trades: List[Dict[str, Any]] = []

    in_trade = False
    direction = ""
    opt_type = ""
    short_strike = 0.0
    long_strike = 0.0
    target_expiry: Optional[date] = None
    entry_dt: Optional[datetime] = None
    entry_spot = 0.0
    entry_vix = 0.0
    short_entry_px = 0.0
    long_entry_px = 0.0
    net_credit = 0.0

    # Skip first 20 bars for EMA20 warmup
    for i in range(20, len(h1)):
        row = h1.iloc[i]
        bar_dt = row["dt"].to_pydatetime()
        close = float(row["close"])
        ema9 = float(row["ema9"])
        ema20 = float(row["ema20"])
        bar_iv = max(float(row["vix"]) / 100.0, 0.05)

        if not in_trade:
            if close > ema20 and ema9 > ema20:
                direction = "BULL"
                opt_type = "PE"
            elif close < ema20 and ema9 < ema20:
                direction = "BEAR"
                opt_type = "CE"
            else:
                continue

            target_expiry = get_swing_expiry(bar_dt.date(), expiry_weekday=expiry_weekday, min_cal_days=4)
            dte = compute_fractional_dte(target_expiry, bar_dt)
            short_strike = select_strike_by_delta(close, dte, bar_iv, opt_type, target_delta=short_delta, r=r)
            long_strike = select_strike_by_delta(close, dte, bar_iv, opt_type, target_delta=long_delta, r=r)

            # Ensure at least 150-pt spread width
            if opt_type == "PE" and long_strike > short_strike - 150:
                long_strike = short_strike - 150
            elif opt_type == "CE" and long_strike < short_strike + 150:
                long_strike = short_strike + 150

            short_entry_px = black_76_price(close, short_strike, dte, bar_iv, r, opt_type)
            long_entry_px = black_76_price(close, long_strike, dte, bar_iv, r, opt_type)
            net_credit = round(short_entry_px - long_entry_px, 2)
            if net_credit <= 5.0:
                continue

            entry_dt = bar_dt
            entry_spot = close
            entry_vix = bar_iv * 100.0
            in_trade = True
            continue

        # Manage open swing spread
        dte = compute_fractional_dte(target_expiry, bar_dt)
        cur_short_px = black_76_price(close, short_strike, dte, bar_iv, r, opt_type)
        cur_long_px = black_76_price(close, long_strike, dte, bar_iv, r, opt_type)
        cur_spread_val = cur_short_px - cur_long_px
        held_cal_days = (bar_dt.date() - entry_dt.date()).days

        exit_reason = ""
        if cur_spread_val <= net_credit * (1.0 - target_profit_pct):
            exit_reason = "TARGET_PROFIT"
        elif cur_spread_val >= net_credit * (1.0 + sl_credit_mult):
            exit_reason = "SPREAD_SL"
        elif (direction == "BULL" and close < ema20) or (direction == "BEAR" and close > ema20):
            exit_reason = "EMA20_REVERSAL"
        elif (target_expiry - bar_dt.date()).days <= 1 and bar_dt.hour >= 14:
            exit_reason = "PRE_EXPIRY_EXIT"
        elif held_cal_days >= max_hold_days and bar_dt.hour >= 14:
            exit_reason = "MAX_HOLD_EXIT"

        if exit_reason:
            short_costs = calculate_trade_costs(short_entry_px, cur_short_px, qty, slippage_per_share=slippage)
            long_costs = calculate_trade_costs(cur_long_px, long_entry_px, qty, slippage_per_share=slippage)
            gross_pnl = round(short_costs["gross_pnl"] + long_costs["gross_pnl"], 2)
            total_costs = round(short_costs["total_costs"] + long_costs["total_costs"], 2)
            net_pnl = round(gross_pnl - total_costs, 2)

            trades.append({
                "entry_date": entry_dt.strftime("%Y-%m-%d %H:%M"),
                "exit_date": bar_dt.strftime("%Y-%m-%d %H:%M"),
                "hold_days": held_cal_days,
                "direction": direction,
                "spread": f"{int(short_strike)}/{int(long_strike)}{opt_type}",
                "expiry": str(target_expiry),
                "entry_spot": round(entry_spot, 1),
                "vix": round(entry_vix, 2),
                "credit": net_credit,
                "exit_debit": round(cur_spread_val, 2),
                "exit_reason": exit_reason,
                "gross_pnl": gross_pnl,
                "total_costs": total_costs,
                "net_pnl": net_pnl,
            })
            in_trade = False

    net_pnls = [t["net_pnl"] for t in trades]
    wins = sum(1 for p in net_pnls if p > 0)
    losses = len(net_pnls) - wins
    gw = sum(p for p in net_pnls if p > 0)
    gl = abs(sum(p for p in net_pnls if p < 0))
    pf = round(gw / gl, 2) if gl > 0 else (99.99 if gw > 0 else 0.0)
    cum = np.cumsum(net_pnls) if net_pnls else np.array([0.0])
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.max(peak - cum)) if len(cum) else 0.0

    return {
        "summary": {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / len(trades)) * 100.0, 1) if trades else 0.0,
            "gross_pnl": round(sum(t["gross_pnl"] for t in trades), 2),
            "total_costs": round(sum(t["total_costs"] for t in trades), 2),
            "net_pnl": round(sum(net_pnls), 2),
            "profit_factor": pf,
            "max_drawdown": round(max_dd, 2),
            "avg_hold_days": round(float(np.mean([t["hold_days"] for t in trades])), 1) if trades else 0.0,
        },
        "trades": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Positional Defined-Risk Credit Spread")
    parser.add_argument("--cache", type=str, default="data/nifty_5m_cache.parquet")
    parser.add_argument("--fetch-kite", action="store_true")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--lots", type=int, default=10)
    parser.add_argument("--slippage", type=float, default=0.50)
    args = parser.parse_args()

    if args.fetch_kite:
        df = fetch_kite_data(days=args.days, include_vix=True)
        src = f"Kite Actual NIFTY + INDIA VIX ({args.days}d)"
    else:
        df = pd.read_parquet(ROOT_DIR / args.cache)
        src = f"{args.cache} (iv=0.13)"

    ts_col = "date" if "date" in df.columns else "timestamps"
    dt = pd.to_datetime(df[ts_col])
    print(f"Loaded {len(df)} bars | {dt.dt.date.nunique()} trading days ({dt.min().date()} to {dt.max().date()}) [{src}]")

    res = run_positional_spread_backtest(df, lots=args.lots, slippage=args.slippage)
    s = res["summary"]
    print("\nPositional Defined-Risk Credit Spread (0.30d / 0.12d | 1H EMA20):")
    print(tabulate(
        [[
            s["trades"],
            f"{s['wins']}W / {s['losses']}L",
            f"{s['win_rate_pct']:.1f}%",
            f"{s['avg_hold_days']}d",
            f"Rs. {s['gross_pnl']:+,.2f}",
            f"Rs. {s['total_costs']:,.2f}",
            f"Rs. {s['net_pnl']:+,.2f}",
            s["profit_factor"],
            f"Rs. {s['max_drawdown']:,.2f}",
        ]],
        headers=["Trades", "W/L", "Win%", "AvgHold", "Gross P&L", "Costs+Slip", "Net P&L", "PF", "Max DD"],
        tablefmt="github",
    ))

    if res["trades"]:
        print("\nTrade Log:")
        rows = [
            [
                t["entry_date"],
                t["exit_date"],
                f"{t['hold_days']}d",
                t["direction"],
                t["spread"],
                t["vix"],
                t["credit"],
                t["exit_debit"],
                t["exit_reason"],
                f"Rs. {t['net_pnl']:+,.0f}",
            ]
            for t in res["trades"]
        ]
        print(tabulate(
            rows,
            headers=["Entry", "Exit", "Hold", "Dir", "Spread", "VIX%", "Credit", "Debit", "Reason", "Net P&L"],
            tablefmt="github",
        ))


if __name__ == "__main__":
    main()
