import math
import sys
from datetime import date, datetime
from pathlib import Path
import numpy as np
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_silver_directional_buyer import load_silverm_5m_latest, fast_b76
from scripts.backtest_silver_strangle import run_strangle_backtest, get_monthly_expiry, calculate_dte_fraction
from scripts.backtest_silver_barbell_1y import compute_atr_pct_map, build_combined, summarize_daily
from strategy.options_model import black_76_price
from strategy.silver_options.chain import get_strike_step, round_to_strike_step
from strategy.silver_options.models import calculate_silver_mcx_costs


def run_exact_5m_buyer_custom(
    df: pd.DataFrame,
    active_days,
    rv_map: dict | None = None,
    fixed_iv: float = 0.30,
    iv_crush: float = 0.002,
    slip: float = 0.0025,
    max_loss_rs: float = 6000.0,
    sl_pct: float = 0.30,
    tgt_pct: float = 0.60,
    trail_be_pct: float = 0.25,
    lots: int = 2,
) -> pd.DataFrame:
    """
    Runs the updated Combo 2A Directional Buyer on exact 5-minute MCX bars
    (`18:00-20:00` entry window, `hard_floor` + `sl_pct`, `+25%` BE trail, `+60%` target, `23:10` EOD)
    with optional dynamic realized volatility (`rv_map`) per trading day.
    """
    mult = float(lots * 5)
    trades = []

    for td in active_days:
        expiry = get_monthly_expiry(td, skip_0dte=True)
        cal_dte = (expiry - td).days
        day_df = df[df["day"] == td].sort_values("date").reset_index(drop=True)
        if len(day_df) < 30:
            continue

        typ = (day_df["high"] + day_df["low"] + day_df["close"]) / 3.0
        vol = day_df["volume"].clip(lower=1.0)
        day_df["vwap"] = (typ * vol).cumsum() / vol.cumsum()
        day_df["ema9"] = day_df["close"].ewm(span=9, adjust=False).mean()
        day_df["ema21"] = day_df["close"].ewm(span=21, adjust=False).mean()
        day_df["vol_ma"] = day_df["volume"].rolling(12, min_periods=1).mean()

        sess = day_df[(day_df["time"] >= "18:00") & (day_df["time"] <= "23:10")].reset_index(drop=True)
        if len(sess) < 4:
            continue

        base_iv = max(0.22, float(rv_map.get(td, fixed_iv))) if rv_map is not None else fixed_iv
        ev_start_dt = sess.iloc[0]["date"]
        in_pos = False
        opt_type = None
        strike = 0.0
        entry_px = 0.0
        sl_level = 0.0
        tgt_level = 0.0
        entry_time = ""

        for idx, row in sess.iterrows():
            curr_dt = row["date"]
            t = row["time"]
            c, h, l = float(row["close"]), float(row["high"]), float(row["low"])
            vw, e9, e21 = float(row["vwap"]), float(row["ema9"]), float(row["ema21"])
            v, vma = float(row["volume"]), float(row["vol_ma"])

            mins = max(0.0, (curr_dt - ev_start_dt).total_seconds() / 60.0)
            curr_iv = max(0.15, base_iv - iv_crush * min(1.0, mins / 190.0))
            dte = calculate_dte_fraction(curr_dt, expiry)

            if in_pos:
                w_spot = l if opt_type == "CE" else h
                b_spot = h if opt_type == "CE" else l
                val_c = fast_b76(c, strike, dte, curr_iv, opt_type=opt_type)
                val_w = fast_b76(w_spot, strike, dte, curr_iv, opt_type=opt_type)
                val_b = fast_b76(b_spot, strike, dte, curr_iv, opt_type=opt_type)
                hard_floor = (entry_px - (max_loss_rs / mult)) if max_loss_rs > 0 else 0.0
                eff_sl = max(sl_level, hard_floor)

                reason = None
                if t >= "23:10" or idx == len(sess) - 1:
                    exit_px = round(val_c * (1.0 - slip), 2)
                    reason = "EOD"
                elif eff_sl > 0 and val_w <= eff_sl:
                    exit_px = round(eff_sl * (1.0 - slip), 2)
                    reason = "MAX_LOSS" if hard_floor >= sl_level and max_loss_rs > 0 else "SL"
                elif tgt_pct > 0 and val_b >= tgt_level:
                    exit_px = round(tgt_level * (1.0 - slip), 2)
                    reason = "TARGET"

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
                elif trail_be_pct > 0 and val_c >= entry_px * (1.0 + trail_be_pct):
                    sl_level = max(sl_level, entry_px)
                continue

            if t > "20:00":
                continue

            sig_dir = None
            if c > vw and e9 > e21 and v > 1.3 * vma and (c - vw) / vw < 0.012:
                sig_dir = "CE"
            elif c < vw and e9 < e21 and v > 1.3 * vma and (vw - c) / vw < 0.012:
                sig_dir = "PE"

            if sig_dir:
                step = get_strike_step(c)
                strike = round_to_strike_step(c, step)
                theo = fast_b76(c, strike, dte, curr_iv, opt_type=sig_dir)
                entry_px = round(theo * (1.0 + slip), 2)
                if entry_px <= 20.0:
                    continue
                opt_type = sig_dir
                entry_time = t
                in_pos = True
                sl_level = entry_px * (1.0 - sl_pct) if sl_pct > 0 else 0.0
                tgt_level = entry_px * (1.0 + tgt_pct) if tgt_pct > 0 else 1e9

    return pd.DataFrame(trades)


