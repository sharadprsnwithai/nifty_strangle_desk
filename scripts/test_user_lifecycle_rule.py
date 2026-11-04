import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, time as dt_time
import pandas as pd
from scripts.backtest_indrajit_orb import TARGET_STOCKS, DATA_CACHE_DIR

# Load cached data for all 30 stocks
all_stock_data = {}
for item in TARGET_STOCKS:
    sym = item["symbol"]
    clean_sym = sym.replace("&", "_")
    cache_file = DATA_CACHE_DIR / f"{clean_sym}_15m.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        all_stock_data[sym] = (df, item["lot"])

print(f"Loaded {len(all_stock_data)} cached stock datasets.")


def simulate_user_rule_fixed(df, lot_size, apply_wick_filter=True, max_sl_pct=0.025):
    df = df.copy()
    df["d_only"] = df["date"].dt.date
    df["time_only"] = df["date"].dt.time
    trades = []

    for d in sorted(df["d_only"].unique()):
        day_df = df[df["d_only"] == d].sort_values("date").reset_index(drop=True)
        if len(day_df) < 5:
            continue

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

            # ACTIVE TRADE LIFECYCLE
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


all_trades = []
stock_stats = {}
for sym, (df, lot) in all_stock_data.items():
    res = simulate_user_rule_fixed(df, lot, apply_wick_filter=True)
    if res:
        tdf = pd.DataFrame(res)
        wins = len(tdf[tdf["total_pnl_r"] > 0.05])
        tot = len(tdf)
        r_sum = tdf["total_pnl_r"].sum()
        rs_sum = tdf["total_pnl_rs"].sum()
        stock_stats[sym] = {
            "trades": tot,
            "win_rate": round(wins / tot * 100, 1),
            "r_sum": round(r_sum, 1),
            "rs_sum": round(rs_sum, 0)
        }
        for t in res:
            t["symbol"] = sym
            all_trades.append(t)

at_df = pd.DataFrame(all_trades)
tot_trades = len(at_df)
tot_wins = len(at_df[at_df["total_pnl_r"] > 0.05])
tot_loss = len(at_df[at_df["total_pnl_r"] < -0.05])
tot_be = tot_trades - tot_wins - tot_loss
tot_r = at_df["total_pnl_r"].sum()
tot_rs = at_df["total_pnl_rs"].sum()
gross_win = at_df[at_df["total_pnl_rs"] > 0]["total_pnl_rs"].sum()
gross_loss = abs(at_df[at_df["total_pnl_rs"] < 0]["total_pnl_rs"].sum())
pf = gross_win / gross_loss if gross_loss > 0 else 99.0

target_hits = len(at_df[at_df["half_booked"] == True])

print("=" * 76)
print("  CORRECTED LIFECYCLE: 1:1 Trail to BE | 1:2.5 Book 50% | 15:15 EOD Square-Off")
print("=" * 76)
print(f"Total Trades        : {tot_trades}")
print(f"Trades Hitting 2.5R : {target_hits} ({target_hits / tot_trades * 100:.1f}%)")
print(f"Win Rate            : {tot_wins / tot_trades * 100:.1f}% ({tot_wins} Wins / {tot_loss} Losses / {tot_be} Breakevens)")
print(f"Total R Expectancy  : +{tot_r:.1f} R")
print(f"Total Net P&L       : Rs {tot_rs:,.2f}")
print(f"Profit Factor       : {pf:.2f}")
print(f"Average P&L/Trade   : Rs {tot_rs / tot_trades:,.2f} (+{tot_r / tot_trades:.2f} R)")
print("=" * 76)

sorted_stocks = sorted(stock_stats.items(), key=lambda x: x[1]["rs_sum"], reverse=True)
print("\nTop 10 Performers:")
print(f"{'Stock':<14} | {'Trades':<8} | {'Win Rate':<10} | {'Total R':<10} | {'Net P&L (Rs)':<16}")
print("-" * 65)
for s, st in sorted_stocks[:10]:
    print(f"{s:<14} | {st['trades']:<8} | {st['win_rate']:>6.1f}%   | {st['r_sum']:>8.1f}R  | Rs {st['rs_sum']:>12,.0f}")
