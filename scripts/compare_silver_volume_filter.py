import os
import sys
from datetime import time as dt_time
import pandas as pd
import numpy as np

def run_silver_backtest():
    parquet_path = "data/silverm_15m_1year.parquet"
    if not os.path.exists(parquet_path):
        print(f"Error: {parquet_path} not found")
        return

    df = pd.read_parquet(parquet_path)
    df["date"] = pd.to_datetime(df["date"])
    df["time"] = df["date"].dt.time
    df["day"] = df["date"].dt.date
    df = df.sort_values("date").reset_index(drop=True)

    # 50 EMA on 15m close
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # Model A: Baseline rolling 20-period SMA
    df["vol_sma20"] = df["volume"].rolling(20).mean().shift(1)

    # Model B: Time-slot specific volume profiling (rolling 20-day SMA for each time slot)
    df["vol_slot_sma20"] = df.groupby("time")["volume"].transform(
        lambda s: s.rolling(20, min_periods=5).mean().shift(1)
    )

    days = sorted(df["day"].unique())

    sl_pts = 500.0
    tgt1_pts = 800.0
    friction_2l = 650.0   # Rs 650 friction per 2-lot trade
    friction_4l = 1300.0  # Rs 1300 friction per 4-lot trade

    models = [
        {"name": "Baseline (Rolling 20 SMA @ 1.2x)", "type": "rolling_sma", "mult": 1.2},
        {"name": "Point 4: Time-Slot Profiling @ 1.0x", "type": "slot_profiling", "mult": 1.0},
        {"name": "Point 4: Time-Slot Profiling @ 1.1x", "type": "slot_profiling", "mult": 1.1},
        {"name": "Point 4: Time-Slot Profiling @ 1.2x", "type": "slot_profiling", "mult": 1.2},
        {"name": "Point 4: Time-Slot Profiling @ 1.25x", "type": "slot_profiling", "mult": 1.25},
    ]

    all_results = {}

    for m in models:
        trades = []
        for d in days:
            day_df = df[df["day"] == d].sort_values("date").reset_index(drop=True)
            ref_bar = day_df[day_df["time"] == dt_time(17, 45)]
            if ref_bar.empty:
                continue

            ref_high = float(ref_bar["high"].iloc[0])
            ref_low = float(ref_bar["low"].iloc[0])

            session_df = day_df[(day_df["time"] >= dt_time(18, 0)) & (day_df["time"] <= dt_time(22, 45))].copy()
            if session_df.empty:
                continue

            active_trade = None

            for idx, bar in session_df.iterrows():
                b_time = bar["time"]
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                b_open = float(bar["open"])
                b_close = float(bar["close"])
                b_vol = float(bar["volume"])
                ema50_val = float(bar["ema50"])

                if active_trade is None:
                    if b_time <= dt_time(19, 30):
                        if m["type"] == "rolling_sma":
                            vol_ref = float(bar["vol_sma20"]) if not np.isnan(bar["vol_sma20"]) else 100.0
                        else:
                            vol_ref = float(bar["vol_slot_sma20"]) if not np.isnan(bar["vol_slot_sma20"]) else 100.0

                        vol_ok = b_vol >= m["mult"] * vol_ref

                        long_ok = (b_high > ref_high) and (b_close > ref_high) and (b_close > ema50_val) and vol_ok
                        short_ok = (b_low < ref_low) and (b_close < ref_low) and (b_close < ema50_val) and vol_ok

                        if long_ok:
                            entry = ref_high + 5.0
                            active_trade = {
                                "day": d, "direction": "LONG", "entry_time": b_time, "entry_price": entry,
                                "sl": entry - sl_pts, "tgt1": entry + tgt1_pts, "be_moved": False,
                                "lot1_closed": False, "lot1_pts": 0.0, "lot2_closed": False, "lot2_pts": 0.0,
                                "exit_time": None, "exit_reason": None,
                            }
                        elif short_ok:
                            entry = ref_low - 5.0
                            active_trade = {
                                "day": d, "direction": "SHORT", "entry_time": b_time, "entry_price": entry,
                                "sl": entry + sl_pts, "tgt1": entry - tgt1_pts, "be_moved": False,
                                "lot1_closed": False, "lot1_pts": 0.0, "lot2_closed": False, "lot2_pts": 0.0,
                                "exit_time": None, "exit_reason": None,
                            }
                else:
                    direction = active_trade["direction"]
                    entry = active_trade["entry_price"]
                    curr_sl = active_trade["sl"]
                    tgt1 = active_trade["tgt1"]

                    # 1. Manage Lot 1 (Target 1: +800 pts)
                    if not active_trade["lot1_closed"]:
                        if direction == "LONG":
                            if b_high >= tgt1:
                                active_trade["lot1_pts"] = tgt1_pts
                                active_trade["lot1_closed"] = True
                                active_trade["sl"] = entry  # Trailed to Breakeven
                                active_trade["be_moved"] = True
                            elif b_low <= curr_sl:
                                active_trade["lot1_pts"] = -sl_pts
                                active_trade["lot2_pts"] = -sl_pts
                                active_trade["lot1_closed"] = True
                                active_trade["lot2_closed"] = True
                                active_trade["exit_time"] = b_time
                                active_trade["exit_reason"] = "INITIAL_SL"
                                active_trade["total_pts"] = -sl_pts * 2
                                active_trade["gross_pnl"] = active_trade["total_pts"] * 5.0
                                active_trade["net_pnl_2l"] = active_trade["gross_pnl"] - friction_2l
                                active_trade["net_pnl_4l"] = (active_trade["gross_pnl"] * 2) - friction_4l
                                trades.append(active_trade)
                                break
                        else:  # SHORT
                            if b_low <= tgt1:
                                active_trade["lot1_pts"] = tgt1_pts
                                active_trade["lot1_closed"] = True
                                active_trade["sl"] = entry
                                active_trade["be_moved"] = True
                            elif b_high >= curr_sl:
                                active_trade["lot1_pts"] = -sl_pts
                                active_trade["lot2_pts"] = -sl_pts
                                active_trade["lot1_closed"] = True
                                active_trade["lot2_closed"] = True
                                active_trade["exit_time"] = b_time
                                active_trade["exit_reason"] = "INITIAL_SL"
                                active_trade["total_pts"] = -sl_pts * 2
                                active_trade["gross_pnl"] = active_trade["total_pts"] * 5.0
                                active_trade["net_pnl_2l"] = active_trade["gross_pnl"] - friction_2l
                                active_trade["net_pnl_4l"] = (active_trade["gross_pnl"] * 2) - friction_4l
                                trades.append(active_trade)
                                break

                    # 2. Manage Lot 2 (Runner) after Lot 1 is closed
                    if active_trade["lot1_closed"] and not active_trade["lot2_closed"]:
                        curr_sl = active_trade["sl"]
                        if direction == "LONG":
                            if b_low <= curr_sl:
                                active_trade["lot2_pts"] = 0.0 if active_trade["be_moved"] else -sl_pts
                                active_trade["lot2_closed"] = True
                                active_trade["exit_time"] = b_time
                                active_trade["exit_reason"] = "BREAKEVEN_SL"
                                active_trade["total_pts"] = active_trade["lot1_pts"] + active_trade["lot2_pts"]
                                active_trade["gross_pnl"] = active_trade["total_pts"] * 5.0
                                active_trade["net_pnl_2l"] = active_trade["gross_pnl"] - friction_2l
                                active_trade["net_pnl_4l"] = (active_trade["gross_pnl"] * 2) - friction_4l
                                trades.append(active_trade)
                                break
                        else:  # SHORT
                            if b_high >= curr_sl:
                                active_trade["lot2_pts"] = 0.0 if active_trade["be_moved"] else -sl_pts
                                active_trade["lot2_closed"] = True
                                active_trade["exit_time"] = b_time
                                active_trade["exit_reason"] = "BREAKEVEN_SL"
                                active_trade["total_pts"] = active_trade["lot1_pts"] + active_trade["lot2_pts"]
                                active_trade["gross_pnl"] = active_trade["total_pts"] * 5.0
                                active_trade["net_pnl_2l"] = active_trade["gross_pnl"] - friction_2l
                                active_trade["net_pnl_4l"] = (active_trade["gross_pnl"] * 2) - friction_4l
                                trades.append(active_trade)
                                break

                    # 3. Session Cutoff at 22:45
                    if b_time == dt_time(22, 45):
                        pts = (b_close - entry) if direction == "LONG" else (entry - b_close)
                        if not active_trade["lot1_closed"]:
                            active_trade["lot1_pts"] = pts
                            active_trade["lot1_closed"] = True
                        if not active_trade["lot2_closed"]:
                            active_trade["lot2_pts"] = pts
                            active_trade["lot2_closed"] = True
                        active_trade["exit_time"] = b_time
                        active_trade["exit_reason"] = "EOD_CUTOFF"
                        active_trade["total_pts"] = active_trade["lot1_pts"] + active_trade["lot2_pts"]
                        active_trade["gross_pnl"] = active_trade["total_pts"] * 5.0
                        active_trade["net_pnl_2l"] = active_trade["gross_pnl"] - friction_2l
                        active_trade["net_pnl_4l"] = (active_trade["gross_pnl"] * 2) - friction_4l
                        trades.append(active_trade)
                        break

        tdf = pd.DataFrame(trades)
        if not tdf.empty:
            tdf["month"] = pd.to_datetime(tdf["day"]).dt.strftime("%Y-%m")
        all_results[m["name"]] = tdf

    # Save detailed trade logs for baseline and Point 4 @ 1.2x
    all_results["Baseline (Rolling 20 SMA @ 1.2x)"].to_csv("data/silver_baseline_trades.csv", index=False)
    all_results["Point 4: Time-Slot Profiling @ 1.2x"].to_csv("data/silver_point4_slot_trades.csv", index=False)

    # Metrics Summary
    summary = []
    for name, tdf in all_results.items():
        if tdf.empty:
            continue
        tot_trades = len(tdf)
        wins = (tdf["net_pnl_2l"] > 0).sum()
        losses = (tdf["net_pnl_2l"] <= 0).sum()
        wr = (wins / tot_trades) * 100.0 if tot_trades > 0 else 0.0
        gross = tdf["gross_pnl"].sum()
        net_2l = tdf["net_pnl_2l"].sum()
        net_4l = tdf["net_pnl_4l"].sum()

        cum_2l = tdf["net_pnl_2l"].cumsum()
        peak_2l = cum_2l.cummax()
        max_dd_2l = (peak_2l - cum_2l).max()

        cum_4l = tdf["net_pnl_4l"].cumsum()
        peak_4l = cum_4l.cummax()
        max_dd_4l = (peak_4l - cum_4l).max()

        wins_sum = tdf[tdf["net_pnl_2l"] > 0]["net_pnl_2l"].sum()
        loss_sum = abs(tdf[tdf["net_pnl_2l"] < 0]["net_pnl_2l"].sum())
        pf = (wins_sum / loss_sum) if loss_sum > 0 else 99.9

        avg_win_2l = tdf[tdf["net_pnl_2l"] > 0]["net_pnl_2l"].mean()
        avg_loss_2l = abs(tdf[tdf["net_pnl_2l"] < 0]["net_pnl_2l"].mean())
        payoff = avg_win_2l / avg_loss_2l if avg_loss_2l > 0 else 99.9

        summary.append({
            "Model": name,
            "Trades": tot_trades,
            "Wins": wins,
            "Losses": losses,
            "Win Rate (%)": round(wr, 1),
            "Payoff Ratio": f"{payoff:.2f}:1",
            "Net PnL 2L (Rs)": round(net_2l, 2),
            "Max DD 2L (Rs)": round(max_dd_2l, 2),
            "Profit Factor": round(pf, 2),
            "Return / DD": round(net_2l / max_dd_2l, 2) if max_dd_2l > 0 else 0.0,
            "Net PnL 4L (Rs)": round(net_4l, 2),
            "Max DD 4L (Rs)": round(max_dd_4l, 2),
        })

    sdf = pd.DataFrame(summary)
    print("=" * 140)
    print("  MCX SILVER MINI (SILVERM) - BASELINE vs TIME-SLOT VOLUME PROFILING (1-YEAR CUMULATIVE)")
    print("=" * 140)
    print(sdf.to_string(index=False))
    print("=" * 140)

    # Monthly comparison
    base_tdf = all_results["Baseline (Rolling 20 SMA @ 1.2x)"]
    p4_12_tdf = all_results["Point 4: Time-Slot Profiling @ 1.2x"]

    months = sorted(list(set(base_tdf["month"].unique()).union(set(p4_12_tdf["month"].unique()))))
    monthly_rows = []
    for mo in months:
        b_m = base_tdf[base_tdf["month"] == mo]
        p_12_m = p4_12_tdf[p4_12_tdf["month"] == mo]

        b_pnl_2l = b_m["net_pnl_2l"].sum() if not b_m.empty else 0.0
        b_pnl_4l = b_m["net_pnl_4l"].sum() if not b_m.empty else 0.0
        b_trades = len(b_m)
        b_wins = (b_m["net_pnl_2l"] > 0).sum()
        b_losses = b_trades - b_wins
        b_wr = (b_wins / b_trades * 100.0) if b_trades > 0 else 0.0

        p12_pnl_2l = p_12_m["net_pnl_2l"].sum() if not p_12_m.empty else 0.0
        p12_pnl_4l = p_12_m["net_pnl_4l"].sum() if not p_12_m.empty else 0.0
        p12_trades = len(p_12_m)
        p12_wins = (p_12_m["net_pnl_2l"] > 0).sum()
        p12_losses = p12_trades - p12_wins
        p12_wr = (p12_wins / p12_trades * 100.0) if p12_trades > 0 else 0.0

        # Monthly DD for 2L
        b_cum = b_m["net_pnl_2l"].cumsum()
        b_dd = (b_cum.cummax() - b_cum).max() if not b_m.empty else 0.0

        p_cum = p_12_m["net_pnl_2l"].cumsum()
        p_dd = (p_cum.cummax() - p_cum).max() if not p_12_m.empty else 0.0

        monthly_rows.append({
            "Month": mo,
            "Base Trd": b_trades,
            "Base W/L": f"{b_wins}/{b_losses}",
            "Base Win%": round(b_wr, 1),
            "Base Net 2L (Rs)": round(b_pnl_2l, 2),
            "Base DD 2L": round(b_dd, 2),
            "P4 Trd": p12_trades,
            "P4 W/L": f"{p12_wins}/{p12_losses}",
            "P4 Win%": round(p12_wr, 1),
            "P4 Net 2L (Rs)": round(p12_pnl_2l, 2),
            "P4 DD 2L": round(p_dd, 2),
            "Net 2L Diff": round(p12_pnl_2l - b_pnl_2l, 2),
            "P4 Net 4L (Rs)": round(p12_pnl_4l, 2),
        })

    mcomp_df = pd.DataFrame(monthly_rows)
    mcomp_df.to_csv("data/silver_monthly_comparison_baseline_vs_point4.csv", index=False)
    print("\n" + "=" * 145)
    print("                                MONTH-BY-MONTH PERFORMANCE BREAKDOWN: BASELINE vs POINT 4 (1.2x)")
    print("=" * 145)
    print(mcomp_df.to_string(index=False))
    print("=" * 145)

if __name__ == "__main__":
    run_silver_backtest()