def run_exact_5m_section():
    df5 = load_silverm_5m_latest()
    today_d = datetime.now().date()
    df5 = df5[df5["day"] < today_d].copy()
    day_vol = df5.groupby("day")["volume"].sum()
    active_days = sorted(day_vol[day_vol > 1000].index)
    atr_pct_map = compute_atr_pct_map(df5, active_days)

    daily = df5[df5["day"].isin(active_days)].groupby("day")["close"].last().sort_index()
    rv20_map = (np.log(daily / daily.shift(1)).rolling(20, min_periods=5).std().shift(1) * math.sqrt(252)).fillna(0.35).to_dict()

    gated_days = [
        d for d in active_days
        if ((get_monthly_expiry(d, skip_0dte=True) - d).days <= 10) or (atr_pct_map.get(d, 0.0) >= 3.0)
    ]

    # Measure empirical evening / full-day range ratios & VWAP skip rate on exact 5m data
    strangle_ratios = []
    buyer_ratios = []
    for d in active_days:
        sub = df5[df5["day"] == d]
        d_rng = float(sub["high"].max() - sub["low"].min())
        if d_rng <= 0:
            continue
        ev_s = sub[(sub["time"] >= "20:00") & (sub["time"] <= "23:10")]
        ev_b = sub[(sub["time"] >= "18:00") & (sub["time"] <= "23:10")]
        if len(ev_s) >= 10:
            strangle_ratios.append(float(ev_s["high"].max() - ev_s["low"].min()) / d_rng)
        if len(ev_b) >= 10:
            buyer_ratios.append(float(ev_b["high"].max() - ev_b["low"].min()) / d_rng)

    print("=" * 110)
    print(f"  [PART 1] EXACT 5-MINUTE MCX SILVERM COMBO 2A BACKTEST ({active_days[0]} to {active_days[-1]}, {len(active_days)} Sessions)")
    print("=" * 110)

    # Check both Fixed IV=30% and Dynamic RV20 IV across Realistic / Zero-Crush / Stress-Spread
    str_real = run_strangle_backtest(df5, evening_iv_crush=0.002, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)
    str_zero = run_strangle_backtest(df5, evening_iv_crush=0.0, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)
    str_stre = run_strangle_backtest(df5, evening_iv_crush=0.002, slippage_pct_per_leg_side=0.0050, slippage_pts=0.0)

    rows = []
    for lbl, b_kw, s_df in [
        ("Combo 2A (IV=30%) | Realistic (0.2% crush, 0.25% slip)", dict(rv_map=None, iv_crush=0.002, slip=0.0025), str_real),
        ("Combo 2A (IV=30%) | Zero Crush Stress (0.0% crush)", dict(rv_map=None, iv_crush=0.0, slip=0.0025), str_zero),
        ("Combo 2A (IV=30%) | Wide Spread Stress (0.50%/side slip)", dict(rv_map=None, iv_crush=0.002, slip=0.0050), str_stre),
        ("Combo 2A (Dynamic RV20 IV) | Realistic (0.2% crush)", dict(rv_map=rv20_map, iv_crush=0.002, slip=0.0025), str_real),
        ("Combo 2A (Dynamic RV20 IV) | Zero Crush Stress (0.0% crush)", dict(rv_map=rv20_map, iv_crush=0.0, slip=0.0025), str_zero),
        ("Primary Strangle Only (<=10 DTE, IV=30%) | Realistic", None, str_real),
    ]:
        if b_kw is None:
            b_df = pd.DataFrame(columns=["date", "net_pnl_inr"])
        else:
            b_df = run_exact_5m_buyer_custom(df5, gated_days, **b_kw)
        comb = build_combined(b_df, s_df)
        rows.append(summarize_daily(comb, lbl))

    print(tabulate(
        rows,
        headers=["5m Backtest Variant", "ActiveDays", "Win%", "Buyer Net", "Strangle Net", "Total Net", "PF", "Max DD", "Avg/Day"],
        tablefmt="pipe",
    ))

    # Measure empirical Buyer trigger & alignment rates against full-day direction
    b_ref = run_exact_5m_buyer_custom(df5, gated_days, rv_map=None, iv_crush=0.002, slip=0.0025)
    b_ref["day"] = pd.to_datetime(b_ref["date"]).dt.date
    day_hl = df5[df5["day"].isin(active_days)].groupby("day").agg(open=("open", "first"), close=("close", "last"))
    b_m = b_ref.merge(day_hl, on="day")
    b_m["day_dir"] = np.where(b_m["close"] >= b_m["open"], "CE", "PE")
    trig_rate = len(b_ref) / max(1, len(gated_days))
    align_rate = float((b_m["dir"] == b_m["day_dir"]).mean())
    dte_elig = sum(1 for d in active_days if (get_monthly_expiry(d, skip_0dte=True) - d).days <= 10)
    vwap_pass_rate = len(str_real) / max(1, dte_elig)

    return {
        "strangle_ratio": float(np.mean(strangle_ratios)),
        "buyer_ratio": float(np.mean(buyer_ratios)),
        "trig_rate": trig_rate,
        "align_rate": align_rate,
        "vwap_pass_rate": vwap_pass_rate,
    }


