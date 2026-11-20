import sys, os
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Tuple, Optional
import numpy as np
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))
from strategy.options_model import compute_fractional_dte, black_76_price
from strategy.costs import calculate_trade_costs
from strategy.backtest import get_weekly_expiry_for_date, select_strike_above_target


def calculate_daily_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Computes Daily ATR on Nifty 5m bars aggregated to daily."""
    df_copy = df.copy()
    if "date" in df_copy.columns:
        df_copy["dt"] = pd.to_datetime(df_copy["date"])
    else:
        df_copy["dt"] = pd.to_datetime(df_copy["timestamps"])
    df_copy["trade_date"] = df_copy["dt"].dt.date
    
    daily = df_copy.groupby("trade_date").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).sort_index()
    
    prev_close = daily["close"].shift(1)
    tr1 = daily["high"] - daily["low"]
    tr2 = (daily["high"] - prev_close).abs()
    tr3 = (daily["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=1).mean()
    return atr


def run_variant_simulation(
    bars_df: pd.DataFrame,
    variant_name: str,
    # Parameters
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
    strategy_max_loss: Optional[float] = 10000.0,
    use_leg_sl: bool = False,
    trail_opposite_to_cost: bool = False,
    morning_filter: bool = False,
    morning_filter_threshold_pct: float = 0.60,
    use_atr_strikes: bool = False,
    atr_multiplier: float = 1.25,
    use_iron_condor: bool = False,
    wing_offset_pts: float = 200.0,
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

    # Pre-calculate daily ATR
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
        morning_range_pct = (morning_range / spot_open_915) * 100.0

        # Check Morning Filter if enabled
        if morning_filter:
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

        # Strike Selection
        if use_atr_strikes:
            current_atr = daily_atr.get(t_date, 150.0)
            if pd.isna(current_atr) or current_atr <= 0:
                current_atr = 150.0
            strike_offset = max(current_atr * atr_multiplier, 150.0)
            ce_strike = round((spot_10am + strike_offset) / 50.0) * 50.0
            pe_strike = round((spot_10am - strike_offset) / 50.0) * 50.0
            ce_entry = black_76_price(spot_10am, ce_strike, entry_dte, iv, r, "CE")
            pe_entry = black_76_price(spot_10am, pe_strike, entry_dte, iv, r, "PE")
        else:
            # Baseline: First strike >= 30.0
            ce_strike, ce_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "CE", 30.0, r)
            pe_strike, pe_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "PE", 30.0, r)
            ce_entry = ce_theo_price
            pe_entry = pe_theo_price

        # Wings for Iron Condor (if enabled)
        wing_cost_net = 0.0
        if use_iron_condor:
            ce_wing_strike = ce_strike + wing_offset_pts
            pe_wing_strike = pe_strike - wing_offset_pts
            ce_wing_entry = black_76_price(spot_10am, ce_wing_strike, entry_dte, iv, r, "CE")
            pe_wing_entry = black_76_price(spot_10am, pe_wing_strike, entry_dte, iv, r, "PE")

        ce_sl = round(ce_entry * 1.50, 2)
        ce_target = round(ce_entry * 0.20, 2)
        pe_sl = round(pe_entry * 1.50, 2)
        pe_target = round(pe_entry * 0.20, 2)

        intraday_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= exit_cutoff_time)]

        ce_active = True
        pe_active = True
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_reason = "TIME_EXIT"
        pe_exit_reason = "TIME_EXIT"

        effective_max_loss = strategy_max_loss if strategy_max_loss is not None else 10000.0

        for _, bar in intraday_bars.iterrows():
            curr_dt = bar["dt"]
            curr_dte = compute_fractional_dte(target_expiry, curr_dt)
            bar_high_spot = float(bar["high"])
            bar_low_spot = float(bar["low"])
            bar_close_spot = float(bar["close"])

            # 1. Strategy-Level Combined Max Loss Check
            if strategy_max_loss is not None and (ce_active or pe_active):
                ce_5m_price = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE") if ce_active else ce_exit_price
                pe_5m_price = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE") if pe_active else pe_exit_price
                current_loss = ((ce_5m_price - ce_entry) + (pe_5m_price - pe_entry)) * total_qty

                if current_loss >= effective_max_loss:
                    if ce_active:
                        ce_active = False
                        ce_exit_price = round(ce_5m_price, 2)
                        ce_exit_reason = "STRATEGY_MAX_LOSS_10K"
                    if pe_active:
                        pe_active = False
                        pe_exit_price = round(pe_5m_price, 2)
                        pe_exit_reason = "STRATEGY_MAX_LOSS_10K"
                    break

            # 2. Evaluate individual CE leg
            if ce_active:
                if use_leg_sl:
                    ce_max_price = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_max_price >= ce_sl:
                        ce_active = False
                        ce_exit_price = ce_sl
                        ce_exit_reason = "STOP_LOSS"
                        # Trail opposite PE leg to Breakeven/Cost if enabled
                        if trail_opposite_to_cost and pe_active:
                            pe_sl = pe_entry

                if ce_active:
                    ce_min_price = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                    if ce_min_price <= ce_target:
                        ce_active = False
                        ce_exit_price = ce_target
                        ce_exit_reason = "DECAY_TARGET"
                        if not use_leg_sl and pe_active:
                            pe_5m_price = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                            pe_active = False
                            pe_exit_price = round(pe_5m_price, 2)
                            pe_exit_reason = "CO_TARGET_EXIT"

            # 3. Evaluate individual PE leg
            if pe_active:
                if use_leg_sl:
                    pe_max_price = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_max_price >= pe_sl:
                        pe_active = False
                        pe_exit_price = pe_sl
                        pe_exit_reason = "STOP_LOSS"
                        # Trail opposite CE leg to Breakeven/Cost if enabled
                        if trail_opposite_to_cost and ce_active:
                            ce_sl = ce_entry

                if pe_active:
                    pe_min_price = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                    if pe_min_price <= pe_target:
                        pe_active = False
                        pe_exit_price = pe_target
                        pe_exit_reason = "DECAY_TARGET"
                        if not use_leg_sl and ce_active:
                            ce_5m_price = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                            ce_active = False
                            ce_exit_price = round(ce_5m_price, 2)
                            ce_exit_reason = "CO_TARGET_EXIT"

            if not ce_active and not pe_active:
                break

        # Cutoff exit at 14:45 if still active
        last_bar = intraday_bars.iloc[-1] if not intraday_bars.empty else entry_bar
        exit_dte = compute_fractional_dte(target_expiry, last_bar["dt"])
        if ce_active:
            ce_exit_price = round(black_76_price(float(last_bar["close"]), ce_strike, exit_dte, iv, r, "CE"), 2)
        if pe_active:
            pe_exit_price = round(black_76_price(float(last_bar["close"]), pe_strike, exit_dte, iv, r, "PE"), 2)

        # Compute costs for short legs
        ce_cost = calculate_trade_costs(ce_entry, ce_exit_price, total_qty, slippage_per_share=slippage)
        pe_cost = calculate_trade_costs(pe_entry, pe_exit_price, total_qty, slippage_per_share=slippage)

        day_gross_pnl = round(ce_cost["gross_pnl"] + pe_cost["gross_pnl"], 2)
        day_taxes = round(ce_cost["statutory_taxes"] + pe_cost["statutory_taxes"], 2)
        day_slippage = round(ce_cost["total_slippage"] + pe_cost["total_slippage"], 2)
        day_net_pnl = round(ce_cost["net_pnl"] + pe_cost["net_pnl"], 2)

        # Condor wing accounting if enabled (long options held to end of day)
        if use_iron_condor:
            ce_wing_exit = round(black_76_price(float(last_bar["close"]), ce_wing_strike, exit_dte, iv, r, "CE"), 2)
            pe_wing_exit = round(black_76_price(float(last_bar["close"]), pe_wing_strike, exit_dte, iv, r, "PE"), 2)
            # Long pnl = exit - entry
            wing_gross = ((ce_wing_exit - ce_wing_entry) + (pe_wing_exit - pe_wing_entry)) * total_qty
            wing_slip = 2 * (slippage * total_qty)
            wing_tax = 100.0  # approximate broker/STT on wings
            wing_net = wing_gross - wing_slip - wing_tax
            day_gross_pnl += round(wing_gross, 2)
            day_slippage += round(wing_slip, 2)
            day_taxes += round(wing_tax, 2)
            day_net_pnl += round(wing_net, 2)

        trade_logs.append({
            "date": str(t_date),
            "spot_10am": spot_10am,
            "ce_strike": ce_strike,
            "ce_entry": ce_entry,
            "ce_exit": ce_exit_price,
            "ce_reason": ce_exit_reason,
            "pe_strike": pe_strike,
            "pe_entry": pe_entry,
            "pe_exit": pe_exit_price,
            "pe_reason": pe_exit_reason,
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

    cum_net = pd.concat([pd.Series([0.0]), trades_df["net_pnl"]]).cumsum()
    peak = cum_net.cummax()
    max_dd = round(float((cum_net - peak).min()), 2)

    trades_df["month"] = pd.to_datetime(trades_df["date"]).dt.strftime("%Y-%m")
    m_grp = trades_df.groupby("month")["net_pnl"].sum()
    worst_month = round(float(m_grp.min()), 2)
    worst_month_name = m_grp.idxmin()
    oct_2026_pnl = round(float(m_grp.get("2026-10", 0.0)), 2)
    mar_2026_pnl = round(float(m_grp.get("2026-03", 0.0)), 2)

    return {
        "variant": variant_name,
        "days_traded": total_days,
        "skipped_days": skipped_days,
        "win_rate": win_rate,
        "net_pnl": total_net,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "avg_daily_net": round(total_net / total_days, 2) if total_days else 0.0,
        "worst_month": f"{worst_month_name} ({worst_month:,.0f})",
        "oct_2026_pnl": oct_2026_pnl,
        "mar_2026_pnl": mar_2026_pnl,
        "trades_df": trades_df
    }


def main():
    print("=" * 115)
    print("    NIFTY 10:00 AM SHORT STRANGLE: BATTLE OF THE ENHANCEMENTS (3-YEAR BACKTEST: 2023 - 2026)")
    print("    10 Lots (650 Qty) | Slippage: 0.50 pts/share | Brokerage & Taxes Included")
    print("=" * 115)

    df = pd.read_parquet("data/nifty_5m_3y.parquet")
    print(f"Loaded {len(df)} 5m bars from {df['date'].min()} to {df['date'].max()}\n")

    variants = [
        {
            "name": "0. Baseline (10k Combined SL, Co-Target Exit)",
            "use_leg_sl": False,
            "trail_opposite_to_cost": False,
            "morning_filter": False,
            "use_atr_strikes": False,
            "use_iron_condor": False
        },
        {
            "name": "1. Baseline + 50% Leg SL (Indiv Leg Stops)",
            "use_leg_sl": True,
            "trail_opposite_to_cost": False,
            "morning_filter": False,
            "use_atr_strikes": False,
            "use_iron_condor": False
        },
        {
            "name": "2. Leg SL + Trailing Opposite Leg to Cost",
            "use_leg_sl": True,
            "trail_opposite_to_cost": True,
            "morning_filter": False,
            "use_atr_strikes": False,
            "use_iron_condor": False
        },
        {
            "name": "3. Morning Range Trend Filter (>0.6% Range Skip)",
            "use_leg_sl": False,
            "trail_opposite_to_cost": False,
            "morning_filter": True,
            "morning_filter_threshold_pct": 0.60,
            "use_atr_strikes": False,
            "use_iron_condor": False
        },
        {
            "name": "4. Dynamic ATR Strikes (1.25x ATR OTM Distance)",
            "use_leg_sl": False,
            "trail_opposite_to_cost": False,
            "morning_filter": False,
            "use_atr_strikes": True,
            "atr_multiplier": 1.25,
            "use_iron_condor": False
        },
        {
            "name": "5. Defined-Risk Iron Condor (+200pt OTM Wings)",
            "use_leg_sl": False,
            "trail_opposite_to_cost": False,
            "morning_filter": False,
            "use_atr_strikes": False,
            "use_iron_condor": True,
            "wing_offset_pts": 200.0
        },
        {
            "name": "6. Smart Strangle Pro (Trend Filter + Trailing to Cost)",
            "use_leg_sl": True,
            "trail_opposite_to_cost": True,
            "morning_filter": True,
            "morning_filter_threshold_pct": 0.60,
            "use_atr_strikes": False,
            "use_iron_condor": False
        }
    ]

    results = []
    trades_dict = {}

    for var in variants:
        print(f"Running simulation for: {var['name']}...")
        r = run_variant_simulation(
            df,
            variant_name=var["name"],
            use_leg_sl=var["use_leg_sl"],
            trail_opposite_to_cost=var["trail_opposite_to_cost"],
            morning_filter=var["morning_filter"],
            morning_filter_threshold_pct=var.get("morning_filter_threshold_pct", 0.60),
            use_atr_strikes=var["use_atr_strikes"],
            atr_multiplier=var.get("atr_multiplier", 1.25),
            use_iron_condor=var["use_iron_condor"],
            wing_offset_pts=var.get("wing_offset_pts", 200.0),
        )
        trades_dict[var["name"]] = r["trades_df"]
        results.append({
            "Variant": r["variant"],
            "Days Traded": r["days_traded"],
            "Skipped Days": r["skipped_days"],
            "Win Rate (%)": f"{r['win_rate']:.1f}%",
            "Net PnL (₹)": f"₹{r['net_pnl']:,.2f}",
            "Profit Factor": f"{r['profit_factor']:.2f}",
            "Max Drawdown (₹)": f"₹{r['max_drawdown']:,.2f}",
            "Avg Daily Net (₹)": f"₹{r['avg_daily_net']:,.2f}",
            "Worst Month": r["worst_month"],
            "Oct 2026 PnL": f"₹{r['oct_2026_pnl']:,.2f}",
            "Mar 2026 PnL": f"₹{r['mar_2026_pnl']:,.2f}"
        })

    res_df = pd.DataFrame(results)
    print("\n" + "=" * 135)
    print("                             HEAD-TO-HEAD COMPARISON: STRANGLE ENHANCEMENTS VS BASELINE")
    print("=" * 135)
    print(tabulate(res_df, headers="keys", tablefmt="grid", showindex=False))

    # Save summary table to CSV
    res_df.to_csv("data/strangle_enhancements_comparison.csv", index=False)
    print("\nSaved detailed comparison to data/strangle_enhancements_comparison.csv")


if __name__ == "__main__":
    main()
