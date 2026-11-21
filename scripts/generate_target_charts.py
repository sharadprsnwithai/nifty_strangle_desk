import os
import json
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

MOMENTUM_30_LOTS = {
    "ABB": 125, "ABCAPITAL": 5400, "ADANIENSOL": 675, "ADANIGREEN": 600,
    "ADANIPOWER": 1350, "BHARATFORG": 500, "BHEL": 2625, "BSE": 375,
    "CGPOWER": 850, "CUMMINSIND": 300, "FEDERALBNK": 5000, "GLENMARK": 725,
    "GVT_D": 1000, "HINDALCO": 1400, "IDEA": 71475, "KEI": 150,
    "LAURUSLABS": 1700, "LTF": 4462, "MCX": 250, "MOTHERSON": 6150,
    "NATIONALUM": 3750, "NTPC": 1500, "POLYCAB": 125, "POWERINDIA": 100,
    "SAIL": 8000, "SHRIRAMFIN": 300, "SOLARINDS": 75, "TATASTEEL": 5500,
    "TORNTPHARM": 250, "VEDL": 1550,
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

def run_backtest(target_pct, sl_pct=0.010, lots_per_trade=2, data_dir="data/cache_15m_1y"):
    consolidation_candles = ['09:30', '09:45', '10:00', '10:15']
    entry_start_time = '10:30'
    entry_cutoff = '13:00'
    vol_threshold = 1.5

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

            c0 = day_bars[day_bars['time'] == '09:15']
            if len(c0) == 0:
                continue
            c0 = c0.iloc[0]
            h1 = c0['high']
            l1 = c0['low']

            c_cons = day_bars[day_bars['time'].isin(consolidation_candles)]
            if len(c_cons) < len(consolidation_candles):
                continue
            if not ((c_cons['close'] <= h1) & (c_cons['close'] >= l1)).all():
                continue

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
                        'gross_pnl': gross,
                        'charges': chg,
                        'net_pnl': gross - chg,
                        'month': str(d)[:7]
                    })
                    break

    return pd.DataFrame(all_trades)

def get_stats(df):
    total_trd = len(df)
    wins = len(df[df['net_pnl'] > 0])
    losses = len(df[df['net_pnl'] < 0])
    wr = (wins / total_trd) * 100 if total_trd > 0 else 0
    gross_profit = df[df['gross_pnl'] > 0]['gross_pnl'].sum()
    gross_loss = abs(df[df['gross_pnl'] < 0]['gross_pnl'].sum())
    charges = df['charges'].sum()
    net_pnl = df['net_pnl'].sum()
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    daily = df.groupby('day')['net_pnl'].sum().reset_index()
    daily['cum_pnl'] = daily['net_pnl'].cumsum()
    daily['peak'] = daily['cum_pnl'].cummax()
    daily['drawdown'] = daily['peak'] - daily['cum_pnl']
    max_dd = daily['drawdown'].max()

    tgt_hits = len(df[df['exit_reason'] == 'TARGET_HIT'])
    sl_hits = len(df[df['exit_reason'] == 'SL_HIT'])
    eod_exits = len(df[df['exit_reason'] == '15:00_EOD'])

    avg_win = df[df['net_pnl'] > 0]['net_pnl'].mean() if wins > 0 else 0.0
    avg_loss = df[df['net_pnl'] < 0]['net_pnl'].mean() if losses > 0 else 0.0

    monthly = df.groupby('month')['net_pnl'].sum()

    return {
        "trades": total_trd, "wins": wins, "losses": losses, "win_rate": wr,
        "profit_factor": profit_factor, "gross_profit": gross_profit, "gross_loss": gross_loss,
        "charges": charges, "net_pnl": net_pnl, "max_dd": max_dd, "avg_win": avg_win,
        "avg_loss": avg_loss, "tgt_hits": tgt_hits, "sl_hits": sl_hits, "eod_exits": eod_exits,
        "monthly": monthly.to_dict(), "daily": daily
    }

