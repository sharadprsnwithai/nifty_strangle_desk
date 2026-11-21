import pandas as pd
import numpy as np

# Load trades
df_3c = pd.read_csv('data/momentum_30_2lots_1year_trades.csv')
df_4c = pd.read_csv('data/momentum_30_2lots_4candles_trades.csv')
df_nifty = pd.read_csv('data/strangle_10lots_1year_monthly_report.csv')

MARGIN_PCT = 0.22

def analyze_margin(df, entry_start):
    df = df.copy()
    df['contract_value'] = df['entry_price'] * df['quantity']
    df['margin_req'] = df['contract_value'] * MARGIN_PCT
    df['entry_dt'] = pd.to_datetime(df['day'] + ' ' + df['entry_time'])
    df['exit_dt'] = pd.to_datetime(df['day'] + ' ' + df['exit_time'])

    days = df['day'].unique()
    daily_peak_concurrent = []
    daily_peak_margin = []

    for d in days:
        sub = df[df['day'] == d]
        timestamps = pd.date_range(f"{d} {entry_start}", f"{d} 15:00", freq="15min")
        peak_active = 0
        peak_m = 0
        for ts in timestamps:
            active = sub[(sub['entry_dt'] <= ts) & (sub['exit_dt'] > ts)]
            cnt = len(active)
            m = active['margin_req'].sum()
            if cnt > peak_active:
                peak_active = cnt
            if m > peak_m:
                peak_m = m
        daily_peak_concurrent.append(peak_active)
        daily_peak_margin.append(peak_m)

    return {
        'avg_concurrent': np.mean(daily_peak_concurrent),
        'median_concurrent': np.median(daily_peak_concurrent),
        'p90_concurrent': np.percentile(daily_peak_concurrent, 90),
        'max_concurrent': np.max(daily_peak_concurrent),
        'avg_margin': np.mean(daily_peak_margin),
        'median_margin': np.median(daily_peak_margin),
        'p90_margin': np.percentile(daily_peak_margin, 90),
        'max_margin': np.max(daily_peak_margin)
    }

m_3c = analyze_margin(df_3c, "10:15")
m_4c = analyze_margin(df_4c, "10:30")

print("==================== MARGIN COMPARISON ====================")
print(f"{'Metric':<32} | {'3 Candles (Baseline)':<22} | {'4 Candles (New)':<22}")
print("-" * 80)
print(f"{'Average Concurrent Positions':<32} | {m_3c['avg_concurrent']:.2f} stocks{'':<13} | {m_4c['avg_concurrent']:.2f} stocks{'':<13}")
print(f"{'Median Concurrent Positions':<32} | {m_3c['median_concurrent']:.0f} stocks{'':<15} | {m_4c['median_concurrent']:.0f} stocks{'':<15}")
print(f"{'90th Pct Concurrent Positions':<32} | {m_3c['p90_concurrent']:.0f} stocks{'':<15} | {m_4c['p90_concurrent']:.0f} stocks{'':<15}")
print(f"{'Max Concurrent Positions':<32} | {m_3c['max_concurrent']:.0f} stocks{'':<15} | {m_4c['max_concurrent']:.0f} stocks{'':<15}")
print(f"{'Median Peak Margin Required':<32} | Rs {m_3c['median_margin']:>12,.2f}{'':<6} | Rs {m_4c['median_margin']:>12,.2f}{'':<6}")
print(f"{'Average Peak Margin Required':<32} | Rs {m_3c['avg_margin']:>12,.2f}{'':<6} | Rs {m_4c['avg_margin']:>12,.2f}{'':<6}")
print(f"{'90th Pct Peak Margin Required':<32} | Rs {m_3c['p90_margin']:>12,.2f}{'':<6} | Rs {m_4c['p90_margin']:>12,.2f}{'':<6}")
print(f"{'Max Historical Margin Peak':<32} | Rs {m_3c['max_margin']:>12,.2f}{'':<6} | Rs {m_4c['max_margin']:>12,.2f}{'':<6}")

# Combined Desk Analysis with Nifty 10 Lots
nifty_total = df_nifty['Net Realized PnL (Rs)'].sum()
comb_3c_total = nifty_total + df_3c['net_pnl'].sum()
comb_4c_total = nifty_total + df_4c['net_pnl'].sum()

print("\n================ COMBINED DESK (NIFTY 10L + STOCKS 2L) ================")
print(f"NIFTY 10L 1-Yr Net PnL       : Rs {nifty_total:>14,.2f}")
print(f"Stocks 3C Desk 1-Yr Net PnL  : Rs {df_3c['net_pnl'].sum():>14,.2f}")
print(f"Stocks 4C Desk 1-Yr Net PnL  : Rs {df_4c['net_pnl'].sum():>14,.2f}")
print("-" * 65)
print(f"Combined 3-Candle Net PnL    : Rs {comb_3c_total:>14,.2f} (Avg: Rs {comb_3c_total/13:>10,.2f}/mo)")
print(f"Combined 4-Candle Net PnL    : Rs {comb_4c_total:>14,.2f} (Avg: Rs {comb_4c_total/13:>10,.2f}/mo)")
print(f"Monthly ROI on Rs 35L Capital: {((comb_4c_total/13)/3500000)*100:.2f}% / month (Annualized: {((comb_4c_total)/3500000)*100:.1f}%)")
print(f"Monthly ROI on Rs 40L Capital: {((comb_4c_total/13)/4000000)*100:.2f}% / month (Annualized: {((comb_4c_total)/4000000)*100:.1f}%)")
