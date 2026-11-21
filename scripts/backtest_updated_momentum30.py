import os
import glob
import pandas as pd
import numpy as np

# Official F&O Lot Sizes for Nifty 200 Momentum 30 (Updated)
MOMENTUM_30_LOTS = {
    "ABB": 125,
    "ABCAPITAL": 5400,
    "ADANIENSOL": 675,
    "ADANIGREEN": 600,
    "ADANIPOWER": 1350,
    "BHARATFORG": 500,
    "BHEL": 2625,
    "BSE": 375,
    "CGPOWER": 850,
    "CUMMINSIND": 300,
    "FEDERALBNK": 5000,
    "GLENMARK": 725,
    "GVT_D": 1000,
    "HINDALCO": 1400,
    "IDEA": 71475,
    "KEI": 150,
    "LAURUSLABS": 1700,
    "LTF": 4462,
    "MCX": 250,
    "MOTHERSON": 6150,
    "NATIONALUM": 3750,
    "NTPC": 1500,
    "POLYCAB": 125,
    "POWERINDIA": 100,
    "SAIL": 8000,
    "SHRIRAMFIN": 300,
    "SOLARINDS": 75,
    "TATASTEEL": 5500,
    "TORNTPHARM": 250,
    "VEDL": 1550,
}

STOCK_NAMES = {
    "ABB": "ABB India Limited",
    "ABCAPITAL": "Aditya Birla Capital Limited",
    "ADANIENSOL": "Adani Energy Solutions Limited",
    "ADANIGREEN": "Adani Green Energy Limited",
    "ADANIPOWER": "Adani Power Limited",
    "BHARATFORG": "Bharat Forge Limited",
    "BHEL": "Bharat Heavy Electricals Limited",
    "BSE": "BSE Limited",
    "CGPOWER": "CG Power and Industrial Solutions",
    "CUMMINSIND": "Cummins India Limited",
    "FEDERALBNK": "The Federal Bank Limited",
    "GLENMARK": "Glenmark Pharmaceuticals Limited",
    "GVT_D": "GE Vernova T&D India Limited",
    "HINDALCO": "Hindalco Industries Limited",
    "IDEA": "Vodafone Idea Limited",
    "KEI": "KEI Industries Limited",
    "LAURUSLABS": "Laurus Labs Limited",
    "LTF": "L&T Finance Limited",
    "MCX": "Multi Commodity Exchange of India",
    "MOTHERSON": "Samvardhana Motherson International",
    "NATIONALUM": "National Aluminium Company",
    "NTPC": "NTPC Limited",
    "POLYCAB": "Polycab India Limited",
    "POWERINDIA": "Hitachi Energy India Limited",
    "SAIL": "Steel Authority of India Limited",
    "SHRIRAMFIN": "Shriram Finance Limited",
    "SOLARINDS": "Solar Industries India Limited",
    "TATASTEEL": "Tata Steel Limited",
    "TORNTPHARM": "Torrent Pharmaceuticals Limited",
    "VEDL": "Vedanta Limited",
}

def calculate_futures_charges(entry_price, exit_price, quantity, side="LONG", orders_count=2):
    buy_turnover = entry_price * quantity if side == "LONG" else exit_price * quantity
    sell_turnover = exit_price * quantity if side == "LONG" else entry_price * quantity
    total_turnover = buy_turnover + sell_turnover

    brokerage = 20.0 * orders_count
    stt = sell_turnover * 0.0002
    exchange_txn = total_turnover * 0.000019
    gst = (brokerage + exchange_txn) * 0.18
    stamp_duty = buy_turnover * 0.00002
    sebi = total_turnover * 0.000001

    return brokerage + stt + exchange_txn + gst + stamp_duty + sebi

