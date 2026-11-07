import os
import sys
import pandas as pd
import numpy as np
from datetime import time as dt_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kite import get_kite_client

def backtest_mcx_crude():
    kite = get_kite_client()
    token = 146796039 # CRUDEOIL26NOVFUT
    data = kite.historical_data(token, '2026-07-20', '2026-11-06', '15minute')
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df['time'] = df['date'].dt.time
    df['day'] = df['date'].dt.date

    # 20-period volume SMA
    df['vol_sma20'] = df['volume'].rolling(20).mean().shift(1)

    print(f"Loaded {len(df)} 15m candles from {df['day'].min()} to {df['day'].max()}")

    days = sorted(df['day'].unique())
    trades = []

    # Parameters
    fixed_risk_pts = 25.0   # 25 points risk on Crude Oil (Rs 2,500 on 100-bbl lot, or Rs 250 on Crude Mini 10-bbl lot)
    target_pts = 50.0       # 1:2 R:R -> 50 points target (Rs 5,000 / Rs 500)
    lot_size = 100          # 1 lot CRUDEOIL = 100 barrels

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
                # Look for breakout only until 19:30 (initial US data window)
                if b_time <= dt_time(19, 30):
                    vol_ok = b_vol >= 1.2 * vol_avg
                    # Long trigger: price crosses above ref_high
                    if b_high > ref_high and b_close > ref_high and vol_ok:
                        entry = ref_high + 1.0
                        active_trade = {
                            'day': d,
                            'direction': 'LONG',
                            'entry_time': b_time,
                            'entry_price': entry,
                            'sl_price': entry - fixed_risk_pts,
                            'target_price': entry + target_pts,
                        }
                    # Short trigger: price crosses below ref_low
                    elif b_low < ref_low and b_close < ref_low and vol_ok:
                        entry = ref_low - 1.0
                        active_trade = {
                            'day': d,
                            'direction': 'SHORT',
                            'entry_time': b_time,
                            'entry_price': entry,
                            'sl_price': entry + fixed_risk_pts,
                            'target_price': entry - target_pts,
                        }
            else:
                # Manage active trade
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
                        active_trade['pnl_rs'] = target_pts * lot_size
                        trades.append(active_trade)
                        break
                    elif b_low <= sl:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = sl
                        active_trade['exit_reason'] = 'STOP_LOSS'
                        active_trade['pts'] = -fixed_risk_pts
                        active_trade['pnl_rs'] = -fixed_risk_pts * lot_size
                        trades.append(active_trade)
                        break
                else: # SHORT
                    if b_low <= tgt:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = tgt
                        active_trade['exit_reason'] = 'TARGET_HIT'
                        active_trade['pts'] = target_pts
                        active_trade['pnl_rs'] = target_pts * lot_size
                        trades.append(active_trade)
                        break
                    elif b_high >= sl:
                        active_trade['exit_time'] = b_time
                        active_trade['exit_price'] = sl
                        active_trade['exit_reason'] = 'STOP_LOSS'
                        active_trade['pts'] = -fixed_risk_pts
                        active_trade['pnl_rs'] = -fixed_risk_pts * lot_size
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
                    active_trade['pnl_rs'] = pts * lot_size
                    trades.append(active_trade)
                    break

    tdf = pd.DataFrame(trades)
    print("\n" + "=" * 70)
    print("  MCX CRUDE OIL 18:00 US SESSION ORB BACKTEST")
    print("=" * 70)
    print(f"Total Trading Days    : {len(days)}")
    print(f"Total Trades Taken    : {len(tdf)}")
    wins = tdf[tdf['pts'] > 0]
    losses = tdf[tdf['pts'] <= 0]
    win_rate = (len(wins) / len(tdf)) * 100 if len(tdf) > 0 else 0
    total_pts = tdf['pts'].sum()
    total_pnl = tdf['pnl_rs'].sum()
    pf = abs(wins['pnl_rs'].sum() / losses['pnl_rs'].sum()) if len(losses) > 0 and losses['pnl_rs'].sum() != 0 else np.nan

    print(f"Winning Trades        : {len(wins)} ({win_rate:.1f}%)")
    print(f"Losing Trades         : {len(losses)} ({100-win_rate:.1f}%)")
    print(f"Net Points Captured   : {total_pts:+.1f} pts")
    print(f"Total Net PnL (1 Lot) : Rs {total_pnl:+,.0f}")
    print(f"Profit Factor         : {pf:.2f}")
    print(f"Reward-to-Risk (R:R)  : 2.00 : 1 (Fixed 50 pts Tgt / 25 pts SL)")

    print("\nExit Breakdown:")
    for reason, grp in tdf.groupby('exit_reason'):
        print(f"  - {reason:<16}: {len(grp):2d} trades | Net PnL: Rs {grp['pnl_rs'].sum():+8,.0f}")

    print("\nSample Trades:")
    cols = ['day', 'direction', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'exit_reason', 'pts', 'pnl_rs']
    print(tdf[cols].head(10).to_string(index=False))

if __name__ == '__main__':
    backtest_mcx_crude()
