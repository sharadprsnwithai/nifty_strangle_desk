import logging
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Tuple
import pandas as pd
from strategy.options_model import compute_fractional_dte, black_76_price
from strategy.costs import calculate_trade_costs

logger = logging.getLogger("strategy.backtest")


def get_weekly_expiry_for_date(d: date, expiry_weekday: int = 1) -> Tuple[date, bool]:
    """
    Returns (target_expiry, is_zero_dte).
    NSE NIFTY weekly expiry is Tuesday (weekday 1).
    If d is Tuesday, is_zero_dte=True -> target is next Tuesday (d + 7).
    Otherwise, target is current/upcoming Tuesday.
    """
    days_to_expiry = (expiry_weekday - d.weekday()) % 7
    current_expiry = d + timedelta(days=days_to_expiry)
    if d == current_expiry:
        # 0 DTE: shift to next week
        return d + timedelta(days=7), True
    return current_expiry, False


def select_strike_above_target(
    spot: float,
    dte_days: float,
    iv: float,
    opt_type: str,
    target_price: float = 30.0,
    r: float = 0.065
) -> Tuple[float, float]:
    """Finds strike in steps of 50 where theoretical price >= target_price (closest from above)."""
    atm_strike = round(spot / 50.0) * 50.0
    valid_strikes = []
    opt_type_upper = opt_type.upper()

    if opt_type_upper == "CE":
        # Search upward OTM strikes
        for s in range(int(atm_strike), int(atm_strike + 2500), 50):
            p = black_76_price(spot, float(s), dte_days, iv, r, "CE")
            if p >= target_price:
                valid_strikes.append((p, float(s)))
        if not valid_strikes:
            fallback_strike = atm_strike + 50.0
            return fallback_strike, black_76_price(spot, fallback_strike, dte_days, iv, r, "CE")
        valid_strikes.sort(key=lambda x: x[0])
        return valid_strikes[0][1], valid_strikes[0][0]
    else:
        # Search downward OTM strikes
        for s in range(int(atm_strike), int(atm_strike - 2500), -50):
            p = black_76_price(spot, float(s), dte_days, iv, r, "PE")
            if p >= target_price:
                valid_strikes.append((p, float(s)))
        if not valid_strikes:
            fallback_strike = atm_strike - 50.0
            return fallback_strike, black_76_price(spot, fallback_strike, dte_days, iv, r, "PE")
        valid_strikes.sort(key=lambda x: x[0])
        return valid_strikes[0][1], valid_strikes[0][0]


