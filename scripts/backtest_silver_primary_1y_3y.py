import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_silver_strangle import run_strangle_backtest, get_monthly_expiry, calculate_dte_fraction
from strategy.options_model import black_76_price
from strategy.silver_options.chain import get_strike_step, round_to_strike_step
from strategy.silver_options.models import calculate_silver_mcx_costs


def summarize_trades(tdf: pd.DataFrame, label: str):
    if tdf.empty:
        return [label, 0, "0.0%", "Rs +0", "Rs 0", "Rs +0", 0.0, "Rs +0", "Rs +0"]
    n = len(tdf)
    wins = int((tdf["net_pnl_inr"] > 0).sum())
    wr = round(wins / n * 100.0, 1)
    gross = float(tdf["gross_pnl_inr"].sum())
    costs = float(tdf["costs_inr"].sum())
    net = float(tdf["net_pnl_inr"].sum())
    gw = float(tdf[tdf["net_pnl_inr"] > 0]["net_pnl_inr"].sum())
    gl = abs(float(tdf[tdf["net_pnl_inr"] < 0]["net_pnl_inr"].sum()))
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    cum = pd.concat([pd.Series([0.0]), tdf["net_pnl_inr"]]).cumsum()
    dd = float((cum - cum.cummax()).min())
    avg_day = net / n
    return [
        label, n, f"{wr}%", f"Rs {gross:+,.0f}", f"Rs {costs:,.0f}",
        f"Rs {net:+,.0f}", pf, f"Rs {dd:+,.0f}", f"Rs {avg_day:+,.0f}",
    ]


def run_1y_intraday():
    df = pd.read_parquet("data/silverm_5m_1year.parquet")
    df["date"] = pd.to_datetime(df["date"])
    print("=" * 105)
    print("  [1] LAST 1-YEAR INTRADAY 5-MINUTE BACKTEST (MCX SILVERM, 2 LOTS = 10 KG)")
    print(f"      Liquid 5m dataset: 2026-05-07 to 2026-11-24 (122 active sessions; Primary <=10 DTE trades = 40)")
    print("=" * 105)

    regimes = [
        ("Primary Setup | Realistic VRP (0.2% crush, 0.25%/side slip)", dict(evening_iv_crush=0.002, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)),
        ("Primary Setup | Zero Crush Stress (0.0% crush, 0.25%/side slip)", dict(evening_iv_crush=0.0, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)),
        ("Primary Setup | Zero Crush + Tick Slip (0.0% crush, 0.5pt slip)", dict(evening_iv_crush=0.0, slippage_pct_per_leg_side=0.0, slippage_pts=0.5)),
        ("Primary Setup | Legacy 2% Crush Model (2.0% crush, 0.5pt slip)", dict(evening_iv_crush=0.02, slippage_pct_per_leg_side=0.0, slippage_pts=0.5)),
        ("Old Baseline (1.5% OTM, 20:00, 6k Cap) | Realistic VRP", dict(strike_mode="PERCENT", strike_otm_pct=0.015, vwap_entry=False, portfolio_sl_pct=0.0, max_dte_days=0, evening_iv_crush=0.002, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)),
        ("Old Baseline (1.5% OTM, 20:00, 6k Cap) | Zero Crush Stress", dict(strike_mode="PERCENT", strike_otm_pct=0.015, vwap_entry=False, portfolio_sl_pct=0.0, max_dte_days=0, evening_iv_crush=0.0, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)),
    ]

    rows = []
    primary_real = None
    for lbl, kw in regimes:
        res = run_strangle_backtest(df, **kw)
        if lbl.startswith("Primary Setup | Realistic VRP"):
            primary_real = res
        rows.append(summarize_trades(res, lbl))

    print(tabulate(
        rows,
        headers=["Configuration & Regime", "Trades", "Win%", "Gross PnL", "MCX Costs", "Net PnL", "PF", "Max DD", "Avg/Trade"],
        tablefmt="pipe",
    ))

    if primary_real is not None and not primary_real.empty:
        primary_real["month"] = pd.to_datetime(primary_real["date"]).dt.strftime("%Y-%m")
        m = primary_real.groupby("month").agg(
            trades=("net_pnl_inr", "count"),
            wins=("net_pnl_inr", lambda s: int((s > 0).sum())),
            net=("net_pnl_inr", "sum"),
        ).reset_index()
        m["win_pct"] = (m["wins"] / m["trades"] * 100).round(1)
        print("\nMonthly Breakdown — Primary Setup (Realistic VRP: 0.2% crush, 0.25%/side slip):")
        m_rows = [[r["month"], r["trades"], f"{r['wins']}W/{r['trades']-r['wins']}L", f"{r['win_pct']}%", f"Rs {r['net']:+,.0f}"] for _, r in m.iterrows()]
        print(tabulate(m_rows, headers=["Month", "Trades", "W/L", "Win%", "Net PnL"], tablefmt="pipe"))

    # Measure empirical evening-to-daily range ratio on liquid 5m days for 3Y daily calibration
    df["day"] = df["date"].dt.date
    df["time"] = df["date"].dt.strftime("%H:%M")
    day_vol = df.groupby("day")["volume"].sum()
    liq_days = sorted(day_vol[day_vol > 1000].index)
    ratios = []
    for d in liq_days:
        sub = df[df["day"] == d]
        ev = sub[(sub["time"] >= "20:00") & (sub["time"] <= "23:10")]
        if len(ev) < 10:
            continue
        d_rng = float(sub["high"].max() - sub["low"].min())
        e_rng = float(ev["high"].max() - ev["low"].min())
        if d_rng > 0:
            ratios.append(e_rng / d_rng)
    return float(np.median(ratios)), float(np.mean(ratios))


