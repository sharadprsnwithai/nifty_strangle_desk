"""
Backtest: High-Beta & Alpha F&O Universe + Bank Nifty
Position Size: 2 Lots Baseline
- 15m Candle 1 (09:15 - 09:30): Mark H1, L1
- Candles 2, 3, 4 (09:30 - 10:15): Closes inside [L1, H1]
- Trigger after 10:15 AM (until 13:00):
  - Breakout volume >= 1.5x 20-period moving average
  - Long: High > H1 -> BUY Future
  - Short: Low < L1 -> SELL Future
- Risk Management: 1.0% Hard Stop Loss on both lots
- Target / Exit: 1.5% Profit Target OR 15:00 PM EOD Exit
"""

import os
import glob
import pandas as pd
import numpy as np

LOT_SIZES = {
    'BANKNIFTY': 30,
    'MCX': 225,
    'BSE': 200,
    'ANGELONE': 2500,
    'CDSL': 475,
    'TATASTEEL': 2750,
    'HINDALCO': 700,
    'VEDL': 1150,
    'SAIL': 4700,
    'JINDALSTEL': 625,
    'NATIONALUM': 1875,
    'DLF': 950,
    'GODREJPROP': 325,
    'OBEROIRLTY': 350,
    'BHEL': 2625,
    'CGPOWER': 850,
    'ABB': 125,
    'DIXON': 50,
    'TRENT': 225,
    'POLYCAB': 125,
    'PERSISTENT': 125,
    'COFORGE': 475,
    'LAURUSLABS': 425,
    'GLENMARK': 375,
    'TORNTPHARM': 125,
    'ABCAPITAL': 1550,
    'CHOLAFIN': 625,
    'INDIGO': 150,
    'ASHOKLEY': 5000,
    'CUMMINSIND': 100,
    'MOTHERSON': 6150,
}

def calculate_futures_charges(entry_price, exit_price, lot_size, side="BUY", orders_count=2):
    buy_turnover = entry_price * lot_size if side == "BUY" else exit_price * lot_size
    sell_turnover = exit_price * lot_size if side == "BUY" else entry_price * lot_size
    total_turnover = buy_turnover + sell_turnover

    brokerage = 20.0 * orders_count
    stt = sell_turnover * 0.0002
    exchange_txn = total_turnover * 0.000019
    gst = (brokerage + exchange_txn) * 0.18
    stamp_duty = buy_turnover * 0.00002
    sebi = total_turnover * 0.000001

    return brokerage + stt + exchange_txn + gst + stamp_duty + sebi

def run_simulation(data_dir="data/cache_15m_1y", target_pct=0.015, sl_pct=0.01, vol_threshold=1.5, entry_cutoff="13:00"):
    all_trades = []

    for symbol, lot in sorted(LOT_SIZES.items()):
        fpath = os.path.join(data_dir, f"{symbol}_15m.parquet")
        if not os.path.exists(fpath):
            print(f"Missing parquet file for {symbol}")
            continue

        unit_lot = lot * 2  # 2 Lots

        df = pd.read_parquet(fpath)
        df['datetime'] = pd.to_datetime(df['date'])
        df['day'] = df['datetime'].dt.date
        df['time'] = df['datetime'].dt.strftime('%H:%M')
        df['vol_ma20'] = df['volume'].rolling(20).mean()

        days = df['day'].unique()
        for d in days:
            day_bars = df[df['day'] == d].sort_values('datetime').reset_index(drop=True)
            if len(day_bars) < 10:
                continue

            c0 = day_bars[day_bars['time'] == '09:15']
            if len(c0) == 0:
                continue
            c0 = c0.iloc[0]
            h1 = c0['high']
            l1 = c0['low']

            c_cons = day_bars[day_bars['time'].isin(['09:30', '09:45', '10:00'])]
            if len(c_cons) < 3:
                continue
            if not ((c_cons['close'] <= h1) & (c_cons['close'] >= l1)).all():
                continue

            trigger_bars = day_bars[day_bars['time'] >= '10:15'].reset_index(drop=True)
            for idx, bar in trigger_bars.iterrows():
                if bar['time'] > entry_cutoff:
                    break

                long_t = bar['high'] > h1
                short_t = bar['low'] < l1
                if long_t and short_t:
                    continue

                vol_ratio = bar['volume'] / (bar['vol_ma20'] + 1)
                if vol_ratio < vol_threshold:
                    continue

                if long_t:
                    entry = max(bar['open'], h1)
                    sl = entry * (1.0 - sl_pct)
                    tgt = entry * (1.0 + target_pct) if target_pct else None
                    subsequent = trigger_bars.iloc[idx:]
                    exit_p = None
                    reason = None
                    for _, b in subsequent.iterrows():
                        if b['low'] <= sl:
                            exit_p = sl
                            reason = 'SL_HIT'
                            break
                        if tgt and b['high'] >= tgt:
                            exit_p = tgt
                            reason = 'TARGET_HIT'
                            break
                        if b['time'] >= '15:00':
                            exit_p = b['close']
                            reason = '15:00_EOD'
                            break
                    if exit_p is None:
                        exit_p = subsequent.iloc[-1]['close']
                        reason = '15:00_EOD'

                    gross = (exit_p - entry) * unit_lot
                    chg = calculate_futures_charges(entry, exit_p, unit_lot, 'BUY', orders_count=2)
                    all_trades.append({
                        'symbol': symbol,
                        'day': d,
                        'side': 'LONG',
                        'entry_time': bar['time'],
                        'entry_price': entry,
                        'exit_price': exit_p,
                        'exit_reason': reason,
                        'gross_pnl': gross,
                        'charges': chg,
                        'net_pnl': gross - chg,
                        'month': str(d)[:7]
                    })
                    break
                elif short_t:
                    entry = min(bar['open'], l1)
                    sl = entry * (1.0 + sl_pct)
                    tgt = entry * (1.0 - target_pct) if target_pct else None
                    subsequent = trigger_bars.iloc[idx:]
                    exit_p = None
                    reason = None
                    for _, b in subsequent.iterrows():
                        if b['high'] >= sl:
                            exit_p = sl
                            reason = 'SL_HIT'
                            break
                        if tgt and b['low'] <= tgt:
                            exit_p = tgt
                            reason = 'TARGET_HIT'
                            break
                        if b['time'] >= '15:00':
                            exit_p = b['close']
                            reason = '15:00_EOD'
                            break
                    if exit_p is None:
                        exit_p = subsequent.iloc[-1]['close']
                        reason = '15:00_EOD'

                    gross = (entry - exit_p) * unit_lot
                    chg = calculate_futures_charges(entry, exit_p, unit_lot, 'SELL', orders_count=2)
                    all_trades.append({
                        'symbol': symbol,
                        'day': d,
                        'side': 'SHORT',
                        'entry_time': bar['time'],
                        'entry_price': entry,
                        'exit_price': exit_p,
                        'exit_reason': reason,
                        'gross_pnl': gross,
                        'charges': chg,
                        'net_pnl': gross - chg,
                        'month': str(d)[:7]
                    })
                    break

    return pd.DataFrame(all_trades)

