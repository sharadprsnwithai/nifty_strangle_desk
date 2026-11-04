import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, time as dt_time, timedelta
import pandas as pd
import numpy as np
from scripts.backtest_indrajit_orb import TARGET_STOCKS, DATA_CACHE_DIR

# Load NIFTY 50 data
nifty_file = DATA_CACHE_DIR / "NIFTY_50_15m.csv"
df_nifty = pd.read_csv(nifty_file, parse_dates=["date"])
df_nifty["d_only"] = df_nifty["date"].dt.date
df_nifty["time_only"] = df_nifty["date"].dt.time

# Build NIFTY daily lookup map:
# For each day: open price, and for each 15-min timestamp: close price, IB high/low/mid
nifty_day_map = {}
for d, g in df_nifty.groupby("d_only"):
    g_sorted = g.sort_values("date").reset_index(drop=True)
    if len(g_sorted) < 4:
        continue
    day_open = float(g_sorted.loc[0, "open"])
    ib_high = float(g_sorted.iloc[:4]["high"].max())
    ib_low = float(g_sorted.iloc[:4]["low"].min())
    ib_mid = (ib_high + ib_low) / 2.0

    bar_map = {}
    for _, row in g_sorted.iterrows():
        bar_map[row["time_only"]] = float(row["close"])

    nifty_day_map[d] = {
        "day_open": day_open,
        "ib_high": ib_high,
        "ib_low": ib_low,
        "ib_mid": ib_mid,
        "bars": bar_map
    }

print(f"Loaded NIFTY 50 mapping for {len(nifty_day_map)} days.")

# Load 30 stocks cached data
all_stock_data = {}
for item in TARGET_STOCKS:
    sym = item["symbol"]
    clean_sym = sym.replace("&", "_")
    cache_file = DATA_CACHE_DIR / f"{clean_sym}_15m.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        all_stock_data[sym] = (df, item["lot"])

print(f"Loaded {len(all_stock_data)} cached stock datasets.")


