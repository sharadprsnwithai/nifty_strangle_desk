import pandas as pd
import numpy as np

# 1. Previous list: Nifty 200 Momentum 30 (29 stocks)
df_prev = pd.read_csv('data/vcp_2lots_baseline_trades.csv')

# 2. High-Beta & Alpha universe (30 stocks without Bank Nifty)
df_hb = pd.read_csv('data/high_beta_universe_2lots_trades.csv')
df_hb = df_hb[df_hb['symbol'] != 'BANKNIFTY'].copy()

def compute_metrics(df, name):
    trades = len(df)
    wins = len(df[df['net_pnl'] > 0])
    win_rate = (wins / trades) * 100
    gross = df['gross_pnl'].sum()
    charges = df['charges'].sum()
    net = df['net_pnl'].sum()
    
    df['cum'] = df['net_pnl'].cumsum()
    df['peak'] = df['cum'].cummax()
    df['dd'] = df['peak'] - df['cum']
    max_dd = df['dd'].max()
    
    gw = df[df['gross_pnl'] > 0]['gross_pnl'].sum()
    gl = abs(df[df['gross_pnl'] < 0]['gross_pnl'].sum())
    pf = gw / gl if gl > 0 else 999.0
    
    monthly = df.groupby('month')['net_pnl'].sum()
    green_m = len(monthly[monthly > 0])
    total_m = len(monthly)
    
    return {
        'name': name,
        'trades': trades,
        'win_rate': win_rate,
        'gross': gross,
        'charges': charges,
        'net': net,
        'max_dd': max_dd,
        'pf': pf,
        'green_months': f'{green_m}/{total_m} ({green_m/total_m*100:.1f}%)',
        'monthly': monthly
    }

m_prev = compute_metrics(df_prev, 'List 1: Nifty 200 Momentum 30 (29 Stocks)')
m_hb = compute_metrics(df_hb, 'List 2: High-Beta & Alpha (30 Stocks, No BN)')

print('=' * 100)
print('HEAD-TO-HEAD COMPARISON: LIST 1 (MOMENTUM 30) vs LIST 2 (HIGH-BETA & ALPHA)')
print('=' * 100)
print(f"| {'Metric':<30} | {'List 1: Nifty 200 Momentum 30':>28} | {'List 2: High-Beta & Alpha':>28} |")
print('|:' + '-'*30 + '-|:' + '-'*28 + '-|:' + '-'*28 + ':|')
print(f"| {'Total Stocks':<30} | {'29 Stocks':>28} | {'30 Stocks (No BN)':>28} |")
print(f"| {'Total Trades (1 Year)':<30} | {m_prev['trades']:>28} | {m_hb['trades']:>28} |")
print(f"| {'Win Rate':<30} | {m_prev['win_rate']:>27.1f}% | {m_hb['win_rate']:>27.1f}% |")
print(f"| {'Gross Realized PnL':<30} | Rs {m_prev['gross']:>25,.2f} | Rs {m_hb['gross']:>25,.2f} |")
print(f"| {'F&O Charges & Friction':<30} | Rs {m_prev['charges']:>25,.2f} | Rs {m_hb['charges']:>25,.2f} |")
print(f"| {'Net Realized PnL':<30} | Rs {m_prev['net']:>25,.2f} | Rs {m_hb['net']:>25,.2f} |")
print(f"| {'Profit Factor':<30} | {m_prev['pf']:>28.2f} | {m_hb['pf']:>28.2f} |")
print(f"| {'Max Drawdown':<30} | Rs {m_prev['max_dd']:>25,.2f} | Rs {m_hb['max_dd']:>25,.2f} |")
print(f"| {'Green Months':<30} | {m_prev['green_months']:>28} | {m_hb['green_months']:>28} |")

print('\n' + '=' * 100)
print('MONTH-BY-MONTH PNL COMPARISON')
print('=' * 100)
months = sorted(set(m_prev['monthly'].index).union(set(m_hb['monthly'].index)))
print(f"| {'Month':<10} | {'List 1: Momentum 30 Net':>26} | {'List 2: High-Beta Net':>26} | {'Winner':>15} |")
print('|:' + '-'*10 + '-|:' + '-'*26 + '-|:' + '-'*26 + '-|:' + '-'*15 + ':|')
for m in months:
    v1 = m_prev['monthly'].get(m, 0.0)
    v2 = m_hb['monthly'].get(m, 0.0)
    winner = 'List 1 (+₹{:,.0f})'.format(v1-v2) if v1 > v2 else 'List 2 (+₹{:,.0f})'.format(v2-v1)
    print(f"| {m:<10} | Rs {v1:>23,.2f} | Rs {v2:>23,.2f} | {winner:>15} |")

# Check overlap between List 1 and List 2
symbols_prev = set(df_prev['symbol'].unique())
symbols_hb = set(df_hb['symbol'].unique())
overlap = symbols_prev.intersection(symbols_hb)
only_prev = symbols_prev - symbols_hb
only_hb = symbols_hb - symbols_prev
print('\n' + '=' * 100)
print('PORTFOLIO COMPOSITION ANALYSIS')
print('=' * 100)
print(f'Overlapping Stocks ({len(overlap)}):', sorted(overlap))
print(f'Exclusive to List 1 (Momentum 30) ({len(only_prev)}):', sorted(only_prev))
print(f'Exclusive to List 2 (High-Beta & Alpha) ({len(only_hb)}):', sorted(only_hb))
