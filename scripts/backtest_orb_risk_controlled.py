import sys
from pathlib import Path
from datetime import datetime, time as dt_time
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backtest_indrajit_orb import TARGET_STOCKS, DATA_CACHE_DIR

# ---------------------------------------------------------------------------
# Data Preloading
# ---------------------------------------------------------------------------
nifty_file = DATA_CACHE_DIR / "NIFTY_50_15m.csv"
df_nifty = pd.read_csv(nifty_file, parse_dates=["date"])
df_nifty["d_only"] = df_nifty["date"].dt.date
df_nifty["time_only"] = df_nifty["date"].dt.time

nifty_day_map = {}
for d, g in df_nifty.groupby("d_only"):
    g_sorted = g.sort_values("date").reset_index(drop=True)
    if len(g_sorted) < 4:
        continue
    day_open = float(g_sorted.loc[0, "open"])
    bar_map = {row["time_only"]: float(row["close"]) for _, row in g_sorted.iterrows()}
    nifty_day_map[d] = {
        "day_open": day_open,
        "bars": bar_map
    }

stock_data = {}
for item in TARGET_STOCKS:
    sym = item["symbol"]
    clean_sym = sym.replace("&", "_")
    cache_file = DATA_CACHE_DIR / f"{clean_sym}_15m.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        df["d_only"] = df["date"].dt.date
        df["time_only"] = df["date"].dt.time
        stock_data[sym] = {
            "df": df,
            "lot": item["lot"]
        }

# Pre-calculate daily High, Low, Close for PDH / PDL across all stocks
daily_stock_stats = {}
all_days_set = set()
for sym, s_info in stock_data.items():
    daily_stock_stats[sym] = {}
    for d, g in s_info["df"].groupby("d_only"):
        daily_stock_stats[sym][d] = {
            "high": float(g["high"].max()),
            "low": float(g["low"].min()),
            "close": float(g["close"].iloc[-1])
        }
        all_days_set.add(d)

trading_days = sorted(list(all_days_set))


