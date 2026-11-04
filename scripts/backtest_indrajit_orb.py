import os
import sys
import json
import logging
from datetime import datetime, timedelta, date, time as dt_time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from kite import get_kite_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("orb.backtest")

DATA_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache_15m"
DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

TARGET_STOCKS = [
    {"symbol": "ABB", "token": 3329, "lot": 125},
    {"symbol": "ADANIENSOL", "token": 2615553, "lot": 675},
    {"symbol": "ADANIGREEN", "token": 912129, "lot": 600},
    {"symbol": "ADANIPOWER", "token": 4451329, "lot": 3550},
    {"symbol": "ABCAPITAL", "token": 5533185, "lot": 1550},
    {"symbol": "BSE", "token": 5013761, "lot": 200},
    {"symbol": "BHARATFORG", "token": 108033, "lot": 500},
    {"symbol": "BHEL", "token": 112129, "lot": 1250},
    {"symbol": "CGPOWER", "token": 194561, "lot": 850},
    {"symbol": "CUMMINSIND", "token": 486657, "lot": 100},
    {"symbol": "FEDERALBNK", "token": 261889, "lot": 2500},
    {"symbol": "GVT&D", "token": 4296449, "lot": 125},
    {"symbol": "GLENMARK", "token": 1895937, "lot": 375},
    {"symbol": "HINDALCO", "token": 348929, "lot": 700},
    {"symbol": "POWERINDIA", "token": 4724993, "lot": 25},
    {"symbol": "KEI", "token": 3407361, "lot": 175},
    {"symbol": "LTF", "token": 6386689, "lot": 2250},
    {"symbol": "LAURUSLABS", "token": 4923905, "lot": 425},
    {"symbol": "MCX", "token": 7982337, "lot": 225},
    {"symbol": "NTPC", "token": 2977281, "lot": 1600},
    {"symbol": "NATIONALUM", "token": 1629185, "lot": 1875},
    {"symbol": "POLYCAB", "token": 2455041, "lot": 75},
    {"symbol": "MOTHERSON", "token": 1076225, "lot": 6150},
    {"symbol": "SHRIRAMFIN", "token": 1102337, "lot": 825},
    {"symbol": "SOLARINDS", "token": 3412993, "lot": 25},
    {"symbol": "SAIL", "token": 758529, "lot": 4700},
    {"symbol": "TATASTEEL", "token": 895745, "lot": 2750},
    {"symbol": "TORNTPHARM", "token": 900609, "lot": 125},
    {"symbol": "VEDL", "token": 784129, "lot": 1975},
    {"symbol": "IDEA", "token": 3677697, "lot": 36125},
]


def fetch_or_load_data(kite, symbol_info: Dict[str, Any], days: int = 120) -> pd.DataFrame:
    symbol = symbol_info["symbol"]
    token = symbol_info["token"]
    clean_sym = symbol.replace("&", "_")
    cache_file = DATA_CACHE_DIR / f"{clean_sym}_15m.csv"

    if cache_file.exists():
        # Check freshness
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
        if (datetime.now() - mtime).total_seconds() < 86400:  # < 1 day old
            df = pd.read_csv(cache_file, parse_dates=["date"])
            return df

    logger.info("Fetching 15m historical candles for %s from Kite...", symbol)
    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)

    try:
        raw = kite.historical_data(instrument_token=token, from_date=from_d, to_date=to_d, interval="15minute")
        df = pd.DataFrame(raw)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df.to_csv(cache_file, index=False)
            return df
    except Exception as e:
        logger.error("Error fetching historical data for %s: %s", symbol, e)
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])
    return pd.DataFrame()


