#!/usr/bin/env python3
"""
Backtester for Milind Upasani's MTF Techno-Funda Breakout Strategy (Cash Buying).
Parameters:
  - Capital per trade: Rs 1,00,000
  - Stop Loss: Fixed 5.0%
  - Target: Fixed 10.0% (1:2 Risk-Reward)
  - Max holding period: 45 trading days
  - Universe: 248 liquid equities from data/swing_daily_2y.parquet (2-year history)
"""

import argparse
import sys
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.techno_funda.technicals import calculate_rsi


def run_techno_funda_cash_backtest(
    sl_pct: Optional[float] = 0.05,
    target_pct: Optional[float] = 0.10,
    allocation_per_trade: float = 100000.0,
    max_holding_days: int = 45,
    monthly_rsi_min: float = 58.0,
    weekly_rsi_min: float = 58.0,
    daily_rsi_trigger: float = 65.0,
    daily_rsi_max: float = 75.0,
    data_path: Path = Path("data/swing_daily_2y.parquet"),
) -> pd.DataFrame:
    print("=" * 80)
    print("  MTF TECHNO-FUNDA CASH BUYING BACKTEST")
    if sl_pct is not None and target_pct is not None:
        print(f"  Mode: Fixed Risk-Reward (SL: {sl_pct*100:.1f}% | Target: {target_pct*100:.1f}% | R:R 1:{target_pct/sl_pct:.1f})")
    else:
        print("  Mode: PURE TIME EXIT (No Stop Loss, No Target)")
    print(f"  Monthly RSI: >={monthly_rsi_min:.0f} | Weekly RSI: >={weekly_rsi_min:.0f} | Daily Trigger: >={daily_rsi_trigger:.0f}")
    print(f"  Capital Per Trade: Rs {allocation_per_trade:,.0f} | Holding Period: {max_holding_days} Days")
    print("=" * 80)

    raw_df = pd.read_parquet(data_path)
    raw_df["date"] = pd.to_datetime(raw_df["date"])

    trades = []
    symbols = raw_df["symbol"].unique()
    print(f"Loaded {len(raw_df)} bars across {len(symbols)} symbols. Processing signals...")

    for sym in symbols:
        sub = raw_df[raw_df["symbol"] == sym].sort_values("date").reset_index(drop=True)
        if len(sub) < 60:
            continue

        # 1. Daily RSI
        sub["daily_rsi"] = calculate_rsi(sub["close"], 14)
        sub["prev_daily_rsi"] = sub["daily_rsi"].shift(1)

        # 2. Weekly RSI
        w_df = sub[["date", "close"]].set_index("date").resample("W-FRI").last().dropna().reset_index()
        w_df["weekly_rsi"] = calculate_rsi(w_df["close"], 14)

        # 3. Monthly RSI
        m_df = sub[["date", "close"]].set_index("date").resample("ME").last().dropna().reset_index()
        m_df["monthly_rsi"] = calculate_rsi(m_df["close"], 14)

        # Merge backward to prevent lookahead bias
        sub = pd.merge_asof(sub, w_df[["date", "weekly_rsi"]], on="date", direction="backward")
        sub = pd.merge_asof(sub, m_df[["date", "monthly_rsi"]], on="date", direction="backward")

        # 4. Signal Detection: Monthly >= min, Weekly >= min, Daily crossed 65
        crossed_daily = (sub["daily_rsi"] >= daily_rsi_trigger) & (sub["prev_daily_rsi"] < daily_rsi_trigger)
        in_zone = (sub["daily_rsi"] >= daily_rsi_trigger) & (sub["daily_rsi"] <= daily_rsi_max)
        trend_aligned = (sub["monthly_rsi"] >= monthly_rsi_min) & (sub["weekly_rsi"] >= weekly_rsi_min)

        signals = trend_aligned & (crossed_daily | in_zone)

        # 5. Simulate Trades
        i = 0
        n = len(sub)
        while i < n - 1:
            if signals.iloc[i]:
                # Entry next trading day at Open
                entry_idx = i + 1
                entry_date = sub["date"].iloc[entry_idx]
                entry_price = float(sub["open"].iloc[entry_idx])
                if entry_price <= 0:
                    i += 1
                    continue

                shares = int(allocation_per_trade / entry_price)
                if shares <= 0:
                    i += 1
                    continue

                actual_cost = shares * entry_price
                sl_price = round(entry_price * (1.0 - sl_pct), 2) if sl_pct is not None else None
                target_price = round(entry_price * (1.0 + target_pct), 2) if target_pct is not None else None

                exit_price = None
                exit_date = None
                exit_reason = None
                hold_days = 0

                # Track forward bars
                for k in range(entry_idx, min(entry_idx + max_holding_days, n)):
                    hold_days += 1
                    bar_open = float(sub["open"].iloc[k])
                    bar_high = float(sub["high"].iloc[k])
                    bar_low = float(sub["low"].iloc[k])
                    bar_close = float(sub["close"].iloc[k])
                    curr_date = sub["date"].iloc[k]

                    if target_price is not None and sl_price is not None:
                        # Check Gaps at Open
                        if bar_open >= target_price:
                            exit_price = bar_open
                            exit_date = curr_date
                            exit_reason = "TARGET_GAP"
                            break
                        elif bar_open <= sl_price:
                            exit_price = bar_open
                            exit_date = curr_date
                            exit_reason = "SL_GAP"
                            break

                        # Check Intraday Hits (Conservative: check SL first if both breach)
                        hit_sl = bar_low <= sl_price
                        hit_target = bar_high >= target_price

                        if hit_sl and hit_target:
                            exit_price = sl_price
                            exit_date = curr_date
                            exit_reason = "SL_HIT"
                            break
                        elif hit_target:
                            exit_price = target_price
                            exit_date = curr_date
                            exit_reason = "TARGET_HIT"
                            break
                        elif hit_sl:
                            exit_price = sl_price
                            exit_date = curr_date
                            exit_reason = "SL_HIT"
                            break

                # Max Holding Time Exit
                if exit_price is None:
                    last_idx = min(entry_idx + max_holding_days - 1, n - 1)
                    exit_price = float(sub["close"].iloc[last_idx])
                    exit_date = sub["date"].iloc[last_idx]
                    exit_reason = f"TIME_EXIT_{max_holding_days}D"

                gross_pnl = (exit_price - entry_price) * shares
                # Cash Delivery statutory costs (~0.25% turnover + slippage 0.1% + DP Rs 15.93)
                turnover = actual_cost + (shares * exit_price)
                charges = (turnover * 0.0025) + 15.93
                net_pnl = gross_pnl - charges
                ret_pct = ((exit_price - entry_price) / entry_price) * 100.0

                trades.append({
                    "symbol": sym,
                    "signal_date": str(sub["date"].iloc[i].date()),
                    "entry_date": str(entry_date.date()),
                    "entry_price": entry_price,
                    "shares": shares,
                    "cost": actual_cost,
                    "exit_date": str(exit_date.date()),
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "hold_days": hold_days,
                    "return_pct": ret_pct,
                    "gross_pnl": gross_pnl,
                    "charges": charges,
                    "net_pnl": net_pnl,
                })

                # Jump to exit day to avoid overlapping same-stock entries
                i = k
            i += 1

    trades_df = pd.DataFrame(trades)
    return trades_df