def run_portfolio_simulation(
    apply_wick_filter: bool = True,
    apply_pdh_pdl_filter: bool = True,
    apply_nifty_filter: bool = True,
    sizing_mode: str = "RISK_PARITY",  # "RISK_PARITY" or "FUTURES_LOT"
    fixed_risk_rs: float = 2500.0,
    max_trades_per_day: int = 3,       # Cap on total trades executed per day
    daily_loss_limit_rs: float = 7500.0, # Daily portfolio stop-loss circuit breaker
    max_sl_pct: float = 0.025
):
    """
    Simulates a time-synchronized, multi-stock trading desk across the 30 stocks.
    Evaluates:
      1. Fixed-Risk sizing (Risk Parity) vs Futures 1-lot.
      2. Daily max trades cap (first N valid triggered setups).
      3. Portfolio circuit breaker (if realized + unrealized loss reaches limit, close all and stop for day).
    """
    all_trades = []
    daily_summaries = []
    circuit_breaker_trips = 0

    for d_idx, d in enumerate(trading_days):
        prev_day = trading_days[d_idx - 1] if d_idx > 0 else None
        n_info = nifty_day_map.get(d)

        # Collect today's data for all 30 stocks
        day_stocks = {}
        for sym, s_info in stock_data.items():
            g = s_info["df"][s_info["df"]["d_only"] == d].sort_values("date").reset_index(drop=True)
            if len(g) >= 20:  # Must have full trading day
                day_stocks[sym] = {
                    "df": g,
                    "lot": s_info["lot"],
                    "ib_high": float(g.iloc[:4]["high"].max()),
                    "ib_low": float(g.iloc[:4]["low"].min()),
                    "pdh": daily_stock_stats[sym].get(prev_day, {}).get("high"),
                    "pdl": daily_stock_stats[sym].get(prev_day, {}).get("low")
                }

        if not day_stocks:
            continue

        # Timestamps for today from bar 4 (10:15) to bar 24 (15:15)
        # Use first available stock to get time index
        ref_df = list(day_stocks.values())[0]["df"]
        eval_indices = list(range(4, len(ref_df)))

        active_positions = []  # list of open trades
        pending_orders = []    # list of limit/stop entry triggers
        traded_today_stocks = set()
        day_closed_trades = []
        day_realized_pnl_rs = 0.0
        circuit_tripped = False

        for bar_idx in eval_indices:
            if circuit_tripped:
                break

            current_time = ref_df.iloc[bar_idx]["time_only"]
            is_eod_bar = (bar_idx == len(ref_df) - 1) or (current_time >= dt_time(15, 15))

            # -------------------------------------------------------------
            # STEP 1: Process and Update Existing Active Positions
            # -------------------------------------------------------------
            remaining_active = []
            for pos in active_positions:
                sym = pos["symbol"]
                stk = day_stocks[sym]
                bar = stk["df"].iloc[bar_idx]
                b_high, b_low, b_close = float(bar["high"]), float(bar["low"]), float(bar["close"])

                pos_closed = False
                pos_exit_reason = None
                pnl_rs = 0.0
                pnl_r = 0.0

                if pos["direction"] == "LONG":
                    # Check SL first
                    if b_low <= pos["current_sl"]:
                        pos_closed = True
                        if pos["half_booked"]:
                            rem_pnl_r = (pos["current_sl"] - pos["entry_trigger"]) / pos["risk"]
                            rem_pnl_rs = (pos["current_sl"] - pos["entry_trigger"]) * pos["half_qty"]
                            pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                            pnl_rs = pos["half_pnl_rs"] + rem_pnl_rs
                            pos_exit_reason = "TARGET_2.5_THEN_BE"
                        else:
                            pnl_r = (pos["current_sl"] - pos["entry_trigger"]) / pos["risk"]
                            pnl_rs = (pos["current_sl"] - pos["entry_trigger"]) * pos["qty"]
                            pos_exit_reason = "BREAKEVEN" if pos["be_moved"] else "STOP_LOSS"

                    else:
                        # Check Target 2.5R
                        if b_high >= pos["target_2_5r"] and not pos["half_booked"]:
                            pos["half_booked"] = True
                            pos["half_pnl_rs"] = (pos["target_2_5r"] - pos["entry_trigger"]) * pos["half_qty"]

                        # Check 1:1 Trail to BE
                        if b_high >= pos["target_1r"] and not pos["be_moved"]:
                            pos["be_moved"] = True
                            pos["current_sl"] = pos["entry_trigger"]

                        # Check EOD
                        if is_eod_bar:
                            pos_closed = True
                            rem_pnl_r = (b_close - pos["entry_trigger"]) / pos["risk"]
                            rem_pnl_rs = (b_close - pos["entry_trigger"]) * (pos["half_qty"] if pos["half_booked"] else pos["qty"])
                            if pos["half_booked"]:
                                pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                                pnl_rs = pos["half_pnl_rs"] + rem_pnl_rs
                                pos_exit_reason = "TARGET_2.5_THEN_EOD"
                            else:
                                pnl_r = rem_pnl_r
                                pnl_rs = rem_pnl_rs
                                pos_exit_reason = "EOD_SQUAREOFF"

                else:  # SHORT
                    if b_high >= pos["current_sl"]:
                        pos_closed = True
                        if pos["half_booked"]:
                            rem_pnl_r = (pos["entry_trigger"] - pos["current_sl"]) / pos["risk"]
                            rem_pnl_rs = (pos["entry_trigger"] - pos["current_sl"]) * pos["half_qty"]
                            pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                            pnl_rs = pos["half_pnl_rs"] + rem_pnl_rs
                            pos_exit_reason = "TARGET_2.5_THEN_BE"
                        else:
                            pnl_r = (pos["entry_trigger"] - pos["current_sl"]) / pos["risk"]
                            pnl_rs = (pos["entry_trigger"] - pos["current_sl"]) * pos["qty"]
                            pos_exit_reason = "BREAKEVEN" if pos["be_moved"] else "STOP_LOSS"

                    else:
                        if b_low <= pos["target_2_5r"] and not pos["half_booked"]:
                            pos["half_booked"] = True
                            pos["half_pnl_rs"] = (pos["entry_trigger"] - pos["target_2_5r"]) * pos["half_qty"]

                        if b_low <= pos["target_1r"] and not pos["be_moved"]:
                            pos["be_moved"] = True
                            pos["current_sl"] = pos["entry_trigger"]

                        if is_eod_bar:
                            pos_closed = True
                            rem_pnl_r = (pos["entry_trigger"] - b_close) / pos["risk"]
                            rem_pnl_rs = (pos["entry_trigger"] - b_close) * (pos["half_qty"] if pos["half_booked"] else pos["qty"])
                            if pos["half_booked"]:
                                pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                                pnl_rs = pos["half_pnl_rs"] + rem_pnl_rs
                                pos_exit_reason = "TARGET_2.5_THEN_EOD"
                            else:
                                pnl_r = rem_pnl_r
                                pnl_rs = rem_pnl_rs
                                pos_exit_reason = "EOD_SQUAREOFF"

                if pos_closed:
                    day_realized_pnl_rs += pnl_rs
                    trade_rec = {
                        "date": str(d),
                        "symbol": sym,
                        "direction": pos["direction"],
                        "entry_time": str(pos["entry_time"]),
                        "exit_time": str(current_time),
                        "entry_trigger": pos["entry_trigger"],
                        "risk": pos["risk"],
                        "qty": pos["qty"],
                        "total_pnl_r": round(pnl_r, 2),
                        "total_pnl_rs": round(pnl_rs, 2),
                        "final_exit_reason": pos_exit_reason
                    }
                    day_closed_trades.append(trade_rec)
                    all_trades.append(trade_rec)
                else:
                    remaining_active.append(pos)

            active_positions = remaining_active

            # -------------------------------------------------------------
            # STEP 2: Circuit Breaker Check (Realized + Unrealized MTM)
            # -------------------------------------------------------------
            unrealized_mtm_rs = 0.0
            for pos in active_positions:
                sym = pos["symbol"]
                bar = day_stocks[sym]["df"].iloc[bar_idx]
                b_close = float(bar["close"])
                if pos["direction"] == "LONG":
                    cur_diff = b_close - pos["entry_trigger"]
                else:
                    cur_diff = pos["entry_trigger"] - b_close
                
                rem_qty = pos["half_qty"] if pos["half_booked"] else pos["qty"]
                unrealized_mtm_rs += pos.get("half_pnl_rs", 0.0) + (cur_diff * rem_qty)

            total_day_mtm = day_realized_pnl_rs + unrealized_mtm_rs

            if daily_loss_limit_rs > 0 and total_day_mtm <= -daily_loss_limit_rs:
                # TRIP CIRCUIT BREAKER!
                circuit_tripped = True
                circuit_breaker_trips += 1
                # Emergency square-off all active positions at current bar close
                for pos in active_positions:
                    sym = pos["symbol"]
                    bar = day_stocks[sym]["df"].iloc[bar_idx]
                    b_close = float(bar["close"])
                    if pos["direction"] == "LONG":
                        rem_pnl_r = (b_close - pos["entry_trigger"]) / pos["risk"]
                        rem_pnl_rs = (b_close - pos["entry_trigger"]) * (pos["half_qty"] if pos["half_booked"] else pos["qty"])
                    else:
                        rem_pnl_r = (pos["entry_trigger"] - b_close) / pos["risk"]
                        rem_pnl_rs = (pos["entry_trigger"] - b_close) * (pos["half_qty"] if pos["half_booked"] else pos["qty"])

                    if pos["half_booked"]:
                        pnl_r = 0.5 * 2.5 + 0.5 * rem_pnl_r
                        pnl_rs = pos["half_pnl_rs"] + rem_pnl_rs
                    else:
                        pnl_r = rem_pnl_r
                        pnl_rs = rem_pnl_rs

                    day_realized_pnl_rs += pnl_rs
                    trade_rec = {
                        "date": str(d),
                        "symbol": sym,
                        "direction": pos["direction"],
                        "entry_time": str(pos["entry_time"]),
                        "exit_time": str(current_time),
                        "entry_trigger": pos["entry_trigger"],
                        "risk": pos["risk"],
                        "qty": pos["qty"],
                        "total_pnl_r": round(pnl_r, 2),
                        "total_pnl_rs": round(pnl_rs, 2),
                        "final_exit_reason": "CIRCUIT_BREAKER_HALT"
                    }
                    day_closed_trades.append(trade_rec)
                    all_trades.append(trade_rec)

                active_positions = []
                pending_orders = []
                break  # Stop processing this day completely

            # -------------------------------------------------------------
            # STEP 3: Process Pending Entry Orders on this bar
            # -------------------------------------------------------------
            remaining_pending = []
            for order in pending_orders:
                sym = order["symbol"]
                stk = day_stocks[sym]
                bar = stk["df"].iloc[bar_idx]
                b_high, b_low = float(bar["high"]), float(bar["low"])

                # Check if day trade limit reached
                trades_taken_today = len(day_closed_trades) + len(active_positions)
                if trades_taken_today >= max_trades_per_day:
                    continue  # Discard order as cap reached

                filled = False
                if order["direction"] == "LONG" and b_high >= order["entry_trigger"]:
                    filled = True
                elif order["direction"] == "SHORT" and b_low <= order["entry_trigger"]:
                    filled = True

                if filled:
                    risk_per_share = order["risk"]
                    if sizing_mode == "RISK_PARITY":
                        qty = max(1, int(fixed_risk_rs / risk_per_share))
                    else:  # FUTURES_LOT
                        qty = stk["lot"]

                    new_pos = {
                        "symbol": sym,
                        "direction": order["direction"],
                        "entry_time": current_time,
                        "entry_trigger": order["entry_trigger"],
                        "initial_sl": order["initial_sl"],
                        "current_sl": order["initial_sl"],
                        "risk": risk_per_share,
                        "qty": qty,
                        "half_qty": qty / 2.0,
                        "target_1r": order["entry_trigger"] + risk_per_share if order["direction"] == "LONG" else order["entry_trigger"] - risk_per_share,
                        "target_2_5r": order["entry_trigger"] + 2.5 * risk_per_share if order["direction"] == "LONG" else order["entry_trigger"] - 2.5 * risk_per_share,
                        "half_booked": False,
                        "half_pnl_rs": 0.0,
                        "be_moved": False
                    }
                    active_positions.append(new_pos)
                    traded_today_stocks.add(sym)
                else:
                    # Decrement validity bars
                    order["bars_valid"] -= 1
                    if order["bars_valid"] > 0:
                        remaining_pending.append(order)

            pending_orders = remaining_pending

            # -------------------------------------------------------------
            # STEP 4: Scan for New Breakout Triggers on Current Bar
            # (Only allowed up to bar 16, i.e., 13:15, and if cap not reached)
            # -------------------------------------------------------------
            trades_taken_today = len(day_closed_trades) + len(active_positions)
            if bar_idx <= 16 and trades_taken_today < max_trades_per_day:
                candidate_orders = []
                for sym, stk in day_stocks.items():
                    if sym in traded_today_stocks:
                        continue
                    if any(o["symbol"] == sym for o in pending_orders):
                        continue

                    bar = stk["df"].iloc[bar_idx]
                    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
                    c_range = h - l
                    if c_range <= 0 or (c_range / c) > max_sl_pct:
                        continue

                    direction = None
                    wick_ratio = 0.0
                    if c > stk["ib_high"]:
                        upper_wick = h - c
                        wick_ratio = upper_wick / c_range
                        if (not apply_wick_filter) or (wick_ratio <= 0.20):
                            direction = "LONG"
                    elif c < stk["ib_low"]:
                        lower_wick = c - l
                        wick_ratio = lower_wick / c_range
                        if (not apply_wick_filter) or (wick_ratio <= 0.20):
                            direction = "SHORT"

                    if not direction:
                        continue

                    # PDH / PDL Filter
                    if apply_pdh_pdl_filter and stk["pdh"] is not None and stk["pdl"] is not None:
                        if direction == "LONG" and c < stk["pdh"]:
                            continue
                        elif direction == "SHORT" and c > stk["pdl"]:
                            continue

                    # NIFTY Direction Filter
                    if apply_nifty_filter and n_info is not None:
                        n_close = n_info["bars"].get(current_time)
                        if n_close is not None:
                            if direction == "LONG" and n_close < n_info["day_open"]:
                                continue
                            elif direction == "SHORT" and n_close > n_info["day_open"]:
                                continue

                    entry_trigger = h + 0.05 if direction == "LONG" else l - 0.05
                    initial_sl = l - 0.05 if direction == "LONG" else h + 0.05
                    risk = abs(entry_trigger - initial_sl)
                    if risk <= 0:
                        continue

                    candidate_orders.append({
                        "symbol": sym,
                        "direction": direction,
                        "entry_trigger": entry_trigger,
                        "initial_sl": initial_sl,
                        "risk": risk,
                        "wick_ratio": wick_ratio,
                        "bars_valid": 2
                    })

                # Sort candidate orders by cleanest wick ratio (strongest momentum conviction)
                candidate_orders.sort(key=lambda x: x["wick_ratio"])
                # Add up to available capacity
                slots_left = max_trades_per_day - (len(day_closed_trades) + len(active_positions) + len(pending_orders))
                if slots_left > 0:
                    pending_orders.extend(candidate_orders[:slots_left])

        # Record daily summary
        daily_summaries.append({
            "date": str(d),
            "trades_count": len(day_closed_trades),
            "pnl_rs": day_realized_pnl_rs,
            "circuit_tripped": circuit_tripped
        })

    return pd.DataFrame(all_trades), pd.DataFrame(daily_summaries), circuit_breaker_trips