def run_orb_simulation(
    df: pd.DataFrame,
    lot_size: int,
    apply_wick_filter: bool = True,
    exit_mode: str = "MODE_1",  # MODE_1: 1:1 trail to BE, hold till 15:15; MODE_2: fixed 1:2; MODE_3: 50% at 1:2 + BE trail
    max_sl_pct: float = 0.025   # Skip if breakout candle range > 2.5% of price
) -> List[Dict[str, Any]]:
    if df.empty:
        return []

    df = df.copy()
    # Normalize date column
    df["d_only"] = df["date"].dt.date
    df["time_only"] = df["date"].dt.time

    trades = []
    days = df["d_only"].unique()

    for d in sorted(days):
        day_df = df[df["d_only"] == d].sort_values("date").reset_index(drop=True)
        # Regular trading day has 25 15-minute candles (09:15 to 15:15 close)
        if len(day_df) < 5:
            continue

        # Initial Balance: first 4 candles (09:15 - 10:15)
        ib_candles = day_df.iloc[:4]
        ib_high = ib_candles["high"].max()
        ib_low = ib_candles["low"].min()

        trade_done = False

        # Evaluation from Candle 4 (10:15 - 10:30) up to Candle 16 (13:15 - 13:30)
        # Candle indices: 4 is 10:15, 5 is 10:30 ... 16 is 13:15
        eval_candles = day_df.iloc[4:17]

        for i, idx in enumerate(eval_candles.index):
            if trade_done:
                break

            candle = day_df.iloc[idx]
            o = float(candle["open"])
            h = float(candle["high"])
            l = float(candle["low"])
            c = float(candle["close"])
            candle_range = h - l

            if candle_range <= 0:
                continue

            # Skip excessively wide breakout candles (protects against huge news spikes)
            if (candle_range / c) > max_sl_pct:
                continue

            direction = None
            breakout_wick_ratio = 0.0

            # 1. Bullish Breakout Check
            if c > ib_high:
                upper_wick = h - c
                breakout_wick_ratio = upper_wick / candle_range
                if (not apply_wick_filter) or (breakout_wick_ratio <= 0.20):
                    direction = "LONG"

            # 2. Bearish Breakdown Check
            elif c < ib_low:
                lower_wick = c - l
                breakout_wick_ratio = lower_wick / candle_range
                if (not apply_wick_filter) or (breakout_wick_ratio <= 0.20):
                    direction = "SHORT"

            if direction is None:
                continue

            # Setup Trade Parameters
            if direction == "LONG":
                entry_trigger = h + 0.05
                initial_sl = l - 0.05
                risk = entry_trigger - initial_sl
            else:
                entry_trigger = l - 0.05
                initial_sl = h + 0.05
                risk = initial_sl - entry_trigger

            if risk <= 0:
                continue

            # Check if entry is confirmed in the immediate next candle(s) (within next 2 candles)
            subsequent_candles = day_df.iloc[idx + 1:]
            if subsequent_candles.empty:
                continue

            # Confirmation search (max 2 candles to trigger entry)
            entry_candle_idx = None
            entry_filled = False
            for sub_i, (sub_idx, sub_c) in enumerate(subsequent_candles.iloc[:2].iterrows()):
                if direction == "LONG" and float(sub_c["high"]) >= entry_trigger:
                    entry_filled = True
                    entry_candle_idx = sub_idx
                    break
                elif direction == "SHORT" and float(sub_c["low"]) <= entry_trigger:
                    entry_filled = True
                    entry_candle_idx = sub_idx
                    break

            if not entry_filled:
                # Breakout did not confirm with price follow-through
                continue

            # Trade is LIVE from entry_candle_idx
            entry_time = day_df.loc[entry_candle_idx, "date"]
            current_sl = initial_sl
            target_1r = entry_trigger + risk if direction == "LONG" else entry_trigger - risk
            target_2r = entry_trigger + (2.0 * risk) if direction == "LONG" else entry_trigger - (2.0 * risk)

            exit_price = None
            exit_time = None
            exit_reason = None
            be_moved = False

            # Monitor remaining candles of the day
            active_candles = day_df.loc[entry_candle_idx:]

            for a_idx, c_bar in active_candles.iterrows():
                bar_high = float(c_bar["high"])
                bar_low = float(c_bar["low"])
                bar_close = float(c_bar["close"])
                bar_time = c_bar["date"]

                # Is this the last candle of the day (15:15 IST)?
                is_eod = (a_idx == day_df.index[-1]) or (c_bar["time_only"] >= dt_time(15, 15))

                if direction == "LONG":
                    # Check SL hit
                    if bar_low <= current_sl:
                        exit_price = current_sl
                        exit_time = bar_time
                        exit_reason = "BREAKEVEN" if be_moved else "STOP_LOSS"
                        break

                    # Check 1:1 Breakeven trigger (for Mode 1 & Mode 3)
                    if bar_high >= target_1r and not be_moved:
                        be_moved = True
                        current_sl = entry_trigger  # Trail SL to entry price

                    # Mode 2 Check: Fixed 1:2 Target
                    if exit_mode == "MODE_2":
                        if bar_high >= target_2r:
                            exit_price = target_2r
                            exit_time = bar_time
                            exit_reason = "TARGET_2R"
                            break

                    # Mode 3 Check: 50% at 2R, 50% trail
                    if exit_mode == "MODE_3" and bar_high >= target_2r:
                        exit_price = target_2r
                        exit_time = bar_time
                        exit_reason = "TARGET_2R_HYBRID"
                        break

                    # EOD Check
                    if is_eod:
                        exit_price = bar_close
                        exit_time = bar_time
                        exit_reason = "EOD_SQUAREOFF"
                        break

                else:  # SHORT
                    # Check SL hit
                    if bar_high >= current_sl:
                        exit_price = current_sl
                        exit_time = bar_time
                        exit_reason = "BREAKEVEN" if be_moved else "STOP_LOSS"
                        break

                    # Check 1:1 Breakeven trigger
                    if bar_low <= target_1r and not be_moved:
                        be_moved = True
                        current_sl = entry_trigger

                    # Mode 2 Check: Fixed 1:2 Target
                    if exit_mode == "MODE_2":
                        if bar_low <= target_2r:
                            exit_price = target_2r
                            exit_time = bar_time
                            exit_reason = "TARGET_2R"
                            break

                    # Mode 3 Check
                    if exit_mode == "MODE_3" and bar_low <= target_2r:
                        exit_price = target_2r
                        exit_time = bar_time
                        exit_reason = "TARGET_2R_HYBRID"
                        break

                    # EOD Check
                    if is_eod:
                        exit_price = bar_close
                        exit_time = bar_time
                        exit_reason = "EOD_SQUAREOFF"
                        break

            if exit_price is None:
                last_bar = day_df.iloc[-1]
                exit_price = float(last_bar["close"])
                exit_time = last_bar["date"]
                exit_reason = "EOD_SQUAREOFF"

            # Compute PnL
            if direction == "LONG":
                points_pnl = exit_price - entry_trigger
            else:
                points_pnl = entry_trigger - exit_price

            r_pnl = points_pnl / risk
            rupees_pnl = points_pnl * lot_size

            trades.append({
                "date": str(d),
                "direction": direction,
                "entry_time": str(entry_time),
                "entry_price": round(entry_trigger, 2),
                "initial_sl": round(initial_sl, 2),
                "risk": round(risk, 2),
                "exit_time": str(exit_time),
                "exit_price": round(exit_price, 2),
                "exit_reason": exit_reason,
                "points_pnl": round(points_pnl, 2),
                "r_pnl": round(r_pnl, 2),
                "rupees_pnl": round(rupees_pnl, 2),
                "wick_ratio": round(breakout_wick_ratio * 100, 1)
            })

            trade_done = True

    return trades


