"""
Comprehensive 3-Year MCX Silver Options Desk Backtest using actual Kite API Data.

This script:
1. Retrieves and caches 3 years of actual continuous daily market data for MCX Silver Mini (SILVERM) directly from Kite API.
2. Evaluates two distinct quantitative option models:
   - Model A (Webinar Tactical Reversal Desk): Bull Call Spreads on deep oversold RSI (<25) & Bullish Divergences.
   - Model B (Regime-Aware Trend Spread Desk): 200 EMA Macro Regime + RSI Pullback Spreads.
3. Computes exact Indian derivatives transaction frictions:
   - MCX exchange turnover charges (0.0021%)
   - Commodity Transaction Tax (CTT, 0.01%)
   - SEBI turnover charges (0.0001%)
   - GST (18%) on brokerage and exchange fees
   - Stamp duty (0.003%)
   - Brokerage (₹20/order entry and exit per leg = ₹80 per spread)
   - Realistic bid-ask slippage buffer (₹200 per lot per leg = ₹400 per trade).
4. Outputs comprehensive overall metrics, yearly breakdowns (2023-2026), and saves detailed CSV trade logs.
"""

import os
import sys
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kite.client import get_kite_client
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.technical_signals import compute_rsi
from strategy.options_model import black_76_price
from strategy.silver_options.backtest import calculate_mcx_costs


def fetch_or_load_silver_data(cache_path: str = "data/silverm_3y_daily.parquet") -> pd.DataFrame:
    """Fetches 3 years of continuous daily data for MCX SILVERM from Kite API or loads cache."""
    if os.path.exists(cache_path):
        print(f"Loading cached 3-year Kite data from {cache_path}...")
        df = pd.read_parquet(cache_path)
        return df

    print("Connecting to Kite API to fetch 3-year continuous historical data for MCX SILVERM...")
    kite = get_kite_client()
    instruments = kite.instruments("MCX")
    silverm_futs = [i for i in instruments if i.get("name") == "SILVERM" and i.get("instrument_type") == "FUT"]
    if not silverm_futs:
        raise RuntimeError("No active SILVERM futures instrument found on MCX via Kite API.")

    silverm_futs.sort(key=lambda x: x["expiry"])
    target_token = silverm_futs[0]["instrument_token"]
    symbol = silverm_futs[0]["tradingsymbol"]
    print(f"Using instrument {symbol} (token {target_token}) for continuous historical data...")

    to_date = datetime.now()
    from_date = to_date - timedelta(days=365 * 3)

    raw_data = kite.historical_data(target_token, from_date=from_date, to_date=to_date, interval="day", continuous=True)
    if not raw_data:
        raise RuntimeError("Kite API returned empty historical data for SILVERM.")

    df = pd.DataFrame(raw_data)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.sort_values("date").reset_index(drop=True)

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    df.to_parquet(cache_path, index=False)
    print(f"Fetched and cached {len(df)} daily bars into {cache_path}.")
    return df


