"""
Comprehensive Backtest of Dhan's Intraday Commodity Option Selling Strategy for MCX Silver Mini (SILVERM).

Strategy Mechanics:
1. Timeframe: 5-minute candles on MCX Silver Mini futures.
2. Indicators:
   - Williams %R (period = 70)
   - Triple EMA: 5 EMA, 15 EMA, 50 EMA
3. Entry Rules:
   - Bullish Momentum: Williams %R(70) > -20 (Overbought) AND 5 EMA > 15 EMA > 50 EMA AND current High > previous High
     -> Sell 0.30-0.40 Delta OTM Put Option.
   - Bearish Momentum: Williams %R(70) < -80 (Oversold) AND 5 EMA < 15 EMA < 50 EMA AND current Low < previous Low
     -> Sell 0.30-0.40 Delta OTM Call Option.
4. Exit Rules:
   - Stop Loss: Super Order at 1.5x entry premium (50% loss of credit).
   - Trend Reversal: 5 EMA crosses 50 EMA in opposite direction AND price breaks crossover extreme.
   - Intraday EOD: Flat by 23:15 IST (no overnight positions).
5. Exact MCX transaction costs & ₹400/trade slippage buffer.
"""

import os
import sys
import math
from datetime import datetime, time
import numpy as np
import pandas as pd
from scipy.stats import norm

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategy.options_model import black_76_price
from strategy.silver_options.backtest import calculate_mcx_costs


def compute_williams_r(df: pd.DataFrame, period: int = 70) -> pd.Series:
    """Computes Williams %R over given lookback period."""
    highest_high = df["high"].rolling(period).max()
    lowest_low = df["low"].rolling(period).min()
    wr = ((highest_high - df["close"]) / (highest_high - lowest_low).replace(0, np.nan)) * -100.0
    return wr.fillna(-50.0)


def find_target_delta_strike(
    spot: float,
    opt_type: str,
    target_delta: float = 0.35,
    dte_days: float = 20.0,
    iv: float = 0.32,
    r: float = 0.065,
    strike_step: float = 500.0,
) -> tuple[float, float, float]:
    """
    Finds the strike closest to target_delta and returns (strike, delta, premium).
    """
    t = max(dte_days / 252.0, 1e-4)
    sigma = max(iv, 0.05)
    d = math.exp(-r * t)
    sqrt_t = math.sqrt(t)

    # Search strikes within +/- 8% of spot
    min_k = round((spot * 0.90) / strike_step) * strike_step
    max_k = round((spot * 1.10) / strike_step) * strike_step

    best_strike = spot
    best_diff = 999.0
    best_delta = 0.0
    best_price = 0.0

    current_k = min_k
    while current_k <= max_k:
        if current_k <= 0:
            current_k += strike_step
            continue

        denom = sigma * sqrt_t
        d1 = (math.log(spot / current_k) + 0.5 * sigma * sigma * t) / denom
        d2 = d1 - denom

        if opt_type == "CE":
            delta = d * norm.cdf(d1)
            price = d * (spot * norm.cdf(d1) - current_k * norm.cdf(d2))
        else:
            delta = -d * norm.cdf(-d1)
            price = d * (current_k * norm.cdf(-d2) - spot * norm.cdf(-d1))

        price = max(1.0, float(price))
        abs_delta = abs(delta)
        diff = abs(abs_delta - target_delta)

        if diff < best_diff:
            best_diff = diff
            best_strike = current_k
            best_delta = delta
            best_price = price

        current_k += strike_step

    return best_strike, best_delta, best_price