def run_backtest(lots_per_trade=2, data_dir="data/cache_15m_1y", target_pct=0.015, sl_pct=0.010, vol_threshold=1.5, entry_cutoff="13:00"):
    all_trades = []

    for symbol in sorted(MOMENTUM_30_LOTS.keys()):
        filepath = os.path.join(data_dir, f"{symbol}_15m.parquet")
        if not os.path.exists(filepath):
            continue

        lot_size = MOMENTUM_30_LOTS[symbol]
        trade_qty = lot_size * lots_per_trade

        df = pd.read_parquet(filepath)
        df['datetime'] = pd.to_datetime(df['date'])
        df['day'] = df['datetime'].dt.date
        df['time'] = df['datetime'].dt.strftime('%H:%M')
        df['vol_ma20'] = df['volume'].rolling(20, min_periods=5).mean()

        days = df['day'].unique()
        for d in days:
            day_bars = df[df['day'] == d].sort_values('datetime').reset_index(drop=True)
            if len(day_bars) < 10:
                continue

            # C1 Opening Bar (09:15)
            c0 = day_bars[day_bars['time'] == '09:15']
            if len(c0) == 0:
                continue
            c0 = c0.iloc[0]
            h1 = c0['high']
            l1 = c0['low']

            # Consolidation Bars (09:30, 09:45, 10:00, 10:15)
            c_cons = day_bars[day_bars['time'].isin(['09:30', '09:45', '10:00', '10:15'])]
            if len(c_cons) < 4:
                continue
            # All consolidation bars must close inside [l1, h1]
            if not ((c_cons['close'] <= h1) & (c_cons['close'] >= l1)).all():
                continue

            # Breakout Scan (10:30 onwards up to cutoff)
            trigger_bars = day_bars[day_bars['time'] >= '10:30'].reset_index(drop=True)
            for idx, bar in trigger_bars.iterrows():
                if bar['time'] > entry_cutoff:
                    break

                long_t = bar['high'] > h1
                short_t = bar['low'] < l1
                if long_t and short_t:
                    continue

                vol_ratio = bar['volume'] / (bar['vol_ma20'] + 1e-9)
                if vol_ratio < vol_threshold:
                    continue

                if long_t:
                    entry = max(bar['open'], h1)
                    sl = entry * (1.0 - sl_pct)
                    tgt = entry * (1.0 + target_pct)
                    subsequent = trigger_bars.iloc[idx:]
                    exit_p = None
                    reason = None
                    exit_time = None
                    for _, b in subsequent.iterrows():
                        if b['low'] <= sl:
                            exit_p = sl
                            reason = 'SL_HIT'
                            exit_time = b['time']
                            break
                        if tgt and b['high'] >= tgt:
                            exit_p = tgt
                            reason = 'TARGET_HIT'
                            exit_time = b['time']
                            break
                        if b['time'] >= '15:00':
                            exit_p = b['close']
                            reason = '15:00_EOD'
                            exit_time = b['time']
                            break
                    if exit_p is None:
                        exit_p = subsequent.iloc[-1]['close']
                        reason = '15:00_EOD'
                        exit_time = subsequent.iloc[-1]['time']

                    gross = (exit_p - entry) * trade_qty
                    chg = calculate_futures_charges(entry, exit_p, trade_qty, 'LONG', orders_count=2)
                    all_trades.append({
                        'symbol': symbol,
                        'name': STOCK_NAMES.get(symbol, symbol),
                        'day': str(d),
                        'side': 'LONG',
                        'entry_time': bar['time'],
                        'exit_time': exit_time,
                        'entry_price': entry,
                        'exit_price': exit_p,
                        'quantity': trade_qty,
                        'exit_reason': reason,
                        'vol_ratio': round(vol_ratio, 2),
                        'gross_pnl': gross,
                        'charges': chg,
                        'net_pnl': gross - chg,
                        'month': str(d)[:7]
                    })
                    break

                elif short_t:
                    entry = min(bar['open'], l1)
                    sl = entry * (1.0 + sl_pct)
                    tgt = entry * (1.0 - target_pct)
                    subsequent = trigger_bars.iloc[idx:]
                    exit_p = None
                    reason = None
                    exit_time = None
                    for _, b in subsequent.iterrows():
                        if b['high'] >= sl:
                            exit_p = sl
                            reason = 'SL_HIT'
                            exit_time = b['time']
                            break
                        if tgt and b['low'] <= tgt:
                            exit_p = tgt
                            reason = 'TARGET_HIT'
                            exit_time = b['time']
                            break
                        if b['time'] >= '15:00':
                            exit_p = b['close']
                            reason = '15:00_EOD'
                            exit_time = b['time']
                            break
                    if exit_p is None:
                        exit_p = subsequent.iloc[-1]['close']
                        reason = '15:00_EOD'
                        exit_time = subsequent.iloc[-1]['time']

                    gross = (entry - exit_p) * trade_qty
                    chg = calculate_futures_charges(entry, exit_p, trade_qty, 'SHORT', orders_count=2)
                    all_trades.append({
                        'symbol': symbol,
                        'name': STOCK_NAMES.get(symbol, symbol),
                        'day': str(d),
                        'side': 'SHORT',
                        'entry_time': bar['time'],
                        'exit_time': exit_time,
                        'entry_price': entry,
                        'exit_price': exit_p,
                        'quantity': trade_qty,
                        'exit_reason': reason,
                        'vol_ratio': round(vol_ratio, 2),
                        'gross_pnl': gross,
                        'charges': chg,
                        'net_pnl': gross - chg,
                        'month': str(d)[:7]
                    })
                    break

    return pd.DataFrame(all_trades)

if __name__ == '__main__':
    for lots in [1, 2]:
        df = run_backtest(lots_per_trade=lots)
        total_trd = len(df)
        wins = len(df[df['net_pnl'] > 0])
        wr = (wins / total_trd) * 100 if total_trd > 0 else 0
        gross = df['gross_pnl'].sum()
        chg = df['charges'].sum()
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

        print("=" * 80)
        print(f"  BACKTEST RESULTS: {lots} LOTS ({len(MOMENTUM_30_LOTS)} STOCKS) - 1 YEAR")
        print("=" * 80)
        print(f"Total Trades     : {total_trd}")
        print(f"Win Rate         : {wr:.1f}%")
        print(f"Gross PnL        : Rs {gross:>14,.2f}")
        print(f"Total Charges    : Rs {chg:>14,.2f}")
        print(f"Net Realized PnL : Rs {net:>14,.2f}")
        print(f"Max Drawdown     : Rs {max_dd:>14,.2f}")
        print(f"Profit Factor    : {pf:.2f}")
        print(f"Green Months     : {green_m}/{total_m} ({green_m/total_m*100:.1f}%)")

        if lots == 2:
            df.to_csv("data/momentum_30_2lots_1year_trades.csv", index=False)
            print("\nSaved trade records to data/momentum_30_2lots_1year_trades.csv")
