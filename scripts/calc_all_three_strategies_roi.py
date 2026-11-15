import pandas as pd
import numpy as np

# Load 3 strategies side-by-side
df = pd.read_csv('data/three_strategies_1year_side_by_side.csv')
df_s10 = pd.read_csv('data/strangle_10lots_1year_monthly_report.csv')

# Merge 10-lot strangle into it
df = pd.merge(df, df_s10[['Month', 'Net Realized PnL (Rs)']], on='Month', suffixes=('', '_Strangle10L'))
df['Total_Prod_PnL'] = df['Net Realized PnL (Rs)_Strangle10L'] + df['Net PnL (Rs)'] + df['Silver_NetPnL']

# Capital calculations
# Strangle 2L: Margin 2.50L, Safe Capital: 2.75L
# Strangle 10L: Margin 12.50L, Safe Capital: 13.50L (or 20.0L conservative)
# Stock ORB 2L: Peak Margin 11.44L, Safe Capital: 12.00L
# Silver Mini 2L: Margin 1.50L, Safe Capital: 2.15L

# Combined Setup A (2L Strangle + 2L ORB + 2L Silver):
# Peak intraday margin: 2.50L + 11.44L = 13.94L
# Silver evening margin: 1.50L
# Total Capital required (Unified Account): 13.94L + Drawdown Buffer (~1.5L) = 15.50 Lakhs
# If Segregated (Equity F&O + Commodity): 14.50L + 2.00L = 16.50 Lakhs

# Combined Setup B (Production: 10L Strangle + 2L ORB + 2L Silver):
# Peak intraday margin: 12.50L + 11.44L = 23.94L
# Silver evening margin: 1.50L
# Total Capital required (Unified Account): 23.94L + Drawdown Buffer (~2.5L) = 26.50 Lakhs (or 30 Lakhs conservative)

print("=" * 110)
print("                   STRATEGY-BY-STRATEGY 1-YEAR PERFORMANCE SUMMARY")
print("=" * 110)

s2_pnl = df['Net Realized PnL (Rs)'].sum()
s10_pnl = df['Net Realized PnL (Rs)_Strangle10L'].sum()
orb_pnl = df['Net PnL (Rs)'].sum()
sil_pnl = df['Silver_NetPnL'].sum()

strategies = [
    {
        "name": "1A. NIFTY 10:00 AM Short Strangle (2 Lots)",
        "pnl": s2_pnl,
        "peak_margin": 250000.0,
        "safe_capital": 275000.0,
        "max_dd": 10422.70,
        "wr": df['Wins_Strangle'].sum() / df['Trading Days'].sum() * 100,
        "win_months": f"{(df['Net Realized PnL (Rs)'] > 0).sum()} / {len(df)}"
    },
    {
        "name": "1B. NIFTY 10:00 AM Short Strangle (10 Lots)",
        "pnl": s10_pnl,
        "peak_margin": 1250000.0,
        "safe_capital": 1350000.0,
        "max_dd": 48714.94,
        "wr": df_s10['Wins'].sum() / df_s10['Trading Days'].sum() * 100,
        "win_months": f"{(df['Net Realized PnL (Rs)_Strangle10L'] > 0).sum()} / {len(df)}"
    },
    {
        "name": "2. Stock 15-Min ORB Desk (Static 30, 2 Lots)",
        "pnl": orb_pnl,
        "peak_margin": 1143672.32,
        "safe_capital": 1200000.0,
        "max_dd": 27231.77,
        "wr": df['Wins_ORB'].sum() / df['Trades'].sum() * 100,
        "win_months": f"{(df['Net PnL (Rs)'] > 0).sum()} / {len(df)}"
    },
    {
        "name": "3. MCX Silver Mini 18:00 US Breakout (2 Lots)",
        "pnl": sil_pnl,
        "peak_margin": 150000.0,
        "safe_capital": 215000.0,
        "max_dd": 31150.00,
        "wr": df['Silver_Wins'].sum() / df['Silver_Trades'].sum() * 100,
        "win_months": f"{(df['Silver_NetPnL'] > 0).sum()} / {len(df)}"
    }
]

