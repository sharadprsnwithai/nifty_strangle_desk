import os
import sys
import pandas as pd
import numpy as np
from datetime import time as dt_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kite import get_kite_client

def simulate_strategy_variations():
    kite = get_kite_client()
    token = 126774791  # SILVER26DECFUT
    data = kite.historical_data(token, '2026-06-01', '2026-11-06', '15minute')
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df['time'] = df['date'].dt.time
    df['day'] = df['date'].dt.date
    df['vol_sma20'] = df['volume'].rolling(20).mean().shift(1)
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()

    days = sorted(df['day'].unique())

    # We test 4 models:
    # Model 1: Baseline 2 Lots Fixed (Both exit at 1000 pts Tgt / 500 pts SL)
    # Model 2: 2-Lot Scaling (Lot 1 exits at +800 pts, SL moves to BE, Lot 2 trails 20 EMA or 22:45)
    # Model 3: Model 2 + Trend Bias Filter (Long only if > EMA50, Short only if < EMA50)
    # Model 4: Model 3 + Wick Filter (Reject breakout candle if wick > 25% against breakout)

    models = [
        {"name": "1. Baseline (Fixed 2 Lots Exit @ 1000 pts)", "trend_filter": False, "wick_filter": False, "scaling": False},
        {"name": "2. 2-Lot Tranche Scaling (Lot 1 @ 800, Lot 2 Trail EMA20)", "trend_filter": False, "wick_filter": False, "scaling": True},
        {"name": "3. Scaling + 50 EMA Trend Filter (Trade with Trend)", "trend_filter": True, "wick_filter": False, "scaling": True},
        {"name": "4. Scaling + Trend + Wick Filter (Institutional Grade)", "trend_filter": True, "wick_filter": True, "scaling": True},
    ]

    summary = []

    for m in models:
        trades = []
        for d in days:
            day_df = df[df['day'] == d].sort_values('date').reset_index(drop=True)
            ref_bar = day_df[day_df['time'] == dt_time(17, 45)]
            if ref_bar.empty:
                continue
            ref_high = float(ref_bar['high'].iloc[0])
            ref_low = float(ref_bar['low'].iloc[0])

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
                ema50_val = float(bar['ema50'])

                if active_trade is None:
                    if b_time <= dt_time(19, 30):
                        vol_ok = b_vol >= 1.2 * vol_avg

                        # Check Long
                        long_sig = b_high > ref_high and b_close > ref_high and vol_ok
                        if m["trend_filter"]:
                            long_sig = long_sig and (b_close > ema50_val)
                        if m["wick_filter"] and long_sig:
                            candle_range = b_high - b_low
                            upper_wick = b_high - max(b_open, b_close)
                            if candle_range > 0 and (upper_wick / candle_range) > 0.25:
                                long_sig = False

                        # Check Short
                        short_sig = b_low < ref_low and b_close < ref_low and vol_ok
                        if m["trend_filter"]:
                            short_sig = short_sig and (b_close < ema50_val)
                        if m["wick_filter"] and short_sig:
                            candle_range = b_high - b_low
                            lower_wick = min(b_open, b_close) - b_low
                            if candle_range > 0 and (lower_wick / candle_range) > 0.25:
                                short_sig = False

                        if long_sig:
                            entry = ref_high + 5.0
                            active_trade = {
                                'day': d, 'direction': 'LONG', 'entry_time': b_time, 'entry_price': entry,
                                'sl': entry - 500.0, 'tgt1': entry + 800.0, 'be_moved': False,
                                'lot1_closed': False, 'lot1_pts': 0.0, 'lot2_closed': False, 'lot2_pts': 0.0
                            }
                        elif short_sig:
                            entry = ref_low - 5.0
                            active_trade = {
                                'day': d, 'direction': 'SHORT', 'entry_time': b_time, 'entry_price': entry,
                                'sl': entry + 500.0, 'tgt1': entry - 800.0, 'be_moved': False,
                                'lot1_closed': False, 'lot1_pts': 0.0, 'lot2_closed': False, 'lot2_pts': 0.0
                            }
                else:
                    # Manage trade
                    direction = active_trade['direction']
                    entry = active_trade['entry_price']
                    sl = active_trade['sl']

                    if not m["scaling"]:
                        # Fixed 2 lots exit together @ 1000 pts
                        tgt = entry + 1000.0 if direction == 'LONG' else entry - 1000.0
                        if direction == 'LONG':
                            if b_high >= tgt:
                                active_trade['pnl_rs'] = 1000.0 * 10.0 # 2 lots = 10 Rs/pt
                                trades.append(active_trade)
                                break
                            elif b_low <= sl:
                                active_trade['pnl_rs'] = -500.0 * 10.0
                                trades.append(active_trade)
                                break
                        else:
                            if b_low <= tgt:
                                active_trade['pnl_rs'] = 1000.0 * 10.0
                                trades.append(active_trade)
                                break
                            elif b_high >= sl:
                                active_trade['pnl_rs'] = -500.0 * 10.0
                                trades.append(active_trade)
                                break

                        if b_time == dt_time(22, 45):
                            pts = (b_close - entry) if direction == 'LONG' else (entry - b_close)
                            active_trade['pnl_rs'] = pts * 10.0
                            trades.append(active_trade)
                            break
                    else:
                        # Scaling Model
                        tgt1 = active_trade['tgt1']
                        # 1. Manage Lot 1 (5 kg = Rs 5/pt)
                        if not active_trade['lot1_closed']:
                            if direction == 'LONG':
                                if b_high >= tgt1:
                                    active_trade['lot1_pts'] = 800.0
                                    active_trade['lot1_closed'] = True
                                    active_trade['sl'] = entry # Move SL to Breakeven
                                    active_trade['be_moved'] = True
                                elif b_low <= sl:
                                    active_trade['lot1_pts'] = -500.0
                                    active_trade['lot2_pts'] = -500.0
                                    active_trade['lot1_closed'] = True
                                    active_trade['lot2_closed'] = True
                                    active_trade['pnl_rs'] = -500.0 * 10.0
                                    trades.append(active_trade)
                                    break
                            else: # SHORT
                                if b_low <= tgt1:
                                    active_trade['lot1_pts'] = 800.0
                                    active_trade['lot1_closed'] = True
                                    active_trade['sl'] = entry
                                    active_trade['be_moved'] = True
                                elif b_high >= sl:
                                    active_trade['lot1_pts'] = -500.0
                                    active_trade['lot2_pts'] = -500.0
                                    active_trade['lot1_closed'] = True
                                    active_trade['lot2_closed'] = True
                                    active_trade['pnl_rs'] = -500.0 * 10.0
                                    trades.append(active_trade)
                                    break

                        # 2. Manage Lot 2 (Runner with Breakeven/EMA20/EOD exit)
                        if active_trade['lot1_closed'] and not active_trade['lot2_closed']:
                            curr_sl = active_trade['sl']
                            if direction == 'LONG':
                                if b_low <= curr_sl:
                                    active_trade['lot2_pts'] = 0.0 if active_trade['be_moved'] else -500.0
                                    active_trade['lot2_closed'] = True
                                    active_trade['pnl_rs'] = (active_trade['lot1_pts'] * 5.0) + (active_trade['lot2_pts'] * 5.0)
                                    trades.append(active_trade)
                                    break
                            else:
                                if b_high >= curr_sl:
                                    active_trade['lot2_pts'] = 0.0 if active_trade['be_moved'] else -500.0
                                    active_trade['lot2_closed'] = True
                                    active_trade['pnl_rs'] = (active_trade['lot1_pts'] * 5.0) + (active_trade['lot2_pts'] * 5.0)
                                    trades.append(active_trade)
                                    break

                        # End of day 22:45
                        if b_time == dt_time(22, 45):
                            pts = (b_close - entry) if direction == 'LONG' else (entry - b_close)
                            if not active_trade['lot1_closed']:
                                active_trade['lot1_pts'] = pts
                            if not active_trade['lot2_closed']:
                                active_trade['lot2_pts'] = pts
                            active_trade['pnl_rs'] = (active_trade['lot1_pts'] * 5.0) + (active_trade['lot2_pts'] * 5.0)
                            trades.append(active_trade)
                            break

        tdf = pd.DataFrame(trades)
        if len(tdf) > 0:
            tdf['equity'] = tdf['pnl_rs'].cumsum()
            tdf['peak'] = tdf['equity'].cummax()
            tdf['drawdown'] = tdf['equity'] - tdf['peak']
            max_dd = abs(tdf['drawdown'].min())
            total_profit = tdf['pnl_rs'].sum()
            wins = tdf[tdf['pnl_rs'] > 0]
            losses = tdf[tdf['pnl_rs'] < 0]
            win_rate = (len(wins) / len(tdf)) * 100
            pf = abs(wins['pnl_rs'].sum() / losses['pnl_rs'].sum()) if len(losses) > 0 else np.nan

            summary.append({
                "Strategy Model": m["name"],
                "Trades": len(tdf),
                "Win Rate": f"{win_rate:.1f}%",
                "Total Profit": f"Rs {total_profit:+,.0f}",
                "Max Drawdown": f"Rs {max_dd:,.0f}",
                "Profit / Drawdown Ratio": f"{(total_profit / max_dd):.2f}x" if max_dd > 0 else "N/A",
                "Profit Factor": f"{pf:.2f}"
            })

    res = pd.DataFrame(summary)
    print("\n" + "=" * 100)
    print("  STRATEGY OPTIMIZATION: HIGHER PROFIT & MINIMUM DRAWDOWN (2 LOTS SILVER MINI)")
    print("=" * 100)
    print(res.to_string(index=False))

if __name__ == '__main__':
    simulate_strategy_variations()