def simulate_orb_with_filters(
    df: pd.DataFrame,
    lot_size: int,
    apply_wick_filter: bool = True,
    apply_pdh_pdl_filter: bool = False,
    nifty_filter_mode: str = "NONE",  # NONE, "DAY_OPEN", "IB_MID", "IB_RANGE"
    max_sl_pct: float = 0.025
):
    df = df.copy()
    df["d_only"] = df["date"].dt.date
    df["time_only"] = df["date"].dt.time
    trades = []

    unique_days = sorted(df["d_only"].unique())

    # Pre-calculate PDH and PDL for each day
    daily_stats = {}
    for d, g in df.groupby("d_only"):
        daily_stats[d] = {
            "high": float(g["high"].max()),
            "low": float(g["low"].min()),
            "close": float(g["close"].iloc[-1])
        }

    for d_idx, d in enumerate(unique_days):
        day_df = df[df["d_only"] == d].sort_values("date").reset_index(drop=True)
        if len(day_df) < 5:
            continue

        # Get previous day stats if available
        prev_day = unique_days[d_idx - 1] if d_idx > 0 else None
        pdh = daily_stats[prev_day]["high"] if prev_day in daily_stats else None
        pdl = daily_stats[prev_day]["low"] if prev_day in daily_stats else None

        # NIFTY stats for today
        n_info = nifty_day_map.get(d)

        # Initial Balance: first 4 candles (09:15 - 10:15)
        ib_candles = day_df.iloc[:4]
        ib_high = ib_candles["high"].max()
        ib_low = ib_candles["low"].min()

        eval_candles = day_df.iloc[4:17]
        trade_done = False

        for idx in eval_candles.index:
            if trade_done:
                break
            candle = day_df.iloc[idx]
            o, h, l, c = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
            bar_time = candle["time_only"]
            c_range = h - l
            if c_range <= 0 or (c_range / c) > max_sl_pct:
                continue

            direction = None
            if c > ib_high:
                upper_wick = h - c
                if (not apply_wick_filter) or (upper_wick / c_range <= 0.20):
                    direction = "LONG"
            elif c < ib_low:
                lower_wick = c - l
                if (not apply_wick_filter) or (lower_wick / c_range <= 0.20):
                    direction = "SHORT"

            if not direction:
                continue

            # -------------------------------------------------------------
            # FILTER 1: PDH / PDL FILTER
            # -------------------------------------------------------------
            if apply_pdh_pdl_filter and pdh is not None and pdl is not None:
                if direction == "LONG":
                    # Breakout must clear Previous Day High (or close above PDH)
                    if c < pdh:  # Facing overhead resistance of PDH
                        continue
                elif direction == "SHORT":
                    # Breakdown must clear Previous Day Low (or close below PDL)
                    if c > pdl:  # Facing support of PDL
                        continue

            # -------------------------------------------------------------
            # FILTER 2: NIFTY DIRECTION FILTER
            # -------------------------------------------------------------
            if nifty_filter_mode != "NONE" and n_info is not None:
                n_close = n_info["bars"].get(bar_time)
                if n_close is not None:
                    if nifty_filter_mode == "DAY_OPEN":
                        # Green NIFTY day for Long, Red NIFTY day for Short
                        if direction == "LONG" and n_close < n_info["day_open"]:
                            continue
                        elif direction == "SHORT" and n_close > n_info["day_open"]:
                            continue

                    elif nifty_filter_mode == "IB_MID":
                        # NIFTY above its IB midpoint for Long, below for Short
                        if direction == "LONG" and n_close < n_info["ib_mid"]:
                            continue
                        elif direction == "SHORT" and n_close > n_info["ib_mid"]:
                            continue

                    elif nifty_filter_mode == "IB_RANGE":
                        # Strong NIFTY breakout: above NIFTY IB High for Long, below IB Low for Short
                        if direction == "LONG" and n_close < n_info["ib_high"]:
                            continue
                        elif direction == "SHORT" and n_close > n_info["ib_low"]:
                            continue

            # Setup Trade Parameters
            entry_trigger = h + 0.05 if direction == "LONG" else l - 0.05
            initial_sl = l - 0.05 if direction == "LONG" else h + 0.05
            risk = abs(entry_trigger - initial_sl)
            if risk <= 0:
                continue

            subsequent = day_df.iloc[idx + 1:]
            if subsequent.empty:
                continue

            entry_filled = False
            entry_idx = None
            for sub_idx, sub_c in subsequent.iloc[:2].iterrows():
                if direction == "LONG" and float(sub_c["high"]) >= entry_trigger:
                    entry_filled = True
                    entry_idx = sub_idx
                    break
                elif direction == "SHORT" and float(sub_c["low"]) <= entry_trigger:
                    entry_filled = True
                    entry_idx = sub_idx
                    break

            if not entry_filled:
                continue

            # ACTIVE TRADE LIFECYCLE (User's Rule: 1:1 Trail to BE, 1:2.5 Book 50%, 15:15 EOD)
            active_candles = day_df.loc[entry_idx:]
            current_sl = initial_sl
            be_moved = False
            target_1r = entry_trigger + risk if direction == "LONG" else entry_trigger - risk
            target_2_5r = entry_trigger + 2.5 * risk if direction == "LONG" else entry_trigger - 2.5 * risk

            half_booked = False
            half_pnl_rs = 0.0
            half_qty = lot_size / 2.0

            total_pnl_r = 0.0
            total_pnl_rs = 0.0
            final_exit_reason = None

            for a_idx, c_bar in active_candles.iterrows():
                b_high = float(c_bar["high"])
                b_low = float(c_bar["low"])
                b_close = float(c_bar["close"])
                is_eod = (a_idx == day_df.index[-1]) or (c_bar["time_only"] >= dt_time(15, 15))

                if direction == "LONG":
                    # 1. Check existing SL first
                    if b_low <= current_sl:
                        if half_booked:
                            rem_pnl_r = (current_sl - entry_trigger) / risk
                            rem_pnl_rs = (current_sl - entry_trigger) * half_qty
                            total_pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                            total_pnl_rs = half_pnl_rs + rem_pnl_rs
                            final_exit_reason = "TARGET_2.5_THEN_BE"
                        else:
                            total_pnl_r = (current_sl - entry_trigger) / risk
                            total_pnl_rs = (current_sl - entry_trigger) * lot_size
                            final_exit_reason = "BREAKEVEN" if be_moved else "STOP_LOSS"
                        break

                    # 2. Check 1:2.5 Book 50%
                    if b_high >= target_2_5r and not half_booked:
                        half_booked = True
                        half_pnl_rs = (target_2_5r - entry_trigger) * half_qty

                    # 3. Check 1:1 trail for subsequent candles
                    if b_high >= target_1r and not be_moved:
                        be_moved = True
                        current_sl = entry_trigger

                    # 4. EOD Check
                    if is_eod:
                        rem_pnl_r = (b_close - entry_trigger) / risk
                        rem_pnl_rs = (b_close - entry_trigger) * (half_qty if half_booked else lot_size)
                        if half_booked:
                            total_pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                            total_pnl_rs = half_pnl_rs + rem_pnl_rs
                            final_exit_reason = "TARGET_2.5_THEN_EOD"
                        else:
                            total_pnl_r = rem_pnl_r
                            total_pnl_rs = rem_pnl_rs
                            final_exit_reason = "EOD_SQUAREOFF"
                        break

                else:  # SHORT
                    if b_high >= current_sl:
                        if half_booked:
                            rem_pnl_r = (entry_trigger - current_sl) / risk
                            rem_pnl_rs = (entry_trigger - current_sl) * half_qty
                            total_pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                            total_pnl_rs = half_pnl_rs + rem_pnl_rs
                            final_exit_reason = "TARGET_2.5_THEN_BE"
                        else:
                            total_pnl_r = (entry_trigger - current_sl) / risk
                            total_pnl_rs = (entry_trigger - current_sl) * lot_size
                            final_exit_reason = "BREAKEVEN" if be_moved else "STOP_LOSS"
                        break

                    if b_low <= target_2_5r and not half_booked:
                        half_booked = True
                        half_pnl_rs = (entry_trigger - target_2_5r) * half_qty

                    if b_low <= target_1r and not be_moved:
                        be_moved = True
                        current_sl = entry_trigger

                    if is_eod:
                        rem_pnl_r = (entry_trigger - b_close) / risk
                        rem_pnl_rs = (entry_trigger - b_close) * (half_qty if half_booked else lot_size)
                        if half_booked:
                            total_pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                            total_pnl_rs = half_pnl_rs + rem_pnl_rs
                            final_exit_reason = "TARGET_2.5_THEN_EOD"
                        else:
                            total_pnl_r = rem_pnl_r
                            total_pnl_rs = rem_pnl_rs
                            final_exit_reason = "EOD_SQUAREOFF"
                        break

            trades.append({
                "date": str(d),
                "direction": direction,
                "total_pnl_r": round(total_pnl_r, 2),
                "total_pnl_rs": round(total_pnl_rs, 2),
                "half_booked": half_booked,
                "final_exit_reason": final_exit_reason
            })
            trade_done = True

    return trades


