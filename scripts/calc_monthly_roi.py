import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_orb_risk_controlled import run_portfolio_simulation

df_tr, df_day, cb = run_portfolio_simulation(
    apply_wick_filter=True,
    apply_pdh_pdl_filter=True,
    apply_nifty_filter=True,
    sizing_mode="RISK_PARITY",
    fixed_risk_rs=2500,
    max_trades_per_day=5,
    daily_loss_limit_rs=10000
)

df_tr["date_dt"] = pd.to_datetime(df_tr["date"])
df_tr["month"] = df_tr["date_dt"].dt.to_period("M")

df_day["date_dt"] = pd.to_datetime(df_day["date"])
df_day["month"] = df_day["date_dt"].dt.to_period("M")

print("=" * 80)
print("=== MONTH-BY-MONTH PERFORMANCE (SETUP B2: Risk Parity Rs 2,500 Risk) ===")
print("=" * 80)

monthly_days = df_day.groupby("month").agg(
    trading_days=("pnl_rs", "count"),
    pnl_rs=("pnl_rs", "sum"),
    win_days=("pnl_rs", lambda x: (x > 0).sum()),
    loss_days=("pnl_rs", lambda x: (x < 0).sum())
).reset_index()

monthly_trades = df_tr.groupby("month").agg(
    trades=("total_pnl_rs", "count"),
    win_trades=("total_pnl_r", lambda x: (x > 0).sum()),
    win_rate=("total_pnl_r", lambda x: (x > 0).mean() * 100),
    profit_factor=("total_pnl_rs", lambda x: x[x > 0].sum() / abs(x[x < 0].sum()) if abs(x[x < 0].sum()) > 0 else np.nan)
).reset_index()

m_summary = pd.merge(monthly_days, monthly_trades, on="month")

# Margin & Capital analysis
# For equity intraday (MIS), SEBI mandates 20% margin (5x leverage)
df_tr["trade_value"] = df_tr["entry_trigger"] * df_tr["qty"]
df_tr["mis_margin"] = df_tr["trade_value"] * 0.20

# Let's find maximum concurrent margin used on any given day
# For each day, sum trade_value of trades that day
daily_exposure = df_tr.groupby("date")["trade_value"].sum()
max_daily_gross_exposure = daily_exposure.max()
avg_daily_gross_exposure = daily_exposure.mean()
max_daily_mis_margin = max_daily_gross_exposure * 0.20
avg_daily_mis_margin = avg_daily_gross_exposure * 0.20

print(f"Total Trading Days in Backtest : {len(df_day)} days (approx. 4 months)")
print(f"Total Net P&L                  : Rs {df_tr['total_pnl_rs'].sum():,.2f}")
print(f"Average Monthly Net P&L        : Rs {df_tr['total_pnl_rs'].sum() / len(m_summary):,.2f}")
print(f"Max Daily Peak-to-Trough DD    : Rs {df_day['pnl_rs'].cumsum().cummax().sub(df_day['pnl_rs'].cumsum()).max():,.2f}")
print("-" * 80)
print(f"Average Gross Exposure per Day : Rs {avg_daily_gross_exposure:,.0f} (MIS Margin ~Rs {avg_daily_mis_margin:,.0f})")
print(f"Peak Gross Exposure on any Day : Rs {max_daily_gross_exposure:,.0f} (Peak MIS Margin ~Rs {max_daily_mis_margin:,.0f})")
print("=" * 80)

# Capital Base options
capital_options = [
    ("Conservative Capital (1% Risk / Trade)", 250000),   # Rs 2,500 = 1% of 2.5L
    ("Moderate Capital (1.25% Risk / Trade)", 200000),    # Rs 2,500 = 1.25% of 2L
    ("Aggressive Capital (1.67% Risk / Trade)", 150000),  # Rs 2,500 = 1.67% of 1.5L
]

print("\n--- MONTHLY BREAKDOWN & ROI MATRIX ---")
for cap_name, cap_val in capital_options:
    print(f"\n>> {cap_name} : Capital = Rs {cap_val:,.0f}")
    print(f"{'Month':<10} | {'Days':<5} | {'Trades':<6} | {'Win Rate':<8} | {'PF':<5} | {'Net P&L (Rs)':<14} | {'Monthly ROI %':<14}")
    print("-" * 75)
    for _, row in m_summary.iterrows():
        roi = (row["pnl_rs"] / cap_val) * 100
        print(f"{str(row['month']):<10} | {row['trading_days']:>4} | {row['trades']:>6} | {row['win_rate']:>7.1f}% | {row['profit_factor']:>4.2f} | Rs {row['pnl_rs']:>11,.0f} | {roi:>12.2f}%")
    total_roi = (df_tr["total_pnl_rs"].sum() / cap_val) * 100
    avg_m_roi = (df_tr["total_pnl_rs"].sum() / len(m_summary) / cap_val) * 100
    print("-" * 75)
    print(f"{'TOTAL':<10} | {len(df_day):>4} | {len(df_tr):>6} | {df_tr['total_pnl_r'].gt(0).mean()*100:>7.1f}% | {df_tr['total_pnl_rs'][df_tr['total_pnl_rs']>0].sum()/abs(df_tr['total_pnl_rs'][df_tr['total_pnl_rs']<0].sum()):>4.2f} | Rs {df_tr['total_pnl_rs'].sum():>11,.0f} | {total_roi:>12.2f}% (Avg: {avg_m_roi:.2f}%/mo)")