if __name__ == '__main__':
    print("=" * 115)
    print("  HIGH-BETA & ALPHA F&O UNIVERSE + BANK NIFTY: 2 LOTS BASELINE BACKTEST (1 YEAR)")
    print("=" * 115)

    df = run_simulation()
    total_trades = len(df)
    wins = len(df[df['net_pnl'] > 0])
    win_rate = (wins / total_trades) * 100
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

    monthly = df.groupby('month')['net_pnl'].sum().reset_index()
    green_m = len(monthly[monthly['net_pnl'] > 0])
    total_m = len(monthly)

    print(f"Total Symbols    : {len(LOT_SIZES)}")
    print(f"Total Trades     : {total_trades}")
    print(f"Win Rate         : {win_rate:.1f}%")
    print(f"Gross PnL        : Rs {gross:>14,.2f}")
    print(f"Total Charges    : Rs {charges:>14,.2f}")
    print(f"Net Realized PnL : Rs {net:>14,.2f}")
    print(f"Max Drawdown     : Rs {max_dd:>14,.2f}")
    print(f"Profit Factor    : {pf:.2f}")
    print(f"Green Months     : {green_m}/{total_m} ({green_m/total_m*100:.1f}%)")

    print("\n" + "=" * 55)
    print("            MONTHLY PERFORMANCE LOG")
    print("=" * 55)
    for _, r in monthly.iterrows():
        status = "GREEN" if r['net_pnl'] > 0 else "RED"
        print(f"| {r['month']} | Rs {r['net_pnl']:>12,.2f} | {status} |")

    print("\n" + "=" * 90)
    print("                    PER-SYMBOL BREAKDOWN (RANKED BY NET PNL)")
    print("=" * 90)
    stock_stats = df.groupby('symbol').agg(
        trades=('net_pnl', 'count'),
        win_rate=('net_pnl', lambda x: (x > 0).mean() * 100),
        gross=('gross_pnl', 'sum'),
        charges=('charges', 'sum'),
        net=('net_pnl', 'sum'),
        pf=('gross_pnl', lambda x: (x[x > 0].sum() / abs(x[x < 0].sum())) if abs(x[x < 0].sum()) > 0 else 999.0)
    ).reset_index().sort_values('net', ascending=False)

    print(f"| {'Symbol':<12} | {'Trades':>6} | {'Win Rate':>8} | {'Gross PnL':>14} | {'Charges':>10} | {'Net Realized PnL':>16} | {'PF':>6} |")
    print("|:" + "-"*12 + "-|:" + "-"*6 + "-|:" + "-"*8 + "-|:" + "-"*14 + "-|:" + "-"*10 + "-|:" + "-"*16 + "-|:" + "-"*6 + ":|")
    for _, r in stock_stats.iterrows():
        print(f"| {r['symbol']:<12} | {r['trades']:>6} | {r['win_rate']:>7.1f}% | Rs {r['gross']:>11,.2f} | Rs {r['charges']:>7,.2f} | Rs {r['net']:>13,.2f} | {r['pf']:>6.2f} |")

    df.to_csv('data/high_beta_universe_2lots_trades.csv', index=False)
    print("\nSaved trade records to data/high_beta_universe_2lots_trades.csv")
