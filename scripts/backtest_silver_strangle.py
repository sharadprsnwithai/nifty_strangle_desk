import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import math
import numpy as np
import pandas as pd
from scipy.stats import norm

from config import load_config
from kite import get_kite_client
from strategy.options_model import black_76_price

def fetch_kite_silver_5m():
    cache_path = Path("data/silver_5m_kite.parquet")
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        return df

    print("Fetching Silver 5m candles from Kite API...")
    cfg = load_config()
    k = get_kite_client(config=cfg, check_market_hours=False, ignore_holidays=True)
    token = 126774791  # SILVER26DECFUT

    all_candles = []
    end = datetime.now()
    for i in range(4):
        start = end - timedelta(days=50)
        c = k.historical_data(instrument_token=token, from_date=start, to_date=end, interval="5minute")
        if c:
            all_candles = c + all_candles
            end = c[0]["date"] - timedelta(minutes=5)
        else:
            break

    df = pd.DataFrame(all_candles)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df

def get_monthly_expiry(current_date: date) -> date:
    """
    MCX Silver options typically expire on the 4th Friday or around 26th-27th of the month.
    """
    year = current_date.year
    month = current_date.month
    
    # Check expiry around 26th
    tentative = date(year, month, 26)
    # If already past tentative expiry in current month, take next month
    if current_date > tentative:
        if month == 12:
            tentative = date(year + 1, 1, 26)
        else:
            tentative = date(year, month + 1, 26)
    return tentative

def calculate_dte_fraction(current_dt: datetime, expiry_date: date) -> float:
    calendar_days = (expiry_date - current_dt.date()).days
    if calendar_days < 0:
        return 1e-4
    
    # MCX session is 09:00 to 23:30 (14.5 hours = 870 minutes)
    market_open = 9 * 60
    market_close = 23 * 60 + 30
    total_session_mins = float(market_close - market_open)
    
    current_mins = current_dt.hour * 60 + current_dt.minute
    remaining_today = max(0.0, float(market_close - max(market_open, current_mins)))
    fraction_today = remaining_today / total_session_mins
    
    trading_days = max(1, calendar_days * 5 / 7)
    return max(1e-4, float(trading_days) + fraction_today)

