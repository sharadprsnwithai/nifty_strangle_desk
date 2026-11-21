"""
Comprehensive Backtest of MCX Silver Mini Options Desk with Hybrid Stop Loss & Daily Max Loss Circuit Breaker.

Simulates 4 configurations side-by-side:
1. Baseline: 1 Lot (5kg), Pure 1.5x SL, No Daily Cap
2. Hybrid SL: 1 Lot (5kg), Cap ₹10k per lot, No Daily Cap
3. Hybrid SL + Daily Circuit Breaker: 1 Lot (5kg), Cap ₹10k, Daily Cap ₹15k
4. Production Configuration: 2 Lots (10kg), Hybrid SL (₹20k max trade loss), Daily Circuit Breaker (₹30k cap)

Evaluates on:
- 5-minute Intraday Dataset (45,608 candles, bar-by-bar path & intra-candle extremes)
- 3-Year Historical Dataset (772 trading days, 2023 - 2026 multi-year robustness)
"""

import os
import sys
import math
from datetime import datetime, time
import numpy as np
import pandas as pd
from scipy.stats import norm

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategy.options_model import black_76_price
from strategy.silver_options.backtest import calculate_mcx_costs


def compute_williams_r(df: pd.DataFrame, period: int = 70) -> pd.Series:
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
    t = max(dte_days / 252.0, 1e-4)
    sigma = max(iv, 0.05)
    d = math.exp(-r * t)
    sqrt_t = math.sqrt(t)

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
        diff = abs(abs(delta) - target_delta)

        if diff < best_diff:
            best_diff = diff
            best_strike = current_k
            best_delta = delta
            best_price = price

        current_k += strike_step

    return best_strike, best_delta, best_price


