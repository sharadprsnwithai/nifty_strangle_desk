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

def run_backtest_with_trailing(
    consolidation_candles=['09:30', '09:45', '10:00', '10:15'],
    entry_start_time='10:30',
    lots_per_trade=2,
    data_dir="data/cache_15m_1y",
    target_pct=0.020,
    sl_pct=0.010,
    trail_to_cost_trigger_pct=0.010, # None or 0.010 (+1.0% profit triggers SL -> Cost)
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
                    initial_sl = entry * (1.0 - sl_pct)
                    current_sl = initial_sl
                    tgt = entry * (1.0 + target_pct)
                    be_trigger = entry * (1.0 + trail_to_cost_trigger_pct) if trail_to_cost_trigger_pct else None

                    subsequent = trigger_bars.iloc[idx:]
                    exit_p = None
                    reason = None
                    exit_time = None
                    trailed_to_cost = False

                    for _, b in subsequent.iterrows():
                        # Check Stop Loss first (conservative)
                        if b['low'] <= current_sl:
                            exit_p = current_sl
                            reason = 'COST_EXIT' if trailed_to_cost else 'SL_HIT'
                            exit_time = b['time']
                            break

                        # Check if target hit
                        if tgt and b['high'] >= tgt:
                            exit_p = tgt
                            reason = 'TARGET_HIT'
                            exit_time = b['time']
                            break

                        # Check if Breakeven / Trail-to-Cost trigger hit on this bar
                        if be_trigger and not trailed_to_cost and b['high'] >= be_trigger:
                            trailed_to_cost = True
                            current_sl = entry # Trail SL to cost!
                            # After trailing to cost in this bar, did it drop back down to cost?
                            if b['low'] <= current_sl:
                                exit_p = current_sl
                                reason = 'COST_EXIT'
                                exit_time = b['time']
                                break

                        # Check 15:00 EOD square-off
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
                        'trailed_to_cost': trailed_to_cost,
                        'vol_ratio': round(vol_ratio, 2),
                        'gross_pnl': gross,
                        'charges': chg,
                        'net_pnl': gross - chg,
                        'month': str(d)[:7]
                    })
                    break

                elif short_t:
                    entry = min(bar['open'], l1)
                    initial_sl = entry * (1.0 + sl_pct)
                    current_sl = initial_sl
                    tgt = entry * (1.0 - target_pct)
                    be_trigger = entry * (1.0 - trail_to_cost_trigger_pct) if trail_to_cost_trigger_pct else None

                    subsequent = trigger_bars.iloc[idx:]
                    exit_p = None
                    reason = None
                    exit_time = None
                    trailed_to_cost = False

                    for _, b in subsequent.iterrows():
                        # Check Stop Loss first (conservative)
                        if b['high'] >= current_sl:
                            exit_p = current_sl
                            reason = 'COST_EXIT' if trailed_to_cost else 'SL_HIT'
                            exit_time = b['time']
                            break

                        # Check if target hit
                        if tgt and b['low'] <= tgt:
                            exit_p = tgt
                            reason = 'TARGET_HIT'
                            exit_time = b['time']
                            break

                        # Check if Breakeven / Trail-to-Cost trigger hit on this bar
                        if be_trigger and not trailed_to_cost and b['low'] <= be_trigger:
                            trailed_to_cost = True
                            current_sl = entry # Trail SL to cost!
                            if b['high'] >= current_sl:
                                exit_p = current_sl
                                reason = 'COST_EXIT'
                                exit_time = b['time']
                                break

                        # Check 15:00 EOD square-off
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
                        'trailed_to_cost': trailed_to_cost,
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
    cost_exits = len(df[df['exit_reason'] == 'COST_EXIT'])
    eod_exits = len(df[df['exit_reason'] == '15:00_EOD'])

    # Average Win vs Average Loss
    avg_win = df[df['net_pnl'] > 0]['net_pnl'].mean() if wins > 0 else 0.0
    avg_loss = df[df['net_pnl'] < 0]['net_pnl'].mean() if losses > 0 else 0.0
    rr_realized = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

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
        "rr_realized": rr_realized,
        "tgt_hits": tgt_hits,
        "sl_hits": sl_hits,
        "cost_exits": cost_exits,
        "eod_exits": eod_exits,
        "green_months": f"{green_months}/{total_months}",
        "monthly": monthly.to_dict()
    }

