import sys, os
from pathlib import Path
import pandas as pd
import numpy as np
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))

print("=" * 115)
print("     MCX SILVER MINI (SILVERM) 18:00 US SESSION BREAKOUT: 4 LOTS (RS 10,000 STOP LOSS)")
print("=" * 115)

df = pd.read_csv("data/silver_1year_trades.csv")
df['month'] = pd.to_datetime(df['day']).dt.strftime('%Y-%m')

# For 4 lots:
# 2 lots baseline -> 4 lots is exactly 2x
# Each 2-lot trade has friction ~Rs 650
# For 4 lots, friction = Rs 1,300 per trade
friction_4l = 1300.0

df['gross_pnl_4l'] = df['total_pnl'] * 2.0
df['net_pnl_4l'] = df['gross_pnl_4l'] - friction_4l

# Check stop loss adherence:
# For initial SL exit: 500 pts * 20 = -10,000 Rs gross.
# With friction: -10,000 - 1,300 = -11,300 Rs net.

monthly = []
for m, grp in df.groupby('month'):
    n = len(grp)
    w = (grp['net_pnl_4l'] > 0).sum()
    l = (grp['net_pnl_4l'] <= 0).sum()
    wr = (w / n) * 100.0 if n > 0 else 0
    gross = grp['gross_pnl_4l'].sum()
    net = grp['net_pnl_4l'].sum()
    
    # Intramonth Drawdown for 4 lots
    cum = grp['net_pnl_4l'].cumsum()
    peak = cum.cummax()
    dd = (peak - cum).max()
    
    # Profit factor
    wins_sum = grp[grp['net_pnl_4l'] > 0]['net_pnl_4l'].sum()
    loss_sum = abs(grp[grp['net_pnl_4l'] < 0]['net_pnl_4l'].sum())
    pf = (wins_sum / loss_sum) if loss_sum > 0 else 99.9

    monthly.append({
        'Month': m,
        'Trades': n,
        'Wins': w,
        'Losses': l,
        'Win Rate (%)': round(wr, 1),
        'Gross PnL (Rs)': round(gross, 2),
        'Net PnL (Rs)': round(net, 2),
        'Max Drawdown (Rs)': round(dd, 2),
        'Profit Factor': round(pf, 2)
    })

mdf = pd.DataFrame(monthly)
print("\n" + "=" * 115)
print("                                    MONTHLY PERFORMANCE BREAKDOWN (1-YEAR EMPIRICAL)")
print("=" * 115)
print(tabulate(mdf, headers="keys", tablefmt="pipe", showindex=False))

# Cumulative metrics:
tot_trades = len(df)
tot_wins = (df['net_pnl_4l'] > 0).sum()
tot_losses = (df['net_pnl_4l'] <= 0).sum()
tot_gross = df['gross_pnl_4l'].sum()
tot_net = df['net_pnl_4l'].sum()
total_friction = friction_4l * tot_trades

cum_full = df['net_pnl_4l'].cumsum()
peak_full = cum_full.cummax()
max_dd = (peak_full - cum_full).max()

wins_sum = df[df['net_pnl_4l'] > 0]['net_pnl_4l'].sum()
loss_sum = abs(df[df['net_pnl_4l'] < 0]['net_pnl_4l'].sum())
pf = (wins_sum / loss_sum) if loss_sum > 0 else 99.9

avg_win = df[df['net_pnl_4l'] > 0]['net_pnl_4l'].mean()
avg_loss = abs(df[df['net_pnl_4l'] < 0]['net_pnl_4l'].mean())

win_months = (mdf['Net PnL (Rs)'] > 0).sum()
tot_months = len(mdf)

print("\n" + "=" * 115)
print("                           1-YEAR EMPIRICAL CUMULATIVE METRICS (4 LOTS)")
print("=" * 115)
print(f"  {'Total Trades Taken':<30}: {tot_trades:>15}")
print(f"  {'Winning Trades':<30}: {tot_wins:>15} ({tot_wins/tot_trades*100:.1f}%)")
print(f"  {'Losing Trades':<30}: {tot_losses:>15} ({tot_losses/tot_trades*100:.1f}%)")
print(f"  {'Gross PnL (Rs)':<30}: {tot_gross:>15,.2f}")
print(f"  {'Total Taxes, CTT & Slippage':<30}: {total_friction:>15,.2f}")
print(f"  {'Net Realized PnL (Rs)':<30}: {tot_net:>15,.2f}")
print(f"  {'Profit Factor':<30}: {pf:>15.2f}")
print(f"  {'Max Drawdown (Rs)':<30}: {max_dd:>15,.2f}")
print(f"  {'Average Win':<30}: +Rs {avg_win:>12,.2f}")
print(f"  {'Average Loss':<30}: -Rs {avg_loss:>12,.2f}")
print(f"  {'Reward / Risk Ratio':<30}: {avg_win / avg_loss:>15.2f}x")
print(f"  {'Profitable Months':<30}: {win_months} / {tot_months} ({win_months/tot_months*100:.1f}%)")

# 3-Year Projection based on Empirical Run:
print("\n" + "=" * 115)
print("                     3-YEAR STATISTICAL PROJECTION (ESTIMATED BASED ON 184 EMPIRICAL TRADES)")
print("=" * 115)
# In 3 years (~550 trading days):
scale = 3.0
print(f"  {'Projected Trades (3 Years)':<30}: ~{int(tot_trades * scale):>14}")
print(f"  {'Projected Net PnL (Rs)':<30}: ~Rs {tot_net * scale:>11,.2f}")
print(f"  {'Estimated Max DD Range':<30}: ~Rs {max_dd * 1.3:>11,.2f} - {max_dd * 1.6:,.2f}")
print(f"  {'Capital Required (4 Lots)':<30}: ~Rs {400000:>11,.2f} (MIS)")
print(f"  {'Annualized ROI (on Rs 4L)':<30}: {tot_net / 400000 * 100:>14.1f}% per year")