def run_5m_simulation(
    df: pd.DataFrame,
    lots: int = 2,
    lot_size: int = 5,
    use_hybrid_sl: bool = True,
    sl_multiplier: float = 1.5,
    sl_per_lot: float = 10000.0,
    use_daily_circuit_breaker: bool = True,
    daily_max_loss_per_lot: float = 15000.0,
    session_start: time = time(9, 15),
    session_end: time = time(18, 0),
    slippage_per_lot_per_leg: float = 200.0,
) -> pd.DataFrame:
    total_qty = lots * lot_size
    daily_max_loss = daily_max_loss_per_lot * lots

    df = df.copy()
    df["wr70"] = compute_williams_r(df, period=70)
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema15"] = df["close"].ewm(span=15, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["iv"] = (df["log_ret"].rolling(288).std() * np.sqrt(252 * 180)).clip(lower=0.20, upper=0.55).fillna(0.32)

    trades = []
    in_trade = False
    active_trade = {}

    current_trading_date = None
    today_realized_pnl = 0.0
    trading_halted_for_day = False

    for i in range(71, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]
        curr_dt = curr["date"]
        bar_date = curr_dt.date()
        curr_time = curr_dt.time()
        curr_spot = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_iv = float(curr["iv"])

        # Reset daily tracking on new calendar day
        if current_trading_date != bar_date:
            current_trading_date = bar_date
            today_realized_pnl = 0.0
            trading_halted_for_day = False

        if in_trade:
            bars_elapsed = i - active_trade["entry_bar_idx"]
            elapsed_days = bars_elapsed / 180.0
            remaining_dte = max(active_trade["entry_dte"] - elapsed_days, 1e-4)

            opt_type = active_trade["opt_type"]
            strike = active_trade["strike"]

            curr_opt_price = black_76_price(
                spot=curr_spot,
                strike=strike,
                dte_days=remaining_dte,
                iv=curr_iv,
                r=0.065,
                opt_type=opt_type,
            )

            worst_spot = curr_high if opt_type == "CE" else curr_low
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

            # Check Stop Loss
            sl_price = active_trade["sl_price"]
            if worst_opt_price >= sl_price:
                exit_triggered = True
                exit_reason = "STOP_LOSS"
                exit_price = max(sl_price, curr_opt_price)

            # Check Session End Cutoff (18:00)
            elif curr_time >= session_end:
                exit_triggered = True
                exit_reason = "SESSION_CLOSE"
                exit_price = curr_opt_price

            # Check Trend Reversal
            else:
                if opt_type == "PE":
                    crossover = curr["ema5"] < curr["ema50"] and prev["ema5"] >= prev["ema50"]
                    if crossover and curr_low < prev["low"]:
                        exit_triggered = True
                        exit_reason = "TREND_REVERSAL"
                        exit_price = curr_opt_price
                else:
                    crossover = curr["ema5"] > curr["ema50"] and prev["ema5"] <= prev["ema50"]
                    if crossover and curr_high > prev["high"]:
                        exit_triggered = True
                        exit_reason = "TREND_REVERSAL"
                        exit_price = curr_opt_price

            # Check Daily Max Loss on open unrealized drawdown
            if not exit_triggered and use_daily_circuit_breaker:
                unrealized_pnl = (active_trade["entry_premium"] - curr_opt_price) * total_qty
                if today_realized_pnl + unrealized_pnl <= -daily_max_loss:
                    exit_triggered = True
                    exit_reason = "DAILY_MAX_LOSS_LIMIT"
                    exit_price = curr_opt_price

            if exit_triggered:
                entry_premium = active_trade["entry_premium"]
                points = entry_premium - exit_price
                gross_pnl = points * total_qty

                turnover = (entry_premium + exit_price) * total_qty
                mcx_costs = calculate_mcx_costs(turnover, legs=1)
                total_slippage = slippage_per_lot_per_leg * 2 * lots
                net_pnl = gross_pnl - mcx_costs - total_slippage

                trades.append(
                    {
                        "date": bar_date,
                        "entry_time": active_trade["entry_time"],
                        "exit_time": curr_dt,
                        "direction": active_trade["direction"],
                        "opt_type": opt_type,
                        "strike": strike,
                        "delta": active_trade["delta"],
                        "entry_premium": round(entry_premium, 2),
                        "exit_price": round(exit_price, 2),
                        "sl_price": round(sl_price, 2),
                        "gross_pnl": round(gross_pnl, 2),
                        "mcx_costs": round(mcx_costs, 2),
                        "slippage": round(total_slippage, 2),
                        "net_pnl": round(net_pnl, 2),
                        "exit_reason": exit_reason,
                        "bars_held": bars_elapsed,
                    }
                )

                today_realized_pnl += net_pnl
                if use_daily_circuit_breaker and today_realized_pnl <= -daily_max_loss:
                    trading_halted_for_day = True

                in_trade = False
                active_trade = {}
                continue

        if not in_trade:
            if trading_halted_for_day:
                continue

            if curr_time < session_start or curr_time >= session_end:
                continue

            is_bullish = (
                curr["wr70"] > -20.0
                and curr["ema5"] > curr["ema15"]
                and curr["ema15"] > curr["ema50"]
                and curr_high > prev["high"]
            )

            is_bearish = (
                curr["wr70"] < -80.0
                and curr["ema5"] < curr["ema15"]
                and curr["ema15"] < curr["ema50"]
                and curr_low < prev["low"]
            )

            if is_bullish and not is_bearish:
                strike, delta, premium = find_target_delta_strike(
                    spot=curr_spot, opt_type="PE", target_delta=0.35, dte_days=20.0, iv=curr_iv
                )
                if use_hybrid_sl:
                    allowed_pts = min(premium * 0.5, sl_per_lot / lot_size)
                    sl_p = round(premium + allowed_pts, 2)
                else:
                    sl_p = round(premium * sl_multiplier, 2)

                in_trade = True
                active_trade = {
                    "entry_bar_idx": i,
                    "entry_time": curr_dt,
                    "direction": "BULLISH (SHORT PUT)",
                    "opt_type": "PE",
                    "strike": strike,
                    "delta": delta,
                    "entry_premium": premium,
                    "sl_price": sl_p,
                    "entry_dte": 20.0,
                }

            elif is_bearish and not is_bullish:
                strike, delta, premium = find_target_delta_strike(
                    spot=curr_spot, opt_type="CE", target_delta=0.35, dte_days=20.0, iv=curr_iv
                )
                if use_hybrid_sl:
                    allowed_pts = min(premium * 0.5, sl_per_lot / lot_size)
                    sl_p = round(premium + allowed_pts, 2)
                else:
                    sl_p = round(premium * sl_multiplier, 2)

                in_trade = True
                active_trade = {
                    "entry_bar_idx": i,
                    "entry_time": curr_dt,
                    "direction": "BEARISH (SHORT CALL)",
                    "opt_type": "CE",
                    "strike": strike,
                    "delta": delta,
                    "entry_premium": premium,
                    "sl_price": sl_p,
                    "entry_dte": 20.0,
                }

    return pd.DataFrame(trades)


