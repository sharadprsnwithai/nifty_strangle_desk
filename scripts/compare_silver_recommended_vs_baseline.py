import math
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_silver_strangle import get_monthly_expiry, calculate_dte_fraction
from strategy.silver_options.models import calculate_silver_mcx_costs

SQRT2 = math.sqrt(2.0)


def fast_norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def fast_black_76(spot: float, strike: float, dte_days: float, iv: float = 0.30, r: float = 0.065, opt_type: str = "CE") -> float:
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


def run_silver_variant(
    df: pd.DataFrame,
    name: str,
    lots: int = 2,
    lot_size_kg: int = 5,
    strike_otm_pct: float = 0.015,
    liquid_strike_step: Optional[float] = None,  # None = standard 500/1000 step; 2000 or 5000 = liquid round strike snapping
    sl_pct: float = 0.40,
    trail_to_cost: bool = True,
    trail_activation_decay_pct: Optional[float] = None,  # 0.30 matches live SilverStrangleDesk proactive trail
    target_decay_pct: float = 0.70,
    portfolio_max_loss_rs: Optional[float] = None,
    slippage_pct_per_leg_side: float = 0.0,  # % of option price lost on entry/exit spread
    base_iv: float = 0.30,
    evening_iv_crush: float = 0.02,
) -> Dict[str, Any]:
    df = df.copy()
    df["time"] = df["date"].dt.strftime("%H:%M")
    df["day"] = df["date"].dt.date

    day_vol = df.groupby("day")["volume"].sum()
    active_days = sorted(day_vol[day_vol > 1000].index)
    multiplier = float(lots * lot_size_kg)

    rows: List[Dict[str, Any]] = []
    for trade_day in active_days:
        day_df = df[df["day"] == trade_day].sort_values("date").reset_index(drop=True)
        evening_df = day_df[(day_df["time"] >= "20:00") & (day_df["time"] <= "23:15")].reset_index(drop=True)
        if len(evening_df) < 5:
            continue
        entry_row = evening_df.iloc[0]
        if entry_row["time"] > "20:10":
            continue

        spot_entry = float(entry_row["open"])
        entry_dt = entry_row["date"]
        expiry = get_monthly_expiry(trade_day)
        dte_entry = calculate_dte_fraction(entry_dt, expiry)

        if liquid_strike_step is not None:
            step = liquid_strike_step
            ce_strike = math.ceil((spot_entry * (1.0 + strike_otm_pct)) / step) * step
            pe_strike = math.floor((spot_entry * (1.0 - strike_otm_pct)) / step) * step
        else:
            step = 1000.0 if spot_entry > 100000 else 500.0
            ce_strike = round((spot_entry * (1.0 + strike_otm_pct)) / step) * step
            pe_strike = round((spot_entry * (1.0 - strike_otm_pct)) / step) * step

        ce_theo = fast_black_76(spot_entry, ce_strike, dte_entry, iv=base_iv, opt_type="CE")
        pe_theo = fast_black_76(spot_entry, pe_strike, dte_entry, iv=base_iv, opt_type="PE")
        if ce_theo <= 5.0 or pe_theo <= 5.0:
            continue

        # Entry sell receives worse price by slippage_pct_per_leg_side
        ce_entry = round(ce_theo * (1.0 - slippage_pct_per_leg_side), 2)
        pe_entry = round(pe_theo * (1.0 - slippage_pct_per_leg_side), 2)

        ce_sl = ce_entry * (1.0 + sl_pct)
        pe_sl = pe_entry * (1.0 + sl_pct)
        ce_status = "OPEN"
        pe_status = "OPEN"
        ce_exit = ce_entry
        pe_exit = pe_entry
        ce_reason = "EOD"
        pe_reason = "EOD"

        total_bars = len(evening_df)
        for idx, row in evening_df.iterrows():
            curr_dt = row["date"]
            curr_spot = float(row["close"])
            curr_high = float(row["high"])
            curr_low = float(row["low"])
            prog = idx / max(1, total_bars - 1)
            curr_iv = max(0.15, base_iv - evening_iv_crush * prog)
            curr_dte = calculate_dte_fraction(curr_dt, expiry)

            ce_worst = fast_black_76(curr_high, ce_strike, curr_dte, iv=curr_iv, opt_type="CE")
            pe_worst = fast_black_76(curr_low, pe_strike, curr_dte, iv=curr_iv, opt_type="PE")
            ce_curr = fast_black_76(curr_spot, ce_strike, curr_dte, iv=curr_iv, opt_type="CE")
            pe_curr = fast_black_76(curr_spot, pe_strike, curr_dte, iv=curr_iv, opt_type="PE")

            # Portfolio Max Loss Cap check (matches live SilverStrangleDesk order)
            if portfolio_max_loss_rs is not None:
                ce_mark = ce_curr if ce_status == "OPEN" else ce_exit
                pe_mark = pe_curr if pe_status == "OPEN" else pe_exit
                curr_pnl = ((ce_entry - ce_mark) + (pe_entry - pe_mark)) * multiplier
                if curr_pnl <= -abs(portfolio_max_loss_rs):
                    if ce_status == "OPEN":
                        ce_status = "MAX_LOSS"
                        ce_exit = round(ce_curr * (1.0 + slippage_pct_per_leg_side), 2)
                        ce_reason = "MAX_PORTFOLIO_LOSS"
                    if pe_status == "OPEN":
                        pe_status = "MAX_LOSS"
                        pe_exit = round(pe_curr * (1.0 + slippage_pct_per_leg_side), 2)
                        pe_reason = "MAX_PORTFOLIO_LOSS"
                    break

            if ce_status == "OPEN" and ce_worst >= ce_sl:
                ce_status = "STOPPED"
                ce_exit = round(ce_sl * (1.0 + slippage_pct_per_leg_side), 2)
                ce_reason = "SL"
                if trail_to_cost and pe_status == "OPEN":
                    pe_sl = min(pe_sl, pe_entry)

            if pe_status == "OPEN" and pe_worst >= pe_sl:
                pe_status = "STOPPED"
                pe_exit = round(pe_sl * (1.0 + slippage_pct_per_leg_side), 2)
                pe_reason = "SL"
                if trail_to_cost and ce_status == "OPEN":
                    ce_sl = min(ce_sl, ce_entry)

            if trail_to_cost and trail_activation_decay_pct is not None and ce_status == "OPEN" and pe_status == "OPEN":
                if (ce_entry - ce_curr) / ce_entry >= trail_activation_decay_pct:
                    pe_sl = min(pe_sl, pe_entry)
                if (pe_entry - pe_curr) / pe_entry >= trail_activation_decay_pct:
                    ce_sl = min(ce_sl, ce_entry)

            if ce_status == "OPEN" and pe_status == "OPEN":
                comb_entry = ce_entry + pe_entry
                comb_curr = ce_curr + pe_curr
                if (comb_entry - comb_curr) / comb_entry >= target_decay_pct:
                    ce_status = "TARGET"
                    pe_status = "TARGET"
                    ce_exit = round(ce_curr * (1.0 + slippage_pct_per_leg_side), 2)
                    pe_exit = round(pe_curr * (1.0 + slippage_pct_per_leg_side), 2)
                    ce_reason = "TARGET_DECAY"
                    pe_reason = "TARGET_DECAY"
                    break

            if idx == total_bars - 1:
                if ce_status == "OPEN":
                    ce_exit = round(ce_curr * (1.0 + slippage_pct_per_leg_side), 2)
                if pe_status == "OPEN":
                    pe_exit = round(pe_curr * (1.0 + slippage_pct_per_leg_side), 2)

        gross_pts = (ce_entry - ce_exit) + (pe_entry - pe_exit)
        gross_rs = round(gross_pts * multiplier, 2)
        costs_rs = round(
            calculate_silver_mcx_costs(ce_entry, multiplier, is_sell=True)
            + calculate_silver_mcx_costs(ce_exit, multiplier, is_sell=False)
            + calculate_silver_mcx_costs(pe_entry, multiplier, is_sell=True)
            + calculate_silver_mcx_costs(pe_exit, multiplier, is_sell=False),
            2,
        )
        net_rs = round(gross_rs - costs_rs, 2)
        rows.append({
            "date": str(trade_day),
            "gross_rs": gross_rs,
            "costs_rs": costs_rs,
            "net_rs": net_rs,
            "ce_reason": ce_reason,
            "pe_reason": pe_reason,
        })

    tdf = pd.DataFrame(rows)
    days = len(tdf)
    wins = int((tdf["net_rs"] > 0).sum())
    win_rate = round((wins / days) * 100.0, 1) if days else 0.0
    total_gross = round(float(tdf["gross_rs"].sum()), 2)
    total_costs = round(float(tdf["costs_rs"].sum()), 2)
    total_net = round(float(tdf["net_rs"].sum()), 2)
    gw = float(tdf[tdf["net_rs"] > 0]["net_rs"].sum())
    gl = abs(float(tdf[tdf["net_rs"] < 0]["net_rs"].sum()))
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    cum = pd.concat([pd.Series([0.0]), tdf["net_rs"]]).cumsum()
    max_dd = round(float((cum - cum.cummax()).min()), 2)
    tdf["month"] = pd.to_datetime(tdf["date"]).dt.strftime("%Y-%m")
    m_net = tdf.groupby("month")["net_rs"].sum()

    return {
        "variant": name,
        "days": days,
        "win_rate": win_rate,
        "gross_rs": total_gross,
        "costs_rs": total_costs,
        "net_rs": total_net,
        "pf": pf,
        "max_dd": max_dd,
        "avg_day": round(total_net / days, 2) if days else 0.0,
        "worst_month": f"{m_net.idxmin()} ({m_net.min():+,.0f})",
        "roi_2l": round((total_net / 200000.0) * 100.0, 1),
    }


