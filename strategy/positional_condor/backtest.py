import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd

from strategy.costs import calculate_trade_costs
from strategy.positional_condor.indicator import compute_rsi, classify_regime
from strategy.options_model import black_76_price

logger = logging.getLogger("condor.backtest")


def run_positional_monthly_backtest(
    df_daily: pd.DataFrame,
    initial_capital: float = 100000.0,
    lots: int = 1,
    target_pct: float = 0.04,
    stop_pct: float = 0.03,
    slippage_per_share: float = 0.50
) -> Dict[str, Any]:
    """
    Simulates monthly positional trading across historical daily NIFTY bars.
    Implements Uday Chandra's rules:
    - 40 <= RSI <= 60: 1:1 Iron Condor (aims for ~5% max profit/loss)
    - RSI < 40: 1:2:1 Bearish Put Ratio
    - RSI > 60: 1:2:1 Bullish Call Ratio
    - Monthly target exit: +3.5% to +4.0%
    - Monthly max loss stop: -3.0%
    """
    df = df_daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["rsi"] = compute_rsi(df["close"], period=14)

    df["month"] = df["date"].dt.strftime("%Y-%m")
    unique_months = df["month"].unique()

    monthly_records = []
    lot_size = 65
    qty = lots * lot_size
    capital = initial_capital * lots

    for m in unique_months:
        m_df = df[df["month"] == m].copy().reset_index(drop=True)
        # Skip months without sufficient RSI warm-up
        if len(m_df) < 5 or m_df["rsi"].dropna().empty:
            continue

        m_start_idx = 0
        initial_rsi = float(m_df["rsi"].iloc[m_start_idx])
        if np.isnan(initial_rsi):
            # Find first valid RSI
            valid = m_df["rsi"].dropna()
            if valid.empty:
                continue
            initial_rsi = float(valid.iloc[0])

        current_regime = classify_regime(initial_rsi)
        month_entry_spot = float(m_df["close"].iloc[m_start_idx])
        days_in_month = len(m_df)

        month_realized_pnl = 0.0
        month_costs = 0.0
        trade_status = "OPEN"
        rolls_count = 0
        active_entry_spot = month_entry_spot

        for day_idx in range(m_start_idx, days_in_month):
            spot = float(m_df["close"].iloc[day_idx])
            rsi_val = float(m_df["rsi"].iloc[day_idx])
            regime = classify_regime(rsi_val) if not np.isnan(rsi_val) else current_regime

            # Fractional days left in the monthly cycle
            days_left = max(1, days_in_month - day_idx)
            theta_decay_factor = (days_in_month - days_left) / float(days_in_month)

            # Estimate trade MTM based on structure and spot deviation
            spot_diff = spot - active_entry_spot
            unrealized_pnl = 0.0

            if current_regime == "SIDEWAYS":
                # 1:1 Iron Condor: Max profit ~5%, max loss ~5%
                # Range width ~ +/- 300 points
                if abs(spot_diff) <= 250:
                    unrealized_pnl = (capital * target_pct) * theta_decay_factor
                else:
                    excess = abs(spot_diff) - 250
                    loss_points = min(200.0, excess)
                    unrealized_pnl = ((capital * target_pct) * theta_decay_factor) - (loss_points * qty * 0.4)
            elif current_regime == "BEARISH":
                # 1:2:1 Put Ratio: profit if market drops, small loss on upside
                if spot_diff <= 0:
                    drop = abs(spot_diff)
                    if drop <= 250:
                        unrealized_pnl = min(capital * target_pct, (drop * qty * 0.35) + (capital * 0.01 * theta_decay_factor))
                    else:
                        unrealized_pnl = max(-capital * 0.025, (capital * target_pct) - ((drop - 250) * qty * 0.2))
                else:
                    unrealized_pnl = -min(capital * 0.018, spot_diff * qty * 0.08)
            else:  # BULLISH
                # 1:2:1 Call Ratio: profit if market rallies, small loss on downside
                if spot_diff >= 0:
                    rally = spot_diff
                    if rally <= 250:
                        unrealized_pnl = min(capital * target_pct, (rally * qty * 0.35) + (capital * 0.01 * theta_decay_factor))
                    else:
                        unrealized_pnl = max(-capital * 0.025, (capital * target_pct) - ((rally - 250) * qty * 0.2))
                else:
                    unrealized_pnl = -min(capital * 0.018, abs(spot_diff) * qty * 0.08)

            total_month_pnl = month_realized_pnl + unrealized_pnl
            month_pct = total_month_pnl / capital

            # Target circuit breaker (+4%)
            if month_pct >= target_pct:
                trade_status = "TARGET_LOCKED"
                month_realized_pnl = capital * target_pct
                costs = calculate_trade_costs(100.0, 50.0, qty, slippage_per_share=slippage_per_share)
                month_costs += costs["total_costs"]
                break

            # Stop loss circuit breaker (-4%)
            if month_pct <= -stop_pct:
                trade_status = "STOPPED_OUT"
                month_realized_pnl = -capital * stop_pct
                costs = calculate_trade_costs(100.0, 150.0, qty, slippage_per_share=slippage_per_share)
                month_costs += costs["total_costs"]
                break

            # Check for regime transition
            if regime != current_regime:
                rolls_count += 1
                month_realized_pnl += unrealized_pnl
                costs = calculate_trade_costs(80.0, 80.0, qty, slippage_per_share=slippage_per_share)
                month_costs += costs["total_costs"]
                current_regime = regime
                active_entry_spot = spot
                continue

            # Check condor break-even roll
            if current_regime == "SIDEWAYS" and abs(spot_diff) > 280:
                rolls_count += 1
                month_realized_pnl += unrealized_pnl
                costs = calculate_trade_costs(80.0, 80.0, qty, slippage_per_share=slippage_per_share)
                month_costs += costs["total_costs"]
                active_entry_spot = spot

        if trade_status == "OPEN":
            month_realized_pnl += unrealized_pnl
            costs = calculate_trade_costs(80.0, 40.0, qty, slippage_per_share=slippage_per_share)
            month_costs += costs["total_costs"]
            trade_status = "EXPIRED"

        net_month_pnl = month_realized_pnl - month_costs
        net_return_pct = (net_month_pnl / capital) * 100.0

        monthly_records.append({
            "month": m,
            "status": trade_status,
            "regime": current_regime,
            "rolls": rolls_count,
            "gross_pnl": round(month_realized_pnl, 2),
            "costs": round(month_costs, 2),
            "net_pnl": round(net_month_pnl, 2),
            "return_pct": round(net_return_pct, 2)
        })

    results_df = pd.DataFrame(monthly_records)
    total_months = len(results_df)
    profitable_months = int((results_df["net_pnl"] > 0).sum()) if total_months > 0 else 0
    total_net_pnl = float(results_df["net_pnl"].sum()) if total_months > 0 else 0.0
    win_rate = (profitable_months / total_months * 100.0) if total_months > 0 else 0.0

    return {
        "total_months": total_months,
        "profitable_months": profitable_months,
        "losing_months": total_months - profitable_months,
        "win_rate": round(win_rate, 1),
        "total_net_pnl": round(total_net_pnl, 2),
        "monthly_records": results_df.to_dict(orient="records")
    }
