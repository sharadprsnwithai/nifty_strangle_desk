import os
import sys
import pandas as pd
import numpy as np
from datetime import time as dt_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kite import get_kite_client

def backtest_mcx_silver(sl_pts=500.0, target_pts=1000.0):
    kite = get_kite_client()
    token = 126774791  # SILVER26DECFUT
    data = kite.historical_data(token, '2026-06-01', '2026-11-06', '15minute')
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df['time'] = df['date'].dt.time
    df['day'] = df['date'].dt.date

    # 20-period volume SMA
    df['vol_sma20'] = df['volume'].rolling(20).mean().shift(1)

    print(f"Loaded {len(df)} 15m candles from {df['day'].min()} to {df['day'].max()}")

    days = sorted(df['day'].unique())
    trades = []

    # Silver Mini (5 kg lot) -> 1 pt = Rs 5
    # Silver Micro (1 kg lot) -> 1 pt = Rs 1
    # Silver Mega (30 kg lot) -> 1 pt = Rs 30
    multiplier_mini = 5.0
    multiplier_micro = 1.0

    for d in days:
        day_df = df[df['day'] == d].sort_values('date').reset_index(drop=True)
        # Find 17:45 reference candle (ends at 18:00)
        ref_bar = day_df[day_df['time'] == dt_time(17, 45)]
        if ref_bar.empty:
            continue
        ref_high = float(ref_bar['high'].iloc[0])
        ref_low = float(ref_bar['low'].iloc[0])

        # Trading window: 18:00 to 22:45
        session_df = day_df[(day_df['time'] >= dt_time(18, 0)) & (day_df['time'] <= dt_time(22, 45))].copy()
        if session_df.empty:
            continue

        active_trade = None

        for idx, bar in session_df.iterrows():
            b_time = bar['time']
            b_high = float(bar['high'])
            b_low = float(bar['low'])
            b_open = float(bar['open'])
            b_close = float(bar['close'])
            b_vol = float(bar['volume'])
            vol_avg = float(bar['vol_sma20']) if not np.isnan(bar['vol_sma20']) else 100.0

            if active_trade is None:
                # Look for breakout until 19:30
                if b_time <= dt_time(19, 30):
                    vol_ok = b_vol >= 1.2 * vol_avg
                    # Long trigger
                    if b_high > ref_high and b_close > ref_high and vol_ok:
                        entry = ref_high + 5.0
                        active_trade = {
                            'day': d,
                            'direction': 'LONG',
                            'entry_time': b_time,
                            'entry_price': entry,
                            'sl_price': entry - sl_pts,
                            'target_price': entry + target_pts,
                        }
                    # Short trigger
                    elif b_low < ref_low and b_close < ref_low and vol_ok:
                        entry = ref_low - 5.0
                        active_trade = {
                            'day': d,
                            'direction': 'SHORT',
                            'entry_time': b_time,
                            'entry_price': entry,
                            'sl_price': entry + sl_pts,
                            'target_price': entry - target_pts,
                        }
            else:
                direction = active_trade['direction']
                entry = active_trade['entry_price']
                sl = active_trade['sl_price']
                tgt = active_trade['target_price']

                # Check Target / SL
                if direction == 'LONG':
                    if b_high >= tgt:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = tgt
                        active_trade['exit_reason'] = 'TARGET_HIT'
                        active_trade['pts'] = target_pts
                        active_trade['pnl_mini_rs'] = target_pts * multiplier_mini
                        active_trade['pnl_micro_rs'] = target_pts * multiplier_micro
                        trades.append(active_trade)
                        break
                    elif b_low <= sl:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = sl
                        active_trade['exit_reason'] = 'STOP_LOSS'
                        active_trade['pts'] = -sl_pts
                        active_trade['pnl_mini_rs'] = -sl_pts * multiplier_mini
                        active_trade['pnl_micro_rs'] = -sl_pts * multiplier_micro
                        trades.append(active_trade)
                        break
                else:  # SHORT
                    if b_low <= tgt:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = tgt
                        active_trade['exit_reason'] = 'TARGET_HIT'
                        active_trade['pts'] = target_pts
                        active_trade['pnl_mini_rs'] = target_pts * multiplier_mini
                        active_trade['pnl_micro_rs'] = target_pts * multiplier_micro
                        trades.append(active_trade)
                        break
                    elif b_high >= sl:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = sl
                        active_trade['exit_reason'] = 'STOP_LOSS'
                        active_trade['pts'] = -sl_pts
                        active_trade['pnl_mini_rs'] = -sl_pts * multiplier_mini
                        active_trade['pnl_micro_rs'] = -sl_pts * multiplier_micro
                        trades.append(active_trade)
                        break

                # End of session cutoff at 22:45
                if b_time == dt_time(22, 45):
                    exit_p = b_close
                    pts = (exit_p - entry) if direction == 'LONG' else (entry - exit_p)
                    active_trade['exit_time'] = b_time
                    active_trade['exit_price'] = exit_p
                    active_trade['exit_reason'] = 'EOD_TIME_EXIT'
                    active_trade['pts'] = pts
                    active_trade['pnl_mini_rs'] = pts * multiplier_mini
                    active_trade['pnl_micro_rs'] = pts * multiplier_micro
                    trades.append(active_trade)
                    break

    tdf = pd.DataFrame(trades)
    print("\n" + "=" * 70)
    print(f"  MCX SILVER 18:00 US SESSION ORB BACKTEST (SL: {sl_pts:.0f} pts | TGT: {target_pts:.0f} pts)")
    print("=" * 70)
    print(f"Total Trading Days      : {len(days)}")
    print(f"Total Trades Taken      : {len(tdf)}")
    wins = tdf[tdf['pts'] > 0]
    losses = tdf[tdf['pts'] <= 0]
    win_rate = (len(wins) / len(tdf)) * 100 if len(tdf) > 0 else 0
    total_pts = tdf['pts'].sum()
    total_pnl_mini = tdf['pnl_mini_rs'].sum()
    total_pnl_micro = tdf['pnl_micro_rs'].sum()
    pf = abs(wins['pts'].sum() / abs(losses['pts'].sum())) if len(losses) > 0 and losses['pts'].sum() != 0 else np.nan

    print(f"Winning Trades          : {len(wins)} ({win_rate:.1f}%)")
    print(f"Losing Trades           : {len(losses)} ({100-win_rate:.1f}%)")
    print(f"Net Points Captured     : {total_pts:+.1f} pts")
    print(f"Net PnL (Silver Mini)   : Rs {total_pnl_mini:+,.0f} (5 kg lot, Rs 5/pt)")
    print(f"Net PnL (Silver Micro)  : Rs {total_pnl_micro:+,.0f} (1 kg lot, Rs 1/pt)")
    print(f"Profit Factor           : {pf:.2f}")
    print(f"Reward-to-Risk (R:R)    : {(target_pts/sl_pts):.2f} : 1")

    print("\nExit Breakdown:")
    for reason, grp in tdf.groupby('exit_reason'):
        print(f"  - {reason:<16}: {len(grp):2d} trades | Net Pts: {grp['pts'].sum():+7,.0f} pts | Mini PnL: Rs {grp['pnl_mini_rs'].sum():+9,.0f}")

    print("\nSample Trades:")
    cols = ['day', 'direction', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'exit_reason', 'pts', 'pnl_mini_rs']
    print(tdf[cols].head(10).to_string(index=False))

    return tdf

if __name__ == '__main__':
    backtest_mcx_silver(sl_pts=500.0, target_pts=1000.0)