def run_strangle_backtest(
    df: pd.DataFrame,
    strike_otm_pct: float = 0.015, # 1.5% OTM (~15 Delta)
    sl_pct: float = 0.40,          # 40% single-leg SL
    trail_to_cost: bool = True,
    target_decay_pct: float = 0.70,
    base_iv: float = 0.30,         # 30% annualized IV
    evening_iv_crush: float = 0.02 # 2% IV drop between 20:00 and 23:15
):
    df["time"] = df["date"].dt.strftime("%H:%M")
    df["day"] = df["date"].dt.date
    
    # Find liquid days with daily volume > 1000
    day_vol = df.groupby("day")["volume"].sum()
    active_days = sorted(day_vol[day_vol > 1000].index)
    
    trade_logs = []
    
    for trade_day in active_days:
        day_df = df[df["day"] == trade_day].sort_values("date").reset_index(drop=True)
        
        # We need candles between 20:00 and 23:15
        evening_df = day_df[(day_df["time"] >= "20:00") & (day_df["time"] <= "23:15")].reset_index(drop=True)
        if len(evening_df) < 5:
            continue
            
        entry_row = evening_df.iloc[0]
        entry_time = entry_row["time"]
        if entry_time > "20:10":
            continue
            
        spot_entry = entry_row["open"]
        entry_dt = entry_row["date"]
        expiry = get_monthly_expiry(trade_day)
        dte_entry = calculate_dte_fraction(entry_dt, expiry)
        
        # Strike Selection: 1.5% OTM rounded to nearest 500 or 1000
        strike_step = 1000.0 if spot_entry > 100000 else 500.0
        ce_strike = round((spot_entry * (1.0 + strike_otm_pct)) / strike_step) * strike_step
        pe_strike = round((spot_entry * (1.0 - strike_otm_pct)) / strike_step) * strike_step
        
        # Initial Option Prices (Black-76)
        ce_entry = black_76_price(spot_entry, ce_strike, dte_entry, iv=base_iv, opt_type="CE")
        pe_entry = black_76_price(spot_entry, pe_strike, dte_entry, iv=base_iv, opt_type="PE")
        
        if ce_entry <= 5.0 or pe_entry <= 5.0:
            continue
            
        ce_sl = ce_entry * (1.0 + sl_pct)
        pe_sl = pe_entry * (1.0 + sl_pct)
        
        ce_status = "OPEN"
        pe_status = "OPEN"
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_time = None
        pe_exit_time = None
        ce_exit_reason = "EOD"
        pe_exit_reason = "EOD"
        
        total_evening_candles = len(evening_df)
        
        for idx, row in evening_df.iterrows():
            curr_dt = row["date"]
            curr_spot = row["close"]
            curr_high = row["high"]
            curr_low = row["low"]
            curr_time = row["time"]
            
            # Linear IV crush across the evening session
            prog = idx / max(1, total_evening_candles - 1)
            curr_iv = max(0.15, base_iv - evening_iv_crush * prog)
            curr_dte = calculate_dte_fraction(curr_dt, expiry)
            
            # High/Low worst-case price simulation for SL trigger
            ce_worst_price = black_76_price(curr_high, ce_strike, curr_dte, iv=curr_iv, opt_type="CE")
            pe_worst_price = black_76_price(curr_low, pe_strike, curr_dte, iv=curr_iv, opt_type="PE")
            
            ce_curr_price = black_76_price(curr_spot, ce_strike, curr_dte, iv=curr_iv, opt_type="CE")
            pe_curr_price = black_76_price(curr_spot, pe_strike, curr_dte, iv=curr_iv, opt_type="PE")
            
            # Check CE Stop Loss
            if ce_status == "OPEN" and ce_worst_price >= ce_sl:
                ce_status = "STOPPED"
                ce_exit_price = round(ce_sl, 2)
                ce_exit_time = curr_time
                ce_exit_reason = "SL"
                if trail_to_cost and pe_status == "OPEN":
                    pe_sl = pe_entry # Trail winning PE to cost
                    
            # Check PE Stop Loss
            if pe_status == "OPEN" and pe_worst_price >= pe_sl:
                pe_status = "STOPPED"
                pe_exit_price = round(pe_sl, 2)
                pe_exit_time = curr_time
                pe_exit_reason = "SL"
                if trail_to_cost and ce_status == "OPEN":
                    ce_sl = ce_entry # Trail winning CE to cost
            
            # Check Target Decay (70% total decay)
            if ce_status == "OPEN" and pe_status == "OPEN":
                combined_entry = ce_entry + pe_entry
                combined_curr = ce_curr_price + pe_curr_price
                if (combined_entry - combined_curr) / combined_entry >= target_decay_pct:
                    ce_status = "TARGET"
                    pe_status = "TARGET"
                    ce_exit_price = ce_curr_price
                    pe_exit_price = pe_curr_price
                    ce_exit_time = curr_time
                    pe_exit_time = curr_time
                    ce_exit_reason = "TARGET_DECAY"
                    pe_exit_reason = "TARGET_DECAY"
                    break
                    
            if idx == total_evening_candles - 1:
                # EOD 23:15 exit
                if ce_status == "OPEN":
                    ce_status = "EOD"
                    ce_exit_price = ce_curr_price
                    ce_exit_time = curr_time
                    ce_exit_reason = "EOD"
                if pe_status == "OPEN":
                    pe_status = "EOD"
                    pe_exit_price = pe_curr_price
                    pe_exit_time = curr_time
                    pe_exit_reason = "EOD"
        
        ce_pts = ce_entry - ce_exit_price
        pe_pts = pe_entry - pe_exit_price
        total_pts = ce_pts + pe_pts
        
        trade_logs.append({
            "date": trade_day,
            "spot_entry": spot_entry,
            "ce_strike": ce_strike,
            "pe_strike": pe_strike,
            "ce_entry": ce_entry,
            "ce_exit": ce_exit_price,
            "ce_reason": ce_exit_reason,
            "pe_entry": pe_entry,
            "pe_exit": pe_exit_price,
            "pe_reason": pe_exit_reason,
            "ce_pts": round(ce_pts, 2),
            "pe_pts": round(pe_pts, 2),
            "total_pts": round(total_pts, 2),
            "dte": round(dte_entry, 1)
        })
        
    return pd.DataFrame(trade_logs)

