import os
import sys
from pathlib import Path
import pandas as pd
import numpy as np

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.backtest import run_backtest_simulation

def main():
    parquet_path = "data/nifty_5m_3y.parquet"
    if not os.path.exists(parquet_path):
        print(f"Error: {parquet_path} not found.")
        sys.exit(1)

    df_3y = pd.read_parquet(parquet_path)
    print(f"Loaded {len(df_3y)} 5m bars from {df_3y['date'].min()} to {df_3y['date'].max()}\n")

    # Common parameters
    slippage = 0.50
    lot_size = 65
    lots = 10
    strategy_max_loss = 10000.0

    print("Running simulations across 3 years (Nov 2023 - Nov 2026)...")

    # 1. Current Config: Adaptive 1.25x ATR Strikes + Morning Filter + Single-Leg SL with Trail to Cost
    print("1. Running Current Config (1.25x ATR Strikes + Morning Filter)...")
    res_current = run_backtest_simulation(
        df_3y,
        slippage=slippage,
        lots=lots,
        lot_size=lot_size,
        use_atr_strikes=True,
        atr_multiplier=1.25,
        morning_filter=True,
        morning_filter_threshold_pct=0.60,
        use_leg_sl=True,
        trail_opposite_to_cost=True,
        strategy_max_loss=strategy_max_loss,
    )

    # 2. Config B: Nearest > 30 Premium Strikes WITH Morning Filter + Single-Leg SL with Trail to Cost
    print("2. Running Nearest > 30 Premium Strikes (WITH Morning Filter)...")
    res_30_filter = run_backtest_simulation(
        df_3y,
        slippage=slippage,
        lots=lots,
        lot_size=lot_size,
        use_atr_strikes=False,
        target_price=30.0,
        morning_filter=True,
        morning_filter_threshold_pct=0.60,
        use_leg_sl=True,
        trail_opposite_to_cost=True,
        strategy_max_loss=strategy_max_loss,
    )

    # 3. Config C: Nearest > 30 Premium Strikes WITHOUT Morning Filter (Raw Static 30)
    print("3. Running Nearest > 30 Premium Strikes (WITHOUT Morning Filter - All Days)...")
    res_30_raw = run_backtest_simulation(
        df_3y,
        slippage=slippage,
        lots=lots,
        lot_size=lot_size,
        use_atr_strikes=False,
        target_price=30.0,
        morning_filter=False,
        use_leg_sl=True,
        trail_opposite_to_cost=True,
        strategy_max_loss=strategy_max_loss,
    )

    # Prepare Summaries
    s_curr = res_current["summary"]
    s_30f = res_30_filter["summary"]
    s_30r = res_30_raw["summary"]

    t_curr = pd.DataFrame(res_current["trades"])
    t_30f = pd.DataFrame(res_30_filter["trades"])
    t_30r = pd.DataFrame(res_30_raw["trades"])

    m_curr = pd.DataFrame(res_current["monthly"])
    m_30f = pd.DataFrame(res_30_filter["monthly"])
    m_30r = pd.DataFrame(res_30_raw["monthly"])

    # Additional calculations
    def get_extra_stats(tdf, mdf):
        profitable_months = (mdf["net_pnl"] > 0).sum() if not mdf.empty else 0
        total_months = len(mdf)
        worst_month = mdf["net_pnl"].min() if not mdf.empty else 0.0
        best_month = mdf["net_pnl"].max() if not mdf.empty else 0.0
        # Average strike distance OTM
        tdf['ce_otm'] = tdf['ce_strike'] - tdf['spot_10am']
        tdf['pe_otm'] = tdf['spot_10am'] - tdf['pe_strike']
        avg_otm = (tdf['ce_otm'].mean() + tdf['pe_otm'].mean()) / 2.0
        avg_ce_entry = tdf['ce_entry'].mean()
        avg_pe_entry = tdf['pe_entry'].mean()
        return {
            "profitable_months": f"{profitable_months}/{total_months} ({profitable_months/total_months*100:.1f}%)",
            "worst_month": worst_month,
            "best_month": best_month,
            "avg_otm_pts": avg_otm,
            "avg_ce_entry": avg_ce_entry,
            "avg_pe_entry": avg_pe_entry,
        }

    e_curr = get_extra_stats(t_curr, m_curr)
    e_30f = get_extra_stats(t_30f, m_30f)
    e_30r = get_extra_stats(t_30r, m_30r)

    print("\n" + "=" * 125)
    print("                    NIFTY SHORT STRANGLE BACKTEST COMPARISON (3 YEARS: NOV 2023 - NOV 2026)")
    print("                 (10 LOTS | LOT SIZE: 65 | SLIPPAGE: 0.50 PTS/LEG | BROKERAGE & TAXES INCLUDED)")
    print("=" * 125)
    headers = ["Metric", "Current (1.25x ATR + Filter)", "Nearest > 30 (WITH Filter)", "Nearest > 30 (NO Filter)"]
    rows = [
        ["Days Traded / Evaluated", f"{s_curr['total_days_traded']} traded", f"{s_30f['total_days_traded']} traded", f"{s_30r['total_days_traded']} traded"],
        ["Days Skipped (Trend Filter)", f"{len(t_30r) - len(t_curr)} days skipped", f"{len(t_30r) - len(t_30f)} days skipped", "0 days (trades all)"],
        ["Win Days / Loss Days", f"{s_curr['win_days']}W / {s_curr['loss_days']}L", f"{s_30f['win_days']}W / {s_30f['loss_days']}L", f"{s_30r['win_days']}W / {s_30r['loss_days']}L"],
        ["Win Rate (%)", f"{s_curr['win_rate_pct']:.2f}%", f"{s_30f['win_rate_pct']:.2f}%", f"{s_30r['win_rate_pct']:.2f}%"],
        ["Profit Factor", f"{s_curr['profit_factor']:.2f}", f"{s_30f['profit_factor']:.2f}", f"{s_30r['profit_factor']:.2f}"],
        ["Average OTM Distance", f"{e_curr['avg_otm_pts']:.1f} pts OTM", f"{e_30f['avg_otm_pts']:.1f} pts OTM", f"{e_30r['avg_otm_pts']:.1f} pts OTM"],
        ["Average Premium Sold (CE/PE)", f"Rs {e_curr['avg_ce_entry']:.1f} / Rs {e_curr['avg_pe_entry']:.1f}", f"Rs {e_30f['avg_ce_entry']:.1f} / Rs {e_30f['avg_pe_entry']:.1f}", f"Rs {e_30r['avg_ce_entry']:.1f} / Rs {e_30r['avg_pe_entry']:.1f}"],
        ["Gross Realized PnL (Rs)", f"Rs {s_curr['gross_pnl']:>12,.2f}", f"Rs {s_30f['gross_pnl']:>12,.2f}", f"Rs {s_30r['gross_pnl']:>12,.2f}"],
        ["Statutory Taxes & Brokerage", f"Rs {s_curr['total_taxes_and_brokerage']:>12,.2f}", f"Rs {s_30f['total_taxes_and_brokerage']:>12,.2f}", f"Rs {s_30r['total_taxes_and_brokerage']:>12,.2f}"],
        ["Slippage Deducted (Rs)", f"Rs {s_curr['total_slippage']:>12,.2f}", f"Rs {s_30f['total_slippage']:>12,.2f}", f"Rs {s_30r['total_slippage']:>12,.2f}"],
        ["Net Realized PnL (3 Years)", f"Rs {s_curr['net_pnl']:>12,.2f}", f"Rs {s_30f['net_pnl']:>12,.2f}", f"Rs {s_30r['net_pnl']:>12,.2f}"],
        ["Average Monthly Net PnL", f"Rs {s_curr['net_pnl']/len(m_curr):>12,.2f} / mo", f"Rs {s_30f['net_pnl']/len(m_30f):>12,.2f} / mo", f"Rs {s_30r['net_pnl']/len(m_30r):>12,.2f} / mo"],
        ["Max Historical Drawdown (Rs)", f"Rs {s_curr['max_drawdown']:>12,.2f}", f"Rs {s_30f['max_drawdown']:>12,.2f}", f"Rs {s_30r['max_drawdown']:>12,.2f}"],
        ["Return / Max Drawdown Ratio", f"{s_curr['net_pnl']/abs(s_curr['max_drawdown']):.1f}x", f"{s_30f['net_pnl']/abs(s_30f['max_drawdown']):.1f}x", f"{s_30r['net_pnl']/abs(s_30r['max_drawdown']):.1f}x"],
        ["Profitable Months", e_curr["profitable_months"], e_30f["profitable_months"], e_30r["profitable_months"]],
        ["Worst Month PnL (Rs)", f"Rs {e_curr['worst_month']:>12,.2f}", f"Rs {e_30f['worst_month']:>12,.2f}", f"Rs {e_30r['worst_month']:>12,.2f}"],
        ["Best Month PnL (Rs)", f"Rs {e_curr['best_month']:>12,.2f}", f"Rs {e_30f['best_month']:>12,.2f}", f"Rs {e_30r['best_month']:>12,.2f}"],
    ]

    col_w = [32, 28, 28, 28]
    def print_table(headers, rows):
        header_str = " | ".join(f"{h:<{w}}" for h, w in zip(headers, col_w))
        print(header_str)
        print("-" * len(header_str))
        for row in rows:
            line = " | ".join(f"{str(c):<{w}}" for c, w in zip(row, col_w))
            print(line)

    print_table(headers, rows)
    print("=" * 125)

    # Also compute 1-year recent performance (last 13 months: Nov 2025 to Nov 2026)
    m_curr['month'] = m_curr['month'].astype(str)
    m_30f['month'] = m_30f['month'].astype(str)
    m_30r['month'] = m_30r['month'].astype(str)

    m1y_curr = m_curr[m_curr['month'] >= "2025-11"]
    m1y_30f = m_30f[m_30f['month'] >= "2025-11"]
    m1y_30r = m_30r[m_30r['month'] >= "2025-11"]

    print("\n" + "=" * 125)
    print("                       MOST RECENT 1-YEAR PERFORMANCE (NOV 2025 - NOV 2026)")
    print("=" * 125)
    r1y = [
        ["1-Year Net PnL (10 Lots)", f"Rs {m1y_curr['net_pnl'].sum():>12,.2f}", f"Rs {m1y_30f['net_pnl'].sum():>12,.2f}", f"Rs {m1y_30r['net_pnl'].sum():>12,.2f}"],
        ["1-Year Avg Monthly Net", f"Rs {m1y_curr['net_pnl'].mean():>12,.2f} / mo", f"Rs {m1y_30f['net_pnl'].mean():>12,.2f} / mo", f"Rs {m1y_30r['net_pnl'].mean():>12,.2f} / mo"],
        ["Monthly ROI on Rs 20L (10 Lots)", f"{m1y_curr['net_pnl'].mean()/2000000*100:.2f}% / mo", f"{m1y_30f['net_pnl'].mean()/2000000*100:.2f}% / mo", f"{m1y_30r['net_pnl'].mean()/2000000*100:.2f}% / mo"],
        ["1-Year Net PnL (20 Lots)", f"Rs {m1y_curr['net_pnl'].sum()*2:>12,.2f}", f"Rs {m1y_30f['net_pnl'].sum()*2:>12,.2f}", f"Rs {m1y_30r['net_pnl'].sum()*2:>12,.2f}"],
        ["Monthly ROI on Rs 50L (20 Lots)", f"{(m1y_curr['net_pnl'].mean()*2)/5000000*100:.2f}% / mo", f"{(m1y_30f['net_pnl'].mean()*2)/5000000*100:.2f}% / mo", f"{(m1y_30r['net_pnl'].mean()*2)/5000000*100:.2f}% / mo"],
    ]
    print_table(headers[:1] + headers[1:], r1y)
    print("=" * 125)

if __name__ == "__main__":
    main()
