import sys
from pathlib import Path
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_silver_strangle import run_strangle_backtest


def summarize(tdf: pd.DataFrame, label: str):
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
        label,
        n,
        f"{wr}%",
        f"Rs {gross:+,.0f}",
        f"Rs {costs:,.0f}",
        f"Rs {net:+,.0f}",
        pf,
        f"Rs {dd:+,.0f}",
        f"Rs {avg_day:+,.0f}",
    ]


def main():
    df = pd.read_parquet("data/silverm_5m_1year.parquet")
    df["date"] = pd.to_datetime(df["date"])
    print(f"Loaded SILVERM 5m: {len(df)} bars ({df['date'].min().date()} to {df['date'].max().date()})")

    variants = [
        ("0. Baseline (1.5% OTM, 20:00, Rs 6k Cap)", dict(strike_mode="PERCENT", strike_otm_pct=0.015, vwap_entry=False)),
        ("1. + 20:00 Comb < VWAP Entry", dict(strike_mode="PERCENT", strike_otm_pct=0.015, vwap_entry=True)),
        ("2. + ATR(14) x 0.75 Strikes", dict(strike_mode="ATR", atr_multiplier=0.75, vwap_entry=False)),
        ("3. + ATR(14) x 0.75 + Comb < VWAP + 25% PortSL", dict(strike_mode="ATR", atr_multiplier=0.75, vwap_entry=True, portfolio_sl_pct=0.25)),
        ("4. + 2.5% OTM + Comb < VWAP + 25% PortSL", dict(strike_mode="PERCENT", strike_otm_pct=0.025, vwap_entry=True, portfolio_sl_pct=0.25)),
        ("5. + DTE<=10 + ATR 0.75x + Comb < VWAP + 25% PortSL", dict(strike_mode="ATR", atr_multiplier=0.75, vwap_entry=True, portfolio_sl_pct=0.25, max_dte_days=10)),
    ]

    regimes = [
        ("REGIME A: REALISTIC VRP (0.2% Evening IV Crush, 0.25%/side Slippage)", dict(evening_iv_crush=0.002, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)),
        ("REGIME B: ZERO IV CRUSH STRESS (0.0% IV Crush, 0.25%/side Slippage)", dict(evening_iv_crush=0.0, slippage_pct_per_leg_side=0.0025, slippage_pts=0.0)),
        ("REGIME C: ZERO IV CRUSH + ZERO SLIPPAGE (0.0% Crush, 0.5pt Tick Slip)", dict(evening_iv_crush=0.0, slippage_pct_per_leg_side=0.0, slippage_pts=0.5)),
    ]

    for r_title, r_kw in regimes:
        rows = []
        for v_label, v_kw in variants:
            res = run_strangle_backtest(df, **v_kw, **r_kw)
            rows.append(summarize(res, v_label))
        print(f"\n=== {r_title} ===")
        print(tabulate(
            rows,
            headers=["Variant", "Trades", "Win%", "Gross PnL", "MCX Costs", "Net PnL", "PF", "Max DD", "Avg/Trade"],
            tablefmt="pipe",
        ))


if __name__ == "__main__":
    main()