def main():
    print("Running backtests...")
    df_15 = run_backtest(target_pct=0.015)
    df_20 = run_backtest(target_pct=0.020)
    df_225 = run_backtest(target_pct=0.0225)

    s15 = get_stats(df_15)
    s20 = get_stats(df_20)
    s225 = get_stats(df_225)

    # Save detailed trade files
    os.makedirs("reports", exist_ok=True)
    df_225.to_csv("data/momentum_30_trades_target_2.25.csv", index=False)

    # Plot 1: Cumulative Equity Curve
    plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [2.2, 1.2]})

    d15 = s15['daily'].copy()
    d20 = s20['daily'].copy()
    d225 = s225['daily'].copy()

    d15['date'] = pd.to_datetime(d15['day'])
    d20['date'] = pd.to_datetime(d20['day'])
    d225['date'] = pd.to_datetime(d225['day'])

    ax1.plot(d15['date'], d15['cum_pnl'] / 1e5, label='1.50% Target (Base: +Rs 32.36L | MaxDD: Rs 3.95L)', color='#64748b', linewidth=2.0)
    ax1.plot(d20['date'], d20['cum_pnl'] / 1e5, label='2.00% Target (+Rs 35.72L | MaxDD: Rs 3.35L)', color='#3b82f6', linewidth=2.2)
    ax1.plot(d225['date'], d225['cum_pnl'] / 1e5, label='2.25% Target (BEST: +Rs 37.59L | MaxDD: Rs 3.11L)', color='#10b981', linewidth=2.8)

    ax1.set_title("1-Year Cumulative Realized Equity Curve (Nifty 200 Momentum 30 Stock Futures Desk)", fontsize=14, fontweight='bold', pad=15)
    ax1.set_ylabel("Cumulative Net P&L (₹ Lakhs)", fontsize=12, fontweight='bold')
    ax1.legend(loc='upper left', frameon=True, fontsize=11)
    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))

    # Plot 2: Monthly Net PnL Bar Chart Comparison
    months = sorted(list(s15['monthly'].keys()))
    x = np.arange(len(months))
    width = 0.28

    m15_vals = [s15['monthly'][m] / 1e5 for m in months]
    m20_vals = [s20['monthly'][m] / 1e5 for m in months]
    m225_vals = [s225['monthly'][m] / 1e5 for m in months]

    ax2.bar(x - width, m15_vals, width, label='1.50% Target', color='#94a3b8')
    ax2.bar(x, m20_vals, width, label='2.00% Target', color='#60a5fa')
    ax2.bar(x + width, m225_vals, width, label='2.25% Target (Optimal)', color='#10b981')

    ax2.set_title("Month-by-Month Net Realized P&L Breakdown (₹ Lakhs)", fontsize=13, fontweight='bold', pad=10)
    ax2.set_xticks(x)
    ax2.set_xticklabels([m[2:] for m in months], rotation=0, fontsize=10)
    ax2.set_ylabel("Monthly P&L (₹ L)", fontsize=11, fontweight='bold')
    ax2.legend(loc='upper right', frameon=True, fontsize=10)
    ax2.grid(True, linestyle='--', alpha=0.6)

    plt.tight_layout()
    chart_path = "reports/target_comparison_chart.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print(f"Chart saved successfully to: {chart_path}")

    # Copy to artifact directory for rendering
    artifact_dir = "C:/Users/sharad/.gemini/antigravity/brain/856c9cf3-18e3-4330-a1b9-ddf55f65ae95"
    if os.path.exists(artifact_dir):
        dest_chart = os.path.join(artifact_dir, "target_comparison_chart.png")
        import shutil
        shutil.copyfile(chart_path, dest_chart)
        print(f"Chart copied to artifact directory: {dest_chart}")

    # Summary table output
    print("\n============================== TARGET COMPARISON SUMMARY ==============================")
    print(f"{'Metric':<28} | {'1.50% (Current)':<18} | {'2.00%':<18} | {'2.25% (Optimal)':<20}")
    print("-" * 92)
    print(f"{'Net Realized PnL':<28} | Rs {s15['net_pnl']:>12,.2f}     | Rs {s20['net_pnl']:>12,.2f}     | Rs {s225['net_pnl']:>14,.2f}")
    print(f"{'Profit Factor':<28} | {s15['profit_factor']:>12.2f}     | {s20['profit_factor']:>12.2f}     | {s225['profit_factor']:>14.2f}")
    print(f"{'Win Rate':<28} | {s15['win_rate']:>11.2f}%     | {s20['win_rate']:>11.2f}%     | {s225['win_rate']:>13.2f}%")
    print(f"{'Max Drawdown':<28} | Rs {s15['max_dd']:>12,.2f}     | Rs {s20['max_dd']:>12,.2f}     | Rs {s225['max_dd']:>14,.2f}")
    print(f"{'Winning Trades':<28} | {s15['wins']:>12}     | {s20['wins']:>12}     | {s225['wins']:>14}")
    print(f"{'Losing Trades':<28} | {s15['losses']:>12}     | {s20['losses']:>12}     | {s225['losses']:>14}")
    print(f"{'Target Hits':<28} | {s15['tgt_hits']:>12}     | {s20['tgt_hits']:>12}     | {s225['tgt_hits']:>14}")
    print(f"{'Stop Loss Hits (1.0%)':<28} | {s15['sl_hits']:>12}     | {s20['sl_hits']:>12}     | {s225['sl_hits']:>14}")
    print(f"{'EOD Exits (15:00)':<28} | {s15['eod_exits']:>12}     | {s20['eod_exits']:>12}     | {s225['eod_exits']:>14}")
    print(f"{'Average Win':<28} | Rs {s15['avg_win']:>12,.2f}     | Rs {s20['avg_win']:>12,.2f}     | Rs {s225['avg_win']:>14,.2f}")
    print(f"{'Average Loss':<28} | Rs {s15['avg_loss']:>12,.2f}     | Rs {s20['avg_loss']:>12,.2f}     | Rs {s225['avg_loss']:>14,.2f}")
    print(f"{'Realized Win/Loss R:R':<28} | {abs(s15['avg_win']/s15['avg_loss']):>11.2f}x     | {abs(s20['avg_win']/s20['avg_loss']):>11.2f}x     | {abs(s225['avg_win']/s225['avg_loss']):>13.2f}x")

    print("\n============================== MONTH-BY-MONTH NET PNL ==============================")
    print(f"{'Month':<10} | {'1.50% (Rs)':<16} | {'2.00% (Rs)':<16} | {'2.25% (Rs)':<18} | {'Delta (2.25% vs 1.50%)':<22}")
    print("-" * 90)
    for m in months:
        v15 = s15['monthly'][m]
        v20 = s20['monthly'][m]
        v225 = s225['monthly'][m]
        delta = v225 - v15
        print(f"{m:<10} | {v15:>14,.2f} | {v20:>14,.2f} | {v225:>16,.2f} | {delta:>+20,.2f}")

if __name__ == '__main__':
    main()
