#!/usr/bin/env python3
"""
Backtest Nifty 200 Momentum 30 Stock Futures Desk with Hard Concurrency Limits.
Simulates chronological portfolio execution across all 30 momentum equities.
"""

import os
import sys
import glob
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
import pandas as pd
import numpy as np

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load universe and official lot sizes
from strategy.stock_futures.config import StockFuturesConfig, MOMENTUM_30_LOT_SIZES, STOCK_NAME_MAPPING


def calculate_futures_charges(entry_price: float, exit_price: float, quantity: int, side: str = "LONG", orders_count: int = 2) -> float:
    """Calculates all statutory taxes and broker fees for NSE stock futures."""
    buy_turnover = entry_price * quantity if side == "LONG" else exit_price * quantity
    sell_turnover = exit_price * quantity if side == "LONG" else entry_price * quantity
    total_turnover = buy_turnover + sell_turnover

    brokerage = 20.0 * orders_count
    stt = sell_turnover * 0.0002            # 0.02% on sell turnover
    exchange_txn = total_turnover * 0.000019 # 0.0019% exchange transaction fee
    gst = (brokerage + exchange_txn) * 0.18 # 18% GST
    stamp_duty = buy_turnover * 0.00002     # 0.002% on buy turnover
    sebi = total_turnover * 0.000001        # Rs 10 per crore

    return brokerage + stt + exchange_txn + gst + stamp_duty + sebi