def run_3y_daily_simulation(
    df: pd.DataFrame,
    lots: int = 2,
    lot_size: int = 5,
    use_hybrid_sl: bool = True,
    sl_multiplier: float = 1.5,
    sl_per_lot: float = 10000.0,
    use_daily_circuit_breaker: bool = True,
    daily_max_loss_per_lot: float = 15000.0,
    slippage_per_lot_per_leg: float = 200.0,
) -> pd.DataFrame:
    total_qty = lots * lot_size
    daily_max_loss = daily_max_loss_per_lot * lots

    df = df.copy()
    highest_high = df["high"].rolling(70).max()
    lowest_low = df["low"].rolling(70).min()
    df["wr70"] = ((highest_high - df["close"]) / (highest_high - lowest_low).replace(0, np.nan)) * -100.0
    df["wr70"] = df["wr70"].fillna(-50.0)
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema15"] = df["close"].ewm(span=15, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["iv"] = (df["log_ret"].rolling(20).std() * np.sqrt(252)).clip(lower=0.20, upper=0.55).fillna(0.32)

    trades = []

    for i in range(71, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]
        curr_date = curr["date"]
        spot_open = float(curr["open"])
        spot_close = float(curr["close"])
        high = float(curr["high"])
        low = float(curr["low"])
        iv = float(curr["iv"])

        bull = (
            curr["wr70"] > -20.0
            and curr["ema5"] > curr["ema15"]
            and curr["ema15"] > curr["ema50"]
            and high > prev["high"]
        )
        bear = (
            curr["wr70"] < -80.0
            and curr["ema5"] < curr["ema15"]
            and curr["ema15"] < curr["ema50"]
            and low < prev["low"]
        )

        if bull and not bear:
            strike = round((spot_open * 0.975) / 1000.0) * 1000.0
            entry_prem = black_76_price(spot_open, strike, 20.0, iv=iv, r=0.065, opt_type="PE")
            worst_prem = black_76_price(low, strike, 19.5, iv=iv, r=0.065, opt_type="PE")

            if use_hybrid_sl:
                allowed_pts = min(entry_prem * 0.5, sl_per_lot / lot_size)
                sl_prem = entry_prem + allowed_pts
            else:
                sl_prem = entry_prem * sl_multiplier

            if worst_prem >= sl_prem:
                exit_prem = sl_prem
                reason = "STOP_LOSS"
            else:
                exit_prem = black_76_price(spot_close, strike, 19.0, iv=iv, r=0.065, opt_type="PE")
                reason = "SESSION_CLOSE"

            gross = (entry_prem - exit_prem) * total_qty
            turnover = (entry_prem + exit_prem) * total_qty
            costs = calculate_mcx_costs(turnover, legs=1)
            slippage = slippage_per_lot_per_leg * 2 * lots
            net = gross - costs - slippage

            if use_daily_circuit_breaker and net <= -daily_max_loss:
                net = -daily_max_loss
                reason = "DAILY_MAX_LOSS_LIMIT"

            trades.append(
                {
                    "date": curr_date,
                    "type": "SHORT_PUT",
                    "entry": round(entry_prem, 2),
                    "exit": round(exit_prem, 2),
                    "sl_price": round(sl_prem, 2),
                    "gross_pnl": round(gross, 2),
                    "costs": round(costs, 2),
                    "slippage": round(slippage, 2),
                    "net_pnl": round(net, 2),
                    "reason": reason,
                }
            )

        elif bear and not bull:
            strike = round((spot_open * 1.025) / 1000.0) * 1000.0
            entry_prem = black_76_price(spot_open, strike, 20.0, iv=iv, r=0.065, opt_type="CE")
            worst_prem = black_76_price(high, strike, 19.5, iv=iv, r=0.065, opt_type="CE")

            if use_hybrid_sl:
                allowed_pts = min(entry_prem * 0.5, sl_per_lot / lot_size)
                sl_prem = entry_prem + allowed_pts
            else:
                sl_prem = entry_prem * sl_multiplier

            if worst_prem >= sl_prem:
                exit_prem = sl_prem
                reason = "STOP_LOSS"
            else:
                exit_prem = black_76_price(spot_close, strike, 19.0, iv=iv, r=0.065, opt_type="CE")
                reason = "SESSION_CLOSE"

            gross = (entry_prem - exit_prem) * total_qty
            turnover = (entry_prem + exit_prem) * total_qty
            costs = calculate_mcx_costs(turnover, legs=1)
            slippage = slippage_per_lot_per_leg * 2 * lots
            net = gross - costs - slippage

            if use_daily_circuit_breaker and net <= -daily_max_loss:
                net = -daily_max_loss
                reason = "DAILY_MAX_LOSS_LIMIT"

            trades.append(
                {
                    "date": curr_date,
                    "type": "SHORT_CALL",
                    "entry": round(entry_prem, 2),
                    "exit": round(exit_prem, 2),
                    "sl_price": round(sl_prem, 2),
                    "gross_pnl": round(gross, 2),
                    "costs": round(costs, 2),
                    "slippage": round(slippage, 2),
                    "net_pnl": round(net, 2),
                    "reason": reason,
                }
            )

    return pd.DataFrame(trades)