def main():
    df = pd.read_parquet("data/silverm_5m_1year.parquet")
    df["date"] = pd.to_datetime(df["date"])
    print(f"Loaded SILVERM 5m 1Y: {len(df)} bars ({df['date'].min().date()} to {df['date'].max().date()})")

    variants = [
        ("0. Baseline Backtest (1000-step, No 6k Cap, 0% Slip)", dict(
            liquid_strike_step=None, portfolio_max_loss_rs=None, trail_activation_decay_pct=None, slippage_pct_per_leg_side=0.0
        )),
        ("1. Baseline + Live 6k Cap + 30% Trail (0% Slip, Ideal)", dict(
            liquid_strike_step=None, portfolio_max_loss_rs=6000.0, trail_activation_decay_pct=0.30, slippage_pct_per_leg_side=0.0
        )),
        ("2. Unfiltered NRML on Thin Strikes (1000-step, 6k Cap, 2.5% Slip)", dict(
            liquid_strike_step=None, portfolio_max_loss_rs=6000.0, trail_activation_decay_pct=0.30, slippage_pct_per_leg_side=0.025
        )),
        ("3. Recommended (2000-step Liquid Snap, 6k Cap, 0.5% Tight Slip)", dict(
            liquid_strike_step=2000.0, portfolio_max_loss_rs=6000.0, trail_activation_decay_pct=0.30, slippage_pct_per_leg_side=0.005
        )),
        ("4. Recommended (2000-step Liquid Snap, 6k Cap, 1.0% Tight Slip)", dict(
            liquid_strike_step=2000.0, portfolio_max_loss_rs=6000.0, trail_activation_decay_pct=0.30, slippage_pct_per_leg_side=0.010
        )),
        ("5. Recommended Stress (5000-step Major Strikes, 6k Cap, 0.5% Slip)", dict(
            liquid_strike_step=5000.0, portfolio_max_loss_rs=6000.0, trail_activation_decay_pct=0.30, slippage_pct_per_leg_side=0.005
        )),
    ]

    summary_rows = []
    for name, kw in variants:
        r = run_silver_variant(df, name=name, **kw)
        summary_rows.append([
            r["variant"],
            r["days"],
            f"{r['win_rate']}%",
            f"Rs {r['gross_rs']:+,.0f}",
            f"Rs {r['costs_rs']:,.0f}",
            f"Rs {r['net_rs']:+,.0f}",
            r["pf"],
            f"Rs {r['max_dd']:+,.0f}",
            f"Rs {r['avg_day']:+,.0f}",
            r["worst_month"],
            f"{r['roi_2l']}%",
        ])

    print("\n" + "=" * 135)
    print("   MCX SILVERM EVENING STRANGLE (2 LOTS = 10 KG): RECOMMENDED PATH VS BASELINE (1-YEAR BACKTEST)")
    print("=" * 135)
    print(tabulate(
        summary_rows,
        headers=["Variant", "Days", "Win%", "Gross PnL", "MCX Costs", "Net PnL", "PF", "Max DD", "Avg/Day", "Worst Month", "ROI (2L)"],
        tablefmt="pipe",
    ))


if __name__ == "__main__":
    main()