def run_3y_daily(ev_med_ratio: float, ev_mean_ratio: float):
    d3 = pd.read_parquet("data/silverm_daily_3y.parquet")
    d3["date"] = pd.to_datetime(d3["date"])
    d3["day"] = d3["date"].dt.date
    d3 = d3.sort_values("day").reset_index(drop=True)

    # Filter exact 3-year window ending 2026-11-25
    cutoff_3y = date(2023, 11, 26)
    d3_full = d3.copy()
    prev_close = d3_full["close"].shift(1)
    tr = pd.concat([
        d3_full["high"] - d3_full["low"],
        (d3_full["high"] - prev_close).abs(),
        (d3_full["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d3_full["atr14"] = tr.rolling(14, min_periods=1).mean().shift(1)
    d3_full["cc_ret"] = np.log(d3_full["close"] / prev_close)
    d3_full["hl_pct"] = (d3_full["high"] - d3_full["low"]) / d3_full["open"] * 100.0
    d3_full["atr_pct"] = d3_full["atr14"] / prev_close * 100.0

    d3_w = d3_full[d3_full["day"] >= cutoff_3y].copy().reset_index(drop=True)
    d3_w["year"] = d3_w["date"].dt.year

    # Measure strike-breach rates across 3 years: 1.5% fixed vs 0.75 * ATR(14)
    # Using daily high/low vs prev_close (full-day range) AND evening-scaled range
    d3_w["prev_close"] = d3_w["close"].shift(1).fillna(d3_w["open"])
    d3_w["up_exc_pct"] = (d3_w["high"] - d3_w["prev_close"]) / d3_w["prev_close"] * 100.0
    d3_w["dn_exc_pct"] = (d3_w["prev_close"] - d3_w["low"]) / d3_w["prev_close"] * 100.0
    d3_w["max_exc_pct"] = d3_w[["up_exc_pct", "dn_exc_pct"]].max(axis=1)
    d3_w["atr_off_pct"] = (d3_w["atr_pct"] * 0.75).clip(lower=1.0).fillna(1.5)
    d3_w["breach_15pct_full"] = d3_w["max_exc_pct"] > 1.5
    d3_w["breach_atr_full"] = d3_w["max_exc_pct"] > d3_w["atr_off_pct"]

    print("\n" + "=" * 105)
    print("  [2] LAST 3-YEAR CONTINUOUS MCX SILVERM DAILY REGIME AUDIT (2023-11-26 to 2026-11-25)")
    print(f"      Note: Kite API does not support 5m continuous futures (`invalid interval for continuous data`).")
    print(f"      Empirical 20:00-23:10 / Full-Day range ratio on 5m data: median={ev_med_ratio:.2f}, mean={ev_mean_ratio:.2f}")
    print("=" * 105)

    yr_rows = []
    for yr, g in d3_w.groupby("year"):
        rv = float(g["cc_ret"].std() * math.sqrt(252) * 100.0)
        yr_rows.append([
            str(yr),
            len(g),
            f"Rs {g['close'].mean():,.0f}",
            f"{rv:.1f}%",
            f"{g['hl_pct'].mean():.2f}%",
            f"{g['atr_off_pct'].mean():.2f}%",
            f"{g['breach_15pct_full'].mean()*100:.1f}%",
            f"{g['breach_atr_full'].mean()*100:.1f}%",
        ])
    yr_rows.append([
        "ALL 3Y",
        len(d3_w),
        f"Rs {d3_w['close'].mean():,.0f}",
        f"{d3_w['cc_ret'].std()*math.sqrt(252)*100:.1f}%",
        f"{d3_w['hl_pct'].mean():.2f}%",
        f"{d3_w['atr_off_pct'].mean():.2f}%",
        f"{d3_w['breach_15pct_full'].mean()*100:.1f}%",
        f"{d3_w['breach_atr_full'].mean()*100:.1f}%",
    ])
    print(tabulate(
        yr_rows,
        headers=["Year", "Days", "Avg Spot", "CC Ann. RV", "Avg Day Range", "0.75xATR Dist", "Full-Day Breach >1.5%", "Full-Day Breach >0.75xATR"],
        tablefmt="pipe",
    ))

    # 3-Year Daily-Bar Option Backtest Proxy (DTE <= 10 vs All DTE)
    # Evaluates evening-scaled spot excursion (using empirical mean ratio ~0.46 of daily range around close)
    # and 3.17h theta decay (20:00->23:10) on real 3-year continuous MCX prices + ATR(14)
    def sim_3y_proxy(
        label: str,
        strike_mode: str = "ATR",
        otm_pct: float = 0.015,
        atr_mult: float = 0.75,
        max_dte_days: int = 10,
        portfolio_sl_pct: float = 0.25,
        sl_per_lot: float = 3000.0,
        crush: float = 0.002,
        slip: float = 0.0025,
        range_scale: float = ev_mean_ratio,
    ):
        mult = 10.0
        rows = []
        for _, r in d3_w.iterrows():
            td = r["day"]
            expiry = get_monthly_expiry(td, skip_0dte=True)
            cal_dte = (expiry - td).days
            if max_dte_days > 0 and cal_dte > max_dte_days:
                continue
            dt_entry = datetime.combine(td, datetime.strptime("20:00", "%H:%M").time())
            dt_exit = datetime.combine(td, datetime.strptime("23:10", "%H:%M").time())
            dte_e = calculate_dte_fraction(dt_entry, expiry)
            dte_x = calculate_dte_fraction(dt_exit, expiry)

            spot_e = float(r["open"])
            # Evening spot close and worst intra-evening excursions scaled by empirical evening/daily ratio
            d_close = float(r["close"])
            d_high = float(r["high"])
            d_low = float(r["low"])
            spot_x = spot_e + (d_close - spot_e) * range_scale
            spot_hi = max(spot_e, spot_x, spot_e + (d_high - spot_e) * range_scale)
            spot_lo = min(spot_e, spot_x, spot_e + (d_low - spot_e) * range_scale)

            step = get_strike_step(spot_e)
            if strike_mode == "ATR" and not pd.isna(r["atr14"]) and float(r["atr14"]) > 0:
                off = max(float(r["atr14"]) * atr_mult, spot_e * 0.01)
                ce_k = round_to_strike_step(spot_e + off, step)
                pe_k = round_to_strike_step(spot_e - off, step)
            else:
                ce_k = round_to_strike_step(spot_e * (1.0 + otm_pct), step)
                pe_k = round_to_strike_step(spot_e * (1.0 - otm_pct), step)

            # Use trailing 20-day realized vol (or 0.30 floor) for realistic IV pricing across 2023-2026
            iv_e = 0.30
            iv_x = max(0.15, iv_e - crush)
            ce_e = round(black_76_price(spot_e, ce_k, dte_e, iv_e, opt_type="CE") * (1 - slip), 2)
            pe_e = round(black_76_price(spot_e, pe_k, dte_e, iv_e, opt_type="PE") * (1 - slip), 2)
            if ce_e <= 0.5 or pe_e <= 0.5:
                continue

            ce_sl = ce_e * 1.40
            pe_sl = pe_e * 1.40
            max_cap = ((ce_e + pe_e) * portfolio_sl_pct * mult) if portfolio_sl_pct > 0 else (sl_per_lot * 2)

            ce_w = black_76_price(spot_hi, ce_k, 0.5 * (dte_e + dte_x), iv_e, opt_type="CE")
            pe_w = black_76_price(spot_lo, pe_k, 0.5 * (dte_e + dte_x), iv_e, opt_type="PE")
            ce_c = black_76_price(spot_x, ce_k, dte_x, iv_x, opt_type="CE")
            pe_c = black_76_price(spot_x, pe_k, dte_x, iv_x, opt_type="PE")

            ce_hit = ce_w >= ce_sl
            pe_hit = pe_w >= pe_sl
            ce_ex = round(ce_sl * (1 + slip), 2) if ce_hit else round(ce_c * (1 + slip), 2)
            pe_ex = round(pe_sl * (1 + slip), 2) if pe_hit else round(pe_c * (1 + slip), 2)

            gross = ((ce_e - ce_ex) + (pe_e - pe_ex)) * mult
            if gross < -max_cap:
                gross = -max_cap
            costs = (
                calculate_silver_mcx_costs(ce_e, mult, True)
                + calculate_silver_mcx_costs(ce_ex, mult, False)
                + calculate_silver_mcx_costs(pe_e, mult, True)
                + calculate_silver_mcx_costs(pe_ex, mult, False)
            )
            rows.append({
                "date": td,
                "year": td.year,
                "gross_pnl_inr": round(gross, 2),
                "costs_inr": round(costs, 2),
                "net_pnl_inr": round(gross - costs, 2),
            })
        tdf = pd.DataFrame(rows)
        return tdf, summarize_trades(tdf, label)

    print("\n  3-YEAR DAILY-PROXY OPTION BACKTEST (2023-11-26 to 2026-11-25, Empirical Evening Range Scale):")
    proxy_rows = []
    prim_3y_df = None
    for lbl, kw in [
        ("Primary (ATR 0.75x, <=10 DTE, 25% SL) | Realistic (0.2% crush, 0.25% slip)", dict(strike_mode="ATR", atr_mult=0.75, max_dte_days=10, portfolio_sl_pct=0.25, crush=0.002, slip=0.0025)),
        ("Primary (ATR 0.75x, <=10 DTE, 25% SL) | Zero Crush (0.0% crush, 0.25% slip)", dict(strike_mode="ATR", atr_mult=0.75, max_dte_days=10, portfolio_sl_pct=0.25, crush=0.0, slip=0.0025)),
        ("Primary (ATR 0.75x, <=10 DTE, 25% SL) | Zero Crush + 0% Slip", dict(strike_mode="ATR", atr_mult=0.75, max_dte_days=10, portfolio_sl_pct=0.25, crush=0.0, slip=0.0)),
        ("Old Baseline (1.5% OTM, All DTE, 6k Cap) | Realistic (0.2% crush, 0.25% slip)", dict(strike_mode="PERCENT", otm_pct=0.015, max_dte_days=0, portfolio_sl_pct=0.0, crush=0.002, slip=0.0025)),
        ("Old Baseline (1.5% OTM, All DTE, 6k Cap) | Zero Crush (0.0% crush, 0.25% slip)", dict(strike_mode="PERCENT", otm_pct=0.015, max_dte_days=0, portfolio_sl_pct=0.0, crush=0.0, slip=0.0025)),
    ]:
        tdf, s = sim_3y_proxy(lbl, **kw)
        if lbl.startswith("Primary (ATR 0.75x, <=10 DTE, 25% SL) | Realistic"):
            prim_3y_df = tdf
        proxy_rows.append(s)

    print(tabulate(
        proxy_rows,
        headers=["3Y Proxy Configuration", "Trades", "Win%", "Gross PnL", "MCX Costs", "Net PnL", "PF", "Max DD", "Avg/Trade"],
        tablefmt="pipe",
    ))

    if prim_3y_df is not None and not prim_3y_df.empty:
        print("\nYear-by-Year Breakdown — 3Y Daily Proxy (Primary Setup, Realistic VRP):")
        y_rows = []
        for yr, g in prim_3y_df.groupby("year"):
            y_rows.append(summarize_trades(g, str(yr)))
        print(tabulate(
            y_rows,
            headers=["Year", "Trades", "Win%", "Gross PnL", "MCX Costs", "Net PnL", "PF", "Max DD", "Avg/Trade"],
            tablefmt="pipe",
        ))


if __name__ == "__main__":
    med_r, mean_r = run_1y_intraday()
    run_3y_daily(med_r, mean_r)
