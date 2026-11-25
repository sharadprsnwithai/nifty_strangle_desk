import math
import os
import sys
from datetime import time, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))
from strategy.options_model import compute_fractional_dte
from strategy.costs import calculate_trade_costs
from strategy.backtest import get_weekly_expiry_for_date, run_backtest_simulation

SQRT2 = math.sqrt(2.0)


def fast_norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def fast_black_76(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float = 0.13,
    r: float = 0.065,
    opt_type: str = "CE",
) -> float:
    t = max(dte_days / 252.0, 1e-5)
    sigma = max(iv, 0.01)
    d = math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    denom = sigma * sqrt_t
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / denom
    d2 = d1 - denom
    if opt_type == "CE":
        price = d * (spot * fast_norm_cdf(d1) - strike * fast_norm_cdf(d2))
    else:
        price = d * (strike * fast_norm_cdf(-d2) - spot * fast_norm_cdf(-d1))
    return max(0.05, round(price, 2))


def simulate_vwap_straddle_day(
    day_df: pd.DataFrame,
    t_date: Any,
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
    max_loss: float = 10000.0,
    max_profit: float = 20000.0,
    vwap_anchor: str = "09:15",  # "09:15" or "10:00"
    entry_mode: str = "WAIT_BELOW_VWAP",  # "WAIT_BELOW_VWAP", "AT_10AM_IF_BELOW", "WAIT_CROSS_BELOW"
    exit_above_vwap: bool = False,
    last_entry_time: time = time(13, 30),
    exit_cutoff_time: time = time(14, 45),
) -> Optional[Dict[str, Any]]:
    """
    Simulates an ATM Short Straddle using intraday combined-premium VWAP (TWAP proxy on spot bars):
    - Identifies 10:00 AM ATM strike
    - Computes running intraday average (TWAP proxy for VWAP) of combined ATM straddle price (CE + PE)
    - Enters short ATM straddle when combined price goes below VWAP
    - Exits both legs on Rs 10,000 max loss, Rs 20,000 max profit, optional cross back above VWAP, or 14:45 cutoff
    """
    entry_bars = day_df[(day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute <= 5)]
    if entry_bars.empty:
        entry_bars = day_df[(day_df["dt"].dt.hour == 9) & (day_df["dt"].dt.minute >= 55)]
    if entry_bars.empty:
        return None

    bar_10am = entry_bars.iloc[0]
    time_10am = bar_10am["dt"]
    spot_10am = float(bar_10am["open"])
    atm_strike = round(spot_10am / 50.0) * 50.0

    target_expiry, _ = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)
    total_qty = lots * lot_size

    # Pre-compute combined straddle price and running TWAP (VWAP proxy) bar-by-bar
    anchor_time = time(9, 15) if vwap_anchor == "09:15" else time_10am.time()
    session_bars = day_df[(day_df["dt"].dt.time >= anchor_time) & (day_df["dt"].dt.time <= exit_cutoff_time)].copy()
    if session_bars.empty:
        return None

    cum_sum = 0.0
    cum_cnt = 0
    prev_below = False

    entered = False
    ce_entry = 0.0
    pe_entry = 0.0
    comb_entry = 0.0
    ce_exit = 0.0
    pe_exit = 0.0
    exit_reason = "TIME_EXIT"
    entry_dt = None

    for _, bar in session_bars.iterrows():
        curr_dt = bar["dt"]
        curr_t = curr_dt.time()
        curr_dte = compute_fractional_dte(target_expiry, curr_dt)

        b_high = float(bar["high"])
        b_low = float(bar["low"])
        b_close = float(bar["close"])

        ce_c = fast_black_76(b_close, atm_strike, curr_dte, iv, r, "CE")
        pe_c = fast_black_76(b_close, atm_strike, curr_dte, iv, r, "PE")
        comb_c = ce_c + pe_c

        ce_h = fast_black_76(b_high, atm_strike, curr_dte, iv, r, "CE")
        pe_h = fast_black_76(b_high, atm_strike, curr_dte, iv, r, "PE")
        ce_l = fast_black_76(b_low, atm_strike, curr_dte, iv, r, "CE")
        pe_l = fast_black_76(b_low, atm_strike, curr_dte, iv, r, "PE")

        comb_at_high = ce_h + pe_h
        comb_at_low = ce_l + pe_l
        # Typical combined price for running VWAP proxy
        comb_typical = (comb_at_high + comb_at_low + comb_c) / 3.0
        cum_sum += comb_typical
        cum_cnt += 1
        vwap_val = cum_sum / cum_cnt
        is_below = comb_c < vwap_val

        # Manage open position
        if entered:
            comb_worst = max(comb_at_high, comb_at_low, comb_c)
            comb_best = min(comb_at_high, comb_at_low, comb_c)

            if ((comb_worst - comb_entry) * total_qty) >= max_loss:
                if comb_at_high >= comb_at_low and comb_at_high >= comb_c:
                    ce_exit, pe_exit = ce_h, pe_h
                elif comb_at_low >= comb_c:
                    ce_exit, pe_exit = ce_l, pe_l
                else:
                    ce_exit, pe_exit = ce_c, pe_c
                exit_reason = "MAX_LOSS_10K"
                break

            if ((comb_entry - comb_best) * total_qty) >= max_profit:
                if comb_at_high <= comb_at_low and comb_at_high <= comb_c:
                    ce_exit, pe_exit = ce_h, pe_h
                elif comb_at_low <= comb_c:
                    ce_exit, pe_exit = ce_l, pe_l
                else:
                    ce_exit, pe_exit = ce_c, pe_c
                exit_reason = "MAX_PROFIT_20K"
                break

            if exit_above_vwap and comb_c > vwap_val:
                ce_exit, pe_exit = ce_c, pe_c
                exit_reason = "ABOVE_VWAP_EXIT"
                break

            # Keep last mark-to-market in case of TIME_EXIT
            ce_exit, pe_exit = ce_c, pe_c

        # Evaluate entry at or after 10:00 AM
        elif curr_dt >= time_10am and curr_t <= last_entry_time:
            should_enter = False
            if entry_mode == "AT_10AM_IF_BELOW":
                if curr_dt == time_10am and is_below:
                    should_enter = True
            elif entry_mode == "WAIT_BELOW_VWAP":
                if is_below:
                    should_enter = True
            elif entry_mode == "WAIT_CROSS_BELOW":
                if is_below and not prev_below:
                    should_enter = True

            if should_enter:
                entered = True
                entry_dt = curr_dt
                ce_entry, pe_entry = ce_c, pe_c
                comb_entry = ce_entry + pe_entry
                ce_exit, pe_exit = ce_c, pe_c

        prev_below = is_below

    if not entered:
        return None

    ce_cost = calculate_trade_costs(ce_entry, ce_exit, total_qty, slippage_per_share=slippage)
    pe_cost = calculate_trade_costs(pe_entry, pe_exit, total_qty, slippage_per_share=slippage)

    return {
        "date": str(t_date),
        "entry_time": entry_dt.strftime("%H:%M") if entry_dt else "",
        "atm_strike": atm_strike,
        "comb_entry": round(comb_entry, 2),
        "exit_reason": exit_reason,
        "gross_pnl": round(ce_cost["gross_pnl"] + pe_cost["gross_pnl"], 2),
        "taxes": round(ce_cost["statutory_taxes"] + pe_cost["statutory_taxes"], 2),
        "slippage": round(ce_cost["total_slippage"] + pe_cost["total_slippage"], 2),
        "net_pnl": round(ce_cost["net_pnl"] + pe_cost["net_pnl"], 2),
    }


