import os
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

def run_backtest(
    consolidation_candles=['09:30', '09:45', '10:00', '10:15'],
    entry_start_time='10:30',
    lots_per_trade=2,
    data_dir="data/cache_15m_1y",
    target_pct=0.015,
    sl_pct=0.010,
    vol_threshold=1.5,
    entry_cutoff="13:00"
):
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
            if len(day_bars) < len(consolidation_candles) + 2:
                continue

            # C1 Opening Bar (09:15)
            c0 = day_bars[day_bars['time'] == '09:15']
            if len(c0) == 0:
                continue
            c0 = c0.iloc[0]
            h1 = c0['high']
            l1 = c0['low']

            # Consolidation Bars
            c_cons = day_bars[day_bars['time'].isin(consolidation_candles)]
            if len(c_cons) < len(consolidation_candles):
                continue
            # All consolidation bars must close inside [l1, h1]
            if not ((c_cons['close'] <= h1) & (c_cons['close'] >= l1)).all():
                continue

            # Breakout Scan
            trigger_bars = day_bars[day_bars['time'] >= entry_start_time].reset_index(drop=True)
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

def get_stats(df):
    if len(df) == 0:
        return {}
    total_trd = len(df)
    wins = len(df[df['net_pnl'] > 0])
    losses = len(df[df['net_pnl'] < 0])
    wr = (wins / total_trd) * 100 if total_trd > 0 else 0
    gross_pnl = df['gross_pnl'].sum()
    gross_profit = df[df['gross_pnl'] > 0]['gross_pnl'].sum()
    gross_loss = abs(df[df['gross_pnl'] < 0]['gross_pnl'].sum())
    charges = df['charges'].sum()
    net_pnl = df['net_pnl'].sum()
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    # Max Drawdown
    daily_pnl = df.groupby('day')['net_pnl'].sum().reset_index()
    daily_pnl['cum_pnl'] = daily_pnl['net_pnl'].cumsum()
    daily_pnl['peak'] = daily_pnl['cum_pnl'].cummax()
    daily_pnl['drawdown'] = daily_pnl['peak'] - daily_pnl['cum_pnl']
    max_dd = daily_pnl['drawdown'].max()

    # Targets vs SLs
    tgt_hits = len(df[df['exit_reason'] == 'TARGET_HIT'])
    sl_hits = len(df[df['exit_reason'] == 'SL_HIT'])
    eod_exits = len(df[df['exit_reason'] == '15:00_EOD'])

    # Average Win vs Average Loss
    avg_win = df[df['net_pnl'] > 0]['net_pnl'].mean() if wins > 0 else 0.0
    avg_loss = df[df['net_pnl'] < 0]['net_pnl'].mean() if losses > 0 else 0.0

    # Monthly breakdown
    monthly = df.groupby('month')['net_pnl'].sum()
    green_months = len(monthly[monthly > 0])
    total_months = len(monthly)

    return {
        "trades": total_trd,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "profit_factor": profit_factor,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "charges": charges,
        "net_pnl": net_pnl,
        "max_dd": max_dd,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "tgt_hits": tgt_hits,
        "sl_hits": sl_hits,
        "eod_exits": eod_exits,
        "green_months": f"{green_months}/{total_months}",
        "monthly": monthly.to_dict()
    }

