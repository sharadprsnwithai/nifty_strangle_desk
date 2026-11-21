"""
Comprehensive 3-Year Backtest of the Intraday Williams %R + Triple EMA Option Selling Strategy
on MCX Silver Mini (SILVERM) using actual continuous Kite API historical data (2023-2026).
"""

import os
import sys
import numpy as np
import pandas as pd

# Add repository root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategy.options_model import black_76_price
from strategy.silver_options.backtest import calculate_mcx_costs


def run_3y_intraday_backtest(
    parquet_path: str = "data/silverm_3y_daily.parquet",
    lot_size: int = 5,
    initial_margin: float = 175000.0,
    safe_capital: float = 250000.0,
    sl_multiplier: float = 1.5,
    slippage_per_trade: float = 400.0,
):
    df = pd.read_parquet(parquet_path)
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
            # 0.35 Delta OTM Put (~2.5% below open)
            strike = round((spot_open * 0.975) / 1000.0) * 1000.0
            entry_prem = black_76_price(spot_open, strike, 20.0, iv=iv, r=0.065, opt_type="PE")
            worst_prem = black_76_price(low, strike, 19.5, iv=iv, r=0.065, opt_type="PE")
            if worst_prem >= entry_prem * sl_multiplier:
                exit_prem = entry_prem * sl_multiplier
                reason = "STOP_LOSS (1.5x)"
            else:
                exit_prem = black_76_price(spot_close, strike, 19.0, iv=iv, r=0.065, opt_type="PE")
                reason = "INTRADAY_EOD"

            pts = entry_prem - exit_prem
            gross = pts * lot_size
            costs = calculate_mcx_costs((entry_prem + exit_prem) * lot_size, legs=1) + slippage_per_trade
            net = gross - costs
            trades.append(
                {
                    "date": curr_date,
                    "type": "SHORT_PUT",
                    "spot_open": spot_open,
                    "spot_close": spot_close,
                    "strike": strike,
                    "entry": round(entry_prem, 2),
                    "exit": round(exit_prem, 2),
                    "gross_pnl": round(gross, 2),
                    "costs": round(costs, 2),
                    "net_pnl": round(net, 2),
                    "reason": reason,
                }
            )

        elif bear and not bull:
            # 0.35 Delta OTM Call (~2.5% above open)
            strike = round((spot_open * 1.025) / 1000.0) * 1000.0
            entry_prem = black_76_price(spot_open, strike, 20.0, iv=iv, r=0.065, opt_type="CE")
            worst_prem = black_76_price(high, strike, 19.5, iv=iv, r=0.065, opt_type="CE")
            if worst_prem >= entry_prem * sl_multiplier:
                exit_prem = entry_prem * sl_multiplier
                reason = "STOP_LOSS (1.5x)"
            else:
                exit_prem = black_76_price(spot_close, strike, 19.0, iv=iv, r=0.065, opt_type="CE")
                reason = "INTRADAY_EOD"

            pts = entry_prem - exit_prem
            gross = pts * lot_size
            costs = calculate_mcx_costs((entry_prem + exit_prem) * lot_size, legs=1) + slippage_per_trade
            net = gross - costs
            trades.append(
                {
                    "date": curr_date,
                    "type": "SHORT_CALL",
                    "spot_open": spot_open,
                    "spot_close": spot_close,
                    "strike": strike,
                    "entry": round(entry_prem, 2),
                    "exit": round(exit_prem, 2),
                    "gross_pnl": round(gross, 2),
                    "costs": round(costs, 2),
                    "net_pnl": round(net, 2),
                    "reason": reason,
                }
            )

    t_df = pd.DataFrame(trades)
    return t_df


def main():
    t_df = run_3y_intraday_backtest()
    t_df.to_csv("data/silver_williams_ema_3y_daily_intraday_trades.csv", index=False)
    print(f"Saved {len(t_df)} trades to data/silver_williams_ema_3y_daily_intraday_trades.csv")


if __name__ == "__main__":
    main()