def summarize_trades(trades_df: pd.DataFrame) -> Dict[str, Any]:
    if trades_df.empty:
        return {
            "days": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_pnl": 0.0, "net_pnl": 0.0, "taxes": 0.0, "slippage": 0.0,
            "pf": 0.0, "max_dd": 0.0, "avg_daily": 0.0, "worst_month": "N/A",
        }
    df = trades_df.sort_values("date").reset_index(drop=True)
    days = len(df)
    wins = int((df["net_pnl"] > 0).sum())
    win_rate = round((wins / days) * 100.0, 1)
    gross_pnl = round(float(df["gross_pnl"].sum()), 2)
    net_pnl = round(float(df["net_pnl"].sum()), 2)
    gw = float(df[df["gross_pnl"] > 0]["gross_pnl"].sum())
    gl = abs(float(df[df["gross_pnl"] < 0]["gross_pnl"].sum()))
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    cum = pd.concat([pd.Series([0.0]), df["net_pnl"]]).cumsum()
    max_dd = round(float((cum - cum.cummax()).min()), 2)
    df["month"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    m_net = df.groupby("month")["net_pnl"].sum()
    return {
        "days": days,
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "pf": pf,
        "max_dd": max_dd,
        "avg_daily": round(net_pnl / days, 2),
        "worst_month": f"{m_net.idxmin()} ({m_net.min():+,.0f})",
    }


def run_period(bars_df: pd.DataFrame, period_label: str) -> None:
    df = bars_df.copy()
    df["dt"] = pd.to_datetime(df["date"])
    df["trade_date"] = df["dt"].dt.date

    base_res = run_backtest_simulation(
        df, slippage=0.50, lots=10, lot_size=65, iv=0.13, r=0.065,
        strategy_max_loss=10000.0, use_leg_sl=True, trail_opposite_to_cost=True,
        morning_filter=True, morning_filter_threshold_pct=0.60,
        use_atr_strikes=True, atr_multiplier=1.25,
    )
    base_df = pd.DataFrame(base_res["trades"])
    base_dates = set(base_df["date"].tolist()) if not base_df.empty else set()
    base_sum = summarize_trades(base_df)

    all_dates = sorted(df["trade_date"].unique())
    skipped_days = []
    for t_date in all_dates:
        if str(t_date) in base_dates:
            continue
        day_df = df[df["trade_date"] == t_date].sort_values("dt")
        mb = day_df[(day_df["dt"].dt.hour == 9) | ((day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute == 0))]
        if mb.empty:
            continue
        m_open = float(mb.iloc[0]["open"])
        m_range_pct = ((float(mb["high"].max()) - float(mb["low"].min())) / m_open) * 100.0 if m_open > 0 else 0.0
        if m_range_pct >= 0.60:
            skipped_days.append((t_date, day_df))

    variants = [
        ("1. Wait Comb < VWAP (09:15 anchor) | Hold SL10k/Tgt20k", dict(
            vwap_anchor="09:15", entry_mode="WAIT_BELOW_VWAP", exit_above_vwap=False
        )),
        ("2. Wait Comb < VWAP (09:15 anchor) + Exit > VWAP | SL10k/Tgt20k", dict(
            vwap_anchor="09:15", entry_mode="WAIT_BELOW_VWAP", exit_above_vwap=True
        )),
        ("3. Wait Comb < VWAP (10:00 anchor) | Hold SL10k/Tgt20k", dict(
            vwap_anchor="10:00", entry_mode="WAIT_BELOW_VWAP", exit_above_vwap=False
        )),
        ("4. Wait Comb < VWAP (10:00 anchor) + Exit > VWAP | SL10k/Tgt20k", dict(
            vwap_anchor="10:00", entry_mode="WAIT_BELOW_VWAP", exit_above_vwap=True
        )),
        ("5. At 10:00 Only If Comb < VWAP (09:15) | Hold SL10k/Tgt20k", dict(
            vwap_anchor="09:15", entry_mode="AT_10AM_IF_BELOW", exit_above_vwap=False
        )),
        ("6. Wait Cross Below VWAP (09:15 anchor) | Hold SL10k/Tgt20k", dict(
            vwap_anchor="09:15", entry_mode="WAIT_CROSS_BELOW", exit_above_vwap=False
        )),
    ]

    standalone_rows = []
    combined_rows = [[
        "BASELINE (Strangle + Morning Filter Skip)",
        base_sum["days"], f"{base_sum['win_rate']}%",
        f"Rs {base_sum['net_pnl']:+,.0f}", base_sum["pf"],
        f"Rs {base_sum['max_dd']:+,.0f}", f"Rs {base_sum['avg_daily']:+,.0f}",
        base_sum["worst_month"], "—"
    ]]

    for vname, kw in variants:
        trades = []
        for t_date, day_df in skipped_days:
            r = simulate_vwap_straddle_day(day_df, t_date, **kw)
            if r is not None:
                trades.append(r)
        tdf = pd.DataFrame(trades)
        s = summarize_trades(tdf)
        exits = tdf["exit_reason"].value_counts().to_dict() if not tdf.empty else {}
        standalone_rows.append([
            vname, s["days"], f"{s['win_rate']}%",
            f"Rs {s['gross_pnl']:+,.0f}", f"Rs {s['net_pnl']:+,.0f}",
            s["pf"], f"Rs {s['max_dd']:+,.0f}", f"Rs {s['avg_daily']:+,.0f}",
            str(exits),
        ])

        comb_df = pd.concat([
            base_df[["date", "gross_pnl", "taxes", "slippage", "net_pnl"]],
            tdf[["date", "gross_pnl", "taxes", "slippage", "net_pnl"]] if not tdf.empty else pd.DataFrame(),
        ], ignore_index=True)
        cs = summarize_trades(comb_df)
        combined_rows.append([
            f"Baseline + {vname}",
            cs["days"], f"{cs['win_rate']}%",
            f"Rs {cs['net_pnl']:+,.0f}", cs["pf"],
            f"Rs {cs['max_dd']:+,.0f}", f"Rs {cs['avg_daily']:+,.0f}",
            cs["worst_month"], f"Rs {cs['net_pnl'] - base_sum['net_pnl']:+,.0f}",
        ])

    print("\n" + "=" * 130)
    print(f"  {period_label} | Triggered (>=0.60% Morning Range) Days: {len(skipped_days)}")
    print("=" * 130)
    print("\n[1] STANDALONE VWAP ATM STRADDLE (MAX LOSS 10K / MAX PROFIT 20K) ON TRIGGERED DAYS:")
    print(tabulate(
        standalone_rows,
        headers=["Variant", "Trades", "Win%", "Gross PnL", "Net PnL", "PF", "Max DD", "Avg/Trade", "Exit Breakdown"],
        tablefmt="pipe",
    ))
    print("\n[2] COMBINED PORTFOLIO (BASELINE STRANGLE + VWAP STRADDLE FALLBACK) VS BASELINE:")
    print(tabulate(
        combined_rows,
        headers=["Portfolio Variant", "Days", "Win%", "Net PnL", "PF", "Max DD", "Avg/Day", "Worst Month", "Delta vs Base"],
        tablefmt="pipe",
    ))


def main():
    df3 = pd.read_parquet("data/nifty_5m_3y.parquet")
    df3["dt"] = pd.to_datetime(df3["date"])
    cutoff_1y = df3["dt"].max() - timedelta(days=365)
    df1 = df3[df3["dt"] >= cutoff_1y].copy()

    print(f"1-Year Window: {df1['dt'].min().date()} to {df1['dt'].max().date()}")
    print(f"3-Year Window: {df3['dt'].min().date()} to {df3['dt'].max().date()}")

    run_period(df1, "LAST 1 YEAR")
    run_period(df3, "LAST 3 YEARS")


if __name__ == "__main__":
    main()
