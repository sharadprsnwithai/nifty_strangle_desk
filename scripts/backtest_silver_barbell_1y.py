import sys
from pathlib import Path
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_silver_directional_buyer import load_silverm_5m_latest, run_vwap_vol_buyer
from scripts.backtest_silver_strangle import run_strangle_backtest, get_monthly_expiry


def compute_atr_pct_map(df: pd.DataFrame, active_days) -> dict:
    daily = (
        df[df["day"].isin(active_days)]
        .groupby("day")
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        .sort_index()
    )
    pc = daily["close"].shift(1)
    tr = pd.concat(
        [daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    atr_pct = (tr.rolling(14, min_periods=1).mean().shift(1) / pc * 100.0).fillna(2.0)
    return atr_pct.to_dict()


def summarize_daily(comb_df: pd.DataFrame, label: str):
    if comb_df.empty:
        return [label, 0, "0.0%", "Rs 0", "Rs 0", "Rs 0", 0.0, "Rs 0", "Rs 0"]
    n = len(comb_df)
    wr = round((comb_df["total_net"] > 0).mean() * 100.0, 1)
    b_net = comb_df["buyer_net"].sum()
    s_net = comb_df["strangle_net"].sum()
    tot = comb_df["total_net"].sum()
    gw = comb_df[comb_df["total_net"] > 0]["total_net"].sum()
    gl = abs(comb_df[comb_df["total_net"] < 0]["total_net"].sum())
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    cum = pd.concat([pd.Series([0.0]), comb_df["total_net"]]).cumsum()
    dd = (cum - cum.cummax()).min()
    return [label, n, f"{wr}%", f"Rs {b_net:+,.0f}", f"Rs {s_net:+,.0f}", f"Rs {tot:+,.0f}", pf, f"Rs {dd:+,.0f}", f"Rs {tot/n:+,.0f}"]


def build_combined(buyer_df: pd.DataFrame, strangle_df: pd.DataFrame) -> pd.DataFrame:
    b = buyer_df.groupby("date", as_index=False)["net_pnl_inr"].sum().rename(columns={"net_pnl_inr": "buyer_net"})
    s = strangle_df.copy()
    s["date"] = s["date"].astype(str)
    s = s.groupby("date", as_index=False)["net_pnl_inr"].sum().rename(columns={"net_pnl_inr": "strangle_net"})
    comb = pd.merge(b, s, on="date", how="outer").fillna(0.0).sort_values("date").reset_index(drop=True)
    comb["total_net"] = comb["buyer_net"] + comb["strangle_net"]
    comb["month"] = comb["date"].str.slice(0, 7)
    return comb


def main():
    df = load_silverm_5m_latest()
    day_vol = df.groupby("day")["volume"].sum()
    active_days = sorted(day_vol[day_vol > 1000].index)
    atr_pct_map = compute_atr_pct_map(df, active_days)

    print(f"Liquid MCX SILVERM 5m dataset: {len(active_days)} business days ({active_days[0]} to {active_days[-1]})")

    configs = [
        ("Combo 2A: Buyer (DTE<=10 OR ATR%>=3.0%, no EMA exit) + Primary Strangle", 3.0, False),
        ("Combo 2B: Buyer (DTE<=10 OR ATR%>=3.0%, EMA9/21 exit) + Primary Strangle", 3.0, True),
        ("Combo 2C: Buyer (DTE<=10 OR ATR%>=2.5%, no EMA exit) + Primary Strangle", 2.5, False),
        ("Combo 2D: Buyer (DTE<=10 OR ATR%>=2.5%, EMA9/21 exit) + Primary Strangle", 2.5, True),
        ("Combo 1 (Ref): Both DTE<=10 Only (no EMA exit)", 99.0, False),
        ("Primary Strangle Only (<=10 DTE)", None, False),
    ]

    regimes = [
        ("REGIME A: REALISTIC (0.2% IV Crush, 0.25%/side Slip)", 0.002, 0.0025),
        ("REGIME B: ZERO IV CRUSH (0.0% IV Crush, 0.25%/side Slip)", 0.0, 0.0025),
        ("REGIME C: STRESS SPREAD (0.2% IV Crush, 0.50%/side Slip)", 0.002, 0.0050),
    ]

    focus_comb = None
    focus_2a = None

    for r_lbl, crush_v, slip_v in regimes:
        strangle_df = run_strangle_backtest(
            df, evening_iv_crush=crush_v, slippage_pct_per_leg_side=slip_v, slippage_pts=0.0
        )
        rows = []
        for c_lbl, thr, ema_ex in configs:
            if thr is None:
                comb = build_combined(pd.DataFrame(columns=["date", "net_pnl_inr"]), strangle_df)
            else:
                gated_days = [
                    d for d in active_days
                    if ((get_monthly_expiry(d, skip_0dte=True) - d).days <= 10) or (atr_pct_map.get(d, 0.0) >= thr)
                ]
                buyer_df = run_vwap_vol_buyer(
                    df, gated_days, max_loss_rs=6000.0, sl_pct=0.30, tgt_pct=0.60,
                    ema_exit=ema_ex, iv_crush=crush_v, slip=slip_v
                )
                comb = build_combined(buyer_df, strangle_df)
                if r_lbl.startswith("REGIME A") and c_lbl.startswith("Combo 2B"):
                    focus_comb = comb
                if r_lbl.startswith("REGIME A") and c_lbl.startswith("Combo 2A"):
                    focus_2a = comb
            rows.append(summarize_daily(comb, c_lbl))

        print(f"\n=== 1-YEAR BARBELL PORTFOLIO ({r_lbl}) ===")
        print(tabulate(
            rows,
            headers=["Portfolio Variant", "ActiveDays", "Win%", "Buyer Net", "Strangle Net", "Total Net", "PF", "Max DD", "Avg/Day"],
            tablefmt="pipe",
        ))

    for title, c_df in [
        ("MONTHLY BREAKDOWN — Combo 2A (DTE<=10 OR ATR%>=3.0%, no EMA exit | Realistic)", focus_2a),
        ("MONTHLY BREAKDOWN — Combo 2B (DTE<=10 OR ATR%>=3.0%, EMA9/21 exit | Realistic)", focus_comb),
    ]:
        if c_df is not None and not c_df.empty:
            m = c_df.groupby("month").agg(
                days=("total_net", "count"),
                wins=("total_net", lambda s: int((s > 0).sum())),
                buyer=("buyer_net", "sum"),
                strangle=("strangle_net", "sum"),
                total=("total_net", "sum"),
            ).reset_index()
            m["win_pct"] = (m["wins"] / m["days"] * 100).round(1)
            m_rows = [
                [r["month"], r["days"], f"{r['wins']}W/{r['days']-r['wins']}L", f"{r['win_pct']}%",
                 f"Rs {r['buyer']:+,.0f}", f"Rs {r['strangle']:+,.0f}", f"Rs {r['total']:+,.0f}"]
                for _, r in m.iterrows()
            ]
            print(f"\n=== {title} ===")
            print(tabulate(m_rows, headers=["Month", "Days", "W/L", "Win%", "Buyer Net", "Strangle Net", "Total Net"], tablefmt="pipe"))

    if focus_2a is not None and not focus_2a.empty:
        print("\n=== LAST 10 ACTIVE PORTFOLIO DAYS — Combo 2A (Realistic) ===")
        t10 = focus_2a.tail(10)
        t_rows = [
            [r["date"], f"Rs {r['buyer_net']:+,.0f}", f"Rs {r['strangle_net']:+,.0f}", f"Rs {r['total_net']:+,.0f}"]
            for _, r in t10.iterrows()
        ]
        print(tabulate(t_rows, headers=["Date", "Buyer Net", "Strangle Net", "Combined Net"], tablefmt="pipe"))
        print(f"Last 10 days combined: Wins={(t10['total_net']>0).sum()}/10 | Total=Rs {t10['total_net'].sum():+,.0f}")


if __name__ == "__main__":
    main()