def simulate_williams_ema_strategy(
    df: pd.DataFrame,
    sl_multiplier: float = 1.5,
    target_delta: float = 0.35,
    session_filter: str = "all",  # "all", "morning_afternoon" (09:00-18:00), "evening" (18:00-23:15)
    require_crossover_confirmation: bool = True,
    lot_size: int = 5,
    slippage_per_leg: float = 200.0,  # ₹200 entry + ₹200 exit = ₹400 per trade
) -> pd.DataFrame:
    """
    Simulates the Dhan Williams %R + Triple EMA intraday strategy bar-by-bar.
    """
    df = df.copy()
    df["wr70"] = compute_williams_r(df, period=70)
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema15"] = df["close"].ewm(span=15, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # Realized rolling volatility for IV approximation
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["iv"] = (df["log_ret"].rolling(288).std() * np.sqrt(252 * 180)).clip(lower=0.20, upper=0.55).fillna(0.32)

    trades = []
    in_trade = False
    active_trade = {}

    # Iterate through bars
    for i in range(71, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]
        curr_dt = curr["date"]
        curr_time = curr_dt.time()
        curr_spot = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_iv = float(curr["iv"])

        # Check if in active trade
        if in_trade:
            # Calculate elapsed time in trading days (180 5m bars per 15-hr day)
            bars_elapsed = i - active_trade["entry_bar_idx"]
            elapsed_days = bars_elapsed / 180.0
            remaining_dte = max(active_trade["entry_dte"] - elapsed_days, 1e-4)

            opt_type = active_trade["opt_type"]
            strike = active_trade["strike"]

            # Current option theoretical price
            curr_opt_price = black_76_price(
                spot=curr_spot,
                strike=strike,
                dte_days=remaining_dte,
                iv=curr_iv,
                r=0.065,
                opt_type=opt_type,
            )

            # Intra-bar peak option price to test stop loss
            if opt_type == "CE":
                worst_spot = curr_high
            else:
                worst_spot = curr_low

            worst_opt_price = black_76_price(
                spot=worst_spot,
                strike=strike,
                dte_days=remaining_dte,
                iv=curr_iv,
                r=0.065,
                opt_type=opt_type,
            )

            exit_triggered = False
            exit_reason = ""
            exit_price = curr_opt_price

            # 1. Stop Loss Check
            sl_price = active_trade["entry_premium"] * sl_multiplier
            if worst_opt_price >= sl_price:
                exit_triggered = True
                exit_reason = f"STOP_LOSS ({sl_multiplier}x)"
                exit_price = max(sl_price, curr_opt_price)

            # 2. Intraday EOD Square-off Check (23:15 IST)
            elif curr_time >= time(23, 15):
                exit_triggered = True
                exit_reason = "INTRADAY_EOD (23:15)"
                exit_price = curr_opt_price

            # 3. Trend Reversal Check
            else:
                if opt_type == "PE":
                    # Bullish trade exit: 5 EMA crosses below 50 EMA
                    crossover = curr["ema5"] < curr["ema50"] and prev["ema5"] >= prev["ema50"]
                    if crossover:
                        if not require_crossover_confirmation or curr_low < prev["low"]:
                            exit_triggered = True
                            exit_reason = "TREND_REVERSAL (5 EMA < 50 EMA)"
                            exit_price = curr_opt_price
                else:
                    # Bearish trade exit: 5 EMA crosses above 50 EMA
                    crossover = curr["ema5"] > curr["ema50"] and prev["ema5"] <= prev["ema50"]
                    if crossover:
                        if not require_crossover_confirmation or curr_high > prev["high"]:
                            exit_triggered = True
                            exit_reason = "TREND_REVERSAL (5 EMA > 50 EMA)"
                            exit_price = curr_opt_price

            if exit_triggered:
                # Close trade
                entry_premium = active_trade["entry_premium"]
                points = entry_premium - exit_price
                gross_pnl = points * lot_size

                # Transaction costs
                entry_turnover = entry_premium * lot_size
                exit_turnover = exit_price * lot_size
                turnover = entry_turnover + exit_turnover
                mcx_costs = calculate_mcx_costs(turnover, legs=1)
                total_slippage = slippage_per_leg * 2  # entry + exit
                net_pnl = gross_pnl - mcx_costs - total_slippage

                trades.append(
                    {
                        "entry_time": active_trade["entry_time"],
                        "exit_time": curr_dt,
                        "direction": active_trade["direction"],
                        "opt_type": opt_type,
                        "strike": strike,
                        "delta": active_trade["delta"],
                        "entry_spot": active_trade["entry_spot"],
                        "exit_spot": curr_spot,
                        "entry_premium": round(entry_premium, 2),
                        "exit_price": round(exit_price, 2),
                        "points": round(points, 2),
                        "gross_pnl": round(gross_pnl, 2),
                        "mcx_costs": round(mcx_costs, 2),
                        "slippage": round(total_slippage, 2),
                        "net_pnl": round(net_pnl, 2),
                        "exit_reason": exit_reason,
                        "bars_held": bars_elapsed,
                        "duration_hours": round(bars_elapsed * 5 / 60.0, 2),
                    }
                )
                in_trade = False
                active_trade = {}
                continue

        # If flat, check for entry signals
        if not in_trade:
            # Session filter
            if curr_time < time(9, 15) or curr_time > time(21, 30):
                continue

            if session_filter == "morning_afternoon" and curr_time >= time(18, 0):
                continue
            elif session_filter == "evening" and curr_time < time(18, 0):
                continue

            # Bullish Entry: WR > -20, 5 > 15 > 50 EMA, high > prev_high
            is_bullish = (
                curr["wr70"] > -20.0
                and curr["ema5"] > curr["ema15"]
                and curr["ema15"] > curr["ema50"]
                and curr_high > prev["high"]
            )

            # Bearish Entry: WR < -80, 5 < 15 < 50 EMA, low < prev_low
            is_bearish = (
                curr["wr70"] < -80.0
                and curr["ema5"] < curr["ema15"]
                and curr["ema15"] < curr["ema50"]
                and curr_low < prev["low"]
            )

            if is_bullish and not is_bearish:
                # Sell 0.35 Delta OTM Put
                strike, delta, premium = find_target_delta_strike(
                    spot=curr_spot,
                    opt_type="PE",
                    target_delta=target_delta,
                    dte_days=20.0,
                    iv=curr_iv,
                )
                in_trade = True
                active_trade = {
                    "entry_bar_idx": i,
                    "entry_time": curr_dt,
                    "direction": "BULLISH (SHORT PUT)",
                    "opt_type": "PE",
                    "strike": strike,
                    "delta": delta,
                    "entry_spot": curr_spot,
                    "entry_premium": premium,
                    "entry_dte": 20.0,
                }

            elif is_bearish and not is_bullish:
                # Sell 0.35 Delta OTM Call
                strike, delta, premium = find_target_delta_strike(
                    spot=curr_spot,
                    opt_type="CE",
                    target_delta=target_delta,
                    dte_days=20.0,
                    iv=curr_iv,
                )
                in_trade = True
                active_trade = {
                    "entry_bar_idx": i,
                    "entry_time": curr_dt,
                    "direction": "BEARISH (SHORT CALL)",
                    "opt_type": "CE",
                    "strike": strike,
                    "delta": delta,
                    "entry_spot": curr_spot,
                    "entry_premium": premium,
                    "entry_dte": 20.0,
                }

    return pd.DataFrame(trades)


