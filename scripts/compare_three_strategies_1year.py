import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd
import numpy as np

# Load Strategy 1: NIFTY Short Strangle 2 Lots
df_strangle = pd.read_csv("data/strangle_2lots_1year_monthly_report.csv")
df_strangle['Month'] = df_strangle['Month'].astype(str)

# Load Strategy 2: Stock ORB 15-Min Desk
df_orb = pd.read_csv("data/orb_1year_monthly_report.csv")
df_orb['Month'] = df_orb['Month'].astype(str)

# Load Strategy 3: Silver Mini 18:00 US Breakout Trades
df_silver_trades = pd.read_csv("data/silver_1year_trades.csv")
df_silver_trades['month'] = pd.to_datetime(df_silver_trades['day']).dt.strftime('%Y-%m')

# Deduct standard brokerage, STT, exchange turnover fees, GST and 15 pts slippage (~Rs 650/trade for 2 lots)
friction_per_trade = 650.0
df_silver_trades['net_pnl'] = df_silver_trades['total_pnl'] - friction_per_trade

# Aggregate Silver Monthly
silver_monthly = []
for m, grp in df_silver_trades.groupby('month'):
    trades_cnt = len(grp)
    wins = len(grp[grp['net_pnl'] > 0])
    losses = len(grp[grp['net_pnl'] < 0])
    wr = (wins / trades_cnt) * 100.0 if trades_cnt > 0 else 0.0
    gp = grp[grp['net_pnl'] > 0]['net_pnl'].sum()
    gl = abs(grp[grp['net_pnl'] < 0]['net_pnl'].sum())
    pf = (gp / gl) if gl > 0 else 99.9
    net_pnl = grp['net_pnl'].sum()
    
    # Calculate Max DD within the month
    cum = grp['net_pnl'].cumsum()
    peak = cum.cummax()
    dd = (peak - cum).max()
    
    silver_monthly.append({
        'Month': m,
        'Silver_Trades': trades_cnt,
        'Silver_Wins': wins,
        'Silver_Losses': losses,
        'Silver_WR': wr,
        'Silver_PF': pf,
        'Silver_NetPnL': net_pnl,
        'Silver_MaxDD': dd
    })

df_silver_m = pd.DataFrame(silver_monthly)

# Merge side-by-side
merged = pd.merge(df_strangle[['Month', 'Trading Days', 'Wins', 'Losses', 'Win Rate (%)', 'Profit Factor', 'Net Realized PnL (Rs)', 'Max Drawdown (Rs)']],
                  df_orb[['Month', 'Trades', 'Wins', 'Losses', 'Win Rate (%)', 'Profit Factor', 'Net PnL (Rs)', 'Max Drawdown (Rs)']],
                  on='Month', suffixes=('_Strangle', '_ORB'), how='outer')

merged = pd.merge(merged, df_silver_m, on='Month', how='outer')
merged = merged.sort_values('Month').reset_index(drop=True)
merged = merged.fillna(0.0)

merged['Total_Combined_PnL'] = (
    merged['Net Realized PnL (Rs)'] + 
    merged['Net PnL (Rs)'] + 
    merged['Silver_NetPnL']
)

# Export clean CSV
merged.to_csv("data/three_strategies_1year_side_by_side.csv", index=False)

print("=" * 130)
print(f"{'MONTH':<8} | {'STRANGLE (2L)':<18} | {'STOCK ORB (30S)':<18} | {'SILVER MINI (2L)':<18} | {'COMBINED TOTAL':<18} | {'WINNERS'}")
print("=" * 130)

total_strangle = 0.0
total_orb = 0.0
total_silver = 0.0
total_all = 0.0

for _, r in merged.iterrows():
    m = r['Month']
    s_pnl = r['Net Realized PnL (Rs)']
    s_wr = r['Win Rate (%)_Strangle']
    
    o_pnl = r['Net PnL (Rs)']
    o_wr = r['Win Rate (%)_ORB']
    
    sil_pnl = r['Silver_NetPnL']
    sil_wr = r['Silver_WR']
    
    tot = r['Total_Combined_PnL']
    
    total_strangle += s_pnl
    total_orb += o_pnl
    total_silver += sil_pnl
    total_all += tot
    
    # Check which strategies were profitable that month
    pros = []
    if s_pnl > 0: pros.append("Strangle")
    if o_pnl > 0: pros.append("ORB")
    if sil_pnl > 0: pros.append("Silver")
    pros_str = ", ".join(pros) if pros else "None"
    
    print(f"{m:<8} | ₹{s_pnl:>10,.2f} ({s_wr:>4.1f}%) | ₹{o_pnl:>10,.2f} ({o_wr:>4.1f}%) | ₹{sil_pnl:>10,.2f} ({sil_wr:>4.1f}%) | ₹{tot:>11,.2f}        | {pros_str}")

print("=" * 130)
print(f"{'TOTAL':<8} | ₹{total_strangle:>10,.2f}        | ₹{total_orb:>10,.2f}        | ₹{total_silver:>10,.2f}        | ₹{total_all:>11,.2f}        |")
print("=" * 130)
