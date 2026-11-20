import sys, os
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Tuple, Optional
import numpy as np
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))
from strategy.options_model import compute_fractional_dte, black_76_price
from strategy.costs import calculate_trade_costs
from strategy.backtest import get_weekly_expiry_for_date, calculate_daily_atr


def run_exit_model_simulation(
    bars_df: pd.DataFrame,
    model_name: str,
    # Logic configuration flags:
    exit_mode: str, # "COMMITTED_DUAL", "SINGLE_LEG_50PCT_SL", "SINGLE_LEG_10K_PER_LEG", "SINGLE_LEG_50PCT_TRAIL_BE", "SINGLE_LEG_ON_10K_COMBINED", "SINGLE_LEG_SL_CO_TARGET"
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
    atr_multiplier: float = 1.25,
    morning_filter_threshold_pct: float = 0.60,
    exit_cutoff_time: time = time(14, 45)
) -> Dict[str, Any]:
    df = bars_df.copy()
    if "timestamps" in df.columns:
        df["dt"] = pd.to_datetime(df["timestamps"])
    elif "date" in df.columns:
        df["dt"] = pd.to_datetime(df["date"])
    else:
        df["dt"] = pd.to_datetime(df.index)

    df["trade_date"] = df["dt"].dt.date
    trade_dates = sorted(df["trade_date"].unique())
    total_qty = lots * lot_size

    # Pre-calculate daily ATR for strike selection
    daily_atr = calculate_daily_atr(df, period=14)

    trade_logs: List[Dict[str, Any]] = []
    skipped_days = 0

    for t_date in trade_dates:
        day_df = df[df["trade_date"] == t_date].sort_values("dt")
        
        # 09:15 to 10:00 morning bars
        morning_bars = day_df[(day_df["dt"].dt.hour == 9) | ((day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute == 0))]
        if morning_bars.empty:
            continue
        
        spot_open_915 = float(morning_bars.iloc[0]["open"])
        morning_high = float(morning_bars["high"].max())
        morning_low = float(morning_bars["low"].min())
        morning_range = morning_high - morning_low
        morning_range_pct = (morning_range / spot_open_915) * 100.0 if spot_open_915 > 0 else 0.0

        # Morning Trend Filter (skips violent opening thrusts >= 0.60%)
        if morning_range_pct >= morning_filter_threshold_pct:
            skipped_days += 1
            continue

        # Locate entry candle around 10:00 AM (between 09:55 and 10:05)
        entry_bars = day_df[(day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute <= 5)]
        if entry_bars.empty:
            entry_bars = day_df[(day_df["dt"].dt.hour == 9) & (day_df["dt"].dt.minute >= 55)]
        if entry_bars.empty:
            continue

        entry_bar = entry_bars.iloc[0]
        entry_time = entry_bar["dt"]
        spot_10am = float(entry_bar["open"])

        target_expiry, is_0dte = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)
        entry_dte = compute_fractional_dte(target_expiry, entry_time)

        # Dynamic ATR Strike Selection (1.25x ATR)
        current_atr = daily_atr.get(t_date, 150.0)
        if pd.isna(current_atr) or current_atr <= 0:
            current_atr = 150.0
        strike_offset = max(current_atr * atr_multiplier, 150.0)
        ce_strike = round((spot_10am + strike_offset) / 50.0) * 50.0
        pe_strike = round((spot_10am - strike_offset) / 50.0) * 50.0
        ce_entry = black_76_price(spot_10am, ce_strike, entry_dte, iv, r, "CE")
        pe_entry = black_76_price(spot_10am, pe_strike, entry_dte, iv, r, "PE")

        # Initial SL and targets
        ce_sl = round(ce_entry * 1.50, 2)
        ce_target = round(ce_entry * 0.20, 2)
        pe_sl = round(pe_entry * 1.50, 2)
        pe_target = round(pe_entry * 0.20, 2)

        # For ₹10k per-leg max loss (at total_qty = 650, 10,000 / 650 = 15.38 pts)
        ce_10k_sl = round(ce_entry + (10000.0 / total_qty), 2)
        pe_10k_sl = round(pe_entry + (10000.0 / total_qty), 2)

        intraday_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= exit_cutoff_time)]

        ce_active = True
        pe_active = True
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_reason = "TIME_EXIT"
        pe_exit_reason = "TIME_EXIT"

        for _, bar in intraday_bars.iterrows():
            curr_dt = bar["dt"]
            curr_dte = compute_fractional_dte(target_expiry, curr_dt)
            bar_high_spot = float(bar["high"])
            bar_low_spot = float(bar["low"])
            bar_close_spot = float(bar["close"])

            # -----------------------------------------------------------------
            # MODEL A: COMMITTED DUAL-LEG EXIT (Option 3 Canonical)
            # -----------------------------------------------------------------
            if exit_mode == "COMMITTED_DUAL":
                # 1. 5-min Portfolio Combined Max Loss Check (₹10,000 exit both legs simultaneously)
                if ce_active or pe_active:
                    ce_5m = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE") if ce_active else ce_exit_price
                    pe_5m = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE") if pe_active else pe_exit_price
                    current_loss = ((ce_5m - ce_entry) + (pe_5m - pe_entry)) * total_qty
                    if current_loss >= 10000.0:
                        if ce_active:
                            ce_active = False
                            ce_exit_price = round(ce_5m, 2)
                            ce_exit_reason = "STRATEGY_MAX_LOSS_10K"
                        if pe_active:
                            pe_active = False
                            pe_exit_price = round(pe_5m, 2)
                            pe_exit_reason = "STRATEGY_MAX_LOSS_10K"
                        break

                # 2. Dual-target close: if either hits 80% decay, BOTH exit
                if ce_active:
                    ce_min = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_min <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"
                        if pe_active:
                            pe_5m = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                            pe_active = False
                            pe_exit_price = round(pe_5m, 2)
                            pe_exit_reason = "CO_TARGET_EXIT"
                        break

                if pe_active:
                    pe_min = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_min <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"
                        if ce_active:
                            ce_5m = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                            ce_active = False
                            ce_exit_price = round(ce_5m, 2)
                            ce_exit_reason = "CO_TARGET_EXIT"
                        break

            # -----------------------------------------------------------------
            # MODEL B: SINGLE-LEG EXIT ON 50% SL (Indiv SL, Winning Leg Stays Open)
            # -----------------------------------------------------------------
            elif exit_mode == "SINGLE_LEG_50PCT_SL":
                # Evaluate CE
                if ce_active:
                    ce_max = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                    ce_min = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_max >= ce_sl:
                        ce_active = False
                        ce_exit_price = ce_sl
                        ce_exit_reason = "STOP_LOSS"
                    elif ce_min <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"

                # Evaluate PE
                if pe_active:
                    pe_max = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                    pe_min = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_max >= pe_sl:
                        pe_active = False
                        pe_exit_price = pe_sl
                        pe_exit_reason = "STOP_LOSS"
                    elif pe_min <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"

            # -----------------------------------------------------------------
            # MODEL C: SINGLE-LEG EXIT ON ₹10K PER-LEG MAX LOSS (Original Commit 295ba6a)
            # -----------------------------------------------------------------
            elif exit_mode == "SINGLE_LEG_10K_PER_LEG":
                # Effective stop is whichever is smaller: +50% SL or ₹10,000 leg loss
                eff_ce_sl = min(ce_sl, ce_10k_sl)
                eff_pe_sl = min(pe_sl, pe_10k_sl)

                if ce_active:
                    ce_max = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                    ce_min = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_max >= eff_ce_sl:
                        ce_active = False
                        ce_exit_price = eff_ce_sl
                        ce_exit_reason = "MAX_LOSS_10K" if eff_ce_sl == ce_10k_sl else "STOP_LOSS"
                    elif ce_min <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"

                if pe_active:
                    pe_max = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                    pe_min = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_max >= eff_pe_sl:
                        pe_active = False
                        pe_exit_price = eff_pe_sl
                        pe_exit_reason = "MAX_LOSS_10K" if eff_pe_sl == pe_10k_sl else "STOP_LOSS"
                    elif pe_min <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"

            # -----------------------------------------------------------------
            # MODEL D: SINGLE-LEG 50% SL + TRAIL OPPOSITE LEG TO BREAKEVEN / COST
            # -----------------------------------------------------------------
            elif exit_mode == "SINGLE_LEG_50PCT_TRAIL_BE":
                if ce_active:
                    ce_max = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                    ce_min = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_max >= ce_sl:
                        ce_active = False
                        ce_exit_price = ce_sl
                        ce_exit_reason = "STOP_LOSS"
                        # Trail opposite PE to cost
                        if pe_active:
                            pe_sl = pe_entry
                    elif ce_min <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"

                if pe_active:
                    pe_max = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                    pe_min = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_max >= pe_sl:
                        pe_active = False
                        pe_exit_price = pe_sl
                        pe_exit_reason = "STOP_LOSS"
                        # Trail opposite CE to cost
                        if ce_active:
                            ce_sl = ce_entry
                    elif pe_min <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"

            # -----------------------------------------------------------------
            # MODEL E: COMBINED 10K LOSS EXITS ONLY THE LOSING LEG
            # -----------------------------------------------------------------
            elif exit_mode == "SINGLE_LEG_ON_10K_COMBINED":
                # If combined loss hits 10k, square off ONLY the losing leg!
                if ce_active and pe_active:
                    ce_5m = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                    pe_5m = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                    current_loss = ((ce_5m - ce_entry) + (pe_5m - pe_entry)) * total_qty
                    if current_loss >= 10000.0:
                        ce_loss = (ce_5m - ce_entry) * total_qty
                        pe_loss = (pe_5m - pe_entry) * total_qty
                        if ce_loss > pe_loss:
                            ce_active = False
                            ce_exit_price = round(ce_5m, 2)
                            ce_exit_reason = "SINGLE_LEG_10K_EXIT"
                        else:
                            pe_active = False
                            pe_exit_price = round(pe_5m, 2)
                            pe_exit_reason = "SINGLE_LEG_10K_EXIT"

                # Individual leg decay target checks
                if ce_active:
                    ce_min = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_min <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"

                if pe_active:
                    pe_min = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_min <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"

            # -----------------------------------------------------------------
            # MODEL F: SINGLE-LEG 50% SL + DUAL-TARGET CLOSE (Locks profit on win)
            # -----------------------------------------------------------------
            elif exit_mode == "SINGLE_LEG_SL_CO_TARGET":
                # If a leg hits 50% SL: only that leg exits!
                if ce_active:
                    ce_max = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_max >= ce_sl:
                        ce_active = False
                        ce_exit_price = ce_sl
                        ce_exit_reason = "STOP_LOSS"

                if pe_active:
                    pe_max = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_max >= pe_sl:
                        pe_active = False
                        pe_exit_price = pe_sl
                        pe_exit_reason = "STOP_LOSS"

                # But if a leg hits 80% decay target: close BOTH legs to lock gains
                if ce_active:
                    ce_min = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_min <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"
                        if pe_active:
                            pe_5m = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                            pe_active = False
                            pe_exit_price = round(pe_5m, 2)
                            pe_exit_reason = "CO_TARGET_EXIT"
                        break

                if pe_active:
                    pe_min = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_min <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"
                        if ce_active:
                            ce_5m = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                            ce_active = False
                            ce_exit_price = round(ce_5m, 2)
                            ce_exit_reason = "CO_TARGET_EXIT"
                        break

            if not ce_active and not pe_active:
                break

        # Cutoff exit at 14:45 for any remaining active legs
        last_bar = intraday_bars.iloc[-1] if not intraday_bars.empty else entry_bar
        exit_dte = compute_fractional_dte(target_expiry, last_bar["dt"])
        if ce_active:
            ce_exit_price = round(black_76_price(float(last_bar["close"]), ce_strike, exit_dte, iv, r, "CE"), 2)
        if pe_active:
            pe_exit_price = round(black_76_price(float(last_bar["close"]), pe_strike, exit_dte, iv, r, "PE"), 2)

        # Compute accurate statutory taxes, exchange fees, slippage, and net PnL
        ce_cost = calculate_trade_costs(ce_entry, ce_exit_price, total_qty, slippage_per_share=slippage)
        pe_cost = calculate_trade_costs(pe_entry, pe_exit_price, total_qty, slippage_per_share=slippage)

        day_gross_pnl = round(ce_cost["gross_pnl"] + pe_cost["gross_pnl"], 2)
        day_taxes = round(ce_cost["statutory_taxes"] + pe_cost["statutory_taxes"], 2)
        day_slippage = round(ce_cost["total_slippage"] + pe_cost["total_slippage"], 2)
        day_net_pnl = round(ce_cost["net_pnl"] + pe_cost["net_pnl"], 2)

        trade_logs.append({
            "date": str(t_date),
            "spot_10am": spot_10am,
            "ce_strike": ce_strike,
            "ce_entry": ce_entry,
            "ce_exit": ce_exit_price,
            "ce_reason": ce_exit_reason,
            "ce_pnl": ce_cost["net_pnl"],
            "pe_strike": pe_strike,
            "pe_entry": pe_entry,
            "pe_exit": pe_exit_price,
            "pe_reason": pe_exit_reason,
            "pe_pnl": pe_cost["net_pnl"],
            "gross_pnl": day_gross_pnl,
            "taxes": day_taxes,
            "slippage": day_slippage,
            "net_pnl": day_net_pnl
        })

    trades_df = pd.DataFrame(trade_logs)
    total_days = len(trades_df)
    win_days = int((trades_df["net_pnl"] > 0).sum())
    loss_days = int((trades_df["net_pnl"] < 0).sum())
    win_rate = round((win_days / total_days) * 100.0, 1) if total_days else 0.0

    total_gross = round(float(trades_df["gross_pnl"].sum()), 2)
    total_net = round(float(trades_df["net_pnl"].sum()), 2)
    total_taxes = round(float(trades_df["taxes"].sum()), 2)
    total_slip = round(float(trades_df["slippage"].sum()), 2)

    gross_wins = float(trades_df[trades_df["gross_pnl"] > 0]["gross_pnl"].sum())
    gross_losses = abs(float(trades_df[trades_df["gross_pnl"] < 0]["gross_pnl"].sum()))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0

    # Drawdown & Peak
    cum_net = pd.concat([pd.Series([0.0]), trades_df["net_pnl"]]).cumsum()
    peak = cum_net.cummax()
    max_dd = round(float((cum_net - peak).min()), 2)

    # Consecutive losses calculation
    pnl_series = trades_df["net_pnl"].values
    max_consec_losses = 0
    curr_consec = 0
    for p in pnl_series:
        if p <= 0:
            curr_consec += 1
            if curr_consec > max_consec_losses:
                max_consec_losses = curr_consec
        else:
            curr_consec = 0

    # Severe loss days count (loss <= -Rs 8,000)
    severe_loss_days = int((trades_df["net_pnl"] <= -8000.0).sum())

    # Avg Win and Avg Loss
    avg_win = round(float(trades_df[trades_df["net_pnl"] > 0]["net_pnl"].mean()), 2) if win_days else 0.0
    avg_loss = round(float(trades_df[trades_df["net_pnl"] < 0]["net_pnl"].mean()), 2) if loss_days else 0.0

    trades_df["month"] = pd.to_datetime(trades_df["date"]).dt.strftime("%Y-%m")
    m_grp = trades_df.groupby("month")["net_pnl"].sum()
    profitable_months = int((m_grp > 0).sum())
    total_months = len(m_grp)
    worst_month = round(float(m_grp.min()), 2)
    worst_month_name = m_grp.idxmin()

    return {
        "model_name": model_name,
        "exit_mode": exit_mode,
        "days_traded": total_days,
        "win_days": win_days,
        "loss_days": loss_days,
        "win_rate": win_rate,
        "net_pnl": total_net,
        "gross_pnl": total_gross,
        "taxes": total_taxes,
        "slippage": total_slip,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_consec_losses": max_consec_losses,
        "severe_loss_days": severe_loss_days,
        "profitable_months": f"{profitable_months}/{total_months} ({profitable_months/total_months*100:.1f}%)",
        "worst_month": f"{worst_month_name} ({worst_month:+,.0f})",
        "trades_df": trades_df,
        "monthly_df": m_grp
    }


