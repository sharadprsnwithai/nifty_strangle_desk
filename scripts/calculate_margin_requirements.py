import pandas as pd
import numpy as np

# Load trade log
df = pd.read_csv('data/momentum_30_2lots_1year_trades.csv')

# NSE Stock Futures SPAN + Exposure Margin is mandated by SEBI/NSE at approx 20% to 25% of contract value
# (Typically MWPL / High-Beta stocks require ~22% to 25% margin)
MARGIN_PCT = 0.22

df['contract_value'] = df['entry_price'] * df['quantity']
df['margin_req'] = df['contract_value'] * MARGIN_PCT

# Margin per single trade (2 lots)
avg_trade_margin = df['margin_req'].mean()
median_trade_margin = df['margin_req'].median()
p90_trade_margin = df['margin_req'].quantile(0.90)
max_trade_margin = df['margin_req'].max()

# Concurrency analysis:
# Look at overlapping active trades on any day
# Trades enter between 10:15 and 13:00 and exit between 10:15 and 15:00
df['entry_dt'] = pd.to_datetime(df['day'] + ' ' + df['entry_time'])
df['exit_dt'] = pd.to_datetime(df['day'] + ' ' + df['exit_time'])

# Calculate simultaneous overlapping positions throughout the day
# Sample at 15-minute intervals between 10:15 and 15:00 for each day
days = df['day'].unique()
daily_peak_concurrent = []
daily_peak_margin = []

for d in days:
    sub = df[df['day'] == d]
    # Check 15-minute time stamps
    timestamps = pd.date_range(f"{d} 10:15", f"{d} 15:00", freq="15min")
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

daily_summary = pd.DataFrame({
    'day': days,
    'peak_concurrent': daily_peak_concurrent,
    'peak_margin': daily_peak_margin
})

print("=" * 75)
print("             STOCK FUTURES DESK (2 LOTS) MARGIN ANALYSIS")
print("=" * 75)
print(f"Margin Rate Assumed        : {MARGIN_PCT*100:.0f}% (Standard NSE SPAN + Exposure)")
print(f"Average Margin per Trade   : Rs {avg_trade_margin:>10,.2f}")
print(f"Median Margin per Trade    : Rs {median_trade_margin:>10,.2f}")
print(f"90th Pct Margin per Trade  : Rs {p90_trade_margin:>10,.2f}")
print(f"Max Single Position Margin : Rs {max_trade_margin:>10,.2f} ({df.loc[df['margin_req'].idxmax()]['symbol']})")

print("\n" + "=" * 75)
print("         SIMULTANEOUS CONCURRENT POSITIONS DISTRIBUTION")
print("=" * 75)
print(f"Average Concurrent Positions : {daily_summary['peak_concurrent'].mean():.1f} stocks")
print(f"Median Concurrent Positions  : {daily_summary['peak_concurrent'].median():.1f} stocks")
print(f"75th Pct Concurrent Positions: {daily_summary['peak_concurrent'].quantile(0.75):.1f} stocks")
print(f"90th Pct Concurrent Positions: {daily_summary['peak_concurrent'].quantile(0.90):.1f} stocks")
print(f"Max Concurrent Positions     : {daily_summary['peak_concurrent'].max():.1f} stocks")

print("\n" + "=" * 75)
print("              DAILY PEAK MARGIN UTILIZATION")
print("=" * 75)
print(f"Average Daily Peak Margin    : Rs {daily_summary['peak_margin'].mean():>10,.2f}")
print(f"Median Daily Peak Margin     : Rs {daily_summary['peak_margin'].median():>10,.2f}")
print(f"75th Pct Daily Peak Margin   : Rs {daily_summary['peak_margin'].quantile(0.75):>10,.2f}")
print(f"90th Pct Daily Peak Margin   : Rs {daily_summary['peak_margin'].quantile(0.90):>10,.2f}")
print(f"Max Historical Peak Margin   : Rs {daily_summary['peak_margin'].max():>10,.2f}")

# Stock-by-stock margin table
print("\n" + "=" * 75)
print("          MARGIN REQUIRED PER STOCK (2 LOTS - RANKED)")
print("=" * 75)
stock_m = df.groupby(['symbol', 'name']).agg(
    lot_size=('quantity', lambda q: int(q.iloc[0] / 2)),
    quantity=('quantity', 'first'),
    avg_price=('entry_price', 'mean'),
    margin=('margin_req', 'mean')
).reset_index().sort_values('margin', ascending=False).reset_index(drop=True)

for idx, r in stock_m.iterrows():
    print(f"{idx+1:>2}. {r['symbol']:<11} | 2 Lots ({int(r['quantity']):>6} qty) @ Rs {r['avg_price']:>7.2f} | Margin: Rs {r['margin']:>10,.2f}")
