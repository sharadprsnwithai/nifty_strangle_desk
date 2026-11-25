import math
import os
import sys
from datetime import time, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))
from strategy.options_model import compute_fractional_dte, black_76_price as slow_black_76
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


def _solve_spot_for_price(
    low_spot: float,
    high_spot: float,
    target_price: float,
    strike: float,
    dte_days: float,
    iv: float,
    r: float,
    opt_type: str,
) -> float:
    """Bisects [low_spot, high_spot] to find the spot where option price == target_price."""
    lo, hi = low_spot, high_spot
    for _ in range(25):
        mid = 0.5 * (lo + hi)
        p = fast_black_76(mid, strike, dte_days, iv, r, opt_type)
        if opt_type == "CE":
            if p < target_price:
                lo = mid
            else:
                hi = mid
        else:
            if p > target_price:
                lo = mid
            else:
                hi = mid
    return 0.5 * (lo + hi)


def simulate_atm_straddle_day(
    day_df: pd.DataFrame,
    t_date: Any,
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
    sl_pct: float = 0.10,
    target_pct: float = 0.20,
    trigger_mode: str = "COMBINED_INTRABAR",  # COMBINED_INTRABAR, COMBINED_5M_CLOSE, PER_LEG_COEXIT
    max_loss_cap: Optional[float] = None,
    chop_filter: str = "ALL",  # ALL, DIRECTIONAL_ONLY (<=0.35 or >=0.65), CHOP_ONLY (0.35..0.65)
    exit_cutoff_time: time = time(14, 45),
) -> Optional[Dict[str, Any]]:
    morning_bars = day_df[
        (day_df["dt"].dt.hour == 9) | ((day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute == 0))
    ]
    if morning_bars.empty:
        return None

    m_high = float(morning_bars["high"].max())
    m_low = float(morning_bars["low"].min())
    m_range = m_high - m_low
    if m_range <= 0:
        return None

    entry_bars = day_df[(day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute <= 5)]
    if entry_bars.empty:
        entry_bars = day_df[(day_df["dt"].dt.hour == 9) & (day_df["dt"].dt.minute >= 55)]
    if entry_bars.empty:
        return None

    entry_bar = entry_bars.iloc[0]
    entry_time = entry_bar["dt"]
    spot_10am = float(entry_bar["open"])
    range_pos = (spot_10am - m_low) / m_range

    if chop_filter == "DIRECTIONAL_ONLY" and (0.35 < range_pos < 0.65):
        return None
    if chop_filter == "CHOP_ONLY" and not (0.35 < range_pos < 0.65):
        return None

    target_expiry, _ = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)
    entry_dte = compute_fractional_dte(target_expiry, entry_time)

    atm_strike = round(spot_10am / 50.0) * 50.0
    ce_entry = fast_black_76(spot_10am, atm_strike, entry_dte, iv, r, "CE")
    pe_entry = fast_black_76(spot_10am, atm_strike, entry_dte, iv, r, "PE")
    comb_entry = ce_entry + pe_entry

    comb_sl = comb_entry * (1.0 + sl_pct)
    comb_tgt = comb_entry * (1.0 - target_pct)
    ce_leg_sl = ce_entry * (1.0 + sl_pct)
    pe_leg_sl = pe_entry * (1.0 + sl_pct)
    ce_leg_tgt = ce_entry * (1.0 - target_pct)
    pe_leg_tgt = pe_entry * (1.0 - target_pct)

    total_qty = lots * lot_size
    intraday_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= exit_cutoff_time)]

    ce_exit = ce_entry
    pe_exit = pe_entry
    exit_reason = "TIME_EXIT"

    for _, bar in intraday_bars.iterrows():
        curr_dt = bar["dt"]
        curr_dte = compute_fractional_dte(target_expiry, curr_dt)
        b_high = float(bar["high"])
        b_low = float(bar["low"])
        b_close = float(bar["close"])

        ce_c = fast_black_76(b_close, atm_strike, curr_dte, iv, r, "CE")
        pe_c = fast_black_76(b_close, atm_strike, curr_dte, iv, r, "PE")

        if trigger_mode == "COMBINED_INTRABAR":
            ce_h = fast_black_76(b_high, atm_strike, curr_dte, iv, r, "CE")
            pe_h = fast_black_76(b_high, atm_strike, curr_dte, iv, r, "PE")
            ce_l = fast_black_76(b_low, atm_strike, curr_dte, iv, r, "CE")
            pe_l = fast_black_76(b_low, atm_strike, curr_dte, iv, r, "PE")

            comb_at_high = ce_h + pe_h
            comb_at_low = ce_l + pe_l
            comb_worst = max(comb_at_high, comb_at_low, ce_c + pe_c)
            comb_best = min(comb_at_high, comb_at_low, ce_c + pe_c)

            if max_loss_cap is not None and ((comb_worst - comb_entry) * total_qty) >= max_loss_cap:
                if comb_at_high >= comb_at_low:
                    ce_exit, pe_exit = ce_h, pe_h
                else:
                    ce_exit, pe_exit = ce_l, pe_l
                exit_reason = "MAX_LOSS_CAP"
                break

            if comb_worst >= comb_sl:
                if comb_at_high >= comb_at_low:
                    ce_exit, pe_exit = ce_h, pe_h
                else:
                    ce_exit, pe_exit = ce_l, pe_l
                exit_reason = "COMBINED_SL_10PCT"
                break

            if comb_best <= comb_tgt:
                if comb_at_high <= comb_at_low:
                    ce_exit, pe_exit = ce_h, pe_h
                else:
                    ce_exit, pe_exit = ce_l, pe_l
                exit_reason = "COMBINED_TGT_20PCT"
                break

        elif trigger_mode == "COMBINED_5M_CLOSE":
            comb_c = ce_c + pe_c
            if max_loss_cap is not None and ((comb_c - comb_entry) * total_qty) >= max_loss_cap:
                ce_exit, pe_exit = ce_c, pe_c
                exit_reason = "MAX_LOSS_CAP"
                break
            if comb_c >= comb_sl:
                ce_exit, pe_exit = ce_c, pe_c
                exit_reason = "COMBINED_SL_10PCT"
                break
            if comb_c <= comb_tgt:
                ce_exit, pe_exit = ce_c, pe_c
                exit_reason = "COMBINED_TGT_20PCT"
                break

        elif trigger_mode == "PER_LEG_COEXIT":
            ce_h = fast_black_76(b_high, atm_strike, curr_dte, iv, r, "CE")
            pe_l = fast_black_76(b_low, atm_strike, curr_dte, iv, r, "PE")
            ce_l = fast_black_76(b_low, atm_strike, curr_dte, iv, r, "CE")
            pe_h = fast_black_76(b_high, atm_strike, curr_dte, iv, r, "PE")

            # Check SL first (conservative); evaluate BOTH legs at the exact same trigger spot
            b_open = float(bar["open"])
            if ce_h >= ce_leg_sl:
                trig_spot = b_open if fast_black_76(b_open, atm_strike, curr_dte, iv, r, "CE") >= ce_leg_sl else _solve_spot_for_price(b_low, b_high, ce_leg_sl, atm_strike, curr_dte, iv, r, "CE")
                ce_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "CE")
                pe_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "PE")
                exit_reason = "CE_LEG_SL_10PCT"
                break
            if pe_l >= pe_leg_sl:
                trig_spot = b_open if fast_black_76(b_open, atm_strike, curr_dte, iv, r, "PE") >= pe_leg_sl else _solve_spot_for_price(b_low, b_high, pe_leg_sl, atm_strike, curr_dte, iv, r, "PE")
                ce_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "CE")
                pe_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "PE")
                exit_reason = "PE_LEG_SL_10PCT"
                break
            if ce_l <= ce_leg_tgt:
                trig_spot = b_open if fast_black_76(b_open, atm_strike, curr_dte, iv, r, "CE") <= ce_leg_tgt else _solve_spot_for_price(b_low, b_high, ce_leg_tgt, atm_strike, curr_dte, iv, r, "CE")
                ce_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "CE")
                pe_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "PE")
                exit_reason = "CE_LEG_TGT_20PCT"
                break
            if pe_h <= pe_leg_tgt:
                trig_spot = b_open if fast_black_76(b_open, atm_strike, curr_dte, iv, r, "PE") <= pe_leg_tgt else _solve_spot_for_price(b_low, b_high, pe_leg_tgt, atm_strike, curr_dte, iv, r, "PE")
                ce_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "CE")
                pe_exit = fast_black_76(trig_spot, atm_strike, curr_dte, iv, r, "PE")
                exit_reason = "PE_LEG_TGT_20PCT"
                break
    else:
        if not intraday_bars.empty:
            last_bar = intraday_bars.iloc[-1]
            curr_dte = compute_fractional_dte(target_expiry, last_bar["dt"])
            last_spot = float(last_bar["close"])
            ce_exit = fast_black_76(last_spot, atm_strike, curr_dte, iv, r, "CE")
            pe_exit = fast_black_76(last_spot, atm_strike, curr_dte, iv, r, "PE")

    ce_cost = calculate_trade_costs(ce_entry, ce_exit, total_qty, slippage_per_share=slippage)
    pe_cost = calculate_trade_costs(pe_entry, pe_exit, total_qty, slippage_per_share=slippage)

    return {
        "date": str(t_date),
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
    losses = int((df["net_pnl"] < 0).sum())
    win_rate = round((wins / days) * 100.0, 1)

    gross_pnl = round(float(df["gross_pnl"].sum()), 2)
    net_pnl = round(float(df["net_pnl"].sum()), 2)
    taxes = round(float(df["taxes"].sum()), 2)
    slip = round(float(df["slippage"].sum()), 2)

    gw = float(df[df["gross_pnl"] > 0]["gross_pnl"].sum())
    gl = abs(float(df[df["gross_pnl"] < 0]["gross_pnl"].sum()))
    pf = round(gw / gl, 2) if gl > 0 else 999.0

    cum = pd.concat([pd.Series([0.0]), df["net_pnl"]]).cumsum()
    max_dd = round(float((cum - cum.cummax()).min()), 2)

    df["month"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    m_net = df.groupby("month")["net_pnl"].sum()
    return {
        "days": days,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "taxes": taxes,
        "slippage": slip,
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
        ("1. ATM Straddle: Combined 10% SL / 20% Tgt (Intrabar, All Skipped)", dict(
            trigger_mode="COMBINED_INTRABAR", max_loss_cap=None, chop_filter="ALL"
        )),
        ("2. ATM Straddle: Combined 10% SL / 20% Tgt (5m Close, All Skipped)", dict(
            trigger_mode="COMBINED_5M_CLOSE", max_loss_cap=None, chop_filter="ALL"
        )),
        ("3. ATM Straddle: Combined 10% SL / 20% Tgt + 10k Cap (Intrabar, All)", dict(
            trigger_mode="COMBINED_INTRABAR", max_loss_cap=10000.0, chop_filter="ALL"
        )),
        ("4. ATM Straddle: Per-Leg 10% SL / 20% Tgt Co-Exit (All Skipped)", dict(
            trigger_mode="PER_LEG_COEXIT", max_loss_cap=None, chop_filter="ALL"
        )),
        ("5. ATM Straddle: Combined 10% SL / 20% Tgt (Directional 35/65 Only)", dict(
            trigger_mode="COMBINED_INTRABAR", max_loss_cap=None, chop_filter="DIRECTIONAL_ONLY"
        )),
        ("6. ATM Straddle: Combined 10% SL / 20% Tgt (Chop 35-65 Only)", dict(
            trigger_mode="COMBINED_INTRABAR", max_loss_cap=None, chop_filter="CHOP_ONLY"
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
            r = simulate_atm_straddle_day(day_df, t_date, **kw)
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

    print("\n" + "=" * 125)
    print(f"  {period_label} | Triggered (>=0.60% Morning Range) Days: {len(skipped_days)}")
    print("=" * 125)
    print("\n[1] STANDALONE ATM STRADDLE (10% SL / 20% PROFIT, EXIT BOTH) ON TRIGGERED DAYS:")
    print(tabulate(
        standalone_rows,
        headers=["Variant", "Trades", "Win%", "Gross PnL", "Net PnL", "PF", "Max DD", "Avg/Trade", "Exit Breakdown"],
        tablefmt="pipe",
    ))
    print("\n[2] COMBINED PORTFOLIO (BASELINE STRANGLE + ATM STRADDLE FALLBACK) VS BASELINE:")
    print(tabulate(
        combined_rows,
        headers=["Portfolio Variant", "Days", "Win%", "Net PnL", "PF", "Max DD", "Avg/Day", "Worst Month", "Delta vs Base"],
        tablefmt="pipe",
    ))


def main():
    # Sanity check fast_black_76 matches strategy.options_model.black_76_price
    assert fast_black_76(25000.0, 25000.0, 3.5, 0.13, 0.065, "CE") == slow_black_76(25000.0, 25000.0, 3.5, 0.13, 0.065, "CE")
    assert fast_black_76(25000.0, 25000.0, 3.5, 0.13, 0.065, "PE") == slow_black_76(25000.0, 25000.0, 3.5, 0.13, 0.065, "PE")

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