def main():
    print("=" * 115)
    print("   NIFTY 10:00 AM STRANGLE EXIT LOGIC AUDIT: COMMITTED DUAL-LEG vs SINGLE-LEG COMPARISON")
    print("       3-Year Historical Simulation (2023 - 2026) | 10 Lots (650 Qty) | Slippage: 0.50 pts")
    print("=" * 115)

    df_3y = pd.read_parquet("data/nifty_5m_3y.parquet")
    print(f"Loaded 3-Year Dataset: {len(df_3y)} 5m bars from {df_3y['date'].min()} to {df_3y['date'].max()}\n")

    models_to_test = [
        ("1. Committed Logic (Option 3 Dual-Leg Simultaneous Exit)", "COMMITTED_DUAL"),
        ("2. Single-Leg Exit (50% SL, Opposite Leg Stays Open)", "SINGLE_LEG_50PCT_SL"),
        ("3. Single-Leg Exit (Rs 10k Per-Leg Max Loss / SL)", "SINGLE_LEG_10K_PER_LEG"),
        ("4. Single-Leg Exit (50% SL + Trail Opposite Leg to Breakeven)", "SINGLE_LEG_50PCT_TRAIL_BE"),
        ("5. Single-Leg Exit on 10k Loss (Combined 10k exits ONLY losing leg)", "SINGLE_LEG_ON_10K_COMBINED"),
        ("6. Single-Leg Exit (50% SL on loss, Co-Target on 80% decay)", "SINGLE_LEG_SL_CO_TARGET")
    ]

    results_3y = []
    trade_dfs = {}
    monthly_dfs = {}

    for name, mode in models_to_test:
        print(f"Simulating {name}...")
        res = run_exit_model_simulation(df_3y, name, mode)
        trade_dfs[mode] = res.pop("trades_df")
        monthly_dfs[mode] = res.pop("monthly_df")
        results_3y.append(res)

    summary_df = pd.DataFrame(results_3y)
    cols_display = [
        "model_name", "win_rate", "net_pnl", "profit_factor", "max_drawdown",
        "avg_win", "avg_loss", "max_consec_losses", "severe_loss_days",
        "profitable_months", "worst_month"
    ]

    print("\n" + "=" * 115)
    print("                                3-YEAR COMPREHENSIVE PERFORMANCE MATRIX")
    print("=" * 115)
    print(tabulate(summary_df[cols_display], headers=[
        "Exit Model", "Win %", "Net P&L (₹)", "PF", "Max DD (₹)",
        "Avg Win (₹)", "Avg Loss (₹)", "Max Consec L", "Days ≤ -8k",
        "Win Months", "Worst Month"
    ], tablefmt="pipe", showindex=False))

    # Save comparison table
    summary_df.to_csv("data/strangle_exit_logic_comparison_3y.csv", index=False)

    # Now let's compare specifically on days where Committed Dual-Leg took a loss!
    committed_trades = trade_dfs["COMMITTED_DUAL"].set_index("date")
    committed_10k_loss_days = committed_trades[committed_trades["ce_reason"] == "STRATEGY_MAX_LOSS_10K"].index
    print("\n" + "=" * 115)
    print(f"   HEAD-TO-HEAD BEHAVIOR ON DAYS COMMITTED LOGIC HIT STRATEGY_MAX_LOSS_10K ({len(committed_10k_loss_days)} DAYS)")
    print("=" * 115)

    comp_loss_rows = []
    for mode in ["COMMITTED_DUAL", "SINGLE_LEG_50PCT_SL", "SINGLE_LEG_10K_PER_LEG", "SINGLE_LEG_50PCT_TRAIL_BE", "SINGLE_LEG_ON_10K_COMBINED"]:
        t_df = trade_dfs[mode].set_index("date")
        sub_pnls = t_df.loc[t_df.index.intersection(committed_10k_loss_days), "net_pnl"]
        comp_loss_rows.append({
            "Exit Model": mode,
            "Total PnL on 10k Days": round(sub_pnls.sum(), 2),
            "Avg PnL on 10k Days": round(sub_pnls.mean(), 2),
            "Green Days": int((sub_pnls > 0).sum()),
            "Loss Days": int((sub_pnls <= 0).sum()),
            "Win Rate on 10k Days (%)": round((sub_pnls > 0).sum() / len(sub_pnls) * 100, 1),
            "Worst Single Day (₹)": round(sub_pnls.min(), 2)
        })

    print(tabulate(pd.DataFrame(comp_loss_rows), headers="keys", tablefmt="pipe", showindex=False))

    # Also run on 1-Year dataset to see recent 1-year runway
    print("\n" + "=" * 115)
    print("   1-YEAR HISTORICAL RUNWAY (data/nifty_5m_1y.parquet)")
    print("=" * 115)
    df_1y = pd.read_parquet("data/nifty_5m_1y.parquet")
    results_1y = []
    for name, mode in models_to_test:
        res_1y = run_exit_model_simulation(df_1y, name, mode)
        res_1y.pop("trades_df")
        res_1y.pop("monthly_df")
        results_1y.append(res_1y)
    
    summary_1y_df = pd.DataFrame(results_1y)
    print(tabulate(summary_1y_df[cols_display], headers=[
        "Exit Model", "Win %", "Net P&L (₹)", "PF", "Max DD (₹)",
        "Avg Win (₹)", "Avg Loss (₹)", "Max Consec L", "Days ≤ -8k",
        "Win Months", "Worst Month"
    ], tablefmt="pipe", showindex=False))

    summary_1y_df.to_csv("data/strangle_exit_logic_comparison_1y.csv", index=False)


if __name__ == "__main__":
    main()
