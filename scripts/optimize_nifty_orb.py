import sys
import os
import pandas as pd
import numpy as np
from datetime import time as dt_time
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))


def run_variation(
    df_5m,
    df_15m,
    days_15m,
    days_5m,
    unique_days,
    orb_window="30m",  # '15m' or '30m'
    initial_sl_pts=50.0,
    target1_pts=75.0,
    direction_mode="BOTH",  # 'BOTH', 'LONG_ONLY', 'SHORT_ONLY'
    use_trend_filter=True,
    min_range_pts=25.0,
    max_range_pts=120.0,
    lot_size=65,
    lots=2,
    slippage_pts=0.5,
):
    trades = []
    lot1_lots = max(1, lots // 2)
    lot2_lots = lots - lot1_lots

    lot1_qty = lot1_lots * lot_size
    lot2_qty = lot2_lots * lot_size
    total_qty = lots * lot_size

    ref_cutoff = dt_time(9, 45) if orb_window == "30m" else dt_time(9, 30)

    for d in unique_days:
        if d not in days_15m or d not in days_5m:
            continue
        day_15m = days_15m[d]
        day_5m = days_5m[d]

        # 1. Reference range
        if orb_window == "30m":
            ref_bars = day_15m[(day_15m["time"] >= dt_time(9, 15)) & (day_15m["time"] <= dt_time(9, 30))]
            if len(ref_bars) < 2:
                continue
        else:
            ref_bars = day_15m[day_15m["time"] == dt_time(9, 15)]
            if len(ref_bars) < 1:
                continue

        ref_high = float(ref_bars["high"].max())
        ref_low = float(ref_bars["low"].min())
        rng_pts = ref_high - ref_low

        if rng_pts < min_range_pts or rng_pts > max_range_pts:
            continue

        # 2. Breakout scan
        scan_bars = day_15m[(day_15m["time"] >= ref_cutoff) & (day_15m["time"] <= dt_time(13, 15))]
        active_trade = None

        for idx, bar in scan_bars.iterrows():
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            b_close = float(bar["close"])
            ema50_val = float(bar["ema50"])
            bar_close_time = (bar["date"] + pd.Timedelta(minutes=15)).time()

            long_ok = (b_high > ref_high) and (b_close > ref_high)
            if use_trend_filter:
                long_ok = long_ok and (b_close > ema50_val)

            short_ok = (b_low < ref_low) and (b_close < ref_low)
            if use_trend_filter:
                short_ok = short_ok and (b_close < ema50_val)

            if long_ok and direction_mode in ("BOTH", "LONG_ONLY"):
                active_trade = {
                    "day": d,
                    "direction": "LONG",
                    "entry_time": bar_close_time,
                    "entry_date": bar["date"] + pd.Timedelta(minutes=15),
                    "entry_price": b_close + slippage_pts,
                    "sl_price": (b_close + slippage_pts) - initial_sl_pts,
                    "target1_price": (b_close + slippage_pts) + target1_pts,
                }
                break
            elif short_ok and direction_mode in ("BOTH", "SHORT_ONLY"):
                active_trade = {
                    "day": d,
                    "direction": "SHORT",
                    "entry_time": bar_close_time,
                    "entry_date": bar["date"] + pd.Timedelta(minutes=15),
                    "entry_price": b_close - slippage_pts,
                    "sl_price": (b_close - slippage_pts) + initial_sl_pts,
                    "target1_price": (b_close - slippage_pts) - target1_pts,
                }
                break

        if active_trade is None:
            continue

        direction = active_trade["direction"]
        entry_p = active_trade["entry_price"]
        sl_p = active_trade["sl_price"]
        t1_p = active_trade["target1_price"]
        entry_dt = active_trade["entry_date"]

        subseq_5m = day_5m[day_5m["date"] >= entry_dt].sort_values("date")
        if subseq_5m.empty:
            continue

        t1_hit = False
        lot1_exit_p = None
        lot2_exit_p = None

        for _, bar5 in subseq_5m.iterrows():
            b_high5 = float(bar5["high"])
            b_low5 = float(bar5["low"])
            b_close5 = float(bar5["close"])
            b_time5 = bar5["time"]

            is_eod_cutoff = b_time5 >= dt_time(15, 15)

            if not t1_hit:
                if direction == "LONG":
                    if b_low5 <= sl_p:
                        lot1_exit_p = sl_p - slippage_pts
                        lot2_exit_p = sl_p - slippage_pts
                        break
                    elif b_high5 >= t1_p:
                        t1_hit = True
                        lot1_exit_p = t1_p - slippage_pts
                        sl_p = entry_p
                else:
                    if b_high5 >= sl_p:
                        lot1_exit_p = sl_p + slippage_pts
                        lot2_exit_p = sl_p + slippage_pts
                        break
                    elif b_low5 <= t1_p:
                        t1_hit = True
                        lot1_exit_p = t1_p + slippage_pts
                        sl_p = entry_p

            if t1_hit and lot2_exit_p is None:
                if direction == "LONG":
                    if b_low5 <= sl_p:
                        lot2_exit_p = sl_p - slippage_pts
                        break
                    elif is_eod_cutoff:
                        lot2_exit_p = b_close5 - slippage_pts
                        break
                else:
                    if b_high5 >= sl_p:
                        lot2_exit_p = sl_p + slippage_pts
                        break
                    elif is_eod_cutoff:
                        lot2_exit_p = b_close5 + slippage_pts
                        break

        if lot1_exit_p is None:
            last_p = subseq_5m.iloc[-1]["close"]
            lot1_exit_p = last_p
            lot2_exit_p = last_p

        if direction == "LONG":
            lot1_pts = lot1_exit_p - entry_p
            lot2_pts = lot2_exit_p - entry_p
        else:
            lot1_pts = entry_p - lot1_exit_p
            lot2_pts = entry_p - lot2_exit_p

        gross = (lot1_pts * lot1_qty) + (lot2_pts * lot2_qty)
        turnover = (entry_p * total_qty) + (lot1_exit_p * lot1_qty) + (lot2_exit_p * lot2_qty)
        charges = 60.0 + (turnover * 0.00015)
        net = gross - charges

        trades.append({
            "day": d,
            "direction": direction,
            "gross": gross,
            "net": net,
            "charges": charges,
            "t1_hit": t1_hit,
        })

    if not trades:
        return {}

    tdf = pd.DataFrame(trades)
    tot = len(tdf)
    wins = (tdf["net"] > 0).sum()
    gross_sum = tdf["gross"].sum()
    net_sum = tdf["net"].sum()
    tdf["cum"] = tdf["net"].cumsum()
    max_dd = (tdf["cum"].cummax() - tdf["cum"]).max()
    win_rate = (wins / tot) * 100
    pos_sum = tdf[tdf["net"] > 0]["net"].sum()
    neg_sum = abs(tdf[tdf["net"] < 0]["net"].sum())
    pf = (pos_sum / neg_sum) if neg_sum > 0 else np.nan

    return {
        "Trades": tot,
        "Win Rate (%)": round(win_rate, 1),
        "Gross PnL": round(gross_sum, 0),
        "Net PnL": round(net_sum, 0),
        "Max DD": round(max_dd, 0),
        "Profit Factor": round(pf, 2),
    }


def main():
    df_5m = pd.read_parquet("data/nifty_5m_3y.parquet")
    df_5m["date"] = pd.to_datetime(df_5m["date"])
    if df_5m["date"].dt.tz is not None:
        df_5m["date"] = df_5m["date"].dt.tz_convert("Asia/Kolkata")
    df_5m["time"] = df_5m["date"].dt.time
    df_5m["day"] = df_5m["date"].dt.date
    df_5m = df_5m.sort_values("date").reset_index(drop=True)

    df_indexed = df_5m.set_index("date")
    df_15m = df_indexed.resample("15min", offset="15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()

    df_15m["time"] = df_15m["date"].dt.time
    df_15m["day"] = df_15m["date"].dt.date
    df_15m["ema50"] = df_15m["close"].ewm(span=50, adjust=False).mean()

    days_15m = {d: group for d, group in df_15m.groupby("day")}
    days_5m = {d: group for d, group in df_5m.groupby("day")}
    unique_days = sorted(list(days_15m.keys()))

    configs = [
        # Baseline
        {"name": "1. Baseline (30m ORB, 50 SL, 75 T1, Both)", "orb_window": "30m", "initial_sl_pts": 50.0, "target1_pts": 75.0, "direction_mode": "BOTH", "min_range_pts": 0.0, "max_range_pts": 999.0},
        # Directional Split
        {"name": "2. Short-Only (30m ORB, 50 SL, 75 T1)", "orb_window": "30m", "initial_sl_pts": 50.0, "target1_pts": 75.0, "direction_mode": "SHORT_ONLY", "min_range_pts": 0.0, "max_range_pts": 999.0},
        {"name": "3. Long-Only (30m ORB, 50 SL, 75 T1)", "orb_window": "30m", "initial_sl_pts": 50.0, "target1_pts": 75.0, "direction_mode": "LONG_ONLY", "min_range_pts": 0.0, "max_range_pts": 999.0},
        # Range Filter (Skip days with bloated opening range > 100 pts)
        {"name": "4. Short-Only with Range Filter (Range 30-100 pts)", "orb_window": "30m", "initial_sl_pts": 50.0, "target1_pts": 75.0, "direction_mode": "SHORT_ONLY", "min_range_pts": 30.0, "max_range_pts": 100.0},
        {"name": "5. Both with Range Filter (Range 30-100 pts)", "orb_window": "30m", "initial_sl_pts": 50.0, "target1_pts": 75.0, "direction_mode": "BOTH", "min_range_pts": 30.0, "max_range_pts": 100.0},
        # Wider Targets (Let runners run: 40 SL, 80 T1)
        {"name": "6. Short-Only (40 SL, 80 T1, Range 30-100 pts)", "orb_window": "30m", "initial_sl_pts": 40.0, "target1_pts": 80.0, "direction_mode": "SHORT_ONLY", "min_range_pts": 30.0, "max_range_pts": 100.0},
        {"name": "7. Short-Only (40 SL, 100 T1, Range 30-100 pts)", "orb_window": "30m", "initial_sl_pts": 40.0, "target1_pts": 100.0, "direction_mode": "SHORT_ONLY", "min_range_pts": 30.0, "max_range_pts": 100.0},
        # 15m ORB comparison
        {"name": "8. 15m ORB Short-Only (40 SL, 80 T1)", "orb_window": "15m", "initial_sl_pts": 40.0, "target1_pts": 80.0, "direction_mode": "SHORT_ONLY", "min_range_pts": 20.0, "max_range_pts": 80.0},
    ]

    results = []
    for c in configs:
        res = run_variation(
            df_5m, df_15m, days_15m, days_5m, unique_days,
            orb_window=c["orb_window"],
            initial_sl_pts=c["initial_sl_pts"],
            target1_pts=c["target1_pts"],
            direction_mode=c["direction_mode"],
            min_range_pts=c["min_range_pts"],
            max_range_pts=c["max_range_pts"],
        )
        row = {"Setup / Variant": c["name"]}
        row.update(res)
        results.append(row)

    print("\n" + "=" * 115)
    print("                    NIFTY ORB FUTURES: 3-YEAR PARAMETER COMPARISON")
    print("=" * 115)
    print(tabulate(pd.DataFrame(results), headers="keys", tablefmt="pipe", showindex=False))


if __name__ == "__main__":
    main()