def calc_stats(df_trades, df_daily):
    if df_trades.empty or df_daily.empty:
        return {}

    wins = df_trades[df_trades["total_pnl_r"] > 0]
    losses = df_trades[df_trades["total_pnl_r"] < -0.05]
    be = df_trades[(df_trades["total_pnl_r"] >= -0.05) & (df_trades["total_pnl_r"] <= 0.05)]

    win_rate = len(wins) / len(df_trades) * 100 if len(df_trades) > 0 else 0
    total_profit = wins["total_pnl_rs"].sum()
    total_loss = abs(losses["total_pnl_rs"].sum())
    profit_factor = total_profit / total_loss if total_loss > 0 else np.nan

    df_daily["cum_rs"] = df_daily["pnl_rs"].cumsum()
    df_daily["peak_rs"] = df_daily["cum_rs"].cummax()
    df_daily["dd_rs"] = df_daily["cum_rs"] - df_daily["peak_rs"]
    max_daily_dd = df_daily["dd_rs"].min()

    worst_day = df_daily["pnl_rs"].min()
    best_day = df_daily["pnl_rs"].max()
    winning_days = len(df_daily[df_daily["pnl_rs"] > 0])
    losing_days = len(df_daily[df_daily["pnl_rs"] < 0])

    return {
        "trades": len(df_trades),
        "win_rate": win_rate,
        "wins": len(wins),
        "losses": len(losses),
        "be": len(be),
        "total_pnl_rs": df_trades["total_pnl_rs"].sum(),
        "total_pnl_r": df_trades["total_pnl_r"].sum(),
        "profit_factor": profit_factor,
        "max_daily_dd": max_daily_dd,
        "worst_day": worst_day,
        "best_day": best_day,
        "winning_days": winning_days,
        "losing_days": losing_days,
        "daily_win_rate": winning_days / len(df_daily) * 100
    }