def run_backtest_simulation(
    bars_df: pd.DataFrame,
    slippage: float = 0.50,
    lots: int = 10,
    lot_size: int = 65,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1
) -> Dict[str, Any]:
    """Runs day-by-day historical simulation of the 10:00 AM short strangle."""
    df = bars_df.copy()
    if df.empty:
        return {
            "summary": {
                "total_days_traded": 0,
                "win_days": 0,
                "loss_days": 0,
                "win_rate_pct": 0.0,
                "gross_pnl": 0.0,
                "net_pnl": 0.0,
                "total_taxes_and_brokerage": 0.0,
                "total_slippage": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "avg_daily_net": 0.0
            },
            "trades": [],
            "monthly": []
        }

    if "timestamps" in df.columns:
        df["dt"] = pd.to_datetime(df["timestamps"])
    elif "date" in df.columns:
        df["dt"] = pd.to_datetime(df["date"])
    else:
        df["dt"] = pd.to_datetime(df.index)

    df["trade_date"] = df["dt"].dt.date
    trade_dates = sorted(df["trade_date"].unique())

    total_qty = lots * lot_size
    trade_logs: List[Dict[str, Any]] = []

    for t_date in trade_dates:
        day_df = df[df["trade_date"] == t_date].sort_values("dt")

        # Locate entry candle around 10:00 AM (between 09:55 and 10:05)
        entry_bars = day_df[(day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute <= 5)]
        if entry_bars.empty:
            entry_bars = day_df[(day_df["dt"].dt.hour == 9) & (day_df["dt"].dt.minute >= 55)]
        if entry_bars.empty:
            continue

        entry_bar = entry_bars.iloc[0]
        entry_time = entry_bar["dt"]
        spot_10am = float(entry_bar["open"])

        target_expiry, is_0dte = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)
        entry_dte = compute_fractional_dte(target_expiry, entry_time)

        # Select CE & PE strikes >= 30.0
        ce_strike, ce_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "CE", 30.0, r)
        pe_strike, pe_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "PE", 30.0, r)

        # Theoretical prices at entry
        ce_entry = ce_theo_price
        pe_entry = pe_theo_price

        ce_sl = round(ce_entry * 1.50, 2)
        ce_target = round(ce_entry * 0.20, 2)
        pe_sl = round(pe_entry * 1.50, 2)
        pe_target = round(pe_entry * 0.20, 2)

        # Track legs throughout the day
        intraday_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= time(15, 0))]

        ce_active = True
        pe_active = True
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_reason = "TIME_EXIT"
        pe_exit_reason = "TIME_EXIT"
        ce_exit_time = entry_time
        pe_exit_time = entry_time

        for _, bar in intraday_bars.iterrows():
            curr_dt = bar["dt"]
            curr_dte = compute_fractional_dte(target_expiry, curr_dt)
            bar_high_spot = float(bar["high"])
            bar_low_spot = float(bar["low"])

            # Evaluate CE
            if ce_active:
                ce_max_price = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                ce_min_price = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")

                if ce_max_price >= ce_sl:
                    ce_active = False
                    ce_exit_price = ce_sl
                    ce_exit_reason = "STOP_LOSS"
                    ce_exit_time = curr_dt
                elif ce_min_price <= ce_target:
                    ce_active = False
                    ce_exit_price = ce_target
                    ce_exit_reason = "DECAY_TARGET"
                    ce_exit_time = curr_dt

            # Evaluate PE
            if pe_active:
                pe_max_price = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                pe_min_price = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")

                if pe_max_price >= pe_sl:
                    pe_active = False
                    pe_exit_price = pe_sl
                    pe_exit_reason = "STOP_LOSS"
                    pe_exit_time = curr_dt
                elif pe_min_price <= pe_target:
                    pe_active = False
                    pe_exit_price = pe_target
                    pe_exit_reason = "DECAY_TARGET"
                    pe_exit_time = curr_dt

            if not ce_active and not pe_active:
                break

        # If still active at 3:00 PM, exit at market price
        if ce_active:
            exit_bar = intraday_bars.iloc[-1] if not intraday_bars.empty else entry_bar
            curr_dte = compute_fractional_dte(target_expiry, exit_bar["dt"])
            ce_exit_price = round(black_76_price(float(exit_bar["close"]), ce_strike, curr_dte, iv, r, "CE"), 2)
            ce_exit_time = exit_bar["dt"]

        if pe_active:
            exit_bar = intraday_bars.iloc[-1] if not intraday_bars.empty else entry_bar
            curr_dte = compute_fractional_dte(target_expiry, exit_bar["dt"])
            pe_exit_price = round(black_76_price(float(exit_bar["close"]), pe_strike, curr_dte, iv, r, "PE"), 2)
            pe_exit_time = exit_bar["dt"]

        ce_cost = calculate_trade_costs(ce_entry, ce_exit_price, total_qty, slippage_per_share=slippage)
        pe_cost = calculate_trade_costs(pe_entry, pe_exit_price, total_qty, slippage_per_share=slippage)

        day_gross_pnl = round(ce_cost["gross_pnl"] + pe_cost["gross_pnl"], 2)
        day_taxes = round(ce_cost["statutory_taxes"] + pe_cost["statutory_taxes"], 2)
        day_slippage = round(ce_cost["total_slippage"] + pe_cost["total_slippage"], 2)
        day_net_pnl = round(ce_cost["net_pnl"] + pe_cost["net_pnl"], 2)

        trade_logs.append({
            "date": str(t_date),
            "spot_10am": spot_10am,
            "expiry": str(target_expiry),
            "is_0dte": is_0dte,
            "ce_strike": ce_strike,
            "ce_entry": ce_entry,
            "ce_exit": ce_exit_price,
            "ce_reason": ce_exit_reason,
            "ce_pnl": ce_cost["net_pnl"],
            "pe_strike": pe_strike,
            "pe_entry": pe_entry,
            "pe_exit": pe_exit_price,
            "pe_reason": pe_exit_reason,
            "pe_pnl": pe_cost["net_pnl"],
            "gross_pnl": day_gross_pnl,
            "taxes": day_taxes,
            "slippage": day_slippage,
            "net_pnl": day_net_pnl
        })

    # Performance Analytics Aggregation
    trades_df = pd.DataFrame(trade_logs)
    if trades_df.empty:
        return {
            "summary": {
                "total_days_traded": 0,
                "win_days": 0,
                "loss_days": 0,
                "win_rate_pct": 0.0,
                "gross_pnl": 0.0,
                "net_pnl": 0.0,
                "total_taxes_and_brokerage": 0.0,
                "total_slippage": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "avg_daily_net": 0.0
            },
            "trades": [],
            "monthly": []
        }

    total_days = len(trades_df)
    win_days = int((trades_df["net_pnl"] > 0).sum())
    loss_days = int((trades_df["net_pnl"] < 0).sum())
    win_rate = round((win_days / total_days) * 100.0, 1) if total_days else 0.0

    total_gross = round(float(trades_df["gross_pnl"].sum()), 2)
    total_net = round(float(trades_df["net_pnl"].sum()), 2)
    total_taxes = round(float(trades_df["taxes"].sum()), 2)
    total_slip = round(float(trades_df["slippage"].sum()), 2)

    gross_wins = float(trades_df[trades_df["gross_pnl"] > 0]["gross_pnl"].sum())
    gross_losses = abs(float(trades_df[trades_df["gross_pnl"] < 0]["gross_pnl"].sum()))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0

    # Drawdown
    cum_net = pd.concat([pd.Series([0.0]), trades_df["net_pnl"]]).cumsum()
    peak = cum_net.cummax()
    max_dd = round(float((cum_net - peak).min()), 2)

    # Monthly aggregation
    trades_df["month"] = pd.to_datetime(trades_df["date"]).dt.strftime("%Y-%m")
    monthly_summary = trades_df.groupby("month")["net_pnl"].sum().round(2).reset_index().to_dict(orient="records")

    return {
        "summary": {
            "total_days_traded": total_days,
            "win_days": win_days,
            "loss_days": loss_days,
            "win_rate_pct": win_rate,
            "gross_pnl": total_gross,
            "net_pnl": total_net,
            "total_taxes_and_brokerage": total_taxes,
            "total_slippage": total_slip,
            "profit_factor": profit_factor,
            "max_drawdown": max_dd,
            "avg_daily_net": round(total_net / total_days, 2) if total_days else 0.0
        },
        "trades": trade_logs,
        "monthly": monthly_summary
    }