def run_full_suite():
    kite = get_kite_client()
    print("=" * 80)
    print("  INDRAJIT SHANTHARAJ 15-MIN ORB WITH REJECTION FILTER - 30 STOCKS BACKTEST")
    print("=" * 80)

    all_stock_data = {}
    for item in TARGET_STOCKS:
        sym = item["symbol"]
        df = fetch_or_load_data(kite, item, days=120)
        all_stock_data[sym] = (df, item["lot"])
        print(f"Loaded {sym:<12}: {len(df)} candles across {len(df['date'].dt.date.unique()) if not df.empty else 0} days")

    # Run Comparisons:
    # 1. With Wick Filter vs Without Wick Filter (To test Indrajit's claim)
    # 2. Across 3 Exit Modes:
    #    - MODE 1: 1:1 Breakeven Trail -> Hold till 15:15 EOD (Indrajit's Personal Method)
    #    - MODE 2: Fixed 1:2 R:R Target
    #    - MODE 3: 50% at 1:2 R:R + Breakeven Trail till EOD

    modes = ["MODE_1", "MODE_2", "MODE_3"]
    filter_opts = [True, False]

    summary_records = []

    for apply_filter in filter_opts:
        filter_label = "WITH_20PCT_FILTER" if apply_filter else "WITHOUT_FILTER_RAW_ORB"
        for m in modes:
            total_trades = 0
            win_trades = 0
            loss_trades = 0
            be_trades = 0
            total_r = 0.0
            total_rupees = 0.0
            stock_results = {}

            all_trades_list = []

            for sym, (df, lot) in all_stock_data.items():
                trades = run_orb_simulation(df, lot_size=lot, apply_wick_filter=apply_filter, exit_mode=m)
                if not trades:
                    continue

                t_df = pd.DataFrame(trades)
                t_count = len(t_df)
                total_trades += t_count
                wins = len(t_df[t_df["r_pnl"] > 0.05])
                losses = len(t_df[t_df["r_pnl"] < -0.05])
                be = t_count - wins - losses

                win_trades += wins
                loss_trades += losses
                be_trades += be

                r_sum = t_df["r_pnl"].sum()
                rs_sum = t_df["rupees_pnl"].sum()
                total_r += r_sum
                total_rupees += rs_sum

                stock_results[sym] = {
                    "trades": t_count,
                    "win_rate": (wins / t_count * 100) if t_count > 0 else 0,
                    "r_pnl": round(r_sum, 1),
                    "rupees_pnl": round(rs_sum, 0)
                }
                for t in trades:
                    t["symbol"] = sym
                    all_trades_list.append(t)

            win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

            # Profit Factor calculation
            gross_profit = sum(t["rupees_pnl"] for t in all_trades_list if t["rupees_pnl"] > 0)
            gross_loss = abs(sum(t["rupees_pnl"] for t in all_trades_list if t["rupees_pnl"] < 0))
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 99.0

            summary_records.append({
                "Filter": "20% Wick Filter" if apply_filter else "Raw (No Filter)",
                "Exit Mode": m,
                "Total Trades": total_trades,
                "Win Rate %": round(win_rate, 1),
                "Breakeven Trades": be_trades,
                "Total R": round(total_r, 1),
                "Total P&L (₹)": round(total_rupees, 2),
                "Profit Factor": round(profit_factor, 2),
                "Avg R/Trade": round(total_r / total_trades, 2) if total_trades > 0 else 0,
                "stock_results": stock_results,
                "all_trades": all_trades_list
            })

    # Save results to JSON
    report_file = DATA_CACHE_DIR / "orb_30_stocks_backtest_report.json"
    clean_summary = []
    for s in summary_records:
        rec = dict(s)
        rec.pop("all_trades", None)
        clean_summary.append(rec)

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(clean_summary, f, indent=2)

    # Print Comparison Table
    print("\n" + "=" * 100)
    print("                               BACKTEST RESULTS SUMMARY TABLE")
    print("=" * 100)
    print(f"{'Filter Type':<20} | {'Exit Mode':<10} | {'Trades':<8} | {'Win Rate':<10} | {'Total R':<10} | {'Total P&L (Rs)':<16} | {'Profit Factor':<12}")
    print("-" * 100)
    for s in summary_records:
        print(f"{s['Filter']:<20} | {s['Exit Mode']:<10} | {s['Total Trades']:<8} | {s['Win Rate %']:>6.1f}%   | {s['Total R']:>8.1f}R  | Rs {s['Total P&L (₹)']:>12,.0f} | {s['Profit Factor']:>10.2f}")
    print("=" * 100)

    # Print Top 10 Best and Worst performing stocks under Indrajit's Recommended Method (Filter + Mode 1)
    rec_mode = [s for s in summary_records if s["Filter"] == "20% Wick Filter" and s["Exit Mode"] == "MODE_1"][0]
    st_res = rec_mode["stock_results"]
    sorted_stocks = sorted(st_res.items(), key=lambda x: x[1]["rupees_pnl"], reverse=True)

    print("\n" + "=" * 80)
    print("  TOP PERFORMING STOCKS (20% Wick Filter + 1:1 Breakeven Trail -> EOD)")
    print("=" * 80)
    print(f"{'Stock':<14} | {'Trades':<8} | {'Win Rate':<10} | {'Total R':<10} | {'Net P&L (Rs)':<16}")
    print("-" * 80)
    for sym, res in sorted_stocks[:10]:
        print(f"{sym:<14} | {res['trades']:<8} | {res['win_rate']:>6.1f}%   | {res['r_pnl']:>8.1f}R  | Rs {res['rupees_pnl']:>12,.0f}")

    print("\n" + "=" * 80)
    print("  BOTTOM PERFORMING STOCKS")
    print("=" * 80)
    print(f"{'Stock':<14} | {'Trades':<8} | {'Win Rate':<10} | {'Total R':<10} | {'Net P&L (Rs)':<16}")
    print("-" * 80)
    for sym, res in sorted_stocks[-10:]:
        print(f"{sym:<14} | {res['trades']:<8} | {res['win_rate']:>6.1f}%   | {res['r_pnl']:>8.1f}R  | Rs {res['rupees_pnl']:>12,.0f}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run_full_suite()