if __name__ == "__main__":
    print("=" * 90)
    print("  ORB STRATEGY WITH 3 RISK GUARDRAILS: BACKTEST COMPARISON (83 DAYS, 30 STOCKS)")
    print("  Guardrail 1: Risk Parity Position Sizing (Fixed Rs Risk/trade)")
    print("  Guardrail 2: Max Active Trades / Day Cap (e.g. 3 or 5)")
    print("  Guardrail 3: Daily Portfolio Stop-Loss Circuit Breaker (e.g. Rs 7.5k / 10k / 15k)")
    print("=" * 90)

    # Test Scenarios
    scenarios = [
        {
            "name": "A0. Unconstrained 1-Lot Futures (Option B Baseline)",
            "sizing": "FUTURES_LOT",
            "fixed_risk": 0,
            "max_trades": 30,
            "daily_sl": 0,
            "pdh": True,
            "nifty": True
        },
        {
            "name": "A1. Futures 1-Lot: Max 3 Trades/Day + Rs 15k Daily SL",
            "sizing": "FUTURES_LOT",
            "fixed_risk": 0,
            "max_trades": 3,
            "daily_sl": 15000,
            "pdh": True,
            "nifty": True
        },
        {
            "name": "A2. Futures 1-Lot: Max 5 Trades/Day + Rs 20k Daily SL",
            "sizing": "FUTURES_LOT",
            "fixed_risk": 0,
            "max_trades": 5,
            "daily_sl": 20000,
            "pdh": True,
            "nifty": True
        },
        {
            "name": "B1. Risk-Parity Rs 2,500 Risk: Max 3 Trades/Day + Rs 7,500 Daily SL",
            "sizing": "RISK_PARITY",
            "fixed_risk": 2500,
            "max_trades": 3,
            "daily_sl": 7500,
            "pdh": True,
            "nifty": True
        },
        {
            "name": "B2. Risk-Parity Rs 2,500 Risk: Max 5 Trades/Day + Rs 10,000 Daily SL",
            "sizing": "RISK_PARITY",
            "fixed_risk": 2500,
            "max_trades": 5,
            "daily_sl": 10000,
            "pdh": True,
            "nifty": True
        },
        {
            "name": "C1. Risk-Parity Rs 5,000 Risk: Max 3 Trades/Day + Rs 15,000 Daily SL",
            "sizing": "RISK_PARITY",
            "fixed_risk": 5000,
            "max_trades": 3,
            "daily_sl": 15000,
            "pdh": True,
            "nifty": True
        },
        {
            "name": "C2. Risk-Parity Rs 5,000 Risk: Max 5 Trades/Day + Rs 20,000 Daily SL",
            "sizing": "RISK_PARITY",
            "fixed_risk": 5000,
            "max_trades": 5,
            "daily_sl": 20000,
            "pdh": True,
            "nifty": True
        },
        # Also check Option A (NIFTY Day Open only without PDH/PDL) with Risk Parity Rs 2,500
        {
            "name": "D1. Option A (No PDH/PDL) Risk-Parity Rs 2.5k: Max 3 Trades/Day + Rs 7.5k SL",
            "sizing": "RISK_PARITY",
            "fixed_risk": 2500,
            "max_trades": 3,
            "daily_sl": 7500,
            "pdh": False,
            "nifty": True
        },
        {
            "name": "D2. Option A (No PDH/PDL) Risk-Parity Rs 2.5k: Max 5 Trades/Day + Rs 10k SL",
            "sizing": "RISK_PARITY",
            "fixed_risk": 2500,
            "max_trades": 5,
            "daily_sl": 10000,
            "pdh": False,
            "nifty": True
        }
    ]

    results = []
    for sc in scenarios:
        df_tr, df_day, cb_trips = run_portfolio_simulation(
            apply_wick_filter=True,
            apply_pdh_pdl_filter=sc["pdh"],
            apply_nifty_filter=sc["nifty"],
            sizing_mode=sc["sizing"],
            fixed_risk_rs=sc["fixed_risk"],
            max_trades_per_day=sc["max_trades"],
            daily_loss_limit_rs=sc["daily_sl"]
        )
        st = calc_stats(df_tr, df_day)
        st["name"] = sc["name"]
        st["cb_trips"] = cb_trips
        results.append(st)

    print(f"\n{'Scenario':<55} | {'Trades':<6} | {'Win %':<6} | {'PF':<5} | {'Net P&L':<13} | {'Max DD':<12} | {'Worst Day':<11} | {'CB Trips':<8}")
    print("-" * 125)
    for r in results:
        print(f"{r['name']:<55} | {r['trades']:>6} | {r['win_rate']:>5.1f}% | {r['profit_factor']:>5.2f} | Rs {r['total_pnl_rs']:>9,.0f} | Rs {r['max_daily_dd']:>9,.0f} | Rs {r['worst_day']:>9,.0f} | {r['cb_trips']:>8}")
    print("=" * 125)