if __name__ == '__main__':
    print("--- Running Backtest: 1.0% Stop Loss (Current Baseline) ---")
    df_10 = run_backtest(sl_pct=0.010, target_pct=0.015)
    stats_10 = get_stats(df_10)

    print("\n--- Running Backtest: 0.5% Stop Loss (Requested Variant) ---")
    df_05 = run_backtest(sl_pct=0.005, target_pct=0.015)
    stats_05 = get_stats(df_05)

    print("\n--- Running Backtest: 0.75% Stop Loss (Reference Intermediate) ---")
    df_075 = run_backtest(sl_pct=0.0075, target_pct=0.015)
    stats_075 = get_stats(df_075)

    print("\n========================= STOP LOSS COMPARISON SUMMARY =========================")
    print(f"{'Metric':<30} | {'1.0% SL (Current)':<20} | {'0.5% SL (New)':<20} | {'0.75% SL (Mid)':<20}")
    print("-" * 98)
    print(f"{'Total Trades':<30} | {stats_10['trades']:<20} | {stats_05['trades']:<20} | {stats_075['trades']:<20}")
    print(f"{'Winning Trades':<30} | {stats_10['wins']:<20} | {stats_05['wins']:<20} | {stats_075['wins']:<20}")
    print(f"{'Losing Trades':<30} | {stats_10['losses']:<20} | {stats_05['losses']:<20} | {stats_075['losses']:<20}")
    print(f"{'Win Rate':<30} | {stats_10['win_rate']:.2f}%{'':<14} | {stats_05['win_rate']:.2f}%{'':<14} | {stats_075['win_rate']:.2f}%{'':<14}")
    print(f"{'Profit Factor':<30} | {stats_10['profit_factor']:.2f}{'':<16} | {stats_05['profit_factor']:.2f}{'':<16} | {stats_075['profit_factor']:.2f}{'':<16}")
    print(f"{'Target Hits (1.5%)':<30} | {stats_10['tgt_hits']:<20} | {stats_05['tgt_hits']:<20} | {stats_075['tgt_hits']:<20}")
    print(f"{'Stop Loss Hits':<30} | {stats_10['sl_hits']:<20} | {stats_05['sl_hits']:<20} | {stats_075['sl_hits']:<20}")
    print(f"{'EOD Exits (15:00)':<30} | {stats_10['eod_exits']:<20} | {stats_05['eod_exits']:<20} | {stats_075['eod_exits']:<20}")
    print(f"{'Avg Win Trade':<30} | Rs {stats_10['avg_win']:>12,.2f}     | Rs {stats_05['avg_win']:>12,.2f}     | Rs {stats_075['avg_win']:>12,.2f}")
    print(f"{'Avg Loss Trade':<30} | Rs {stats_10['avg_loss']:>12,.2f}     | Rs {stats_05['avg_loss']:>12,.2f}     | Rs {stats_075['avg_loss']:>12,.2f}")
    print(f"{'Gross Profit':<30} | Rs {stats_10['gross_profit']:>12,.2f}     | Rs {stats_05['gross_profit']:>12,.2f}     | Rs {stats_075['gross_profit']:>12,.2f}")
    print(f"{'Gross Loss':<30} | Rs {stats_10['gross_loss']:>12,.2f}     | Rs {stats_05['gross_loss']:>12,.2f}     | Rs {stats_075['gross_loss']:>12,.2f}")
    print(f"{'Brokerage & Charges':<30} | Rs {stats_10['charges']:>12,.2f}     | Rs {stats_05['charges']:>12,.2f}     | Rs {stats_075['charges']:>12,.2f}")
    print(f"{'Net Realized PnL':<30} | Rs {stats_10['net_pnl']:>12,.2f}     | Rs {stats_05['net_pnl']:>12,.2f}     | Rs {stats_075['net_pnl']:>12,.2f}")
    print(f"{'Max Drawdown':<30} | Rs {stats_10['max_dd']:>12,.2f}     | Rs {stats_05['max_dd']:>12,.2f}     | Rs {stats_075['max_dd']:>12,.2f}")
    print(f"{'Profitable Months':<30} | {stats_10['green_months']:<20} | {stats_05['green_months']:<20} | {stats_075['green_months']:<20}")

    print("\n============================== MONTH-BY-MONTH NET PNL ==============================")
    all_months = sorted(list(set(list(stats_10['monthly'].keys()) + list(stats_05['monthly'].keys()))))
    print(f"{'Month':<10} | {'1.0% SL (Rs)':<16} | {'0.5% SL (Rs)':<16} | {'Delta (0.5 vs 1.0)':<18} | {'0.75% SL (Rs)':<16}")
    print("-" * 86)
    for m in all_months:
        v10 = stats_10['monthly'].get(m, 0.0)
        v05 = stats_05['monthly'].get(m, 0.0)
        v075 = stats_075['monthly'].get(m, 0.0)
        delta = v05 - v10
        print(f"{m:<10} | {v10:>14,.2f} | {v05:>14,.2f} | {delta:>+16,.2f} | {v075:>14,.2f}")