if __name__ == '__main__':
    print("--- 1. Baseline: 1.0% SL, 1.5% Target (No Trailing) ---")
    df_base = run_backtest_with_trailing(sl_pct=0.010, target_pct=0.015, trail_to_cost_trigger_pct=None)
    stats_base = get_stats(df_base)

    print("\n--- 2. Fixed Target: 1.0% SL, 2.0% Target (No Trailing) ---")
    df_fix20 = run_backtest_with_trailing(sl_pct=0.010, target_pct=0.020, trail_to_cost_trigger_pct=None)
    stats_fix20 = get_stats(df_fix20)

    print("\n--- 3. User Rule: 1.0% SL, at +1.0% Profit Trail to Cost, at +2.0% Book Target ---")
    df_trail20 = run_backtest_with_trailing(sl_pct=0.010, target_pct=0.020, trail_to_cost_trigger_pct=0.010)
    stats_trail20 = get_stats(df_trail20)

    print("\n============================== STRATEGY VARIANT COMPARISON ==============================")
    header = f"{'Metric':<25} | {'1.5% Target (Base)':<20} | {'2.0% Target (Fixed)':<20} | {'2.0% Tgt + Trail@1% (New)':<26}"
    print(header)
    print("-" * len(header))

    def row(m, fn):
        return f"{m:<25} | {fn(stats_base):<20} | {fn(stats_fix20):<20} | {fn(stats_trail20):<26}"

    print(row("Total Trades", lambda s: str(s['trades'])))
    print(row("Winning Trades", lambda s: str(s['wins'])))
    print(row("Losing Trades", lambda s: str(s['losses'])))
    print(row("Win Rate", lambda s: f"{s['win_rate']:.2f}%"))
    print(row("Profit Factor", lambda s: f"{s['profit_factor']:.2f}"))
    print(row("Target Hits (2.0%)", lambda s: str(s['tgt_hits'])))
    print(row("Full Stop Losses (1%)", lambda s: str(s['sl_hits'])))
    print(row("Cost / Breakeven Exits", lambda s: str(s['cost_exits'])))
    print(row("EOD Exits (15:00)", lambda s: str(s['eod_exits'])))
    print(row("Avg Win Trade", lambda s: f"Rs {s['avg_win']:,.0f}"))
    print(row("Avg Loss Trade", lambda s: f"Rs {s['avg_loss']:,.0f}"))
    print(row("Realized Win/Loss R:R", lambda s: f"{s['rr_realized']:.2f}x"))
    print(row("Net Realized PnL", lambda s: f"Rs {s['net_pnl']:,.2f}"))
    print(row("Max Drawdown", lambda s: f"Rs {s['max_dd']:,.2f}"))
    print(row("Profitable Months", lambda s: s['green_months']))

    print("\n=================================== MONTH-BY-MONTH NET PNL ===================================")
    all_months = sorted(list(stats_base['monthly'].keys()))
    m_header = f"{'Month':<10} | {'1.5% Base (Rs)':<16} | {'2.0% Fixed (Rs)':<16} | {'2.0% + Trail@1% (Rs)':<20} | {'Delta vs Base':<16}"
    print(m_header)
    print("-" * len(m_header))
    for m in all_months:
        v_base = stats_base['monthly'].get(m, 0.0)
        v_fix20 = stats_fix20['monthly'].get(m, 0.0)
        v_trail = stats_trail20['monthly'].get(m, 0.0)
        delta = v_trail - v_base
        print(f"{m:<10} | {v_base:>14,.2f} | {v_fix20:>14,.2f} | {v_trail:>18,.2f} | {delta:>+15,.2f}")