def run_filter_comparison():
    configs = [
        {"name": "1. Baseline (20% Wick Only)", "wick": True, "pdh": False, "nifty": "NONE"},
        {"name": "2. Wick + PDH/PDL Filter", "wick": True, "pdh": True, "nifty": "NONE"},
        {"name": "3. Wick + NIFTY vs Day Open", "wick": True, "pdh": False, "nifty": "DAY_OPEN"},
        {"name": "4. Wick + NIFTY vs IB Mid", "wick": True, "pdh": False, "nifty": "IB_MID"},
        {"name": "5. Wick + NIFTY vs IB Range", "wick": True, "pdh": False, "nifty": "IB_RANGE"},
        {"name": "6. Wick + PDH/PDL + NIFTY Day Open", "wick": True, "pdh": True, "nifty": "DAY_OPEN"},
        {"name": "7. Wick + PDH/PDL + NIFTY IB Mid", "wick": True, "pdh": True, "nifty": "IB_MID"},
    ]

    print("\n" + "=" * 110)
    print("                    ORB STRATEGY FILTER COMPARISON ACROSS 30 STOCKS (83 DAYS)")
    print("                    Lifecycle: 1:1 Trail to BE | 1:2.5 Book 50% | 15:15 EOD Square-Off")
    print("=" * 110)
    print(f"{'Configuration':<38} | {'Trades':<7} | {'Win Rate %':<11} | {'2.5R Hits':<10} | {'Total R':<9} | {'Net P&L (Rs)':<15} | {'Profit Factor':<12}")
    print("-" * 110)

    results_data = []

    for cfg in configs:
        all_trades = []
        for sym, (df, lot) in all_stock_data.items():
            t_list = simulate_orb_with_filters(
                df,
                lot_size=lot,
                apply_wick_filter=cfg["wick"],
                apply_pdh_pdl_filter=cfg["pdh"],
                nifty_filter_mode=cfg["nifty"]
            )
            for t in t_list:
                t["symbol"] = sym
                all_trades.append(t)

        if not all_trades:
            continue

        at_df = pd.DataFrame(all_trades)
        t_count = len(at_df)
        wins = len(at_df[at_df["total_pnl_r"] > 0.05])
        losses = len(at_df[at_df["total_pnl_r"] < -0.05])
        be = t_count - wins - losses
        win_rate = (wins / t_count * 100) if t_count > 0 else 0
        r_sum = at_df["total_pnl_r"].sum()
        rs_sum = at_df["total_pnl_rs"].sum()

        target_2_5_hits = len(at_df[at_df["half_booked"] == True])
        target_pct = (target_2_5_hits / t_count * 100) if t_count > 0 else 0

        gross_win = at_df[at_df["total_pnl_rs"] > 0]["total_pnl_rs"].sum()
        gross_loss = abs(at_df[at_df["total_pnl_rs"] < 0]["total_pnl_rs"].sum())
        pf = (gross_win / gross_loss) if gross_loss > 0 else 99.0

        print(f"{cfg['name']:<38} | {t_count:<7} | {win_rate:>8.1f}%   | {target_2_5_hits:>4} ({target_pct:>4.1f}%) | {r_sum:>7.1f}R | Rs {rs_sum:>11,.0f} | {pf:>10.2f}")

        results_data.append({
            "name": cfg["name"],
            "trades": t_count,
            "win_rate": win_rate,
            "target_hits": target_2_5_hits,
            "target_pct": target_pct,
            "r_sum": r_sum,
            "rs_sum": rs_sum,
            "pf": pf
        })

    print("=" * 110 + "\n")


if __name__ == "__main__":
    run_filter_comparison()