def evaluate_performance(trades_df: pd.DataFrame, initial_margin: float = 175000.0, safe_capital: float = 250000.0) -> dict:
    """Computes comprehensive performance statistics for trade set."""
    if trades_df.empty:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "total_frictions": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "roi_margin_pct": 0.0,
            "roi_capital_pct": 0.0,
            "avg_trade_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "win_loss_ratio": 0.0,
        }

    total_trades = len(trades_df)
    winning_trades = trades_df[trades_df["net_pnl"] > 0]
    losing_trades = trades_df[trades_df["net_pnl"] <= 0]

    win_count = len(winning_trades)
    win_rate = (win_count / total_trades) * 100.0

    gross_pnl = float(trades_df["gross_pnl"].sum())
    total_frictions = float(trades_df["mcx_costs"].sum() + trades_df["slippage"].sum())
    net_pnl = float(trades_df["net_pnl"].sum())

    total_wins = float(winning_trades["net_pnl"].sum())
    total_losses = abs(float(losing_trades["net_pnl"].sum()))
    profit_factor = (total_wins / total_losses) if total_losses > 0 else float("inf")

    # Cumulative drawdown
    cum_pnl = trades_df["net_pnl"].cumsum()
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(drawdowns.max())
    max_dd_pct = (max_dd / safe_capital) * 100.0

    avg_trade = net_pnl / total_trades
    avg_win = (total_wins / win_count) if win_count > 0 else 0.0
    avg_loss = (total_losses / len(losing_trades)) if len(losing_trades) > 0 else 0.0
    win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else float("inf")

    roi_margin = (net_pnl / initial_margin) * 100.0
    roi_capital = (net_pnl / safe_capital) * 100.0

    return {
        "total_trades": total_trades,
        "win_count": win_count,
        "loss_count": len(losing_trades),
        "win_rate": round(win_rate, 2),
        "gross_pnl": round(gross_pnl, 2),
        "total_frictions": round(total_frictions, 2),
        "net_pnl": round(net_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "roi_margin_pct": round(roi_margin, 2),
        "roi_capital_pct": round(roi_capital, 2),
        "avg_trade_pnl": round(avg_trade, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(win_loss_ratio, 2),
    }


def main():
    parquet_path = "data/silverm_5m_intraday.parquet"
    if not os.path.exists(parquet_path):
        print(f"Error: {parquet_path} not found.")
        sys.exit(1)

    print(f"Loading 5-minute data from {parquet_path}...")
    df = pd.read_parquet(parquet_path)
    print(f"Loaded {len(df)} bars from {df['date'].min()} to {df['date'].max()}.")

    # 1. Benchmark: Dhan Default Setup (SL = 1.5x, Delta = 0.35, All Sessions)
    print("\n" + "=" * 80)
    print("RUNNING BENCHMARK SETUP: Dhan Default (SL=1.5x, Target Delta=0.35, All Sessions)")
    print("=" * 80)
    trades_base = simulate_williams_ema_strategy(
        df,
        sl_multiplier=1.5,
        target_delta=0.35,
        session_filter="all",
        require_crossover_confirmation=True,
    )
    metrics_base = evaluate_performance(trades_base)
    print(f"Total Trades: {metrics_base['total_trades']}")
    print(f"Win Rate:     {metrics_base['win_rate']}% ({metrics_base['win_count']} wins / {metrics_base['loss_count']} losses)")
    print(f"Win/Loss Ratio: {metrics_base['win_loss_ratio']} (Avg Win: ₹{metrics_base['avg_win']}, Avg Loss: ₹{metrics_base['avg_loss']})")
    print(f"Gross PnL:    ₹{metrics_base['gross_pnl']:,.2f}")
    print(f"Total Costs:  ₹{metrics_base['total_frictions']:,.2f} (Brokerage + CTT + MCX Fee + Slippage)")
    print(f"Net PnL:      ₹{metrics_base['net_pnl']:,.2f}")
    print(f"Profit Factor:{metrics_base['profit_factor']}")
    print(f"Max Drawdown: ₹{metrics_base['max_drawdown']:,.2f} ({metrics_base['max_drawdown_pct']}%)")
    print(f"ROI on Margin (₹1.75L): {metrics_base['roi_margin_pct']}%")
    print(f"ROI on Capital (₹2.5L): {metrics_base['roi_capital_pct']}%")

    # Save detailed CSV
    trades_base.to_csv("data/silver_williams_ema_intraday_trades.csv", index=False)
    print("Saved detailed trade log to data/silver_williams_ema_intraday_trades.csv")

    # Monthly breakdown for Benchmark
    trades_base["month"] = pd.to_datetime(trades_base["entry_time"]).dt.to_period("M")
    monthly = trades_base.groupby("month").agg(
        trades=("net_pnl", "count"),
        wins=("net_pnl", lambda x: (x > 0).sum()),
        net_pnl=("net_pnl", "sum"),
        gross_pnl=("gross_pnl", "sum"),
        costs=("mcx_costs", "sum"),
        slippage=("slippage", "sum"),
    )
    monthly["win_rate"] = (monthly["wins"] / monthly["trades"] * 100.0).round(1)
    monthly["net_pnl"] = monthly["net_pnl"].round(2)
    print("\nMONTHLY BREAKDOWN:")
    print(monthly[["trades", "win_rate", "gross_pnl", "net_pnl"]])

    # 2. Parameter Sensitivity: Stop Loss Multipliers
    print("\n" + "=" * 80)
    print("PARAMETER SENSITIVITY: STOP LOSS MULTIPLIER (Delta=0.35)")
    print("=" * 80)
    for sl in [1.3, 1.5, 1.8, 2.0, 2.5]:
        t_df = simulate_williams_ema_strategy(df, sl_multiplier=sl, target_delta=0.35, session_filter="all")
        m = evaluate_performance(t_df)
        print(f"SL {sl}x -> Trades: {m['total_trades']:3d} | Win Rate: {m['win_rate']:5.1f}% | Net PnL: ₹{m['net_pnl']:11,.2f} | PF: {m['profit_factor']:4.2f} | Max DD: ₹{m['max_drawdown']:9,.2f} | ROI: {m['roi_margin_pct']:6.1f}%")

    # 3. Session Timing Sensitivity: Calm Theta Hours (09:00-18:00) vs Evening Volatile Hours (18:00-23:15)
    print("\n" + "=" * 80)
    print("SESSION SENSITIVITY: CALM THETA (09:00-18:00) vs EVENING VOLATILITY (18:00-23:15)")
    print("=" * 80)
    for sess in ["all", "morning_afternoon", "evening"]:
        t_df = simulate_williams_ema_strategy(df, sl_multiplier=1.5, target_delta=0.35, session_filter=sess)
        m = evaluate_performance(t_df)
        label = "Full Day (09:00 - 23:15)" if sess == "all" else ("Calm Theta (09:00 - 18:00)" if sess == "morning_afternoon" else "Evening Vol (18:00 - 23:15)")
        print(f"{label:26s} -> Trades: {m['total_trades']:3d} | Win Rate: {m['win_rate']:5.1f}% | Net PnL: ₹{m['net_pnl']:11,.2f} | PF: {m['profit_factor']:4.2f} | Max DD: ₹{m['max_drawdown']:9,.2f} | ROI: {m['roi_margin_pct']:6.1f}%")

    # 4. Delta Sensitivity: 0.30 vs 0.35 vs 0.40
    print("\n" + "=" * 80)
    print("DELTA SENSITIVITY: 0.30 vs 0.35 vs 0.40")
    print("=" * 80)
    for delta in [0.25, 0.30, 0.35, 0.40]:
        t_df = simulate_williams_ema_strategy(df, sl_multiplier=1.5, target_delta=delta, session_filter="all")
        m = evaluate_performance(t_df)
        print(f"Delta {delta:.2f} -> Trades: {m['total_trades']:3d} | Win Rate: {m['win_rate']:5.1f}% | Net PnL: ₹{m['net_pnl']:11,.2f} | PF: {m['profit_factor']:4.2f} | Max DD: ₹{m['max_drawdown']:9,.2f} | ROI: {m['roi_margin_pct']:6.1f}%")


if __name__ == "__main__":
    main()