for s in strategies:
    roi_margin = (s["pnl"] / s["peak_margin"]) * 100.0
    roi_safe = (s["pnl"] / s["safe_capital"]) * 100.0
    print(f"Strategy: {s['name']}")
    print(f"  - Net Realized P&L     : Rs. {s['pnl']:>12,.2f}")
    print(f"  - Peak Margin Deployed : Rs. {s['peak_margin']:>12,.2f}")
    print(f"  - Safe Capital Required: Rs. {s['safe_capital']:>12,.2f}  (Peak Margin + 2x Max DD buffer)")
    print(f"  - Max Historical DD    : Rs. {s['max_dd']:>12,.2f}")
    print(f"  - Win Rate (Win Months): {s['wr']:.1f}% ({s['win_months']} months profitable)")
    print(f"  - ROI on Safe Capital  : {roi_safe:>10.2f}%")
    print(f"  - ROI on Peak Margin   : {roi_margin:>10.2f}%\n")

print("=" * 110)
print("                   COMBINED MULTI-STRATEGY PORTFOLIO")
print("=" * 110)

# Portfolio A: 2 Lots All
comb_a_pnl = df['Total_Combined_PnL'].sum()
comb_a_safe_cap = 1550000.0  # 15.5 Lakhs
comb_a_roi = (comb_a_pnl / comb_a_safe_cap) * 100.0

# Portfolio B: Production Desk (10L Strangle + 2L ORB + 2L Silver)
comb_b_pnl = df['Total_Prod_PnL'].sum()
comb_b_safe_cap = 2650000.0  # 26.5 Lakhs
comb_b_roi = (comb_b_pnl / comb_b_safe_cap) * 100.0

print(f"Portfolio A (Balanced 2 Lots across all 3 strategies):")
print(f"  - Combined Net P&L     : Rs. {comb_a_pnl:,.2f}")
print(f"  - Max Capital Required : Rs. {comb_a_safe_cap:,.2f} (~15.5 Lakhs)")
print(f"  - Portfolio 1-Year ROI : {comb_a_roi:.2f}%\n")

print(f"Portfolio B (Production Desk: 10L Strangle + 2L ORB + 2L Silver):")
print(f"  - Combined Net P&L     : Rs. {comb_b_pnl:,.2f}")
print(f"  - Max Capital Required : Rs. {comb_b_safe_cap:,.2f} (~26.5 Lakhs, or Rs. 30 Lakhs conservative)")
print(f"  - Portfolio 1-Year ROI : {comb_b_roi:.2f}% (on 26.5L) | {(comb_b_pnl / 3000000.0)*100:.2f}% (on 30L)\n")

print("-" * 110)
print(f"{'Month':<8} | {'Strangle (2L)':<14} | {'Strangle (10L)':<15} | {'Stock ORB (2L)':<15} | {'Silver Mini (2L)':<16} | {'Total (2L All)':<15} | {'Total (Prod Desk)':<18}")
print("-" * 110)
for _, r in df.iterrows():
    print(f"{r['Month']:<8} | Rs {r['Net Realized PnL (Rs)']:>10,.2f} | Rs {r['Net Realized PnL (Rs)_Strangle10L']:>11,.2f} | Rs {r['Net PnL (Rs)']:>11,.2f} | Rs {r['Silver_NetPnL']:>12,.2f} | Rs {r['Total_Combined_PnL']:>11,.2f} | Rs {r['Total_Prod_PnL']:>14,.2f}")
print("-" * 110)
print(f"{'TOTAL':<8} | Rs {s2_pnl:>10,.2f} | Rs {s10_pnl:>11,.2f} | Rs {orb_pnl:>11,.2f} | Rs {sil_pnl:>12,.2f} | Rs {comb_a_pnl:>11,.2f} | Rs {comb_b_pnl:>14,.2f}")
print("=" * 110)
