import pandas as pd
import numpy as np

# Load Nifty monthly
df_nifty = pd.read_csv('data/strangle_10lots_1year_monthly_report.csv')

# Load Stock monthly
df_stock_trades = pd.read_csv('data/momentum_30_2lots_1year_trades.csv')
df_stock = df_stock_trades.groupby('month').agg(
    stock_net=('net_pnl', 'sum'),
    stock_trades=('net_pnl', 'count'),
    stock_wins=('net_pnl', lambda x: (x > 0).sum())
).reset_index()

m_comb = pd.merge(df_nifty[['Month', 'Net Realized PnL (Rs)']], df_stock, left_on='Month', right_on='month')
m_comb.rename(columns={'Net Realized PnL (Rs)': 'nifty_net'}, inplace=True)
m_comb['total_net'] = m_comb['nifty_net'] + m_comb['stock_net']

# ROI calculations on Rs 40 Lakhs (Recommended) and Rs 50 Lakhs (Conservative Uncapped)
cap_40 = 4000000.0
cap_50 = 5000000.0

m_comb['roi_40_pct'] = (m_comb['total_net'] / cap_40) * 100.0
m_comb['roi_50_pct'] = (m_comb['total_net'] / cap_50) * 100.0

m_comb['cum_net'] = m_comb['total_net'].cumsum()
m_comb['cum_roi_40'] = (m_comb['cum_net'] / cap_40) * 100.0
m_comb['cum_roi_50'] = (m_comb['cum_net'] / cap_50) * 100.0

print("=" * 115)
print("     COMBINED DESK (10 LOTS NIFTY SELLING + 2 LOTS STOCK FUTURES) - MONTHLY ROI BREAKDOWN")
print("=" * 115)
print(f"{'Month':<8} | {'NIFTY Net':<14} | {'Stock Net':<14} | {'Combined Net (Rs)':<18} | {'ROI on 40L':<11} | {'ROI on 50L':<11} | {'Cum ROI (40L)':<13}")
print("-" * 115)

for _, r in m_comb.iterrows():
    print(f"{r['Month']:<8} | Rs {r['nifty_net']:>10,.2f} | Rs {r['stock_net']:>10,.2f} | Rs {r['total_net']:>14,.2f} | {r['roi_40_pct']:>9.2f}% | {r['roi_50_pct']:>9.2f}% | {r['cum_roi_40']:>11.2f}%")

print("=" * 115)
print("ANNUALIZED & SUMMARY METRICS:")
print(f"Total Combined Net PnL (1 Year) : Rs. {m_comb['total_net'].sum():>12,.2f}")
print(f"Average Monthly Net Income      : Rs. {m_comb['total_net'].mean():>12,.2f} per month")
print(f"Median Monthly Net Income       : Rs. {m_comb['total_net'].median():>12,.2f} per month")
print(f"Worst Month PnL                 : Rs. {m_comb['total_net'].min():>12,.2f} ({m_comb.loc[m_comb['total_net'].idxmin()]['Month']})")
print(f"Best Month PnL                  : Rs. {m_comb['total_net'].max():>12,.2f} ({m_comb.loc[m_comb['total_net'].idxmax()]['Month']})")
print(f"Winning Months                  : {(m_comb['total_net'] > 0).sum()} / {len(m_comb)} (100.0% Green Months)")
print("-" * 115)
print(f"AVERAGE MONTHLY ROI on Rs. 40 Lakhs : {m_comb['roi_40_pct'].mean():.2f}% / month  (Total Annual ROI: {(m_comb['total_net'].sum() / cap_40)*100:.2f}%)")
print(f"AVERAGE MONTHLY ROI on Rs. 50 Lakhs : {m_comb['roi_50_pct'].mean():.2f}% / month  (Total Annual ROI: {(m_comb['total_net'].sum() / cap_50)*100:.2f}%)")
print("=" * 115)