def run_3y_daily_section(cal: dict):
    d3 = pd.read_parquet("data/silverm_daily_3y.parquet")
    d3["date"] = pd.to_datetime(d3["date"])
    d3["day"] = d3["date"].dt.date
    d3 = d3.sort_values("day").reset_index(drop=True)

    pc = d3["close"].shift(1)
    tr = pd.concat([
        d3["high"] - d3["low"],
        (d3["high"] - pc).abs(),
        (d3["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    d3["atr14"] = tr.rolling(14, min_periods=1).mean().shift(1)
    d3["atr_pct"] = d3["atr14"] / pc * 100.0
    d3["rv20"] = np.log(d3["close"] / pc).rolling(20, min_periods=5).std().shift(1) * math.sqrt(252)
    d3["cc_ret"] = np.log(d3["close"] / pc)

    cutoff_3y = date(2023, 11, 26)
    w3 = d3[d3["day"] >= cutoff_3y].copy().reset_index(drop=True)
    w3["year"] = w3["date"].dt.year
    w3["month"] = w3["date"].dt.strftime("%Y-%m")

    print("\n" + "=" * 110)
    print(f"  [PART 2] 3-YEAR CONTINUOUS MCX SILVERM DAILY REGIMES ({w3['day'].min()} to {w3['day'].max()}, {len(w3)} Sessions)")
    print(f"  Note: Kite Connect rejects <1d continuous futures (`invalid interval for continuous data`).")
    print(f"  Calibrated from 5m overlap -> Strangle range={cal['strangle_ratio']:.3f}x, Buyer range={cal['buyer_ratio']:.3f}x, "
          f"Buyer trigger={cal['trig_rate']*100:.1f}%, Signal-Day Align={cal['align_rate']*100:.1f}%")
    print("=" * 110)

    yr_regimes = []
    for yr, g in w3.groupby("year"):
        cal_dtes = g["day"].apply(lambda d: (get_monthly_expiry(d, skip_0dte=True) - d).days)
        rv = float(g["cc_ret"].std() * math.sqrt(252) * 100.0)
        dte_n = int((cal_dtes <= 10).sum())
        atr3_n = int((g["atr_pct"] >= 3.0).sum())
        comb_n = int(((cal_dtes <= 10) | (g["atr_pct"] >= 3.0)).sum())
        yr_regimes.append([
            str(yr), len(g), f"Rs {g['close'].mean():,.0f}", f"{rv:.1f}%",
            f"{g['atr_pct'].mean():.2f}%", dte_n, atr3_n, comb_n,
        ])
    cal_dtes_all = w3["day"].apply(lambda d: (get_monthly_expiry(d, skip_0dte=True) - d).days)
    yr_regimes.append([
        "ALL 3Y", len(w3), f"Rs {w3['close'].mean():,.0f}",
        f"{w3['cc_ret'].std()*math.sqrt(252)*100:.1f}%", f"{w3['atr_pct'].mean():.2f}%",
        int((cal_dtes_all <= 10).sum()), int((w3["atr_pct"] >= 3.0).sum()),
        int(((cal_dtes_all <= 10) | (w3["atr_pct"] >= 3.0)).sum()),
    ])
    print(tabulate(
        yr_regimes,
        headers=["Year", "Days", "Avg Spot", "Ann. RV", "Avg ATR%", "Strangle (DTE<=10)", "High-Vol (ATR>=3%)", "Barbell Buyer Active"],
        tablefmt="pipe",
    ))

    def sim_3y_combo(
        label: str,
        use_rv: bool = True,
        strangle_crush: float = 0.002,
        buyer_crush: float = 0.002,
        slip: float = 0.0025,
        align_prob: float = cal["align_rate"],
        trig_prob: float = cal["trig_rate"],
        vwap_prob: float = 1.0,
    ):
        mult = 10.0
        s_scale = cal["strangle_ratio"]
        b_scale = cal["buyer_ratio"]
        daily_rows = []

        for _, r in w3.iterrows():
            td = r["day"]
            expiry = get_monthly_expiry(td, skip_0dte=True)
            cal_dte = (expiry - td).days
            se = float(r["open"])
            hi = float(r["high"])
            lo = float(r["low"])
            cl = float(r["close"])
            atr14 = float(r["atr14"]) if not pd.isna(r["atr14"]) else se * 0.02
            atr_pct = float(r["atr_pct"]) if not pd.isna(r["atr_pct"]) else 2.0
            iv = max(0.20, float(r["rv20"])) if (use_rv and not pd.isna(r["rv20"])) else 0.30

            # 1) Primary Strangle leg (<= 10 DTE, 0.75x ATR, 25% portfolio SL)
            s_net = 0.0
            s_active = False
            s_iv = max(0.20, float(r["rv20"])) if (use_rv and not pd.isna(r["rv20"])) else 0.30
            if cal_dte <= 10 and vwap_prob > 0:
                dte_e = calculate_dte_fraction(datetime.combine(td, datetime.strptime("20:00", "%H:%M").time()), expiry)
                dte_x = calculate_dte_fraction(datetime.combine(td, datetime.strptime("23:10", "%H:%M").time()), expiry)
                step = get_strike_step(se)
                off = max(atr14 * 0.75, se * 0.01)
                cek = round_to_strike_step(se + off, step)
                pek = round_to_strike_step(se - off, step)
                ce_e = round(black_76_price(se, cek, dte_e, s_iv, opt_type="CE") * (1.0 - slip), 2)
                pe_e = round(black_76_price(se, pek, dte_e, s_iv, opt_type="PE") * (1.0 - slip), 2)
                if ce_e > 0.5 and pe_e > 0.5:
                    s_active = True
                    cap = (ce_e + pe_e) * 0.25 * mult
                    ce_sl = ce_e * 1.40
                    pe_sl = pe_e * 1.40
                    sx = se + (cl - se) * s_scale
                    shi = max(se, sx, se + (hi - se) * s_scale)
                    slo = min(se, sx, se + (lo - se) * s_scale)
                    iv_sx = max(0.15, s_iv - strangle_crush)
                    ce_w = black_76_price(shi, cek, 0.5 * (dte_e + dte_x), s_iv, opt_type="CE")
                    pe_w = black_76_price(slo, pek, 0.5 * (dte_e + dte_x), s_iv, opt_type="PE")
                    ce_c = black_76_price(sx, cek, dte_x, iv_sx, opt_type="CE")
                    pe_c = black_76_price(sx, pek, dte_x, iv_sx, opt_type="PE")
                    ce_ex = round(ce_sl * (1.0 + slip), 2) if ce_w >= ce_sl else round(ce_c * (1.0 + slip), 2)
                    pe_ex = round(pe_sl * (1.0 + slip), 2) if pe_w >= pe_sl else round(pe_c * (1.0 + slip), 2)
                    gross = max(-cap, ((ce_e - ce_ex) + (pe_e - pe_ex)) * mult)
                    costs = (
                        calculate_silver_mcx_costs(ce_e, mult, True)
                        + calculate_silver_mcx_costs(ce_ex, mult, False)
                        + calculate_silver_mcx_costs(pe_e, mult, True)
                        + calculate_silver_mcx_costs(pe_ex, mult, False)
                    )
                    s_net = vwap_prob * (gross - costs)

            # 2) Directional Buyer leg (DTE <= 10 OR prev_close ATR% >= 3.0%)
            b_net = 0.0
            b_active = False
            if (cal_dte <= 10 or atr_pct >= 3.0) and trig_prob > 0:
                dte_e = calculate_dte_fraction(datetime.combine(td, datetime.strptime("18:45", "%H:%M").time()), expiry)
                dte_x = calculate_dte_fraction(datetime.combine(td, datetime.strptime("23:10", "%H:%M").time()), expiry)
                k_ = round_to_strike_step(se, get_strike_step(se))
                ep = round(black_76_price(se, k_, dte_e, s_iv, opt_type="CE") * (1.0 + slip), 2)
                if ep > 20.0:
                    b_active = True
                    up_m = max(0.0, hi - se) * b_scale
                    dn_m = max(0.0, se - lo) * b_scale
                    cl_m = (cl - se) * b_scale
                    day_bull = (cl >= se)
                    iv_bx = max(0.15, s_iv - buyer_crush)

                    def _b_out(ot: str) -> float:
                        sx_b = se + cl_m
                        ws_b = (se - dn_m) if ot == "CE" else (se + up_m)
                        bs_b = (se + up_m) if ot == "CE" else (se - dn_m)
                        vc = black_76_price(sx_b, k_, dte_x, iv_bx, opt_type=ot)
                        vw = black_76_price(ws_b, k_, 0.5 * (dte_e + dte_x), s_iv, opt_type=ot)
                        vb = black_76_price(bs_b, k_, 0.5 * (dte_e + dte_x), s_iv, opt_type=ot)
                        eff_sl = max(ep * 0.70, ep - 600.0)
                        tgt = ep * 1.60
                        if vw <= eff_sl:
                            ex = round(eff_sl * (1.0 - slip), 2)
                        elif vb >= tgt:
                            ex = round(tgt * (1.0 - slip), 2)
                        else:
                            ex = round(vc * (1.0 - slip), 2)
                        g = (ex - ep) * mult
                        c = calculate_silver_mcx_costs(ep, mult, False) + calculate_silver_mcx_costs(ex, mult, True)
                        return g - c

                    trend_out = _b_out("CE" if day_bull else "PE")
                    counter_out = _b_out("PE" if day_bull else "CE")
                    b_net = trig_prob * (align_prob * trend_out + (1.0 - align_prob) * counter_out)

            if s_active or b_active:
                daily_rows.append({
                    "date": str(td),
                    "year": td.year,
                    "buyer_net": round(b_net, 2),
                    "strangle_net": round(s_net, 2),
                    "total_net": round(b_net + s_net, 2),
                })

        cdf = pd.DataFrame(daily_rows)
        return cdf, summarize_daily(cdf, label)

    print("\n  3-YEAR COMBO 2A BARBELL PORTFOLIO PROXY (2023-11-26 to 2026-11-25):")
    variants = [
        ("1. Combo 2A Barbell (Dynamic RV20 IV) | Realistic (0.2% crush, 72.1% align)", dict(use_rv=True, strangle_crush=0.002, align_prob=cal["align_rate"])),
        ("2. Combo 2A Barbell (Fixed 30% IV)    | Realistic (0.2% crush, 72.1% align)", dict(use_rv=False, strangle_crush=0.002, align_prob=cal["align_rate"])),
        ("3. Combo 2A Barbell (Dynamic RV20 IV) | Zero Strangle Crush + 62.5% Align Stress", dict(use_rv=True, strangle_crush=0.0, align_prob=0.625)),
        ("4. Combo 2A Barbell (Dynamic RV20 IV) | VWAP-Scaled Strangle (0.82x entries)", dict(use_rv=True, strangle_crush=0.002, vwap_prob=cal["vwap_pass_rate"])),
        ("5. Primary Strangle Only (<=10 DTE, Dynamic RV20 IV) | Realistic", dict(use_rv=True, strangle_crush=0.002, trig_prob=0.0)),
        ("6. Primary Strangle Only (<=10 DTE, Fixed 30% IV)    | Zero Crush Stress", dict(use_rv=False, strangle_crush=0.0, trig_prob=0.0)),
        ("7. Directional Buyer Baseline (Unbiased 50% Coin Flip, No Signal Edge)", dict(use_rv=True, strangle_crush=0.002, vwap_prob=0.0, align_prob=0.50)),
    ]

    v_rows = []
    focus_3y = None
    focus_stress = None
    for lbl, kw in variants:
        cdf, s = sim_3y_combo(lbl, **kw)
        if lbl.startswith("1. Combo 2A Barbell"):
            focus_3y = cdf
        elif lbl.startswith("3. Combo 2A Barbell"):
            focus_stress = cdf
        v_rows.append(s)

    print(tabulate(
        v_rows,
        headers=["3-Year Strategy / Regime", "ActiveDays", "Win%", "Buyer Net", "Strangle Net", "Total Net", "PF", "Max DD", "Avg/Day"],
        tablefmt="pipe",
    ))

    for title, c_df in [
        ("YEAR-BY-YEAR BREAKDOWN — Combo 2A (Dynamic RV20 IV, Calibrated 72.1% Signal Align)", focus_3y),
        ("YEAR-BY-YEAR BREAKDOWN — Combo 2A Stress (0.0% Strangle Crush + 62.5% Align)", focus_stress),
    ]:
        if c_df is not None and not c_df.empty:
            y_rows = []
            for yr, g in c_df.groupby("year"):
                y_rows.append(summarize_daily(g, str(yr)))
            y_rows.append(summarize_daily(c_df, "ALL 3Y"))
            print(f"\n=== {title} ===")
            print(tabulate(
                y_rows,
                headers=["Year", "ActiveDays", "Win%", "Buyer Net", "Strangle Net", "Total Net", "PF", "Max DD", "Avg/Day"],
                tablefmt="pipe",
            ))


def main():
    cal = run_exact_5m_section()
    run_3y_daily_section(cal)


if __name__ == "__main__":
    main()