def simulate_options_spread_backtest(df: pd.DataFrame, model_type: str = "regime_trend", config: SilverConfig = None) -> pd.DataFrame:
    """
    Simulates vertical spreads across 3 years.
    model_type: 'webinar_reversal' or 'regime_trend'
    """
    cfg = config or SilverConfig()
    lot_size = cfg.lot_size  # 5 kg per lot

    # Indicators
    df = df.copy()
    df["rsi"] = compute_rsi(df["close"], period=cfg.daily_rsi_period)
    df["ema20"] = df["close"].ewm(span=cfg.hourly_ema_period, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    # Dynamic rolling realized volatility (20-day annualized)
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["iv"] = (df["log_ret"].rolling(20).std() * np.sqrt(252)).clip(lower=0.20, upper=0.65).fillna(0.32)

    trades = []
    i = 50

    while i < len(df) - 10:
        curr = df.iloc[i]
        prev = df.iloc[i - 1]
        spot = curr["close"]
        iv = curr["iv"]

        sig = None

        if model_type == "webinar_reversal":
            # Webinar setup: Deep oversold RSI or Divergence reversal
            window = df.iloc[i - 20 : i]
            min_close = window["close"].min()
            min_rsi = window.loc[window["close"] == min_close, "rsi"].iloc[0]
            bull_div = (curr["close"] < min_close) and (curr["rsi"] > min_rsi) and (curr["rsi"] <= 35)
            bull_oversold = (prev["rsi"] <= 25) and (curr["close"] > prev["close"])

            if (bull_div or bull_oversold) and curr["close"] > prev["close"]:
                sig = "BULL_CALL_SPREAD"

        elif model_type == "regime_trend":
            # Macro 200 EMA trend filter + pullback bounce
            is_macro_bull = curr["close"] > curr["ema200"]
            is_macro_bear = curr["close"] < curr["ema200"]

            bull_signal = (
                is_macro_bull
                and (curr["close"] > curr["ema20"])
                and (curr["ema20"] > curr["ema50"])
                and (prev["rsi"] <= 55)
                and (curr["rsi"] > 50)
                and (curr["close"] > prev["close"])
            )
            bear_signal = (
                is_macro_bear
                and (curr["close"] < curr["ema20"])
                and (curr["ema20"] < curr["ema50"])
                and (prev["rsi"] >= 45)
                and (curr["rsi"] < 50)
                and (curr["close"] < prev["close"])
            )

            if bull_signal:
                sig = "BULL_CALL_SPREAD"
            elif bear_signal:
                sig = "BEAR_PUT_SPREAD"

        if sig is not None:
            width = max(2000.0, round((spot * 0.04) / 1000.0) * 1000.0)
            atm_strike = round(spot / 1000.0) * 1000.0
            dte_init = 15.0  # ~3 weeks to monthly expiry

            if sig == "BULL_CALL_SPREAD":
                k_long = atm_strike
                k_short = atm_strike + width
                p_long = black_76_price(spot, k_long, dte_init, iv, opt_type="CE")
                p_short = black_76_price(spot, k_short, dte_init, iv, opt_type="CE")
            else:
                k_long = atm_strike
                k_short = atm_strike - width
                p_long = black_76_price(spot, k_long, dte_init, iv, opt_type="PE")
                p_short = black_76_price(spot, k_short, dte_init, iv, opt_type="PE")

            net_debit = p_long - p_short

            if net_debit > 0:
                max_profit = width - net_debit
                tp_target = net_debit + 0.75 * max_profit
                sl_target = 0.50 * net_debit

                exit_date = None
                exit_reason = None
                exit_spread_val = None
                holding_days = 0

                for j in range(i + 1, min(i + 22, len(df))):
                    bar = df.iloc[j]
                    holding_days += 1
                    dte_curr = max(0.1, dte_init - holding_days)

                    if sig == "BULL_CALL_SPREAD":
                        v_high = black_76_price(bar["high"], k_long, dte_curr, iv, opt_type="CE") - black_76_price(bar["high"], k_short, dte_curr, iv, opt_type="CE")
                        v_low = black_76_price(bar["low"], k_long, dte_curr, iv, opt_type="CE") - black_76_price(bar["low"], k_short, dte_curr, iv, opt_type="CE")
                    else:
                        v_high = black_76_price(bar["low"], k_long, dte_curr, iv, opt_type="PE") - black_76_price(bar["low"], k_short, dte_curr, iv, opt_type="PE")
                        v_low = black_76_price(bar["high"], k_long, dte_curr, iv, opt_type="PE") - black_76_price(bar["high"], k_short, dte_curr, iv, opt_type="PE")

                    if v_high >= tp_target:
                        exit_date = bar["date"]
                        exit_reason = "TAKE_PROFIT"
                        exit_spread_val = tp_target
                        break
                    elif v_low <= sl_target:
                        exit_date = bar["date"]
                        exit_reason = "STOP_LOSS"
                        exit_spread_val = sl_target
                        break
                    elif dte_curr <= 0.5:
                        exit_date = bar["date"]
                        exit_reason = "EXPIRY_CUTOFF"
                        if sig == "BULL_CALL_SPREAD":
                            exit_spread_val = max(0.0, min(width, bar["close"] - k_long))
                        else:
                            exit_spread_val = max(0.0, min(width, k_long - bar["close"]))
                        break

                if exit_date is None:
                    exit_date = df.iloc[-1]["date"]
                    exit_reason = "END_OF_DATA"
                    bar = df.iloc[-1]
                    if sig == "BULL_CALL_SPREAD":
                        exit_spread_val = max(0.0, min(width, bar["close"] - k_long))
                    else:
                        exit_spread_val = max(0.0, min(width, k_long - bar["close"]))

                pts = exit_spread_val - net_debit
                gross_pnl = pts * lot_size
                turnover = spot * lot_size * 2
                costs = calculate_mcx_costs(turnover=turnover, legs=2)
                slippage = 200.0 * 2  # ₹200 slippage buffer per leg
                net_pnl = gross_pnl - costs - slippage

                trades.append({
                    "entry_date": curr["date"].strftime("%Y-%m-%d"),
                    "exit_date": exit_date.strftime("%Y-%m-%d"),
                    "year": curr["date"].year,
                    "model": model_type,
                    "signal": sig,
                    "spot_entry": round(spot, 0),
                    "long_strike": int(k_long),
                    "short_strike": int(k_short),
                    "spread_width": int(width),
                    "net_debit": round(net_debit, 1),
                    "exit_val": round(exit_spread_val, 1),
                    "holding_days": holding_days,
                    "exit_reason": exit_reason,
                    "gross_pnl": round(gross_pnl, 2),
                    "costs": round(costs, 2),
                    "slippage": round(slippage, 2),
                    "net_pnl": round(net_pnl, 2),
                })
                i = j
                continue
        i += 1

    return pd.DataFrame(trades)


def compute_metrics(tdf: pd.DataFrame, capital: float = 200000.0) -> dict:
    if tdf.empty:
        return {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0, "pf": 0.0, "max_dd": 0.0, "roi": 0.0}

    tot_trades = len(tdf)
    wins = (tdf["net_pnl"] > 0).sum()
    losses = (tdf["net_pnl"] <= 0).sum()
    wr = (wins / tot_trades) * 100.0
    tot_gross = tdf["gross_pnl"].sum()
    tot_costs = tdf["costs"].sum()
    tot_slippage = tdf["slippage"].sum()
    tot_net = tdf["net_pnl"].sum()

    tot_win_val = tdf[tdf["net_pnl"] > 0]["net_pnl"].sum()
    tot_loss_val = abs(tdf[tdf["net_pnl"] < 0]["net_pnl"].sum())
    pf = (tot_win_val / tot_loss_val) if tot_loss_val > 0 else float("inf")

    cum_pnl = tdf["net_pnl"].cumsum()
    peak = cum_pnl.cummax()
    max_dd = (peak - cum_pnl).max()
    roi = (tot_net / capital) * 100.0

    return {
        "trades": tot_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "gross_pnl": tot_gross,
        "costs": tot_costs,
        "slippage": tot_slippage,
        "net_pnl": tot_net,
        "profit_factor": pf,
        "max_dd": max_dd,
        "roi": roi,
    }


def main():
    df = fetch_or_load_silver_data()
    cfg = SilverConfig()

    print("Running Model A: Webinar Tactical Reversal Desk (RSI Divergence)...")
    tdf_webinar = simulate_options_spread_backtest(df, model_type="webinar_reversal", config=cfg)

    print("Running Model B: Regime-Aware Trend Spread Desk (200 EMA + Pullback)...")
    tdf_trend = simulate_options_spread_backtest(df, model_type="regime_trend", config=cfg)

    # Save CSV logs
    os.makedirs("data", exist_ok=True)
    tdf_webinar.to_csv("data/silver_options_3y_webinar_reversal_trades.csv", index=False)
    tdf_trend.to_csv("data/silver_options_3y_trend_spread_trades.csv", index=False)

    capital = 200000.0  # ₹2.0L margin capital per lot

    mA = compute_metrics(tdf_webinar, capital)
    mB = compute_metrics(tdf_trend, capital)

    print("\n" + "=" * 120)
    print("                 MCX SILVER OPTIONS DESK — 3-YEAR BACKTEST PERFORMANCE COMPARISON (KITE DATA)")
    print("=" * 120)

    summary_rows = [
        {
            "Strategy Model": "Model A: Webinar Tactical Reversal (RSI Divergence)",
            "Trades": mA["trades"],
            "Win Rate (%)": f"{mA['win_rate']:.1f}% ({mA.get('wins',0)}W / {mA.get('losses',0)}L)",
            "Gross PnL (Rs)": f"{mA.get('gross_pnl', 0):+,.2f}",
            "Frictions (Rs)": f"-{(mA.get('costs', 0) + mA.get('slippage', 0)):,.2f}",
            "Net PnL (Rs)": f"{mA['net_pnl']:+,.2f}",
            "Profit Factor": f"{mA['profit_factor']:.2f}",
            "Max DD (Rs)": f"{mA['max_dd']:,.2f}",
            "3Y ROI (%)": f"{mA['roi']:+.1f}%",
        },
        {
            "Strategy Model": "Model B: Regime-Aware Trend Spread (200 EMA Pullback)",
            "Trades": mB["trades"],
            "Win Rate (%)": f"{mB['win_rate']:.1f}% ({mB.get('wins',0)}W / {mB.get('losses',0)}L)",
            "Gross PnL (Rs)": f"{mB.get('gross_pnl', 0):+,.2f}",
            "Frictions (Rs)": f"-{(mB.get('costs', 0) + mB.get('slippage', 0)):,.2f}",
            "Net PnL (Rs)": f"{mB['net_pnl']:+,.2f}",
            "Profit Factor": f"{mB['profit_factor']:.2f}",
            "Max DD (Rs)": f"{mB['max_dd']:,.2f}",
            "3Y ROI (%)": f"{mB['roi']:+.1f}%",
        },
    ]

    print(pd.DataFrame(summary_rows).to_string(index=False))
    print("=" * 120)

    print("\n" + "-" * 120)
    print("                       YEAR-BY-YEAR PERFORMANCE BREAKDOWN — MODEL B (REGIME TREND DESK)")
    print("-" * 120)

    yearly_rows = []
    for yr, group in tdf_trend.groupby("year"):
        my = compute_metrics(group, capital)
        yearly_rows.append({
            "Year": yr,
            "Trades": my["trades"],
            "Win Rate (%)": f"{my['win_rate']:.1f}% ({my['wins']}W / {my['losses']}L)",
            "Gross PnL (Rs)": f"{my['gross_pnl']:+,.2f}",
            "Frictions (Rs)": f"-{(my['costs'] + my['slippage']):,.2f}",
            "Net PnL (Rs)": f"{my['net_pnl']:+,.2f}",
            "Profit Factor": f"{my['profit_factor']:.2f}",
            "Max DD (Rs)": f"{my['max_dd']:,.2f}",
            "Yearly ROI (%)": f"{my['roi']:+.1f}%",
        })

    print(pd.DataFrame(yearly_rows).to_string(index=False))
    print("=" * 120)


if __name__ == "__main__":
    main()