def compute_metrics(trades_df: pd.DataFrame, initial_margin: float, safe_capital: float, trading_days: int = 700) -> dict:
    if trades_df.empty:
        return {}

    total_trades = len(trades_df)
    winning = trades_df[trades_df["net_pnl"] > 0]
    losing = trades_df[trades_df["net_pnl"] <= 0]

    win_count = len(winning)
    loss_count = len(losing)
    win_rate = (win_count / total_trades) * 100.0

    gross_pnl = float(trades_df["gross_pnl"].sum())
    costs = float((trades_df["costs"] if "costs" in trades_df.columns else trades_df["mcx_costs"]).sum())
    slippage = float(trades_df["slippage"].sum())
    net_pnl = float(trades_df["net_pnl"].sum())

    total_wins = float(winning["net_pnl"].sum())
    total_losses = abs(float(losing["net_pnl"].sum()))
    profit_factor = (total_wins / total_losses) if total_losses > 0 else float("inf")

    avg_win = (total_wins / win_count) if win_count > 0 else 0.0
    avg_loss = (total_losses / loss_count) if loss_count > 0 else 0.0
    win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else float("inf")

    cum_pnl = trades_df["net_pnl"].cumsum()
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(drawdowns.max())
    max_dd_pct = (max_dd / safe_capital) * 100.0

    # Sharpe ratio
    daily_pnl = trades_df.groupby("date")["net_pnl"].sum()
    mean_daily = daily_pnl.mean()
    std_daily = daily_pnl.std() if len(daily_pnl) > 1 else 1.0
    sharpe = (mean_daily / std_daily) * np.sqrt(252) if std_daily > 0 else 0.0

    roi_margin = (net_pnl / initial_margin) * 100.0
    roi_capital = (net_pnl / safe_capital) * 100.0

    return {
        "trades": total_trades,
        "win_rate": round(win_rate, 1),
        "win_count": win_count,
        "loss_count": loss_count,
        "gross_pnl": round(gross_pnl, 2),
        "costs": round(costs, 2),
        "slippage": round(slippage, 2),
        "net_pnl": round(net_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 2),
        "max_dd_pct": round(max_dd_pct, 1),
        "avg_trade": round(net_pnl / total_trades, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(win_loss_ratio, 2),
        "roi_margin": round(roi_margin, 1),
        "roi_capital": round(roi_capital, 1),
    }


def main():
    print("=" * 90)
    print("MCX SILVER MINI (SILVERM) OPTIONS DESK: COMPREHENSIVE BACKTEST RESULTS")
    print("Strategy: Williams %R (70) + Triple EMA (5, 15, 50) + High/Low Breakout (0.35 Delta OTM)")
    print("=" * 90)

    # 1. 3-Year Daily Simulation
    df_3y = pd.read_parquet("data/silverm_3y_daily.parquet")
    print(f"\n[1] 3-YEAR MULTI-YEAR BACKTEST (772 Trading Days: 2023-11 to 2026-11)")
    print("-" * 90)

    # Config A: 1 Lot Baseline (Pure 1.5x SL, No Daily Cap)
    t_3y_base = run_3y_daily_simulation(df_3y, lots=1, use_hybrid_sl=False, use_daily_circuit_breaker=False)
    m_3y_base = compute_metrics(t_3y_base, initial_margin=175000.0, safe_capital=250000.0)

    # Config B: 1 Lot Hybrid SL + Circuit Breaker (Cap ₹10k SL, Cap ₹15k Daily)
    t_3y_hyb1 = run_3y_daily_simulation(df_3y, lots=1, use_hybrid_sl=True, sl_per_lot=10000.0, use_daily_circuit_breaker=True, daily_max_loss_per_lot=15000.0)
    m_3y_hyb1 = compute_metrics(t_3y_hyb1, initial_margin=175000.0, safe_capital=250000.0)

    # Config C: 2 Lots Production Config (₹20k Max Trade SL, ₹30k Daily Cap)
    t_3y_hyb2 = run_3y_daily_simulation(df_3y, lots=2, use_hybrid_sl=True, sl_per_lot=10000.0, use_daily_circuit_breaker=True, daily_max_loss_per_lot=15000.0)
    m_3y_hyb2 = compute_metrics(t_3y_hyb2, initial_margin=350000.0, safe_capital=500000.0)

    print(f"{'Metric':<25} | {'1 Lot Baseline (1.5x)':<20} | {'1 Lot Hybrid + Circuit':<22} | {'2 Lots PROD DOCKER':<20}")
    print("-" * 90)
    print(f"{'Total Trades':<25} | {m_3y_base['trades']:<20} | {m_3y_hyb1['trades']:<22} | {m_3y_hyb2['trades']:<20}")
    print(f"{'Win Rate (%)':<25} | {m_3y_base['win_rate']:<20}% | {m_3y_hyb1['win_rate']:<22}% | {m_3y_hyb2['win_rate']:<20}%")
    print(f"{'Win / Loss Count':<25} | {m_3y_base['win_count']}W / {m_3y_base['loss_count']}L{' ':<10} | {m_3y_hyb1['win_count']}W / {m_3y_hyb1['loss_count']}L{' ':<11} | {m_3y_hyb2['win_count']}W / {m_3y_hyb2['loss_count']}L")
    print(f"{'Gross PnL (₹)':<25} | ₹{m_3y_base['gross_pnl']:<19,.2f} | ₹{m_3y_hyb1['gross_pnl']:<21,.2f} | ₹{m_3y_hyb2['gross_pnl']:<19,.2f}")
    print(f"{'Frictions (Costs+Slip)':<25} | ₹{(m_3y_base['costs']+m_3y_base['slippage']):<19,.2f} | ₹{(m_3y_hyb1['costs']+m_3y_hyb1['slippage']):<21,.2f} | ₹{(m_3y_hyb2['costs']+m_3y_hyb2['slippage']):<19,.2f}")
    print(f"{'Net Realized PnL (₹)':<25} | ₹{m_3y_base['net_pnl']:<19,.2f} | ₹{m_3y_hyb1['net_pnl']:<21,.2f} | ₹{m_3y_hyb2['net_pnl']:<19,.2f}")
    print(f"{'Profit Factor':<25} | {m_3y_base['profit_factor']:<20} | {m_3y_hyb1['profit_factor']:<22} | {m_3y_hyb2['profit_factor']:<20}")
    print(f"{'Sharpe Ratio (ann.)':<25} | {m_3y_base['sharpe']:<20} | {m_3y_hyb1['sharpe']:<22} | {m_3y_hyb2['sharpe']:<20}")
    print(f"{'Max Drawdown (₹)':<25} | ₹{m_3y_base['max_dd']:<19,.2f} | ₹{m_3y_hyb1['max_dd']:<21,.2f} | ₹{m_3y_hyb2['max_dd']:<19,.2f}")
    print(f"{'Max Drawdown (%)':<25} | {m_3y_base['max_dd_pct']:<20}% | {m_3y_hyb1['max_dd_pct']:<22}% | {m_3y_hyb2['max_dd_pct']:<20}%")
    print(f"{'ROI on Margin (%)':<25} | {m_3y_base['roi_margin']:<20}% | {m_3y_hyb1['roi_margin']:<22}% | {m_3y_hyb2['roi_margin']:<20}%")
    print(f"{'ROI on Capital (%)':<25} | {m_3y_base['roi_capital']:<20}% | {m_3y_hyb1['roi_capital']:<22}% | {m_3y_hyb2['roi_capital']:<20}%")
    print(f"{'Avg Trade PnL (₹)':<25} | ₹{m_3y_base['avg_trade']:<19,.2f} | ₹{m_3y_hyb1['avg_trade']:<21,.2f} | ₹{m_3y_hyb2['avg_trade']:<19,.2f}")
    print(f"{'Win / Loss Ratio':<25} | {m_3y_base['win_loss_ratio']:<20} | {m_3y_hyb1['win_loss_ratio']:<22} | {m_3y_hyb2['win_loss_ratio']:<20}")

    # 3Y Yearly breakdown for 2 Lots Prod Config
    t_3y_hyb2["year"] = pd.to_datetime(t_3y_hyb2["date"]).dt.year
    yearly = t_3y_hyb2.groupby("year").agg(
        trades=("net_pnl", "count"),
        wins=("net_pnl", lambda x: (x > 0).sum()),
        net_pnl=("net_pnl", "sum"),
        costs=("costs", "sum"),
        slippage=("slippage", "sum"),
    )
    yearly["win_rate"] = (yearly["wins"] / yearly["trades"] * 100.0).round(1)
    yearly["net_pnl"] = yearly["net_pnl"].round(2)
    print("\nYEARLY BREAKDOWN (2 Lots PROD Config):")
    print(yearly[["trades", "win_rate", "net_pnl"]])

    # 2. 5-Minute Intraday Simulation
    df_5m = pd.read_parquet("data/silverm_5m_intraday.parquet")
    print(f"\n[2] 5-MINUTE INTRADAY CONTINUOUS BACKTEST (45,608 Candles: 2025-10 to 2026-11)")
    print("-" * 90)

    t_5m_base = run_5m_simulation(df_5m, lots=1, use_hybrid_sl=False, use_daily_circuit_breaker=False, session_end=time(18, 0))
    m_5m_base = compute_metrics(t_5m_base, initial_margin=175000.0, safe_capital=250000.0)

    t_5m_hyb1 = run_5m_simulation(df_5m, lots=1, use_hybrid_sl=True, sl_per_lot=10000.0, use_daily_circuit_breaker=True, daily_max_loss_per_lot=15000.0, session_end=time(18, 0))
    m_5m_hyb1 = compute_metrics(t_5m_hyb1, initial_margin=175000.0, safe_capital=250000.0)

    t_5m_hyb2 = run_5m_simulation(df_5m, lots=2, use_hybrid_sl=True, sl_per_lot=10000.0, use_daily_circuit_breaker=True, daily_max_loss_per_lot=15000.0, session_end=time(18, 0))
    m_5m_hyb2 = compute_metrics(t_5m_hyb2, initial_margin=350000.0, safe_capital=500000.0)

    print(f"{'Metric':<25} | {'1 Lot Baseline (1.5x)':<20} | {'1 Lot Hybrid + Circuit':<22} | {'2 Lots PROD DOCKER':<20}")
    print("-" * 90)
    print(f"{'Total Trades':<25} | {m_5m_base['trades']:<20} | {m_5m_hyb1['trades']:<22} | {m_5m_hyb2['trades']:<20}")
    print(f"{'Win Rate (%)':<25} | {m_5m_base['win_rate']:<20}% | {m_5m_hyb1['win_rate']:<22}% | {m_5m_hyb2['win_rate']:<20}%")
    print(f"{'Win / Loss Count':<25} | {m_5m_base['win_count']}W / {m_5m_base['loss_count']}L{' ':<10} | {m_5m_hyb1['win_count']}W / {m_5m_hyb1['loss_count']}L{' ':<11} | {m_5m_hyb2['win_count']}W / {m_5m_hyb2['loss_count']}L")
    print(f"{'Gross PnL (₹)':<25} | ₹{m_5m_base['gross_pnl']:<19,.2f} | ₹{m_5m_hyb1['gross_pnl']:<21,.2f} | ₹{m_5m_hyb2['gross_pnl']:<19,.2f}")
    print(f"{'Frictions (Costs+Slip)':<25} | ₹{(m_5m_base['costs']+m_5m_base['slippage']):<19,.2f} | ₹{(m_5m_hyb1['costs']+m_5m_hyb1['slippage']):<21,.2f} | ₹{(m_5m_hyb2['costs']+m_5m_hyb2['slippage']):<19,.2f}")
    print(f"{'Net Realized PnL (₹)':<25} | ₹{m_5m_base['net_pnl']:<19,.2f} | ₹{m_5m_hyb1['net_pnl']:<21,.2f} | ₹{m_5m_hyb2['net_pnl']:<19,.2f}")
    print(f"{'Profit Factor':<25} | {m_5m_base['profit_factor']:<20} | {m_5m_hyb1['profit_factor']:<22} | {m_5m_hyb2['profit_factor']:<20}")
    print(f"{'Sharpe Ratio (ann.)':<25} | {m_5m_base['sharpe']:<20} | {m_5m_hyb1['sharpe']:<22} | {m_5m_hyb2['sharpe']:<20}")
    print(f"{'Max Drawdown (₹)':<25} | ₹{m_5m_base['max_dd']:<19,.2f} | ₹{m_5m_hyb1['max_dd']:<21,.2f} | ₹{m_5m_hyb2['max_dd']:<19,.2f}")
    print(f"{'Max Drawdown (%)':<25} | {m_5m_base['max_dd_pct']:<20}% | {m_5m_hyb1['max_dd_pct']:<22}% | {m_5m_hyb2['max_dd_pct']:<20}%")
    print(f"{'ROI on Margin (%)':<25} | {m_5m_base['roi_margin']:<20}% | {m_5m_hyb1['roi_margin']:<22}% | {m_5m_hyb2['roi_margin']:<20}%")
    print(f"{'ROI on Capital (%)':<25} | {m_5m_base['roi_capital']:<20}% | {m_5m_hyb1['roi_capital']:<22}% | {m_5m_hyb2['roi_capital']:<20}%")
    print(f"{'Avg Trade PnL (₹)':<25} | ₹{m_5m_base['avg_trade']:<19,.2f} | ₹{m_5m_hyb1['avg_trade']:<21,.2f} | ₹{m_5m_hyb2['avg_trade']:<19,.2f}")
    print(f"{'Win / Loss Ratio':<25} | {m_5m_base['win_loss_ratio']:<20} | {m_5m_hyb1['win_loss_ratio']:<22} | {m_5m_hyb2['win_loss_ratio']:<20}")

    # Save detailed CSV for analysis
    t_3y_hyb2.to_csv("data/silver_hybrid_2lots_3y_trades.csv", index=False)
    t_5m_hyb2.to_csv("data/silver_hybrid_2lots_5m_trades.csv", index=False)
    print("\nSaved trade logs to data/silver_hybrid_2lots_3y_trades.csv and data/silver_hybrid_2lots_5m_trades.csv")


if __name__ == "__main__":
    main()
