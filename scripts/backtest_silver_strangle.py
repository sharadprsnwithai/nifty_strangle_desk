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
from strategy.silver_options.chain import get_strike_step, round_to_strike_step
from strategy.silver_options.models import calculate_silver_mcx_costs

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

def get_monthly_expiry(current_date: date, skip_0dte: bool = True) -> date:
    """
    MCX Silver options typically expire around the 26th of the month.
    Matches live `skip_0dte=True` rule: if today IS expiry day (`>= tentative`), rolls to next month.
    """
    year = current_date.year
    month = current_date.month

    tentative = date(year, month, 26)
    is_past_or_expiry = (current_date >= tentative) if skip_0dte else (current_date > tentative)
    if is_past_or_expiry:
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
    strike_otm_pct: float = 0.015,         # 1.5% OTM (~15 Delta)
    sl_pct: float = 0.40,                  # 40% single-leg SL
    trail_to_cost: bool = True,
    trail_activation_decay_pct: float = 0.30,
    target_decay_pct: float = 0.70,
    single_leg_exit: bool = True,
    lots: int = 2,
    lot_size_kg: int = 5,                  # SILVERM = 5 kg/lot; SILVER = 30 kg/lot
    sl_per_lot: float = 3000.0,            # Portfolio max loss cap = lots * sl_per_lot (e.g. Rs 6,000)
    entry_time_str: str = "20:00",
    exit_time_str: str = "23:10",
    skip_0dte: bool = True,
    slippage_pts: float = 0.5,             # Round-trip MCX tick slippage per leg
    base_iv: float = 0.30,                 # 30% annualized IV
    evening_iv_crush: float = 0.02,        # 2% IV drop across evening session
):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["time"] = df["date"].dt.strftime("%H:%M")
    df["day"] = df["date"].dt.date

    # Find liquid days with daily volume > 1000 (or fallback to all days if volume column missing/synthetic)
    if "volume" in df.columns and df["volume"].sum() > 0:
        day_vol = df.groupby("day")["volume"].sum()
        active_days = sorted(day_vol[day_vol > 1000].index)
    else:
        active_days = sorted(df["day"].unique())

    multiplier = float(lots * lot_size_kg)
    max_loss_cap = round(float(sl_per_lot) * lots, 2) if sl_per_lot and sl_per_lot > 0 else None
    trade_logs = []

    for trade_day in active_days:
        day_df = df[df["day"] == trade_day].sort_values("date").reset_index(drop=True)

        # Evaluate candles in [entry_time_str, exit_time_str] (aligned with 20:00-23:10 IST desk window)
        evening_df = day_df[(day_df["time"] >= entry_time_str) & (day_df["time"] <= exit_time_str)].reset_index(drop=True)
        if len(evening_df) < 2:
            continue

        entry_row = evening_df.iloc[0]
        entry_time = entry_row["time"]
        if entry_time >= exit_time_str or entry_time > "20:10":
            continue

        spot_entry = float(entry_row["open"])
        entry_dt = entry_row["date"]
        expiry = get_monthly_expiry(trade_day, skip_0dte=skip_0dte)
        dte_entry = calculate_dte_fraction(entry_dt, expiry)

        # Strike Selection matched to live `chain.py`: step 1000 if spot > 120000 else 500
        strike_step = get_strike_step(spot_entry)
        ce_strike = round_to_strike_step(spot_entry * (1.0 + strike_otm_pct), strike_step)
        pe_strike = round_to_strike_step(spot_entry * (1.0 - strike_otm_pct), strike_step)

        # Initial Option Prices (Black-76)
        ce_entry = black_76_price(spot_entry, ce_strike, dte_entry, iv=base_iv, opt_type="CE")
        pe_entry = black_76_price(spot_entry, pe_strike, dte_entry, iv=base_iv, opt_type="PE")

        if ce_entry <= 0.5 or pe_entry <= 0.5:
            continue

        ce_sl = round(ce_entry * (1.0 + sl_pct), 2)
        pe_sl = round(pe_entry * (1.0 + sl_pct), 2)

        ce_status = "OPEN"
        pe_status = "OPEN"
        ce_cost_trail_pending = False
        pe_cost_trail_pending = False
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_time = None
        pe_exit_time = None
        ce_exit_reason = "EOD"
        pe_exit_reason = "EOD"

        total_evening_candles = len(evening_df)

        for idx, row in evening_df.iterrows():
            curr_dt = row["date"]
            curr_spot = float(row["close"])
            curr_high = float(row["high"])
            curr_low = float(row["low"])
            curr_time = row["time"]

            # Linear IV crush across the evening session
            prog = idx / max(1, total_evening_candles - 1)
            curr_iv = max(0.15, base_iv - evening_iv_crush * prog)
            curr_dte = calculate_dte_fraction(curr_dt, expiry)

            ce_curr_price = black_76_price(curr_spot, ce_strike, curr_dte, iv=curr_iv, opt_type="CE")
            pe_curr_price = black_76_price(curr_spot, pe_strike, curr_dte, iv=curr_iv, opt_type="PE")

            # 1. EOD Cutoff (aligned with desk.monitor_cycle)
            if curr_time >= exit_time_str or idx == total_evening_candles - 1:
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
                break

            # 2. Portfolio Max-Loss Cap (sl_per_lot * lots) evaluated on bar close
            ce_mark = ce_curr_price if ce_status == "OPEN" else ce_exit_price
            pe_mark = pe_curr_price if pe_status == "OPEN" else pe_exit_price
            curr_portfolio_pnl = ((ce_entry - ce_mark) + (pe_entry - pe_mark)) * multiplier
            if max_loss_cap is not None and curr_portfolio_pnl <= -max_loss_cap:
                if ce_status == "OPEN":
                    ce_status = "MAX_PORTFOLIO_LOSS"
                    ce_exit_price = ce_curr_price
                    ce_exit_time = curr_time
                    ce_exit_reason = "MAX_PORTFOLIO_LOSS"
                if pe_status == "OPEN":
                    pe_status = "MAX_PORTFOLIO_LOSS"
                    pe_exit_price = pe_curr_price
                    pe_exit_time = curr_time
                    pe_exit_reason = "MAX_PORTFOLIO_LOSS"
                break

            # Apply pending cost trails before intra-bar SL checks (CE favors low spot, PE favors high spot)
            if trail_to_cost and single_leg_exit:
                if ce_status == "OPEN" and ce_cost_trail_pending and ce_sl > ce_entry:
                    if black_76_price(curr_low, ce_strike, curr_dte, iv=curr_iv, opt_type="CE") < ce_entry:
                        ce_sl = ce_entry
                        ce_cost_trail_pending = False
                if pe_status == "OPEN" and pe_cost_trail_pending and pe_sl > pe_entry:
                    if black_76_price(curr_high, pe_strike, curr_dte, iv=curr_iv, opt_type="PE") < pe_entry:
                        pe_sl = pe_entry
                        pe_cost_trail_pending = False

            # High/Low worst-case price simulation for SL trigger
            ce_worst_price = black_76_price(curr_high, ce_strike, curr_dte, iv=curr_iv, opt_type="CE")
            pe_worst_price = black_76_price(curr_low, pe_strike, curr_dte, iv=curr_iv, opt_type="PE")

            # 3. Single-Leg Stop Losses (with co-exit when single_leg_exit=False)
            ce_stopped_this_bar = False
            pe_stopped_this_bar = False

            if ce_status == "OPEN" and ce_worst_price >= ce_sl:
                ce_status = "SL"
                ce_exit_price = round(ce_sl, 2)
                ce_exit_time = curr_time
                ce_exit_reason = "SL"
                ce_stopped_this_bar = True
                if not single_leg_exit and pe_status == "OPEN":
                    pe_status = "SL_CO_EXIT"
                    pe_exit_price = pe_curr_price
                    pe_exit_time = curr_time
                    pe_exit_reason = "SL_CO_EXIT"
                    break

            if pe_status == "OPEN" and not ce_stopped_this_bar and pe_worst_price >= pe_sl:
                pe_status = "SL"
                pe_exit_price = round(pe_sl, 2)
                pe_exit_time = curr_time
                pe_exit_reason = "SL"
                pe_stopped_this_bar = True
                if not single_leg_exit and ce_status == "OPEN":
                    ce_status = "SL_CO_EXIT"
                    ce_exit_price = ce_curr_price
                    ce_exit_time = curr_time
                    ce_exit_reason = "SL_CO_EXIT"
                    break

            # 4. Arm & Lock Cost Trailing (on SL stop-out OR >= 30% favorable decay)
            if trail_to_cost and single_leg_exit:
                if ce_stopped_this_bar and pe_status == "OPEN":
                    pe_cost_trail_pending = True
                if pe_stopped_this_bar and ce_status == "OPEN":
                    ce_cost_trail_pending = True

                if ce_status == "OPEN" and pe_status == "OPEN":
                    ce_decay = (ce_entry - ce_curr_price) / ce_entry
                    pe_decay = (pe_entry - pe_curr_price) / pe_entry
                    if ce_decay >= trail_activation_decay_pct:
                        pe_cost_trail_pending = True
                    if pe_decay >= trail_activation_decay_pct:
                        ce_cost_trail_pending = True

                if ce_status == "OPEN" and ce_cost_trail_pending and ce_sl > ce_entry and ce_curr_price < ce_entry:
                    ce_sl = ce_entry
                    ce_cost_trail_pending = False
                if pe_status == "OPEN" and pe_cost_trail_pending and pe_sl > pe_entry and pe_curr_price < pe_entry:
                    pe_sl = pe_entry
                    pe_cost_trail_pending = False

            # 5. Target Decay (70%): combined when dual-open OR single surviving leg
            if ce_status == "OPEN" and pe_status == "OPEN":
                combined_entry = ce_entry + pe_entry
                combined_curr = ce_curr_price + pe_curr_price
                if (combined_entry - combined_curr) / combined_entry >= target_decay_pct:
                    ce_status = "TARGET_DECAY"
                    pe_status = "TARGET_DECAY"
                    ce_exit_price = ce_curr_price
                    pe_exit_price = pe_curr_price
                    ce_exit_time = curr_time
                    pe_exit_time = curr_time
                    ce_exit_reason = "TARGET_DECAY"
                    pe_exit_reason = "TARGET_DECAY"
                    break
            else:
                if ce_status == "OPEN" and (ce_entry - ce_curr_price) / ce_entry >= target_decay_pct:
                    ce_status = "TARGET_DECAY"
                    ce_exit_price = ce_curr_price
                    ce_exit_time = curr_time
                    ce_exit_reason = "TARGET_DECAY"
                if pe_status == "OPEN" and (pe_entry - pe_curr_price) / pe_entry >= target_decay_pct:
                    pe_status = "TARGET_DECAY"
                    pe_exit_price = pe_curr_price
                    pe_exit_time = curr_time
                    pe_exit_reason = "TARGET_DECAY"

            if ce_status != "OPEN" and pe_status != "OPEN":
                break

        ce_pts = (ce_entry - ce_exit_price) - slippage_pts
        pe_pts = (pe_entry - pe_exit_price) - slippage_pts
        total_pts = ce_pts + pe_pts
        gross_pnl_inr = round(total_pts * multiplier, 2)
        statutory_costs_inr = round(
            calculate_silver_mcx_costs(ce_entry, multiplier, is_sell=True)
            + calculate_silver_mcx_costs(ce_exit_price, multiplier, is_sell=False)
            + calculate_silver_mcx_costs(pe_entry, multiplier, is_sell=True)
            + calculate_silver_mcx_costs(pe_exit_price, multiplier, is_sell=False),
            2,
        )
        net_pnl_inr = round(gross_pnl_inr - statutory_costs_inr, 2)

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
            "gross_pnl_inr": gross_pnl_inr,
            "costs_inr": statutory_costs_inr,
            "net_pnl_inr": net_pnl_inr,
            "dte": round(dte_entry, 1),
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
    win_days = (results["net_pnl_inr"] > 0).sum()
    loss_days = (results["net_pnl_inr"] <= 0).sum()
    win_rate = win_days / len(results) * 100

    avg_win = results[results["total_pts"] > 0]["total_pts"].mean()
    avg_loss = results[results["total_pts"] < 0]["total_pts"].mean()
    profit_factor = abs(results[results["net_pnl_inr"] > 0]["net_pnl_inr"].sum() / results[results["net_pnl_inr"] < 0]["net_pnl_inr"].sum()) if (results["net_pnl_inr"] < 0).any() else 999.0

    # Max Drawdown (Net INR)
    cum_net = results["net_pnl_inr"].cumsum()
    peak = cum_net.cummax()
    max_dd_inr = (peak - cum_net).max()

    print("\n--- OVERALL PERFORMANCE SUMMARY ---")
    print(f"Total Trading Sessions: {len(results)}")
    print(f"Win Sessions: {win_days} | Loss Sessions: {loss_days} | Win Rate: {win_rate:.1f}%")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Total Net Points Captured (after {0.5}pt/leg slippage): {total_pts:,.2f} pts")
    print(f"Average Points per Day: {total_pts / len(results):.2f} pts")
    print(f"Avg Win: +{avg_win:.2f} pts | Avg Loss: {avg_loss:.2f} pts")

    silverm_gross = results["gross_pnl_inr"].sum()
    silverm_costs = results["costs_inr"].sum()
    silverm_net = results["net_pnl_inr"].sum()

    results_big = run_strangle_backtest(df, lot_size_kg=30, sl_per_lot=18000.0)
    silver_gross = results_big["gross_pnl_inr"].sum()
    silver_costs = results_big["costs_inr"].sum()
    silver_net = results_big["net_pnl_inr"].sum()
    cum_big = results_big["net_pnl_inr"].cumsum()
    silver_max_dd = (cum_big.cummax() - cum_big).max()

    print("\n" + "=" * 60)
    print("      REAL MCX DATA BACKTEST RESULTS (2 LOTS)")
    print("=" * 60)
    print("SILVER MINI (SILVERM - 2 Lots = 10 kg, Rs 10/pt | 6k Max Loss):")
    print(f"  Gross P&L (after slippage): Rs {silverm_gross:,.2f}")
    print(f"  Exact MCX Taxes & Brokerage: -Rs {silverm_costs:,.2f}")
    print(f"  NET REALIZED P&L: Rs {silverm_net:,.2f}")
    print(f"  Max Drawdown: Rs {max_dd_inr:,.2f}")
    print("-" * 60)
    print("SILVER BIG (SILVER - 2 Lots = 60 kg, Rs 60/pt):")
    print(f"  Gross P&L (after slippage): Rs {silver_gross:,.2f}")
    print(f"  Exact MCX Taxes & Brokerage: -Rs {silver_costs:,.2f}")
    print(f"  NET REALIZED P&L: Rs {silver_net:,.2f}")
    print(f"  Max Drawdown: Rs {silver_max_dd:,.2f}")
    print("=" * 60)
