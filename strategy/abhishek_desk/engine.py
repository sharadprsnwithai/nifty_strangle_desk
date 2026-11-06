import logging
from datetime import datetime, date, time as dt_time, timedelta
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np

from strategy.abhishek_desk.config import AbhishekConfig
from strategy.options_model import compute_fractional_dte, black_76_price
from strategy.costs import calculate_trade_costs

logger = logging.getLogger("strategy.abhishek")


def get_nearest_weekly_expiry(d: date, expiry_weekday: int = 1) -> date:
    """
    Returns the nearest weekly expiry date (Tuesday = 1 for NIFTY).
    If today is expiry day, uses today's expiry.
    """
    days_ahead = (expiry_weekday - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


class AbhishekStrategyEngine:
    """
    Core implementation of Abhishek's 15-Minute PDH/PDL Pullback-Trap Strategy.
    Simulates trades across historical 15-minute and 5-minute candles.
    """

    def __init__(self, config: Optional[AbhishekConfig] = None):
        self.config = config or AbhishekConfig()

    def simulate_day(
        self,
        trade_date: date,
        pdh: float,
        pdl: float,
        day_15m_bars: pd.DataFrame,
        day_5m_bars: Optional[pd.DataFrame] = None,
        mode: str = "FUTURES",  # "FUTURES" or "OPTIONS"
        trail_runner_to_be: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Simulates the strategy for a single trading day.
        Returns a list of completed trade records.
        """
        if day_15m_bars.empty or pdh <= 0 or pdl <= 0:
            return []

        # Sort bars chronologically
        df_15m = day_15m_bars.sort_values("dt").copy()
        df_5m = day_5m_bars.sort_values("dt").copy() if day_5m_bars is not None and not day_5m_bars.empty else None

        completed_trades: List[Dict[str, Any]] = []
        trades_today_count = 0
        reentry_forbidden = False

        # State tracking for pullbacks
        # Pullback candles: list of dicts with {"open", "high", "low", "close", "dt"}
        pullback_candles: List[Dict[str, Any]] = []
        active_pullback_type: Optional[str] = None  # "RED" for Long above PDH, "GREEN" for Short below PDL

        total_qty = self.config.lots * self.config.lot_size
        half_qty = max(1, total_qty // 2)
        rem_qty_after_t1 = total_qty - half_qty

        # Target weekly expiry for options simulation
        target_expiry = get_nearest_weekly_expiry(trade_date, expiry_weekday=1)

        # Iterate through 15-minute bars
        for idx, row in df_15m.iterrows():
            if trades_today_count >= self.config.max_trades_per_day or reentry_forbidden:
                break

            bar_time = row["dt"].time()
            # Do not open fresh positions after 14:45 IST
            if bar_time >= dt_time(14, 45):
                break

            bar_open = float(row["open"])
            bar_high = float(row["high"])
            bar_low = float(row["low"])
            bar_close = float(row["close"])
            bar_dt = row["dt"]

            is_green = bar_close >= bar_open
            is_red = bar_close < bar_open

            # -------------------------------------------------------------
            # 1. Macro Bias Filter (PDH / PDL)
            # -------------------------------------------------------------
            is_above_pdh = bar_close > pdh
            is_below_pdl = bar_close < pdl

            if not is_above_pdh and not is_below_pdl:
                # Inside the Chop Zone (Between PDH & PDL) -> Reset setup
                pullback_candles = []
                active_pullback_type = None
                continue

            # -------------------------------------------------------------
            # 2. Setup Evaluation: LONG (Above PDH)
            # -------------------------------------------------------------
            if is_above_pdh:
                if active_pullback_type != "RED":
                    pullback_candles = []
                    active_pullback_type = "RED"

                if is_red:
                    pullback_candles.append({
                        "open": bar_open,
                        "high": bar_high,
                        "low": bar_low,
                        "close": bar_close,
                        "dt": bar_dt
                    })

                    # 3-Candle Disqualification Rule: 3 consecutive red candles = Reversal!
                    if len(pullback_candles) > self.config.max_pullback_candles:
                        pullback_candles = []  # Disqualified! Must reset.
                    continue

                elif is_green:
                    # Check if we have 1 or 2 red pullback candles
                    if 1 <= len(pullback_candles) <= self.config.max_pullback_candles:
                        last_red_candle = pullback_candles[-1]
                        # Trigger condition: Green candle closes ABOVE the OPEN of the last Red candle
                        if bar_close > last_red_candle["open"]:
                            # TRIGGER LONG ENTRY!
                            entry_price = bar_close
                            pullback_lows = [c["low"] for c in pullback_candles] + [bar_low]
                            initial_sl = min(pullback_lows) - self.config.sl_buffer_points
                            risk_per_share = entry_price - initial_sl

                            if risk_per_share > 0:
                                target_1_price = entry_price + (self.config.target_1_r * risk_per_share)
                                
                                trade_record = self._execute_trade(
                                    trade_date=trade_date,
                                    direction="LONG",
                                    entry_time=bar_dt,
                                    entry_price=entry_price,
                                    initial_sl=initial_sl,
                                    target_1_price=target_1_price,
                                    risk_per_share=risk_per_share,
                                    total_qty=total_qty,
                                    half_qty=half_qty,
                                    rem_qty=rem_qty_after_t1,
                                    df_15m=df_15m,
                                    df_5m=df_5m,
                                    entry_idx=idx,
                                    mode=mode,
                                    target_expiry=target_expiry,
                                    trail_runner_to_be=trail_runner_to_be
                                )
                                completed_trades.append(trade_record)
                                trades_today_count += 1

                                if trade_record["t1_hit"]:
                                    reentry_forbidden = True
                                    break

                            pullback_candles = []
                        else:
                            # Green candle failed to close above Open -> reset pullback flow
                            pullback_candles = []
                    else:
                        # Green candle with no pullback red candles -> trend continuation, wait
                        pullback_candles = []

            # -------------------------------------------------------------
            # 3. Setup Evaluation: SHORT (Below PDL)
            # -------------------------------------------------------------
            elif is_below_pdl:
                if active_pullback_type != "GREEN":
                    pullback_candles = []
                    active_pullback_type = "GREEN"

                if is_green:
                    pullback_candles.append({
                        "open": bar_open,
                        "high": bar_high,
                        "low": bar_low,
                        "close": bar_close,
                        "dt": bar_dt
                    })

                    # 3-Candle Disqualification Rule: 3 consecutive green candles = Reversal!
                    if len(pullback_candles) > self.config.max_pullback_candles:
                        pullback_candles = []  # Disqualified! Must reset.
                    continue

                elif is_red:
                    # Check if we have 1 or 2 green pullback candles
                    if 1 <= len(pullback_candles) <= self.config.max_pullback_candles:
                        last_green_candle = pullback_candles[-1]
                        # Trigger condition: Red candle closes BELOW the OPEN of the last Green candle
                        if bar_close < last_green_candle["open"]:
                            # TRIGGER SHORT ENTRY!
                            entry_price = bar_close
                            pullback_highs = [c["high"] for c in pullback_candles] + [bar_high]
                            initial_sl = max(pullback_highs) + self.config.sl_buffer_points
                            risk_per_share = initial_sl - entry_price

                            if risk_per_share > 0:
                                target_1_price = entry_price - (self.config.target_1_r * risk_per_share)
                                
                                trade_record = self._execute_trade(
                                    trade_date=trade_date,
                                    direction="SHORT",
                                    entry_time=bar_dt,
                                    entry_price=entry_price,
                                    initial_sl=initial_sl,
                                    target_1_price=target_1_price,
                                    risk_per_share=risk_per_share,
                                    total_qty=total_qty,
                                    half_qty=half_qty,
                                    rem_qty=rem_qty_after_t1,
                                    df_15m=df_15m,
                                    df_5m=df_5m,
                                    entry_idx=idx,
                                    mode=mode,
                                    target_expiry=target_expiry,
                                    trail_runner_to_be=trail_runner_to_be
                                )
                                completed_trades.append(trade_record)
                                trades_today_count += 1

                                if trade_record["t1_hit"]:
                                    reentry_forbidden = True
                                    break

                            pullback_candles = []
                        else:
                            # Red candle failed to close below Open -> reset pullback flow
                            pullback_candles = []
                    else:
                        # Red candle with no pullback green candles -> trend continuation
                        pullback_candles = []

        return completed_trades

    def _execute_trade(
        self,
        trade_date: date,
        direction: str,
        entry_time: datetime,
        entry_price: float,
        initial_sl: float,
        target_1_price: float,
        risk_per_share: float,
        total_qty: int,
        half_qty: int,
        rem_qty: int,
        df_15m: pd.DataFrame,
        df_5m: Optional[pd.DataFrame],
        entry_idx: Any,
        mode: str,
        target_expiry: date,
        trail_runner_to_be: bool
    ) -> Dict[str, Any]:
        """
        Executes and manages an individual trade from entry to exit.
        Evaluates 1:1 R target, Stop Loss, and 3:15 PM IST squareoff.
        Computes accurate P&L for both Futures and Option Buying.
        """
        # Determine tracking bars (use 5m bars if available for sub-candle fidelity)
        if df_5m is not None and not df_5m.empty:
            tracking_bars = df_5m[df_5m["dt"] > entry_time].copy()
        else:
            tracking_bars = df_15m[df_15m["dt"] > entry_time].copy()

        # Option Buying Strike and DTE setup
        atm_strike = round(entry_price / 50.0) * 50.0
        opt_type = "CE" if direction == "LONG" else "PE"
        entry_dte = compute_fractional_dte(target_expiry, entry_time)
        entry_opt_price = black_76_price(
            spot=entry_price,
            strike=atm_strike,
            dte_days=entry_dte,
            iv=self.config.assumed_iv,
            r=self.config.risk_free_rate,
            opt_type=opt_type
        ) if mode == "OPTIONS" else entry_price

        current_sl = initial_sl
        t1_hit = False
        t1_time: Optional[datetime] = None
        t1_exit_price: float = target_1_price
        t1_opt_exit_price: float = 0.0

        exit_time: Optional[datetime] = None
        exit_reason: str = "315_EOD_EXIT"
        exit_price: float = entry_price
        exit_opt_price: float = 0.0

        # Scan subsequent bars
        for _, bar in tracking_bars.iterrows():
            b_dt = bar["dt"]
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            b_close = float(bar["close"])
            b_time = b_dt.time()
            curr_dte = compute_fractional_dte(target_expiry, b_dt) if mode == "OPTIONS" else 0.0

            # 1. Evaluate Target 1 (1.0 R)
            if not t1_hit:
                if (direction == "LONG" and b_high >= target_1_price) or \
                   (direction == "SHORT" and b_low <= target_1_price):
                    t1_hit = True
                    t1_time = b_dt
                    t1_exit_price = target_1_price
                    if mode == "OPTIONS":
                        t1_opt_exit_price = black_76_price(
                            spot=target_1_price,
                            strike=atm_strike,
                            dte_days=curr_dte,
                            iv=self.config.assumed_iv,
                            r=self.config.risk_free_rate,
                            opt_type=opt_type
                        )
                    if trail_runner_to_be:
                        current_sl = entry_price  # Trail SL to Cost

            # 2. Evaluate Stop Loss
            sl_triggered = False
            if direction == "LONG" and b_low <= current_sl:
                sl_triggered = True
                exit_price = current_sl
            elif direction == "SHORT" and b_high >= current_sl:
                sl_triggered = True
                exit_price = current_sl

            if sl_triggered:
                exit_time = b_dt
                if not t1_hit:
                    exit_reason = "FULL_STOP_LOSS"
                else:
                    exit_reason = "RUNNER_TRAILED_STOP" if trail_runner_to_be else "RUNNER_INITIAL_SL"
                
                if mode == "OPTIONS":
                    exit_opt_price = black_76_price(
                        spot=exit_price,
                        strike=atm_strike,
                        dte_days=curr_dte,
                        iv=self.config.assumed_iv,
                        r=self.config.risk_free_rate,
                        opt_type=opt_type
                    )
                break

            # 3. Evaluate 3:15 PM IST Mandatory Exit
            if b_time >= dt_time(15, 15):
                exit_time = b_dt
                exit_price = b_close
                exit_reason = "315_RUNNER_EXIT" if t1_hit else "315_SCRATCH_EXIT"
                if mode == "OPTIONS":
                    exit_opt_price = black_76_price(
                        spot=exit_price,
                        strike=atm_strike,
                        dte_days=curr_dte,
                        iv=self.config.assumed_iv,
                        r=self.config.risk_free_rate,
                        opt_type=opt_type
                    )
                break

        # If not stopped out or 3:15 reached in loop, fallback to last available bar
        if exit_time is None:
            last_bar = tracking_bars.iloc[-1]
            exit_time = last_bar["dt"]
            exit_price = float(last_bar["close"])
            exit_reason = "315_RUNNER_EXIT" if t1_hit else "315_SCRATCH_EXIT"
            if mode == "OPTIONS":
                curr_dte = compute_fractional_dte(target_expiry, exit_time)
                exit_opt_price = black_76_price(
                    spot=exit_price,
                    strike=atm_strike,
                    dte_days=curr_dte,
                    iv=self.config.assumed_iv,
                    r=self.config.risk_free_rate,
                    opt_type=opt_type
                )

        # -------------------------------------------------------------
        # 4. P&L & Transaction Costs Calculation
        # -------------------------------------------------------------
        if mode == "FUTURES":
            # Linear Futures P&L
            slippage = self.config.slippage_per_share
            # Leg 1: Half Qty
            if t1_hit:
                leg1_pts = (t1_exit_price - entry_price - (2 * slippage)) if direction == "LONG" else (entry_price - t1_exit_price - (2 * slippage))
            else:
                leg1_pts = (exit_price - entry_price - (2 * slippage)) if direction == "LONG" else (entry_price - exit_price - (2 * slippage))
            
            # Leg 2: Runner Qty
            leg2_pts = (exit_price - entry_price - (2 * slippage)) if direction == "LONG" else (entry_price - exit_price - (2 * slippage))

            leg1_pnl = leg1_pts * half_qty
            leg2_pnl = leg2_pts * rem_qty
            gross_pnl = leg1_pnl + leg2_pnl

            # Costs (Brokerage ₹40 roundtrip + turnover/taxes ~₹120 per roundtrip)
            total_taxes_brokerage = 160.0
            net_pnl = gross_pnl - total_taxes_brokerage

            # Margin requirement: ~20% of futures contract value
            capital_required = entry_price * total_qty * 0.20

        else:
            # Option Buying P&L (Black-76 model with theta decay)
            slippage = self.config.slippage_per_share
            entry_opt_fill = entry_opt_price + slippage  # Buyer pays ask

            # Leg 1
            if t1_hit:
                t1_opt_fill = max(0.05, t1_opt_exit_price - slippage)
                cost1 = calculate_trade_costs(t1_opt_fill, entry_opt_fill, half_qty, slippage_per_share=0.0)
                leg1_pnl = (t1_opt_fill - entry_opt_fill) * half_qty - cost1["statutory_taxes"] - cost1["brokerage"]
            else:
                exit_opt_fill = max(0.05, exit_opt_price - slippage)
                cost1 = calculate_trade_costs(exit_opt_fill, entry_opt_fill, half_qty, slippage_per_share=0.0)
                leg1_pnl = (exit_opt_fill - entry_opt_fill) * half_qty - cost1["statutory_taxes"] - cost1["brokerage"]

            # Leg 2
            exit_opt_fill = max(0.05, exit_opt_price - slippage)
            cost2 = calculate_trade_costs(exit_opt_fill, entry_opt_fill, rem_qty, slippage_per_share=0.0)
            leg2_pnl = (exit_opt_fill - entry_opt_fill) * rem_qty - cost2["statutory_taxes"] - cost2["brokerage"]

            net_pnl = leg1_pnl + leg2_pnl
            gross_pnl = net_pnl + cost1["brokerage"] + cost1["statutory_taxes"] + cost2["brokerage"] + cost2["statutory_taxes"]

            # Capital required for Option Buying is total premium paid
            capital_required = entry_opt_fill * total_qty

        return {
            "date": str(trade_date),
            "direction": direction,
            "mode": mode,
            "entry_time": entry_time.strftime("%H:%M:%S"),
            "entry_price": entry_price,
            "initial_sl": initial_sl,
            "target_1_price": target_1_price,
            "risk_per_share": risk_per_share,
            "t1_hit": t1_hit,
            "t1_time": t1_time.strftime("%H:%M:%S") if t1_time else None,
            "exit_time": exit_time.strftime("%H:%M:%S") if exit_time else None,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "quantity": total_qty,
            "capital_required": round(capital_required, 2),
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "r_multiple": round(net_pnl / (risk_per_share * total_qty), 2) if (risk_per_share * total_qty) > 0 else 0.0,
            "option_strike": atm_strike if mode == "OPTIONS" else None,
            "entry_opt_price": round(entry_opt_price, 2) if mode == "OPTIONS" else None,
            "exit_opt_price": round(exit_opt_price, 2) if mode == "OPTIONS" else None,
        }
