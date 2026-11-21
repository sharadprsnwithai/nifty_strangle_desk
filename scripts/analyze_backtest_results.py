import pandas as pd

df = pd.read_csv('data/momentum_30_2lots_1year_trades.csv')

print('=== MONTHLY BREAKDOWN ===')
monthly = df.groupby('month').agg(
    trades=('net_pnl', 'count'),
    wins=('net_pnl', lambda x: (x > 0).sum()),
    gross=('gross_pnl', 'sum'),
    charges=('charges', 'sum'),
    net=('net_pnl', 'sum')
).reset_index()
monthly['win_rate'] = (monthly['wins'] / monthly['trades'] * 100).round(1)

for _, r in monthly.iterrows():
    status = 'GREEN' if r['net'] > 0 else 'RED'
    print(f"| {r['month']} | {int(r['trades']):>6} | {int(r['wins']):>4} | {r['win_rate']:>5.1f}% | Rs {r['gross']:>11,.2f} | Rs {r['charges']:>9,.2f} | Rs {r['net']:>11,.2f} | {status} |")

print('\n=== TOP 10 PROFITABLE STOCKS ===')
stock_perf = df.groupby(['symbol', 'name']).agg(
    trades=('net_pnl', 'count'),
    wins=('net_pnl', lambda x: (x > 0).sum()),
    gross=('gross_pnl', 'sum'),
    charges=('charges', 'sum'),
    net=('net_pnl', 'sum')
).reset_index()
stock_perf['win_rate'] = (stock_perf['wins'] / stock_perf['trades'] * 100).round(1)
stock_perf = stock_perf.sort_values('net', ascending=False).reset_index(drop=True)

for idx, r in stock_perf.head(10).iterrows():
    print(f"{idx+1:>2}. {r['symbol']:<11} ({r['name']:<35}): {int(r['trades']):>2} trades | WR {r['win_rate']:>4.1f}% | Net: Rs {r['net']:>10,.2f}")

print('\n=== BOTTOM 5 STOCKS ===')
for idx, r in stock_perf.tail(5).iterrows():
    print(f"{r['symbol']:<11} ({r['name']:<35}): {int(r['trades']):>2} trades | WR {r['win_rate']:>4.1f}% | Net: Rs {r['net']:>10,.2f}")

print('\n=== EXIT REASON BREAKDOWN ===')
exit_breakdown = df.groupby('exit_reason').agg(
    trades=('net_pnl', 'count'),
    wins=('net_pnl', lambda x: (x > 0).sum()),
    net=('net_pnl', 'sum'),
    gross=('gross_pnl', 'sum')
).reset_index()
exit_breakdown['win_rate'] = (exit_breakdown['wins'] / exit_breakdown['trades'] * 100).round(1)
print(exit_breakdown.to_string(index=False))

print('\n=== LONG VS SHORT BREAKDOWN ===')
side_breakdown = df.groupby('side').agg(
    trades=('net_pnl', 'count'),
    wins=('net_pnl', lambda x: (x > 0).sum()),
    gross=('gross_pnl', 'sum'),
    charges=('charges', 'sum'),
    net=('net_pnl', 'sum')
).reset_index()
side_breakdown['win_rate'] = (side_breakdown['wins'] / side_breakdown['trades'] * 100).round(1)
print(side_breakdown.to_string(index=False))