def print_performance_summary(trades_df: pd.DataFrame) -> None:
    if trades_df.empty:
        print("No trades triggered.")
        return

    total_trades = len(trades_df)
    target_hits = len(trades_df[trades_df["exit_reason"].isin(["TARGET_HIT", "TARGET_GAP"])])
    sl_hits = len(trades_df[trades_df["exit_reason"].isin(["SL_HIT", "SL_GAP"])])
    time_exits = len(trades_df[trades_df["exit_reason"] == "TIME_EXIT_45D"])

    win_trades = trades_df[trades_df["net_pnl"] > 0]
    loss_trades = trades_df[trades_df["net_pnl"] <= 0]
    win_rate = (len(win_trades) / total_trades) * 100.0

    total_gross = trades_df["gross_pnl"].sum()
    total_charges = trades_df["charges"].sum()
    total_net = trades_df["net_pnl"].sum()

    gross_profit = win_trades["gross_pnl"].sum()
    gross_loss = abs(loss_trades["gross_pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan

    avg_win = win_trades["net_pnl"].mean() if len(win_trades) else 0.0
    avg_loss = loss_trades["net_pnl"].mean() if len(loss_trades) else 0.0
    avg_hold = trades_df["hold_days"].mean()

    # Drawdown calculation
    trades_df["cum_pnl"] = trades_df["net_pnl"].cumsum()
    trades_df["peak"] = trades_df["cum_pnl"].cummax()
    trades_df["drawdown"] = trades_df["cum_pnl"] - trades_df["peak"]
    max_dd = trades_df["drawdown"].min()

    print("\n" + "=" * 80)
    print("  🏆 MTF TECHNO-FUNDA CASH SWING BACKTEST RESULTS (2-YEAR SIMULATION)")
    print("=" * 80)
    print(f"  Total Trades Taken:        {total_trades}")
    if target_hits > 0 or sl_hits > 0:
        print(f"  Target Hits:               {target_hits} ({target_hits/total_trades*100:.1f}%)")
        print(f"  Stop Loss Hits:            {sl_hits} ({sl_hits/total_trades*100:.1f}%)")
    print(f"  Time Exits:                {time_exits} ({time_exits/total_trades*100:.1f}%)")
    print(f"  Win Rate (Net):            {win_rate:.1f}% ({len(win_trades)} wins / {len(loss_trades)} losses)")
    print("-" * 80)
    print(f"  Total Gross PnL:           Rs {total_gross:+,.2f}")
    print(f"  Total Statutory Charges:   Rs {total_charges:,.2f}")
    print(f"  Net Realized PnL:          Rs {total_net:+,.2f}")
    print(f"  Profit Factor:             {profit_factor:.2f}")
    print(f"  Average Winning Trade:     Rs {avg_win:+,.2f} (+{win_trades['return_pct'].mean():.2f}%)")
    print(f"  Average Losing Trade:      Rs {avg_loss:+,.2f} ({loss_trades['return_pct'].mean():.2f}%)")
    print(f"  Average Holding Time:      {avg_hold:.1f} trading days (~{avg_hold/5:.1f} weeks)")
    print(f"  Maximum Drawdown:          Rs {max_dd:,.2f}")
    if max_dd != 0:
        print(f"  Return / Max Drawdown:     {abs(total_net/max_dd):.2f}x")
    print("=" * 80 + "\n")

    # Sample winning and losing trades
    print("Top 5 Winning Trades:")
    print(trades_df.sort_values("net_pnl", ascending=False)[["symbol", "entry_date", "entry_price", "exit_date", "exit_price", "exit_reason", "return_pct", "net_pnl"]].head(5).to_string(index=False))
    print("\nTop 5 Losing Trades:")
    print(trades_df.sort_values("net_pnl", ascending=True)[["symbol", "entry_date", "entry_price", "exit_date", "exit_price", "exit_reason", "return_pct", "net_pnl"]].head(5).to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtester for MTF Techno-Funda Cash Buying")
    parser.add_argument("--time-only", action="store_true", help="Remove SL & Target, strictly exit on holding days")
    parser.add_argument("--hold-days", type=int, default=45, help="Holding duration in trading days (default 45)")
    parser.add_argument("--sl-pct", type=float, default=5.0, help="Stop Loss percentage (default 5.0)")
    parser.add_argument("--target-pct", type=float, default=10.0, help="Target percentage (default 10.0)")
    parser.add_argument("--capital", type=float, default=100000.0, help="Capital per trade (default 100000)")
    parser.add_argument("--output", type=str, default="data/techno_funda_cash_backtest_trades.csv", help="CSV output path")
    args = parser.parse_args()

    sl = None if args.time_only else (args.sl_pct / 100.0)
    tgt = None if args.time_only else (args.target_pct / 100.0)

    df = run_techno_funda_cash_backtest(
        sl_pct=sl,
        target_pct=tgt,
        allocation_per_trade=args.capital,
        max_holding_days=args.hold_days,
    )
    print_performance_summary(df)
    df.to_csv(args.output, index=False)
    print(f"\nDetailed trade log saved to: {args.output}")
