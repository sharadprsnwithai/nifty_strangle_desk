import pandas as pd
import numpy as np

def run_parameter_sweeps(data_path="data/swing_daily_2y.parquet"):
    df = pd.read_parquet(data_path)
    df['date'] = pd.to_datetime(df['date'])
    if df['date'].dt.tz is not None:
        df['date'] = df['date'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df = df.sort_values(['symbol', 'date']).reset_index(drop=True)

    configs = [
        {"name": "Raw Video Setup (4x Vol, +30% Tgt, Low SL)", "vol_mult": 4.0, "retest": False, "sl_type": "low", "trail": "be15", "base_filter": False},
        {"name": "3x Volume Spike (Wider sample)", "vol_mult": 3.0, "retest": False, "sl_type": "low", "trail": "be15", "base_filter": False},
        {"name": "2x Volume Spike (Standard CANSLIM)", "vol_mult": 2.0, "retest": False, "sl_type": "low", "trail": "be15", "base_filter": False},
        {"name": "3x Vol + 20-day Base Consolidation (<20% range)", "vol_mult": 3.0, "retest": False, "sl_type": "low", "trail": "be15", "base_filter": True},
        {"name": "3x Vol + 20 EMA Trailing Exit (Ride trends)", "vol_mult": 3.0, "retest": False, "sl_type": "ema20", "trail": "ema20", "base_filter": False},
        {"name": "3x Vol + Retest Pullback Entry (Within 2% of Breakout)", "vol_mult": 3.0, "retest": True, "sl_type": "low", "trail": "be15", "base_filter": False},
    ]

    all_symbols = df['symbol'].unique()
    test_start = pd.Timestamp("2025-11-01")

    results = []

    for cfg in configs:
        vol_mult = cfg["vol_mult"]
        dfs_by_sym = {}
        for sym in all_symbols:
            s_df = df[df['symbol'] == sym].copy().sort_values('date').reset_index(drop=True)
            if len(s_df) < 272:
                continue
            s_df['vol_sma20'] = s_df['volume'].rolling(20).mean().shift(1)
            s_df['high_n'] = s_df['high'].rolling(252).max().shift(1)
            s_df['ema20'] = s_df['close'].ewm(span=20, adjust=False).mean()
            s_df['range20'] = (s_df['high'].rolling(20).max().shift(1) - s_df['low'].rolling(20).min().shift(1)) / s_df['close'].shift(1)

            c1 = s_df['close'] > s_df['high_n']
            c2 = (s_df['close'] > s_df['open']) & (s_df['close'] > s_df['close'].shift(1))
            c3 = s_df['volume'] >= (vol_mult * s_df['vol_sma20'])
            c4 = s_df['volume'] >= 50000
            c5 = (s_df['high'] - s_df['low']) / s_df['close'] > 0.005

            if cfg["base_filter"]:
                c6 = s_df['range20'] <= 0.20
                s_df['signal'] = c1 & c2 & c3 & c4 & c5 & c6
            else:
                s_df['signal'] = c1 & c2 & c3 & c4 & c5

            dfs_by_sym[sym] = s_df.set_index('date')

        all_dates = sorted(df[df['date'] >= test_start]['date'].unique())
        active_trades = []
        closed_trades = []

        for d in all_dates:
            rem = []
            for t in active_trades:
                sym = t['symbol']
                s_df = dfs_by_sym.get(sym)
                if s_df is None or d not in s_df.index:
                    rem.append(t)
                    continue
                bar = s_df.loc[d]
                t['hold_days'] += 1

                # Trailing logic
                if cfg["trail"] == "be15":
                    if (bar['high'] - t['entry_price']) / t['entry_price'] >= 0.15:
                        t['current_sl'] = max(t['current_sl'], t['entry_price'])
                elif cfg["trail"] == "ema20":
                    if bar['close'] > bar['ema20']:
                        t['current_sl'] = max(t['current_sl'], bar['ema20'])

                # Target
                if cfg["trail"] != "ema20" and bar['high'] >= t['target_price']:
                    t['exit_price'] = t['target_price']
                    t['pnl_rs'] = t['quantity'] * (t['exit_price'] - t['entry_price'])
                    t['pnl_pct'] = (t['exit_price'] - t['entry_price']) / t['entry_price']
                    t['exit_reason'] = "TARGET_HIT"
                    closed_trades.append(t)
                    continue

                # Stop loss
                if bar['low'] <= t['current_sl']:
                    t['exit_price'] = min(bar['open'], t['current_sl']) if bar['open'] < t['current_sl'] else t['current_sl']
                    t['pnl_rs'] = t['quantity'] * (t['exit_price'] - t['entry_price'])
                    t['pnl_pct'] = (t['exit_price'] - t['entry_price']) / t['entry_price']
                    t['exit_reason'] = "STOP_LOSS"
                    closed_trades.append(t)
                    continue

                if t['hold_days'] >= 60:
                    t['exit_price'] = bar['close']
                    t['pnl_rs'] = t['quantity'] * (t['exit_price'] - t['entry_price'])
                    t['pnl_pct'] = (t['exit_price'] - t['entry_price']) / t['entry_price']
                    t['exit_reason'] = "TIME_EXIT"
                    closed_trades.append(t)
                    continue

                rem.append(t)
            active_trades = rem

            # New entries
            for sym, s_df in dfs_by_sym.items():
                if len(active_trades) >= 10:
                    break
                if any(t['symbol'] == sym for t in active_trades):
                    continue
                if d not in s_df.index:
                    continue
                loc_d = s_df.index.get_loc(d)
                if loc_d == 0:
                    continue
                prev_bar = s_df.iloc[loc_d - 1]

                if prev_bar['signal']:
                    today_bar = s_df.iloc[loc_d]
                    entry_p = float(today_bar['open'])

                    if cfg["retest"]:
                        # Enter only if open is within 2% of breakout level
                        if entry_p > prev_bar['high_n'] * 1.025:
                            continue

                    sl_p = max(float(prev_bar['low']), entry_p * 0.92)
                    tgt_p = entry_p * 1.30
                    qty = int(100000.0 / entry_p)
                    if qty > 0 and sl_p < entry_p:
                        active_trades.append({
                            'symbol': sym,
                            'entry_price': entry_p,
                            'quantity': qty,
                            'initial_sl': sl_p,
                            'current_sl': sl_p,
                            'target_price': tgt_p,
                            'hold_days': 0
                        })

        tdf = pd.DataFrame(closed_trades)
        if len(tdf) > 0:
            wins = tdf[tdf['pnl_rs'] > 0]
            losses = tdf[tdf['pnl_rs'] <= 0]
            pf = abs(wins['pnl_rs'].sum() / losses['pnl_rs'].sum()) if len(losses) > 0 and losses['pnl_rs'].sum() != 0 else np.nan
            results.append({
                "Configuration": cfg["name"],
                "Trades": len(tdf),
                "Win Rate": f"{(len(wins)/len(tdf))*100:.1f}%",
                "Total PnL": f"Rs {tdf['pnl_rs'].sum():+,.0f}",
                "Profit Factor": f"{pf:.2f}",
                "Avg Win": f"+{wins['pnl_pct'].mean()*100:.1f}%" if len(wins)>0 else "0%",
                "Avg Loss": f"{losses['pnl_pct'].mean()*100:.1f}%" if len(losses)>0 else "0%"
            })

    res_df = pd.DataFrame(results)
    print("\n" + "=" * 90)
    print("  STRATEGY VARIATION COMPARISON (LAST 1 YEAR: NOV 2025 - NOV 2026)")
    print("=" * 90)
    print(res_df.to_string(index=False))

if __name__ == '__main__':
    run_parameter_sweeps()