def run_chronological_backtest(
    max_concurrent: int = 2,
    lots_per_trade: int = 2,
    target_pct: float = 0.0225,
    sl_pct: float = 0.010,
    vol_threshold: float = 1.5,
    entry_start_time: str = "10:30",
    entry_cutoff_time: str = "13:00",
    squareoff_time: str = "15:00",
    slippage_pct_per_leg: float = 0.0005, # 0.05% entry + 0.05% exit = 0.10% round trip
    data_dir: str = "data/cache_15m_1y"
) -> Dict[str, Any]:
    cfg = StockFuturesConfig(lots_per_trade=lots_per_trade)
    symbols = cfg.symbols
    lot_sizes = cfg.lot_sizes

    # Preload all 15m parquet files
    all_data = {}
    all_days = set()

    for sym in symbols:
        fp = os.path.join(data_dir, f"{sym}_15m.parquet")
        if not os.path.exists(fp):
            continue
        df = pd.read_parquet(fp)
        df['datetime'] = pd.to_datetime(df['date'])
        df['day'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['time'] = df['datetime'].dt.strftime('%H:%M')
        df['vol_ma20'] = df['volume'].rolling(20, min_periods=5).mean()
        
        # Index by (day, time)
        grouped = {d: group.set_index('time') for d, group in df.groupby('day')}
        all_data[sym] = grouped
        all_days.update(grouped.keys())

    sorted_days = sorted(list(all_days))
    candles_times = [
        "09:15", "09:30", "09:45", "10:00", "10:15",
        "10:30", "10:45", "11:00", "11:15", "11:30", "11:45",
        "12:00", "12:15", "12:30", "12:45", "13:00", "13:15",
        "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00"
    ]
    consolidation_candles = ["09:30", "09:45", "10:00", "10:15"]

    trade_records = []
    daily_stats = []

    for day in sorted_days:
        active_positions: Dict[str, Dict[str, Any]] = {}
        traded_today = set()
        day_pnl = 0.0
        peak_concurrent_today = 0

        for t in candles_times:
            # 1. Update/check exits for active positions
            closed_symbols = []
            for sym, pos in active_positions.items():
                day_dict = all_data.get(sym, {}).get(day)
                if day_dict is None or t not in day_dict.index:
                    continue
                bar = day_dict.loc[t]

                exit_price = None
                exit_reason = None

                if pos['side'] == "LONG":
                    # Check SL first (conservative)
                    if bar['low'] <= pos['sl_price']:
                        exit_price = pos['sl_price']
                        exit_reason = "SL_HIT"
                    elif bar['high'] >= pos['target_price']:
                        exit_price = pos['target_price']
                        exit_reason = "TARGET_HIT"
                    elif t >= squareoff_time:
                        exit_price = bar['close']
                        exit_reason = "15:00_EOD"
                else:  # SHORT
                    if bar['high'] >= pos['sl_price']:
                        exit_price = pos['sl_price']
                        exit_reason = "SL_HIT"
                    elif bar['low'] <= pos['target_price']:
                        exit_price = pos['target_price']
                        exit_reason = "TARGET_HIT"
                    elif t >= squareoff_time:
                        exit_price = bar['close']
                        exit_reason = "15:00_EOD"

                if exit_price is not None:
                    # Calculate P&L
                    qty = pos['quantity']
                    entry_p = pos['entry_price']
                    gross = (exit_price - entry_p) * qty if pos['side'] == "LONG" else (entry_p - exit_price) * qty
                    charges = calculate_futures_charges(entry_p, exit_price, qty, pos['side'])
                    turnover = (entry_p + exit_price) * qty
                    slippage = turnover * slippage_pct_per_leg
                    net = gross - charges - slippage

                    trade_records.append({
                        "symbol": sym,
                        "name": STOCK_NAME_MAPPING.get(sym, sym),
                        "day": day,
                        "side": pos['side'],
                        "entry_time": pos['entry_time'],
                        "exit_time": t,
                        "entry_price": entry_p,
                        "exit_price": exit_price,
                        "quantity": qty,
                        "lots": lots_per_trade,
                        "exit_reason": exit_reason,
                        "vol_ratio": pos['vol_ratio'],
                        "gross_pnl": round(gross, 2),
                        "charges": round(charges, 2),
                        "slippage": round(slippage, 2),
                        "net_pnl": round(net, 2),
                        "net_pnl_raw": round(gross - charges, 2), # without slippage
                        "month": day[:7]
                    })
                    closed_symbols.append(sym)
                    day_pnl += net

            for sym in closed_symbols:
                del active_positions[sym]

            # Track peak concurrent positions
            if len(active_positions) > peak_concurrent_today:
                peak_concurrent_today = len(active_positions)

            # 2. Check entries if within entry scan window (10:30 <= t <= 13:00)
            if t < entry_start_time or t > entry_cutoff_time:
                continue

            if len(active_positions) >= max_concurrent:
                continue

            for sym in symbols:
                if len(active_positions) >= max_concurrent:
                    break
                if sym in active_positions or sym in traded_today:
                    continue

                day_dict = all_data.get(sym, {}).get(day)
                if day_dict is None:
                    continue

                # Must have C1 (09:15) and all consolidation bars (09:30, 09:45, 10:00, 10:15)
                if "09:15" not in day_dict.index:
                    continue
                c1_bar = day_dict.loc["09:15"]
                h1 = c1_bar['high']
                l1 = c1_bar['low']

                has_all_cons = all(cb in day_dict.index for cb in consolidation_candles)
                if not has_all_cons:
                    continue

                cons_closes = [day_dict.loc[cb]['close'] for cb in consolidation_candles]
                if not all(l1 <= cl <= h1 for cl in cons_closes):
                    continue

                # Check current trigger bar
                if t not in day_dict.index:
                    continue
                curr_bar = day_dict.loc[t]

                vol_ma = curr_bar['vol_ma20']
                if pd.isna(vol_ma) or vol_ma <= 0:
                    continue
                vol_ratio = curr_bar['volume'] / vol_ma
                if vol_ratio < vol_threshold:
                    continue

                long_trig = curr_bar['high'] > h1
                short_trig = curr_bar['low'] < l1

                if long_trig and short_trig:
                    continue

                trade_qty = lot_sizes.get(sym, 100) * lots_per_trade

                if long_trig:
                    entry = max(curr_bar['open'], h1)
                    sl = entry * (1.0 - sl_pct)
                    tgt = entry * (1.0 + target_pct)
                    active_positions[sym] = {
                        "side": "LONG",
                        "entry_price": entry,
                        "sl_price": sl,
                        "target_price": tgt,
                        "quantity": trade_qty,
                        "entry_time": t,
                        "vol_ratio": round(vol_ratio, 2)
                    }
                    traded_today.add(sym)

                elif short_trig:
                    entry = min(curr_bar['open'], l1)
                    sl = entry * (1.0 + sl_pct)
                    tgt = entry * (1.0 - target_pct)
                    active_positions[sym] = {
                        "side": "SHORT",
                        "entry_price": entry,
                        "sl_price": sl,
                        "target_price": tgt,
                        "quantity": trade_qty,
                        "entry_time": t,
                        "vol_ratio": round(vol_ratio, 2)
                    }
                    traded_today.add(sym)

        daily_stats.append({
            "day": day,
            "net_pnl": day_pnl,
            "trades_count": len([t for t in trade_records if t['day'] == day]),
            "peak_concurrent": peak_concurrent_today
        })

    tdf = pd.DataFrame(trade_records)
    ddf = pd.DataFrame(daily_stats)
    return {"trades": tdf, "daily": ddf}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Chronological Stock Futures Backtest")
    parser.add_argument("--max-concurrent", type=int, default=2, help="Max concurrent positions (default: 2)")
    parser.add_argument("--lots", type=int, default=2, help="Lots per trade (default: 2)")
    parser.add_argument("--target-pct", type=float, default=0.0225, help="Target pct (default: 0.0225)")
    parser.add_argument("--sl-pct", type=float, default=0.010, help="SL pct (default: 0.010)")
    parser.add_argument("--slip-per-leg", type=float, default=0.0005, help="Slippage pct per leg (default: 0.0005 = 0.05%)")
    args = parser.parse_args()

    print("=" * 115)
    print(f"  STOCK FUTURES BACKTEST (MOMENTUM 30) - HARD CONCURRENCY: {args.max_concurrent} POSITIONS | {args.lots} LOTS")
    print(f"  Target: {args.target_pct*100:.2f}% | Stop Loss: {args.sl_pct*100:.1f}% | Vol Filter: 1.5x | Cutoff: 13:00 | EOD: 15:00")
    print("=" * 115)

    res = run_chronological_backtest(
        max_concurrent=args.max_concurrent,
        lots_per_trade=args.lots,
        target_pct=args.target_pct,
        sl_pct=args.sl_pct,
        slippage_pct_per_leg=args.slip_per_leg
    )

    tdf = res["trades"]
    ddf = res["daily"]

    if len(tdf) == 0:
        print("No trades triggered.")
        sys.exit(0)

    # Save to data directory
    outfile = f"data/stock_futures_backtest_concurrent_{args.max_concurrent}.csv"
    tdf.to_csv(outfile, index=False)
    print(f"Detailed trade log saved to: {outfile}\n")

    # Metrics Summary
    total_trades = len(tdf)
    wins = len(tdf[tdf['net_pnl'] > 0])
    losses = len(tdf[tdf['net_pnl'] < 0])
    win_rate = (wins / total_trades) * 100.0 if total_trades > 0 else 0.0

    gross_profit = tdf[tdf['gross_pnl'] > 0]['gross_pnl'].sum()
    gross_loss = abs(tdf[tdf['gross_pnl'] < 0]['gross_pnl'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.nan

    total_gross = tdf['gross_pnl'].sum()
    total_charges = tdf['charges'].sum()
    total_slippage = tdf['slippage'].sum()
    total_net = tdf['net_pnl'].sum()
    total_net_raw = tdf['net_pnl_raw'].sum()

    # Drawdown
    ddf['cum_pnl'] = ddf['net_pnl'].cumsum()
    ddf['peak'] = ddf['cum_pnl'].cummax()
    ddf['dd'] = ddf['peak'] - ddf['cum_pnl']
    max_dd = ddf['dd'].max()

    # Exits breakdown
    tgt_hits = len(tdf[tdf['exit_reason'] == "TARGET_HIT"])
    sl_hits = len(tdf[tdf['exit_reason'] == "SL_HIT"])
    eod_exits = len(tdf[tdf['exit_reason'] == "15:00_EOD"])

    # Monthly breakdown
    monthly = tdf.groupby('month').agg(
        trades=('net_pnl', 'count'),
        wins=('net_pnl', lambda x: (x > 0).sum()),
        losses=('net_pnl', lambda x: (x < 0).sum()),
        gross_pnl=('gross_pnl', 'sum'),
        charges=('charges', 'sum'),
        slippage=('slippage', 'sum'),
        net_pnl_raw=('net_pnl_raw', 'sum'),
        net_pnl=('net_pnl', 'sum'),
    ).reset_index()

    monthly['win_rate'] = (monthly['wins'] / monthly['trades']) * 100.0

    # Margin requirements calculation for max concurrent
    # Average contract value across trades
    avg_contract = (tdf['entry_price'] * tdf['quantity']).mean()
    margin_req = avg_contract * 0.22 * args.max_concurrent

    print("-" * 115)
    print("                                      OVERALL METRICS SUMMARY")
    print("-" * 115)
    print(f"  Total Trades Executed       : {total_trades}")
    print(f"  Winning / Losing Trades     : {wins} Wins / {losses} Losses")
    print(f"  Win Rate                    : {win_rate:.2f}%")
    print(f"  Profit Factor (Gross)       : {profit_factor:.2f}")
    print(f"  Target Hits (+2.25%)        : {tgt_hits} ({tgt_hits/total_trades*100:.1f}%)")
    print(f"  Stop Loss Hits (-1.0%)      : {sl_hits} ({sl_hits/total_trades*100:.1f}%)")
    print(f"  15:00 EOD Squareoffs        : {eod_exits} ({eod_exits/total_trades*100:.1f}%)")
    print(f"  Average Trade Duration / EOD: {tdf['entry_time'].mode()[0]} -> {tdf['exit_time'].mode()[0]}")
    print("-" * 115)
    print(f"  Gross Realized PnL          : Rs {total_gross:>14,.2f}")
    print(f"  Statutory Taxes & Charges   : Rs {total_charges:>14,.2f}  (STT, Brokerage, GST, Stamp Duty, SEBI)")
    print(f"  Net PnL (Zero Slippage)     : Rs {total_net_raw:>14,.2f}")
    print(f"  Modeled Slippage (0.10% RT) : Rs {total_slippage:>14,.2f}  (0.05% entry + 0.05% exit on turnover)")
    print(f"  Net Realized PnL (Post-All) : Rs {total_net:>14,.2f}")
    print(f"  Max Historical Drawdown     : Rs {max_dd:>14,.2f}")
    print(f"  Average Monthly Net Income  : Rs {total_net / len(monthly):>14,.2f} / month")
    print(f"  Estimated Margin (22% Span) : Rs {margin_req:>14,.2f}  ({args.max_concurrent} concurrent positions)")
    print(f"  Return / Max Drawdown Ratio : {total_net / max_dd:.2f}x")
    print("-" * 115)

    print("\n" + "=" * 115)
    print("                                   MONTH-BY-MONTH BREAKDOWN")
    print("=" * 115)
    print(f"{'Month':<8} | {'Trades':<6} | {'Win Rate':<8} | {'Gross PnL (Rs)':<16} | {'Charges':<10} | {'Slippage':<10} | {'Net PnL (Rs)':<16} | {'Cum Net (Rs)':<16}")
    print("-" * 115)
    cum_net = 0.0
    for _, r in monthly.iterrows():
        cum_net += r['net_pnl']
        print(f"{r['month']:<8} | {r['trades']:>6} | {r['win_rate']:>7.1f}% | Rs {r['gross_pnl']:>12,.2f} | Rs {r['charges']:>7,.0f} | Rs {r['slippage']:>7,.0f} | Rs {r['net_pnl']:>12,.2f} | Rs {cum_net:>12,.2f}")
    print("=" * 115)
