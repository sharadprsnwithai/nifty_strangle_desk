import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_config
from kite import get_kite_client
from scripts.backtest_silver_strangle import get_monthly_expiry, calculate_dte_fraction, run_strangle_backtest
from strategy.silver_options.chain import get_strike_step, round_to_strike_step
from strategy.silver_options.models import calculate_silver_mcx_costs

SQRT2 = math.sqrt(2.0)


def fast_norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def fast_b76(spot: float, strike: float, dte_days: float, iv: float = 0.30, r: float = 0.065, opt_type: str = "CE") -> float:
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


def load_silverm_5m_latest() -> pd.DataFrame:
    df = pd.read_parquet("data/silverm_5m_1year.parquet")
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert("Asia/Kolkata")
    try:
        k = get_kite_client(config=load_config(), check_market_hours=False, ignore_holidays=True)
        last_dt = df["date"].max().tz_localize(None).to_pydatetime()
        c = k.historical_data(123668487, last_dt - timedelta(days=2), datetime.now(), "5minute")
        if c:
            extra = pd.DataFrame(c)
            extra["date"] = pd.to_datetime(extra["date"], utc=True).dt.tz_convert("Asia/Kolkata")
            df = pd.concat([df, extra], ignore_index=True).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    except Exception:
        pass
    df["day"] = df["date"].dt.date
    df["time"] = df["date"].dt.strftime("%H:%M")
    return df


def run_vwap_vol_buyer(
    df: pd.DataFrame,
    active_days,
    entry_start: str = "18:00",
    last_entry: str = "20:00",
    exit_time: str = "23:10",
    vol_mult: float = 1.3,
    max_vwap_dist_pct: float = 0.012,
    sl_pct: float = 0.30,
    tgt_pct: float = 0.60,
    trail_be_pct: float = 0.25,
    max_loss_rs: float = 6000.0,     # intrabar hard cap (0 = disabled)
    ema_exit: bool = False,          # exit if EMA9 crosses against position after entry
    vwap_exit: bool = False,         # exit if close crosses back across VWAP
    max_dte_days: int = 0,           # 0 = all business days
    skip_0dte: bool = True,
    iv: float = 0.30,
    iv_crush: float = 0.002,
    slip: float = 0.0025,
    lots: int = 2,
) -> pd.DataFrame:
    mult = float(lots * 5)
    trades = []

    for td in active_days:
        expiry = get_monthly_expiry(td, skip_0dte=skip_0dte)
        cal_dte = (expiry - td).days
        if max_dte_days > 0 and cal_dte > max_dte_days:
            continue

        day_df = df[df["day"] == td].sort_values("date").reset_index(drop=True)
        if len(day_df) < 30:
            continue

        typ = (day_df["high"] + day_df["low"] + day_df["close"]) / 3.0
        vol = day_df["volume"].clip(lower=1.0)
        day_df["vwap"] = (typ * vol).cumsum() / vol.cumsum()
        day_df["ema9"] = day_df["close"].ewm(span=9, adjust=False).mean()
        day_df["ema21"] = day_df["close"].ewm(span=21, adjust=False).mean()
        day_df["vol_ma"] = day_df["volume"].rolling(12, min_periods=1).mean()

        sess = day_df[(day_df["time"] >= entry_start) & (day_df["time"] <= exit_time)].reset_index(drop=True)
        if len(sess) < 4:
            continue

        in_pos = False
        opt_type = None
        strike = 0.0
        entry_px = 0.0
        sl_level = 0.0
        tgt_level = 0.0
        entry_time = ""
        ev_start_dt = sess.iloc[0]["date"]

        for idx, row in sess.iterrows():
            curr_dt = row["date"]
            t = row["time"]
            c, h, l = float(row["close"]), float(row["high"]), float(row["low"])
            vw, e9, e21 = float(row["vwap"]), float(row["ema9"]), float(row["ema21"])
            v, vma = float(row["volume"]), float(row["vol_ma"])

            mins = max(0.0, (curr_dt - ev_start_dt).total_seconds() / 60.0)
            curr_iv = max(0.15, iv - iv_crush * min(1.0, mins / 190.0))
            dte = calculate_dte_fraction(curr_dt, expiry)

            if in_pos:
                w_spot = l if opt_type == "CE" else h
                b_spot = h if opt_type == "CE" else l
                val_c = fast_b76(c, strike, dte, curr_iv, opt_type=opt_type)
                val_w = fast_b76(w_spot, strike, dte, curr_iv, opt_type=opt_type)
                val_b = fast_b76(b_spot, strike, dte, curr_iv, opt_type=opt_type)

                # Hard Rs cap expressed as price floor so it also protects intrabar
                hard_floor = (entry_px - (max_loss_rs / mult)) if max_loss_rs > 0 else 0.0
                eff_sl = max(sl_level, hard_floor)

                reason = None
                if t >= exit_time or idx == len(sess) - 1:
                    exit_px = round(val_c * (1 - slip), 2)
                    reason = "EOD"
                elif eff_sl > 0 and val_w <= eff_sl:
                    exit_px = round(eff_sl * (1 - slip), 2)
                    reason = "MAX_LOSS" if hard_floor >= sl_level and max_loss_rs > 0 else "SL"
                elif tgt_pct > 0 and val_b >= tgt_level:
                    exit_px = round(tgt_level * (1 - slip), 2)
                    reason = "TARGET"
                elif vwap_exit and ((opt_type == "CE" and c < vw) or (opt_type == "PE" and c > vw)):
                    exit_px = round(val_c * (1 - slip), 2)
                    reason = "VWAP_EXIT"
                elif ema_exit and ((opt_type == "CE" and e9 < e21) or (opt_type == "PE" and e9 > e21)):
                    exit_px = round(val_c * (1 - slip), 2)
                    reason = "EMA_EXIT"

                if reason:
                    gross = round((exit_px - entry_px) * mult, 2)
                    costs = round(
                        calculate_silver_mcx_costs(entry_px, mult, is_sell=False)
                        + calculate_silver_mcx_costs(exit_px, mult, is_sell=True),
                        2,
                    )
                    trades.append({
                        "date": str(td),
                        "month": str(td)[:7],
                        "cal_dte": cal_dte,
                        "entry_time": entry_time,
                        "exit_time": t,
                        "dir": opt_type,
                        "strike": strike,
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "reason": reason,
                        "gross_pnl_inr": gross,
                        "costs_inr": costs,
                        "net_pnl_inr": round(gross - costs, 2),
                    })
                    break
                else:
                    if trail_be_pct > 0 and val_c >= entry_px * (1 + trail_be_pct):
                        sl_level = max(sl_level, entry_px)
                continue

            if t > last_entry:
                continue

            sig_dir = None
            if c > vw and e9 > e21 and v > vol_mult * vma and (c - vw) / vw < max_vwap_dist_pct:
                sig_dir = "CE"
            elif c < vw and e9 < e21 and v > vol_mult * vma and (vw - c) / vw < max_vwap_dist_pct:
                sig_dir = "PE"

            if sig_dir:
                step = get_strike_step(c)
                strike = round_to_strike_step(c, step)
                theo = fast_b76(c, strike, dte, curr_iv, opt_type=sig_dir)
                entry_px = round(theo * (1 + slip), 2)
                if entry_px <= 20.0:
                    continue
                opt_type = sig_dir
                entry_time = t
                in_pos = True
                sl_level = entry_px * (1 - sl_pct) if sl_pct > 0 else 0.0
                tgt_level = entry_px * (1 + tgt_pct) if tgt_pct > 0 else 1e9

    return pd.DataFrame(trades)


