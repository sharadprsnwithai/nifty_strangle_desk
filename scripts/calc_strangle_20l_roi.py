import pandas as pd
import numpy as np

# Load the exact 1-year trade log / monthly report
df = pd.read_csv('data/strangle_10lots_1year_monthly_report.csv')
capital = 2000000.0  # 20 Lakhs

df['Trading Days'] = df['Trading Days'].astype(int)
df['Wins'] = df['Wins'].astype(int)
df['Losses'] = df['Losses'].astype(int)
df['ROI_20L_pct'] = (df['Net Realized PnL (Rs)'] / capital) * 100.0
df['Cum_PnL'] = df['Net Realized PnL (Rs)'].cumsum()
df['Cum_ROI_20L_pct'] = (df['Cum_PnL'] / capital) * 100.0

total_days = df['Trading Days'].sum()
total_wins = df['Wins'].sum()
total_losses = df['Losses'].sum()
overall_win_rate = (total_wins / total_days) * 100.0
total_gross_profit = df['Gross Profit (Rs)'].sum()
total_gross_loss = df['Gross Loss (Rs)'].sum()
overall_profit_factor = total_gross_profit / total_gross_loss if total_gross_loss > 0 else np.nan
total_net_pnl = df['Net Realized PnL (Rs)'].sum()
total_roi_20l = (total_net_pnl / capital) * 100.0
avg_monthly_pnl = df['Net Realized PnL (Rs)'].mean()
avg_monthly_roi = df['ROI_20L_pct'].mean()
max_monthly_drawdown = df['Max Drawdown (Rs)'].max()

# Also let's check max peak-to-trough on monthly cumulative basis
cum_series = df['Cum_PnL']
peak_series = cum_series.cummax()
dd_series = peak_series - cum_series
max_cum_dd = dd_series.max()

print("=" * 110)
print("   NIFTY 50 INDEX 10:00 AM SHORT STRANGLE STRATEGY (10 LOTS) - 1 YEAR PERFORMANCE")
print(f"   Account Capital: Rs. 20,00,000 (20 Lakhs) | Margin Required (~62.5%): Rs. 12,50,000 | Cash Buffer: Rs. 7,50,000")
print("=" * 110)
print(f"{'Month':<8} | {'Days':<4} | {'Win/Loss':<8} | {'Win %':<6} | {'PF':<6} | {'Net P&L (Rs)':<14} | {'Mo ROI (20L)':<12} | {'Cum P&L (Rs)':<14} | {'Cum ROI':<8} | {'Max DD (Rs)':<10}")
print("-" * 110)

for _, r in df.iterrows():
    print(f"{r['Month']:<8} | {r['Trading Days']:>4} | {r['Wins']:>2}W/{r['Losses']:<2}L | {r['Win Rate (%)']:>5.1f}% | {r['Profit Factor']:>6.2f} | Rs {r['Net Realized PnL (Rs)']:>11,.2f} | {r['ROI_20L_pct']:>11.2f}% | Rs {r['Cum_PnL']:>11,.2f} | {r['Cum_ROI_20L_pct']:>6.2f}% | Rs {r['Max Drawdown (Rs)']:>8,.2f}")

print("=" * 110)
print(f"TOTALS / OVERALL METRICS:")
print(f"Total Trading Days      : {total_days} days (Nov 2025 - Nov 2026)")
print(f"Total Wins / Losses     : {total_wins} Wins / {total_losses} Losses")
print(f"Overall Win Rate        : {overall_win_rate:.1f}%")
print(f"Total Gross Profit      : Rs. {total_gross_profit:,.2f}")
print(f"Total Gross Loss        : Rs. {total_gross_loss:,.2f}")
print(f"Overall Profit Factor   : {overall_profit_factor:.2f}")
print(f"Total Net Realized P&L  : Rs. {total_net_pnl:,.2f}")
print(f"Total ROI (20L Capital) : {total_roi_20l:.2f}%")
print(f"Average Monthly Net P&L : Rs. {avg_monthly_pnl:,.2f}")
print(f"Average Monthly ROI     : {avg_monthly_roi:.2f}%")
print(f"Max Single-Month DD     : Rs. {max_monthly_drawdown:,.2f} ({(max_monthly_drawdown / capital)*100:.2f}% of 20L)")
print(f"Winning Months          : {(df['Net Realized PnL (Rs)'] > 0).sum()} of {len(df)} months ({((df['Net Realized PnL (Rs)'] > 0).sum()/len(df))*100:.1f}%)")
print("=" * 110)
