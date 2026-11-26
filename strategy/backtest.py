import logging
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Tuple, Optional
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


def calculate_daily_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Computes Daily ATR on Nifty 5m bars aggregated to daily."""
    df_copy = df.copy()
    if "date" in df_copy.columns:
        df_copy["dt"] = pd.to_datetime(df_copy["date"])
    elif "timestamps" in df_copy.columns:
        df_copy["dt"] = pd.to_datetime(df_copy["timestamps"])
    else:
        df_copy["dt"] = pd.to_datetime(df_copy.index)
    df_copy["trade_date"] = df_copy["dt"].dt.date

    daily = df_copy.groupby("trade_date").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).sort_index()

    prev_close = daily["close"].shift(1)
    tr1 = daily["high"] - daily["low"]
    tr2 = (daily["high"] - prev_close).abs()
    tr3 = (daily["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Shift by 1 day so 10:00 AM entry uses only prior completed days' ATR (no lookahead bias)
    atr = tr.rolling(period, min_periods=1).mean().shift(1)
    return atr


def run_backtest_simulation(
    bars_df: pd.DataFrame,
    slippage: float = 0.50,
    lots: int = 10,
    lot_size: int = 65,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
    strategy_max_loss: Optional[float] = 0.0,
    use_leg_sl: bool = True,
    leg_sl_pct: float = 0.70,
    trail_opposite_to_cost: bool = False,
    roll_untested: bool = True,
    entry_time: time = time(10, 0),
    exit_cutoff_time: time = time(15, 15),
    morning_filter: bool = False,
    morning_filter_threshold_pct: float = 0.60,
    use_atr_strikes: bool = False,
    atr_multiplier: float = 1.25,
    target_price: float = 30.0
) -> Dict[str, Any]:
    """
    Runs day-by-day historical simulation of the 10:00 AM short strangle.
    Defaults to Adaptive model (1.25x ATR Strikes + Morning Range Trend Filter).
    """
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

    # Pre-calculate daily ATR if ATR strike selection enabled
    daily_atr = calculate_daily_atr(df, period=14) if use_atr_strikes else None

    for t_date in trade_dates:
        day_df = df[df["trade_date"] == t_date].sort_values("dt")

        # Evaluate Morning Range Trend Filter strictly before entry_time (e.g. 09:15 to 09:59)
        if morning_filter:
            morning_bars = day_df[day_df["dt"].dt.time < entry_time]
            if not morning_bars.empty:
                m_open = float(morning_bars.iloc[0]["open"])
                m_high = float(morning_bars["high"].max())
                m_low = float(morning_bars["low"].min())
                m_range = m_high - m_low
                m_range_pct = (m_range / m_open) * 100.0 if m_open > 0 else 0.0
                if m_range_pct >= morning_filter_threshold_pct:
                    continue  # Skip violent trend session

        # Locate entry candle around entry_time (within +5m, fallback -5m)
        entry_end_t = (datetime.combine(t_date, entry_time) + timedelta(minutes=5)).time()
        entry_start_t = (datetime.combine(t_date, entry_time) - timedelta(minutes=5)).time()
        entry_bars = day_df[(day_df["dt"].dt.time >= entry_time) & (day_df["dt"].dt.time <= entry_end_t)]
        if entry_bars.empty:
            entry_bars = day_df[(day_df["dt"].dt.time >= entry_start_t) & (day_df["dt"].dt.time < entry_time)]
        if entry_bars.empty:
            continue

        entry_bar = entry_bars.iloc[0]
        entry_dt = entry_bar["dt"]
        spot_10am = float(entry_bar["open"])

        target_expiry, is_0dte = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)
        entry_dte = compute_fractional_dte(target_expiry, entry_dt)

        # Strike Selection: Dynamic ATR or Static Price
        if use_atr_strikes:
            current_atr = daily_atr.get(t_date, 150.0) if daily_atr is not None else 150.0
            if pd.isna(current_atr) or current_atr <= 0:
                current_atr = 150.0
            strike_offset = max(current_atr * atr_multiplier, 150.0)
            ce_strike = round((spot_10am + strike_offset) / 50.0) * 50.0
            pe_strike = round((spot_10am - strike_offset) / 50.0) * 50.0
            ce_entry = black_76_price(spot_10am, ce_strike, entry_dte, iv, r, "CE")
            pe_entry = black_76_price(spot_10am, pe_strike, entry_dte, iv, r, "PE")
        else:
            ce_strike, ce_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "CE", target_price, r)
            pe_strike, pe_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "PE", target_price, r)
            ce_entry = ce_theo_price
            pe_entry = pe_theo_price

        ce_sl = round(ce_entry * (1.0 + leg_sl_pct), 2)
        ce_target = round(ce_entry * 0.20, 2)
        pe_sl = round(pe_entry * (1.0 + leg_sl_pct), 2)
        pe_target = round(pe_entry * 0.20, 2)

        # Track legs throughout the day (including the entry bar from open -> close, matching live monitoring)
        intraday_bars = day_df[(day_df["dt"] >= entry_dt) & (day_df["dt"].dt.time <= exit_cutoff_time)]

        ce_active = True
        pe_active = True
        ce_cost_trail_pending = False
        pe_cost_trail_pending = False
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_reason = "TIME_EXIT"
        pe_exit_reason = "TIME_EXIT"
        ce_exit_time = entry_dt
        pe_exit_time = entry_dt
        rolled_today = False
        roll_gross_pnl = 0.0
        roll_taxes = 0.0
        roll_slippage = 0.0

        for _, bar in intraday_bars.iterrows():
            curr_dt = bar["dt"]
            curr_dte = compute_fractional_dte(target_expiry, curr_dt)
            bar_high_spot = float(bar["high"])
            bar_low_spot = float(bar["low"])
            bar_close_spot = float(bar["close"])

            # 1. 5-minute Strategy-Level Loss Check (both legs exit simultaneously if combined loss >= strategy_max_loss in legacy mode, or exit losing leg only in single-leg mode)
            if strategy_max_loss is None:
                effective_max_loss: Optional[float] = 10000.0
            elif strategy_max_loss <= 0:
                effective_max_loss = None
            else:
                effective_max_loss = strategy_max_loss
            single_leg_exited_this_bar = False
            if (ce_active or pe_active) and effective_max_loss is not None:
                ce_5m_price = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE") if ce_active else ce_exit_price
                pe_5m_price = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE") if pe_active else pe_exit_price
                current_loss = ((ce_5m_price - ce_entry) + (pe_5m_price - pe_entry)) * total_qty

                if current_loss >= effective_max_loss:
                    if use_leg_sl:
                        # In single-leg mode, exit only the losing leg if both are active (do not cascade same-bar)
                        if ce_active and pe_active:
                            ce_loss = (ce_5m_price - ce_entry) * total_qty
                            pe_loss = (pe_5m_price - pe_entry) * total_qty
                            if ce_loss > pe_loss:
                                ce_active = False
                                ce_exit_price = round(ce_5m_price, 2)
                                ce_exit_reason = "SINGLE_LEG_10K_EXIT"
                                ce_exit_time = curr_dt
                                single_leg_exited_this_bar = True
                                if trail_opposite_to_cost and pe_active:
                                    pe_cost_trail_pending = True
                                    if pe_5m_price < pe_entry:
                                        pe_sl = pe_entry
                            else:
                                pe_active = False
                                pe_exit_price = round(pe_5m_price, 2)
                                pe_exit_reason = "SINGLE_LEG_10K_EXIT"
                                pe_exit_time = curr_dt
                                single_leg_exited_this_bar = True
                                if trail_opposite_to_cost and ce_active:
                                    ce_cost_trail_pending = True
                                    if ce_5m_price < ce_entry:
                                        ce_sl = ce_entry
                        elif ce_active:
                            ce_active = False
                            ce_exit_price = round(ce_5m_price, 2)
                            ce_exit_reason = "STRATEGY_MAX_LOSS_10K"
                            ce_exit_time = curr_dt
                            break
                        elif pe_active:
                            pe_active = False
                            pe_exit_price = round(pe_5m_price, 2)
                            pe_exit_reason = "STRATEGY_MAX_LOSS_10K"
                            pe_exit_time = curr_dt
                            break
                    else:
                        if ce_active:
                            ce_active = False
                            ce_exit_price = round(ce_5m_price, 2)
                            ce_exit_reason = "STRATEGY_MAX_LOSS_10K"
                            ce_exit_time = curr_dt
                        if pe_active:
                            pe_active = False
                            pe_exit_price = round(pe_5m_price, 2)
                            pe_exit_reason = "STRATEGY_MAX_LOSS_10K"
                            pe_exit_time = curr_dt
                        break

            # Skip individual leg SL/target on same bar after SINGLE_LEG_10K_EXIT (matches live anti-cascade)
            if not single_leg_exited_this_bar:
                if ce_active and ce_cost_trail_pending and ce_sl > ce_entry:
                    if black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE") < ce_entry:
                        ce_sl = ce_entry
                        ce_cost_trail_pending = False
                if pe_active and pe_cost_trail_pending and pe_sl > pe_entry:
                    if black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE") < pe_entry:
                        pe_sl = pe_entry
                        pe_cost_trail_pending = False

                # 2. Evaluate individual CE leg
                ce_stopped_this_bar = False
                if ce_active:
                    if use_leg_sl:
                        ce_max_price = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                        if ce_max_price >= ce_sl:
                            ce_active = False
                            ce_exit_price = ce_sl
                            ce_exit_reason = "STOP_LOSS"
                            ce_exit_time = curr_dt
                            ce_stopped_this_bar = True
                            if trail_opposite_to_cost and pe_active:
                                pe_cost_trail_pending = True
                                pe_cur_px = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                                if pe_cur_px < pe_entry:
                                    pe_sl = pe_entry

                    if ce_active:
                        ce_min_price = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")
                        if ce_min_price <= ce_target:
                            ce_active = False
                            ce_exit_price = ce_target
                            ce_exit_reason = "DECAY_TARGET"
                            ce_exit_time = curr_dt
                            # Close opposite PE leg simultaneously or roll CE to ATM if underwater
                            if pe_active:
                                pe_5m_price = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                                comb_pnl = ((ce_entry - ce_target) + (pe_entry - pe_5m_price)) * total_qty
                                if roll_untested and comb_pnl <= 0 and not rolled_today:
                                    rc = calculate_trade_costs(ce_entry, ce_target, total_qty, slippage_per_share=slippage)
                                    roll_gross_pnl += rc["gross_pnl"]
                                    roll_taxes += rc["statutory_taxes"]
                                    roll_slippage += rc["total_slippage"]
                                    ce_strike = round(bar_close_spot / 50.0) * 50.0
                                    ce_entry = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                                    ce_sl = round(ce_entry * (1.0 + leg_sl_pct), 2)
                                    ce_target = round(ce_entry * 0.20, 2)
                                    ce_active = True
                                    ce_exit_price = ce_entry
                                    ce_exit_reason = "TIME_EXIT"
                                    rolled_today = True
                                elif not use_leg_sl:
                                    pe_active = False
                                    pe_exit_price = round(pe_5m_price, 2)
                                    pe_exit_reason = "CO_TARGET_EXIT"
                                    pe_exit_time = curr_dt

                # 3. Evaluate individual PE leg
                if pe_active and not ce_stopped_this_bar:
                    if use_leg_sl:
                        pe_max_price = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                        if pe_max_price >= pe_sl:
                            pe_active = False
                            pe_exit_price = pe_sl
                            pe_exit_reason = "STOP_LOSS"
                            pe_exit_time = curr_dt
                            if trail_opposite_to_cost and ce_active:
                                ce_cost_trail_pending = True
                                ce_cur_px = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                                if ce_cur_px < ce_entry:
                                    ce_sl = ce_entry

                    if pe_active:
                        pe_min_price = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")
                        if pe_min_price <= pe_target:
                            pe_active = False
                            pe_exit_price = pe_target
                            pe_exit_reason = "DECAY_TARGET"
                            pe_exit_time = curr_dt
                            # Close opposite CE leg simultaneously or roll PE to ATM if underwater
                            if ce_active:
                                ce_5m_price = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE")
                                comb_pnl = ((ce_entry - ce_5m_price) + (pe_entry - pe_target)) * total_qty
                                if roll_untested and comb_pnl <= 0 and not rolled_today:
                                    rc = calculate_trade_costs(pe_entry, pe_target, total_qty, slippage_per_share=slippage)
                                    roll_gross_pnl += rc["gross_pnl"]
                                    roll_taxes += rc["statutory_taxes"]
                                    roll_slippage += rc["total_slippage"]
                                    pe_strike = round(bar_close_spot / 50.0) * 50.0
                                    pe_entry = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE")
                                    pe_sl = round(pe_entry * (1.0 + leg_sl_pct), 2)
                                    pe_target = round(pe_entry * 0.20, 2)
                                    pe_active = True
                                    pe_exit_price = pe_entry
                                    pe_exit_reason = "TIME_EXIT"
                                    rolled_today = True
                                elif not use_leg_sl:
                                    ce_active = False
                                    ce_exit_price = round(ce_5m_price, 2)
                                    ce_exit_reason = "CO_TARGET_EXIT"
                                    ce_exit_time = curr_dt

            if not ce_active and not pe_active:
                break

            # 4. If one leg exited intra-bar on decay target, re-evaluate strategy total loss with active opposite leg
            if (ce_active or pe_active) and effective_max_loss is not None:
                ce_5m_price = black_76_price(bar_close_spot, ce_strike, curr_dte, iv, r, "CE") if ce_active else ce_exit_price
                pe_5m_price = black_76_price(bar_close_spot, pe_strike, curr_dte, iv, r, "PE") if pe_active else pe_exit_price
                current_loss = ((ce_5m_price - ce_entry) + (pe_5m_price - pe_entry)) * total_qty

                if current_loss >= effective_max_loss:
                    if ce_active:
                        ce_active = False
                        ce_exit_price = round(ce_5m_price, 2)
                        ce_exit_reason = "STRATEGY_MAX_LOSS_10K"
                        ce_exit_time = curr_dt
                    if pe_active:
                        pe_active = False
                        pe_exit_price = round(pe_5m_price, 2)
                        pe_exit_reason = "STRATEGY_MAX_LOSS_10K"
                        pe_exit_time = curr_dt
                    break

            if not ce_active and not pe_active:
                break

        # If still active at cutoff time (14:45), exit at market price
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

        day_gross_pnl = round(ce_cost["gross_pnl"] + pe_cost["gross_pnl"] + roll_gross_pnl, 2)
        day_taxes = round(ce_cost["statutory_taxes"] + pe_cost["statutory_taxes"] + roll_taxes, 2)
        day_slippage = round(ce_cost["total_slippage"] + pe_cost["total_slippage"] + roll_slippage, 2)
        day_net_pnl = round(day_gross_pnl - day_taxes - day_slippage, 2)

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
            "net_pnl": day_net_pnl,
            "rolled": rolled_today
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