def summarize(tdf: pd.DataFrame, label: str):
    if tdf.empty:
        return [label, 0, "0.0%", "Rs 0", "Rs 0", "Rs 0", 0.0, "Rs 0", "Rs 0"]
    n = len(tdf)
    wr = round((tdf["net_pnl_inr"] > 0).mean() * 100.0, 1)
    gross = tdf["gross_pnl_inr"].sum()
    costs = tdf["costs_inr"].sum()
    net = tdf["net_pnl_inr"].sum()
    gw = tdf[tdf["net_pnl_inr"] > 0]["net_pnl_inr"].sum()
    gl = abs(tdf[tdf["net_pnl_inr"] < 0]["net_pnl_inr"].sum())
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    cum = pd.concat([pd.Series([0.0]), tdf["net_pnl_inr"]]).cumsum()
    dd = (cum - cum.cummax()).min()
    return [label, n, f"{wr}%", f"Rs {gross:+,.0f}", f"Rs {costs:,.0f}", f"Rs {net:+,.0f}", pf, f"Rs {dd:+,.0f}", f"Rs {net/n:+,.0f}"]


def main():
    df = load_silverm_5m_latest()
    day_vol = df.groupby("day")["volume"].sum()
    active_days = sorted(day_vol[day_vol > 1000].index)
    print(f"Dataset range: {df['date'].min()} to {df['date'].max()} | Liquid business days (vol>1000): {len(active_days)} ({active_days[0]} to {active_days[-1]})")

    for r_lbl, crush_v, slip_v in [
        ("REGIME A: REALISTIC BUYER HEADWIND (0.2% Evening IV Crush, 0.25%/side Slip)", 0.002, 0.0025),
        ("REGIME B: ZERO IV CRUSH (0.0% Evening IV Crush, 0.25%/side Slip)", 0.0, 0.0025),
        ("REGIME C: STRESS SPREAD (0.2% Evening IV Crush, 0.50%/side Slip)", 0.002, 0.0050),
    ]:
        print(f"\n=== ALL BUSINESS DAYS | 18:00-20:00 VWAP + VOL SURGE ({r_lbl}) ===")
        rows = []
        base_all = None
        for v_lbl, kw in [
            ("1. All Business Days | Hard Rs 6k Intrabar Cap (SL30/Tgt60)", dict(max_loss_rs=6000.0, sl_pct=0.30, tgt_pct=0.60)),
            ("2. All Business Days | Hard Rs 4k Intrabar Cap (SL30/Tgt60)", dict(max_loss_rs=4000.0, sl_pct=0.30, tgt_pct=0.60)),
            ("3. All Business Days | Hard Rs 6k Cap + EMA9/21 Exit", dict(max_loss_rs=6000.0, sl_pct=0.30, tgt_pct=0.60, ema_exit=True)),
            ("4. All Business Days | Hard Rs 6k Cap + VWAP Rev Exit", dict(max_loss_rs=6000.0, sl_pct=0.30, tgt_pct=0.60, vwap_exit=True)),
            ("5. All Business Days | Uncapped 20% Option SL (Tgt60)", dict(max_loss_rs=0.0, sl_pct=0.20, tgt_pct=0.60)),
            ("6. All Business Days | Uncapped 30% Option SL (Tgt60)", dict(max_loss_rs=0.0, sl_pct=0.30, tgt_pct=0.60)),
            ("7. Sub-split: DTE <= 10 Only (Rs 6k Intrabar Cap)", dict(max_dte_days=10, max_loss_rs=6000.0, sl_pct=0.30, tgt_pct=0.60)),
        ]:
            tdf = run_vwap_vol_buyer(df, active_days, iv_crush=crush_v, slip=slip_v, **kw)
            if v_lbl.startswith("1. All Business Days"):
                base_all = tdf
            rows.append(summarize(tdf, v_lbl))
        print(tabulate(rows, headers=["Configuration", "Trd", "Win%", "Gross PnL", "MCX Costs", "Net PnL", "PF", "Max DD", "Avg/Trd"], tablefmt="pipe"))

        if r_lbl.startswith("REGIME A") and base_all is not None and not base_all.empty:
            print("\nMonthly Breakdown — All Business Days (Config 1: Hard Rs 6k Intrabar Cap, Realistic Buyer Headwind):")
            m = base_all.groupby("month").agg(
                trades=("net_pnl_inr", "count"),
                wins=("net_pnl_inr", lambda s: int((s > 0).sum())),
                net=("net_pnl_inr", "sum"),
            ).reset_index()
            m["win_pct"] = (m["wins"] / m["trades"] * 100).round(1)
            m_rows = [[r["month"], r["trades"], f"{r['wins']}W/{r['trades']-r['wins']}L", f"{r['win_pct']}%", f"Rs {r['net']:+,.0f}"] for _, r in m.iterrows()]
            print(tabulate(m_rows, headers=["Month", "Trades", "W/L", "Win%", "Net PnL"], tablefmt="pipe"))

            print("\nLast 10 Trades — All Business Days (Config 1):")
            t10 = base_all.tail(10)
            t_rows = [
                [r["date"], r["cal_dte"], r["entry_time"], r["dir"], f"{r['strike']:,.0f}", f"{r['entry_px']:,.1f}", f"{r['exit_px']:,.1f}", r["reason"], f"Rs {r['net_pnl_inr']:+,.0f}"]
                for _, r in t10.iterrows()
            ]
            print(tabulate(t_rows, headers=["Date", "CalDTE", "Time", "Dir", "Strike", "Entry", "Exit", "Reason", "Net PnL"], tablefmt="pipe"))

            # Combined Portfolio: Config 1 Buyer (All Business Days) + Primary Strangle (<=10 DTE)
            strangle_df = run_strangle_backtest(df, evening_iv_crush=0.002, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)
            strangle_df["date"] = strangle_df["date"].astype(str)
            comb = pd.concat([
                base_all[["date", "net_pnl_inr"]].assign(strat="BUY"),
                strangle_df[["date", "net_pnl_inr"]].assign(strat="STRANGLE"),
            ], ignore_index=True)
            daily_comb = comb.groupby("date")["net_pnl_inr"].sum().reset_index().sort_values("date")
            c_n = len(daily_comb)
            c_wr = round((daily_comb["net_pnl_inr"] > 0).mean() * 100, 1)
            c_net = daily_comb["net_pnl_inr"].sum()
            c_gw = daily_comb[daily_comb["net_pnl_inr"] > 0]["net_pnl_inr"].sum()
            c_gl = abs(daily_comb[daily_comb["net_pnl_inr"] < 0]["net_pnl_inr"].sum())
            c_pf = round(c_gw / c_gl, 2) if c_gl > 0 else 999.0
            c_cum = pd.concat([pd.Series([0.0]), daily_comb["net_pnl_inr"]]).cumsum()
            c_dd = (c_cum - c_cum.cummax()).min()
            print(f"\nCombined Portfolio (All-Days Buyer + <=10 DTE Primary Strangle): ActiveDays={c_n} | Win={c_wr}% | Net=Rs {c_net:+,.0f} | PF={c_pf:.2f} | MaxDD=Rs {c_dd:+,.0f}")


if __name__ == "__main__":
    main()
