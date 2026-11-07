import pandas as pd
import numpy as np
import os
from datetime import datetime

def run_backtest(
    data_path="data/swing_daily_2y.parquet",
    volume_multiplier=4.0,       # 4x volume spike
    lookback_high=252,          # 52-week (1-year) high
    target_pct=0.30,            # +30% target
    max_stop_loss_pct=0.08,     # max 8% initial SL risk
    trail_be_at=0.15,           # Move SL to breakeven at +15%
    max_hold_days=60,           # Max 60 trading days (~3 months)
    capital_per_trade=100000.0, # Rs 1,00,000 per trade
    max_open_positions=10       # Max 10 concurrent open trades
):
    print("=" * 70)
    print("  SWING BREAKOUT STRATEGY BACKTEST (1 YEAR)")
    print(f"  Criteria: 52-Week High Breakout + {volume_multiplier}x Volume Spike")
    print(f"  Target: +{target_pct*100:.0f}% | Initial SL: Breakout Candle Low (Max -{max_stop_loss_pct*100:.0f}%)")
    print(f"  Trailing: Breakeven at +{trail_be_at*100:.0f}% | Max Hold: {max_hold_days} days")
    print(f"  Capital / Trade: Rs {capital_per_trade:,.0f} | Max Concurrent: {max_open_positions}")
    print("=" * 70)

    if not os.path.exists(data_path):
        print(f"Error: {data_path} does not exist.")
        return

    df = pd.read_parquet(data_path)
    df['date'] = pd.to_datetime(df['date'])
    # Normalize timezone to naive or consistent
    if df['date'].dt.tz is not None:
        df['date'] = df['date'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)

    df = df.sort_values(['symbol', 'date']).reset_index(drop=True)

    # 1. Feature Engineering per symbol
    all_symbols = df['symbol'].unique()
    dfs_by_sym = {}

    for sym in all_symbols:
        s_df = df[df['symbol'] == sym].copy().sort_values('date').reset_index(drop=True)
        if len(s_df) < lookback_high + 20:
            continue
        
        # 20-day SMA Volume
        s_df['vol_sma20'] = s_df['volume'].rolling(20).mean().shift(1)
        # Prior highest high excluding current candle
        s_df['high_n'] = s_df['high'].rolling(lookback_high).max().shift(1)
        # 20 EMA for trailing
        s_df['ema20'] = s_df['close'].ewm(span=20, adjust=False).mean()

        # Signal Conditions
        c1 = s_df['close'] > s_df['high_n']                          # 52-week High Breakout
        c2 = (s_df['close'] > s_df['open']) & (s_df['close'] > s_df['close'].shift(1)) # Bullish Green
        c3 = s_df['volume'] >= (volume_multiplier * s_df['vol_sma20']) # Volume Spike
        c4 = s_df['volume'] >= 50000                                 # Minimum liquidity
        c5 = (s_df['high'] - s_df['low']) / s_df['close'] > 0.005    # Not locked in circuit

        s_df['signal'] = c1 & c2 & c3 & c4 & c5
        dfs_by_sym[sym] = s_df.set_index('date')

    # 2. Portfolio Level Chronological Simulation
    # Get all unique dates from the 1-year backtest window (e.g., from Nov 2025 onwards)
    test_start_date = pd.Timestamp("2025-11-01")
    all_dates = sorted(df[df['date'] >= test_start_date]['date'].unique())
    print(f"\nSimulating {len(all_dates)} trading days across {len(dfs_by_sym)} symbols...")

    active_trades = []
    closed_trades = []

    for d in all_dates:
        # A. Check and update existing active trades
        remaining_trades = []
        for trade in active_trades:
            sym = trade['symbol']
            sym_df = dfs_by_sym.get(sym)
            if sym_df is None or d not in sym_df.index:
                remaining_trades.append(trade)
                continue

            bar = sym_df.loc[d]
            high_p = bar['high']
            low_p = bar['low']
            close_p = bar['close']
            trade['hold_days'] += 1

            # Check profit milestone for trailing stop
            unrealized_peak = (high_p - trade['entry_price']) / trade['entry_price']
            if unrealized_peak >= trail_be_at and not trade['be_moved']:
                trade['current_sl'] = max(trade['current_sl'], trade['entry_price'])
                trade['be_moved'] = True

            # Target 1: +30% Target reached
            target_hit = high_p >= trade['target_price']
            # Stop loss hit
            sl_hit = low_p <= trade['current_sl']
            # Time exit
            time_hit = trade['hold_days'] >= max_hold_days

            if target_hit and not trade['target_half_booked']:
                # Partial booking or full exit at target
                # Let's test standard swing profit take at target:
                exit_price = trade['target_price']
                trade['exit_date'] = d
                trade['exit_price'] = exit_price
                trade['exit_reason'] = "TARGET_+30%"
                trade['pnl_pct'] = (exit_price - trade['entry_price']) / trade['entry_price']
                trade['pnl_rs'] = trade['quantity'] * (exit_price - trade['entry_price'])
                closed_trades.append(trade)
                continue
            elif sl_hit:
                # Exited at SL price (or opened below SL)
                exit_price = min(bar['open'], trade['current_sl']) if bar['open'] < trade['current_sl'] else trade['current_sl']
                trade['exit_date'] = d
                trade['exit_price'] = exit_price
                trade['exit_reason'] = "TRAILED_BE_EXIT" if trade['be_moved'] else "STOP_LOSS_HIT"
                trade['pnl_pct'] = (exit_price - trade['entry_price']) / trade['entry_price']
                trade['pnl_rs'] = trade['quantity'] * (exit_price - trade['entry_price'])
                closed_trades.append(trade)
                continue
            elif time_hit:
                # 60-day time decay exit
                exit_price = close_p
                trade['exit_date'] = d
                trade['exit_price'] = exit_price
                trade['exit_reason'] = "MAX_HOLD_TIME_EXIT"
                trade['pnl_pct'] = (exit_price - trade['entry_price']) / trade['entry_price']
                trade['pnl_rs'] = trade['quantity'] * (exit_price - trade['entry_price'])
                closed_trades.append(trade)
                continue

            remaining_trades.append(trade)

        active_trades = remaining_trades

        # B. Check for new signals generated on previous day to enter on day `d` Open
        # Find signals that occurred on the previous bar
        active_symbols = {t['symbol'] for t in active_trades}

        for sym, s_df in dfs_by_sym.items():
            if len(active_trades) >= max_open_positions:
                break
            if sym in active_symbols:
                continue
            if d not in s_df.index:
                continue

            loc_d = s_df.index.get_loc(d)
            if loc_d == 0:
                continue
            prev_bar = s_df.iloc[loc_d - 1]

            if prev_bar['signal']:
                today_bar = s_df.iloc[loc_d]
                entry_price = float(today_bar['open'])
                breakout_low = float(prev_bar['low'])

                # SL is breakout low or capped at max_stop_loss_pct (e.g. 8%)
                sl_by_low = breakout_low
                sl_by_max_pct = entry_price * (1.0 - max_stop_loss_pct)
                initial_sl = max(sl_by_low, sl_by_max_pct)

                target_price = entry_price * (1.0 + target_pct)
                qty = int(capital_per_trade / entry_price)

                if qty > 0 and initial_sl < entry_price:
                    trade = {
                        'symbol': sym,
                        'entry_date': d,
                        'entry_price': entry_price,
                        'quantity': qty,
                        'initial_sl': initial_sl,
                        'current_sl': initial_sl,
                        'target_price': target_price,
                        'be_moved': False,
                        'target_half_booked': False,
                        'hold_days': 0,
                    }
                    active_trades.append(trade)
                    active_symbols.add(sym)

    # Convert closed trades to DataFrame
    trades_df = pd.DataFrame(closed_trades)
    if trades_df.empty:
        print("No trades triggered under these conditions.")
        return

    # Print summary metrics
    total_trades = len(trades_df)
    winning_trades = trades_df[trades_df['pnl_rs'] > 0]
    losing_trades = trades_df[trades_df['pnl_rs'] <= 0]
    win_rate = (len(winning_trades) / total_trades) * 100

    total_pnl = trades_df['pnl_rs'].sum()
    avg_gain = winning_trades['pnl_pct'].mean() * 100 if len(winning_trades) > 0 else 0
    avg_loss = losing_trades['pnl_pct'].mean() * 100 if len(losing_trades) > 0 else 0
    profit_factor = abs(winning_trades['pnl_rs'].sum() / losing_trades['pnl_rs'].sum()) if losing_trades['pnl_rs'].sum() != 0 else np.nan

    avg_hold_days = trades_df['hold_days'].mean()

    print("\n" + "=" * 70)
    print("  BACKTEST PERFORMANCE METRICS (1-YEAR)")
    print("=" * 70)
    print(f"  Total Trades Taken   : {total_trades}")
    print(f"  Winning Trades       : {len(winning_trades)} ({win_rate:.1f}%)")
    print(f"  Losing / Flat Trades : {len(losing_trades)} ({100-win_rate:.1f}%)")
    print(f"  Total Net Realized   : Rs {total_pnl:+,.2f}")
    print(f"  Profit Factor        : {profit_factor:.2f}")
    print(f"  Average Win Gain     : +{avg_gain:.1f}%")
    print(f"  Average Loss         : {avg_loss:.1f}%")
    print(f"  Reward-to-Risk (R:R) : {abs(avg_gain / avg_loss):.2f}:1" if avg_loss != 0 else "N/A")
    print(f"  Average Holding Time : {avg_hold_days:.1f} trading days")
    print("=" * 70)

    # Breakdown by exit reason
    print("\nExit Breakdown:")
    for reason, group in trades_df.groupby('exit_reason'):
        print(f"  - {reason:<22}: {len(group):2d} trades | PnL: Rs {group['pnl_rs'].sum():+10,.0f} | Avg Gain: {group['pnl_pct'].mean()*100:+5.1f}%")

    print("\nSample Trades:")
    cols = ['symbol', 'entry_date', 'entry_price', 'exit_date', 'exit_price', 'exit_reason', 'hold_days', 'pnl_pct', 'pnl_rs']
    trades_sample = trades_df[cols].copy()
    trades_sample['entry_date'] = trades_sample['entry_date'].dt.strftime('%Y-%m-%d')
    trades_sample['exit_date'] = trades_sample['exit_date'].dt.strftime('%Y-%m-%d')
    trades_sample['pnl_pct'] = (trades_sample['pnl_pct'] * 100).map('{:+.1f}%'.format)
    trades_sample['pnl_rs'] = trades_sample['pnl_rs'].map('Rs {:+,.0f}'.format)
    print(trades_sample.head(15).to_string(index=False))

    trades_df.to_csv("data/swing_backtest_trades.csv", index=False)
    print(f"\nDetailed trade log saved to data/swing_backtest_trades.csv")

if __name__ == '__main__':
    run_backtest()