if __name__ == "__main__":
    df = fetch_kite_silver_5m()
    results = run_strangle_backtest(df)
    results.to_csv("data/silver_strangle_backtest_trades.csv", index=False)
    print(f"Executed trades across {len(results)} days.")
    print("\nSample recent trades:")
    print(results.tail(10)[["date", "spot_entry", "ce_strike", "pe_strike", "ce_pts", "pe_pts", "total_pts", "ce_reason", "pe_reason"]])
    
    total_pts = results["total_pts"].sum()
    win_days = (results["total_pts"] > 0).sum()
    loss_days = (results["total_pts"] < 0).sum()
    win_rate = win_days / len(results) * 100
    
    avg_win = results[results["total_pts"] > 0]["total_pts"].mean()
    avg_loss = results[results["total_pts"] < 0]["total_pts"].mean()
    profit_factor = abs(results[results["total_pts"] > 0]["total_pts"].sum() / results[results["total_pts"] < 0]["total_pts"].sum()) if loss_days > 0 else 999.0
    
    # Max Drawdown
    cum_pts = results["total_pts"].cumsum()
    peak = cum_pts.cummax()
    dd = peak - cum_pts
    max_dd_pts = dd.max()
    
    print("\n--- OVERALL PERFORMANCE SUMMARY ---")
    print(f"Total Trading Sessions: {len(results)}")
    print(f"Win Sessions: {win_days} | Loss Sessions: {loss_days} | Win Rate: {win_rate:.1f}%")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Total Net Points Captured: {total_pts:,.2f} pts")
    print(f"Average Points per Day: {total_pts / len(results):.2f} pts")
    print(f"Avg Win: +{avg_win:.2f} pts | Avg Loss: {avg_loss:.2f} pts")
    print(f"Max Drawdown: {max_dd_pts:,.2f} pts")
    
    # Multiplier for 2 LOTS:
    # 1. SILVERM (Silver Mini: 1 lot = 5 kg, 2 lots = 10 kg -> 1 pt = Rs 10)
    silverm_gross = total_pts * 10.0
    # Cost per day: ~Rs 120 (Brokerage Rs 80 + Exchange fees + STT + GST)
    silverm_costs = len(results) * 120.0
    silverm_net = silverm_gross - silverm_costs
    silverm_max_dd = max_dd_pts * 10.0
    
    # 2. SILVER (Big Lot: 1 lot = 30 kg, 2 lots = 60 kg -> 1 pt = Rs 60)
    silver_gross = total_pts * 60.0
    silver_costs = len(results) * 450.0 # Higher turnover STT + exchange charges
    silver_net = silver_gross - silver_costs
    silver_max_dd = max_dd_pts * 60.0
    
    print("\n" + "=" * 60)
    print("      REAL MCX DATA BACKTEST RESULTS (2 LOTS)")
    print("=" * 60)
    print(f"SILVER MINI (SILVERM - 2 Lots = 10 kg, Rs 10/pt):")
    print(f"  Gross P&L: Rs {silverm_gross:,.2f}")
    print(f"  Taxes, Brokerage & Slippage: -Rs {silverm_costs:,.2f}")
    print(f"  NET REALIZED P&L: Rs {silverm_net:,.2f}")
    print(f"  Max Drawdown: Rs {silverm_max_dd:,.2f}")
    print("-" * 60)
    print(f"SILVER BIG (SILVER - 2 Lots = 60 kg, Rs 60/pt):")
    print(f"  Gross P&L: Rs {silver_gross:,.2f}")
    print(f"  Taxes, Brokerage & Slippage: -Rs {silver_costs:,.2f}")
    print(f"  NET REALIZED P&L: Rs {silver_net:,.2f}")
    print(f"  Max Drawdown: Rs {silver_max_dd:,.2f}")
    print("=" * 60)
