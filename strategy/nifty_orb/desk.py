import json
import logging
import os
import tempfile
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect
from kite.client import is_token_error

from notifications.telegram import TelegramNotifier
from strategy.holidays import is_nse_holiday
from .config import NiftyORBConfig
from .contract_resolver import resolve_active_nifty_fut_contract
from .order_manager import NiftyOrderManager

logger = logging.getLogger("nifty_orb.desk")


class NiftyORBDesk:
    """
    Automated trading desk for NIFTY 50 Index Futures 30-Minute ORB Breakout Strategy.
    Executes the 2-lot tranche scaling methodology:
    - Tranche 1 booked at Target 1 (+75 pts).
    - Runner Tranche 2 SL moved to Cost (100% Risk-Free) and rides until 15:15 IST cutoff.
    """

    def __init__(
        self,
        config: Optional[NiftyORBConfig] = None,
        kite: Optional[KiteConnect] = None,
        order_manager: Optional[NiftyOrderManager] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.config = config or NiftyORBConfig()
        self.kite = kite
        self.order_manager = order_manager or NiftyOrderManager(
            mode=self.config.mode,
            kite=self.kite,
            exchange=self.config.exchange,
            product=self.config.product,
            order_type=self.config.order_type,
            limit_buffer_pts=self.config.limit_buffer_pts,
        )
        self.notifier = notifier
        self.state_file = Path(self.config.state_file).resolve()
        self.state: Optional[Dict[str, Any]] = None

    def _ensure_kite_session(self, force_login: bool = False) -> Any:
        """Validates or refreshes Kite session without crashing."""
        if self.kite is None:
            return None
        from kite.client import ensure_kite_session
        try:
            self.kite = ensure_kite_session(self.kite, force_login=force_login)
            if hasattr(self.order_manager, "kite"):
                self.order_manager.kite = self.kite
            return self.kite
        except Exception as e:
            logger.error("Failed to re-authenticate Kite session: %s", e)
            return self.kite

    def _get_fallback_file(self) -> Path:
        fallback_dir = Path(tempfile.gettempdir()) / "trading_state"
        return fallback_dir / self.state_file.name

    def save_state(self, state: Dict[str, Any]) -> None:
        """Atomically saves state to disk with fallback resilience."""
        self.state = state
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error("Primary state save failed at %s (%s). Attempting fallback...", self.state_file, e)
            try:
                fallback_file = self._get_fallback_file()
                fallback_file.parent.mkdir(parents=True, exist_ok=True)
                temp_fallback = fallback_file.with_suffix(".tmp")
                temp_fallback.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
                temp_fallback.replace(fallback_file)
            except Exception as fb_err:
                logger.critical("All state save attempts failed: %s (Fallback: %s)", e, fb_err)

    def load_state(self) -> Optional[Dict[str, Any]]:
        target_file = self.state_file
        if not target_file.exists():
            fallback_file = self._get_fallback_file()
            if fallback_file.exists():
                target_file = fallback_file
            else:
                return None
        try:
            content = target_file.read_text(encoding="utf-8")
            self.state = json.loads(content)
            return self.state
        except Exception as e:
            logger.error("Failed to parse state file %s: %s", target_file, e)
            return None

    def initialize_day(self, today_d: Optional[date] = None) -> Dict[str, Any]:
        """Initializes or resumes daily trading state."""
        today = today_d or date.today()
        today_str = today.strftime("%Y-%m-%d")

        if self.state and self.state.get("date") == today_str:
            return self.state

        existing_state = self.load_state()
        if existing_state and existing_state.get("date") == today_str:
            logger.info("Resuming existing Nifty ORB state for %s", today_str)
            self.state = existing_state
            return existing_state

        contract = resolve_active_nifty_fut_contract(self.kite, today_d=today)
        new_state = {
            "date": today_str,
            "symbol": contract["tradingsymbol"],
            "instrument_token": contract["instrument_token"],
            "lot_size": contract["lot_size"],
            "ref_high": None,
            "ref_low": None,
            "status": "INITIALIZING",
            "trades": [],
            "realized_day_pnl": 0.0,
        }
        self.save_state(new_state)
        return new_state

    # -------------------------------------------------------------------------
    # Telegram Notifications
    # -------------------------------------------------------------------------
    def _notify_range_established(self, sym: str, r_high: float, r_low: float, ema50: float) -> None:
        if not self.notifier:
            return
        rng_pts = r_high - r_low
        msg = (
            f"🔔 <b>NIFTY 30-MIN ORB RANGE ESTABLISHED [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{sym}</code>\n"
            f"Window: <b>09:15 – 09:45 IST</b>\n"
            f"High: <b>₹{r_high:,.1f}</b>\n"
            f"Low: <b>₹{r_low:,.1f}</b> (Spread: {rng_pts:.1f} pts)\n"
            f"15m 50 EMA: <b>₹{ema50:,.1f}</b>\n\n"
            f"Breakout evaluation is active until 13:30 IST."
        )
        self.notifier.send_message(msg)

    def _notify_entry(self, trade: Dict[str, Any]) -> None:
        if not self.notifier:
            return
        direction = trade["direction"]
        sym = trade["symbol"]
        entry = trade["entry_price"]
        lots = trade["lots_total"]
        sl = trade["sl_price"]
        t1 = trade["target1_price"]
        msg = (
            f"🚀 <b>NIFTY FUTURES BREAKOUT ENTRY [{self.config.mode}]</b>\n\n"
            f"Direction: <b>{direction}</b>\n"
            f"Contract: <code>{sym}</code>\n"
            f"Quantity: <b>{lots} Lots</b> ({trade['quantity_total']} units)\n"
            f"Entry Price: <b>₹{entry:,.1f}</b>\n\n"
            f"🛡️ Initial SL: <b>₹{sl:,.1f}</b> ({self.config.initial_sl_pts:.0f} pts)\n"
            f"🎯 Target 1: <b>₹{t1:,.1f}</b> (+{self.config.target1_pts:.0f} pts on Lot 1)\n"
            f"Runner Lot: Will trail to Cost upon Target 1 & ride to 15:15 IST."
        )
        self.notifier.send_message(msg)

    def _notify_target1(self, trade: Dict[str, Any], day_pnl: float) -> None:
        if not self.notifier:
            return
        msg = (
            f"🎯 <b>NIFTY TARGET 1 HIT [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{trade['symbol']}</code> ({trade['direction']})\n"
            f"Tranche 1 Exited: <b>{trade['lot1_lots']} Lots @ ₹{trade['lot1_exit_price']:,.1f}</b>\n"
            f"Booked P&L: <b>+₹{trade['lot1_pnl_rs']:+,.2f}</b>\n\n"
            f"🛡️ <b>Runner ({trade['lot2_lots']} Lots) Stop Loss Trailed to Cost (₹{trade['entry_price']:,.1f})!</b>\n"
            f"Trade is now 100% Risk-Free.\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_sl(self, trade: Dict[str, Any], exit_p: float, trade_pnl: float, day_pnl: float) -> None:
        if not self.notifier:
            return
        msg = (
            f"🛑 <b>NIFTY STOP LOSS HIT [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{trade['symbol']}</code> ({trade['direction']})\n"
            f"Exited: <b>{trade['lots_total']} Lots @ ₹{exit_p:,.1f}</b>\n"
            f"Trade P&L: <b>₹{trade_pnl:+,.2f}</b>\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_runner_exit(self, trade: Dict[str, Any], reason: str, day_pnl: float) -> None:
        if not self.notifier:
            return
        total_pnl = trade.get("lot1_pnl_rs", 0.0) + trade.get("lot2_pnl_rs", 0.0)
        msg = (
            f"🏁 <b>NIFTY RUNNER EXITED [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{trade['symbol']}</code> ({trade['direction']})\n"
            f"Runner Exited: <b>{trade['lot2_lots']} Lots @ ₹{trade['lot2_exit_price']:,.1f}</b>\n"
            f"Exit Reason: <b>{reason}</b>\n"
            f"Runner P&L: <b>₹{trade['lot2_pnl_rs']:+,.2f}</b>\n"
            f"Trade Net P&L: <b>₹{total_pnl:+,.2f}</b>\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    # -------------------------------------------------------------------------
    # Data & Technical Indicators
    # -------------------------------------------------------------------------
    def fetch_recent_candles(self, token: int, num_days: int = 6) -> Optional[pd.DataFrame]:
        if not self.kite:
            return None
        to_d = datetime.now()
        from_d = to_d - timedelta(days=num_days)
        try:
            records = self.kite.historical_data(token, from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d"), "15minute")
            if not records:
                return None
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df["time"] = df["date"].dt.time
            df["day"] = df["date"].dt.date
            return df
        except Exception as e:
            if is_token_error(e):
                self._ensure_kite_session(force_login=True)
                try:
                    records = self.kite.historical_data(token, from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d"), "15minute")
                    if records:
                        df = pd.DataFrame(records)
                        df["date"] = pd.to_datetime(df["date"])
                        df["time"] = df["date"].dt.time
                        df["day"] = df["date"].dt.date
                        return df
                except Exception:
                    pass
            logger.error("Failed to fetch 15m candles for token %s: %s", token, e)
            return None

    def evaluate_reference_range(self, df: pd.DataFrame, target_day: Optional[date] = None) -> Optional[Dict[str, float]]:
        """Calculates 30-min ORB High and Low across 09:15 and 09:30 bars."""
        if df is None or len(df) < 2:
            return None
        t_day = target_day or date.today()
        work_df = df.copy()
        if "day" not in work_df.columns and "date" in work_df.columns:
            work_df["day"] = pd.to_datetime(work_df["date"]).dt.date
        if "time" not in work_df.columns and "date" in work_df.columns:
            work_df["time"] = pd.to_datetime(work_df["date"]).dt.time

        day_bars = work_df[work_df["day"] == t_day]
        ref_bars = day_bars[(day_bars["time"] >= dt_time(9, 15)) & (day_bars["time"] <= dt_time(9, 30))]
        if len(ref_bars) < 2:
            return None

        r_high = float(ref_bars["high"].max())
        r_low = float(ref_bars["low"].min())
        return {"ref_high": r_high, "ref_low": r_low}

    def evaluate_breakout_signal(
        self,
        df: pd.DataFrame,
        ref_high: float,
        ref_low: float,
        current_time: Optional[dt_time] = None,
        current_dt: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Evaluates closed 15m bar breakout with 50 EMA and 1.2x volume filters."""
        if df is None or len(df) < 50:
            return None

        if current_dt is not None:
            eval_dt = current_dt
        elif current_time is not None:
            eval_dt = datetime.combine(date.today(), current_time)
        else:
            eval_dt = datetime.now()

        work_df = df.copy()

        if "day" not in work_df.columns and "date" in work_df.columns:
            work_df["day"] = pd.to_datetime(work_df["date"]).dt.date
        if "time" not in work_df.columns and "date" in work_df.columns:
            work_df["time"] = pd.to_datetime(work_df["date"]).dt.time

        if "date" in work_df.columns:
            try:
                candle_dates = pd.to_datetime(work_df["date"])
                if candle_dates.dt.tz is not None:
                    candle_dates = candle_dates.dt.tz_localize(None)
                eval_naive = eval_dt.replace(tzinfo=None) if hasattr(eval_dt, "tzinfo") and eval_dt.tzinfo else eval_dt
                is_closed = (candle_dates + pd.Timedelta(minutes=15)) <= (eval_naive + pd.Timedelta(seconds=5))
                closed_df = work_df[is_closed]
                work_df = closed_df
                if len(work_df) < 50:
                    return None
            except Exception as e:
                logger.debug("Candle filtering exception: %s", e)

        if len(work_df) < 50:
            return None

        work_df["vol_sma20"] = work_df["volume"].rolling(20).mean().shift(1)
        work_df["ema50"] = work_df["close"].ewm(span=50, adjust=False).mean()

        latest_bar = work_df.iloc[-1]
        b_high = float(latest_bar["high"])
        b_low = float(latest_bar["low"])
        b_close = float(latest_bar["close"])
        b_vol = float(latest_bar["volume"])
        vol_avg = float(latest_bar["vol_sma20"]) if not np.isnan(latest_bar["vol_sma20"]) else 100.0
        ema50_val = float(latest_bar["ema50"])

        vol_ok = b_vol >= self.config.vol_multiplier * vol_avg

        long_ok = (b_high > ref_high) and (b_close > ref_high) and vol_ok
        if self.config.use_trend_filter:
            long_ok = long_ok and (b_close > ema50_val)

        short_ok = (b_low < ref_low) and (b_close < ref_low) and vol_ok
        if self.config.use_trend_filter:
            short_ok = short_ok and (b_close < ema50_val)

        candle_time = latest_bar.get("time")
        if candle_time is None and "date" in latest_bar and hasattr(latest_bar["date"], "time"):
            candle_time = latest_bar["date"].time()

        if long_ok:
            return {
                "direction": "LONG",
                "trigger_price": b_close,
                "ref_high": ref_high,
                "ref_low": ref_low,
                "candle_time": candle_time,
            }
        elif short_ok:
            return {
                "direction": "SHORT",
                "trigger_price": b_close,
                "ref_high": ref_high,
                "ref_low": ref_low,
                "candle_time": candle_time,
            }
        return None

    # -------------------------------------------------------------------------
    # Core Monitoring Cycle
    # -------------------------------------------------------------------------
    def run_monitoring_cycle(
        self,
        current_time: dt_time,
        current_dt: Optional[datetime] = None,
        mock_ltp: Optional[float] = None,
        mock_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Main iteration evaluating range setup, entry trigger, and open trade management."""
        if not self.state:
            self.initialize_day(current_dt.date() if current_dt else None)
        state = self.state
        if not state:
            return

        status = state.get("status", "INITIALIZING")
        today = current_dt.date() if current_dt else date.today()

        if not getattr(self.config, "ignore_holidays", False) and is_nse_holiday(today):
            logger.info("NSE Holiday today (%s). Desk resting.", today)
            return

        token = state["instrument_token"]
        sym = state["symbol"]
        lot_size = state["lot_size"]

        # Fetch market data
        df = mock_df if mock_df is not None else self.fetch_recent_candles(token)
        ltp = mock_ltp
        if ltp is None and self.kite:
            try:
                quote = self.kite.ltp([f"{self.config.exchange}:{sym}"])
                ltp = float(quote.get(f"{self.config.exchange}:{sym}", {}).get("last_price", 0.0))
            except Exception as e:
                logger.error("Failed to fetch LTP: %s", e)

        # 1. INITIALIZING -> Establish 09:15 - 09:45 Range
        if status == "INITIALIZING":
            if current_time >= self.config.ref_end_time:
                ref = self.evaluate_reference_range(df, target_day=today)
                if ref:
                    state["ref_high"] = ref["ref_high"]
                    state["ref_low"] = ref["ref_low"]
                    state["status"] = "SCANNING"
                    self.save_state(state)
                    ema50 = float(df["close"].ewm(span=50).mean().iloc[-1]) if df is not None and len(df) > 50 else 0.0
                    self._notify_range_established(sym, ref["ref_high"], ref["ref_low"], ema50)

        # 2. SCANNING -> Evaluate Breakout Signal
        elif status == "SCANNING":
            if current_time > self.config.trade_window_end:
                logger.info("Trade window cutoff reached (13:30 IST) without breakout. Session completed.")
                state["status"] = "COMPLETED"
                self.save_state(state)
                return

            sig = self.evaluate_breakout_signal(
                df, state["ref_high"], state["ref_low"], current_time, current_dt
            )
            if sig:
                direction = sig["direction"]
                trigger_p = sig["trigger_price"]
                lots = self.config.lots
                lot1_lots = max(1, lots // 2)
                lot2_lots = lots - lot1_lots
                total_qty = lots * lot_size

                tx_type = "BUY" if direction == "LONG" else "SELL"
                ord_res = self.order_manager.place_order(
                    symbol=sym,
                    transaction_type=tx_type,
                    quantity=total_qty,
                    trigger_price=trigger_p,
                )

                entry_p = ord_res["price"]
                sl_p = (entry_p - self.config.initial_sl_pts) if direction == "LONG" else (entry_p + self.config.initial_sl_pts)
                t1_p = (entry_p + self.config.target1_pts) if direction == "LONG" else (entry_p - self.config.target1_pts)

                trade = {
                    "symbol": sym,
                    "direction": direction,
                    "entry_time": str(current_time),
                    "entry_price": entry_p,
                    "lots_total": lots,
                    "quantity_total": total_qty,
                    "lot1_lots": lot1_lots,
                    "lot2_lots": lot2_lots,
                    "sl_price": sl_p,
                    "target1_price": t1_p,
                    "lot1_status": "OPEN",
                    "lot2_status": "OPEN",
                    "lot1_exit_price": None,
                    "lot1_pnl_rs": 0.0,
                    "lot2_exit_price": None,
                    "lot2_pnl_rs": 0.0,
                }
                state["trades"].append(trade)
                state["status"] = "IN_TRADE"
                self.save_state(state)
                self._notify_entry(trade)

        # 3. IN_TRADE & TARGET1_HIT -> Manage Stop Loss, Target 1, Runner & Cutoff
        elif status in ("IN_TRADE", "TARGET1_HIT") and state["trades"]:
            trade = state["trades"][-1]
            direction = trade["direction"]
            if ltp is None or ltp <= 0:
                return

            # A. Check Stop Loss
            sl_hit = (ltp <= trade["sl_price"]) if direction == "LONG" else (ltp >= trade["sl_price"])
            if sl_hit:
                if status == "IN_TRADE":
                    # Full position SL hit
                    exit_tx = "SELL" if direction == "LONG" else "BUY"
                    self.order_manager.place_order(sym, exit_tx, trade["quantity_total"], trigger_price=ltp)
                    pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                    trade_pnl = pts * trade["quantity_total"]
                    state["realized_day_pnl"] += trade_pnl
                    trade["lot1_status"] = "STOPPED_OUT"
                    trade["lot2_status"] = "STOPPED_OUT"
                    trade["lot1_exit_price"] = ltp
                    trade["lot2_exit_price"] = ltp
                    trade["lot1_pnl_rs"] = pts * (trade["lot1_lots"] * lot_size)
                    trade["lot2_pnl_rs"] = pts * (trade["lot2_lots"] * lot_size)
                    state["status"] = "COMPLETED"
                    self.save_state(state)
                    self._notify_sl(trade, ltp, trade_pnl, state["realized_day_pnl"])
                    return
                elif status == "TARGET1_HIT":
                    # Runner hit cost / breakeven
                    runner_qty = trade["lot2_lots"] * lot_size
                    exit_tx = "SELL" if direction == "LONG" else "BUY"
                    self.order_manager.place_order(sym, exit_tx, runner_qty, trigger_price=ltp)
                    pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                    runner_pnl = pts * runner_qty
                    trade["lot2_status"] = "CLOSED"
                    trade["lot2_exit_price"] = ltp
                    trade["lot2_pnl_rs"] = runner_pnl
                    state["realized_day_pnl"] += runner_pnl
                    state["status"] = "COMPLETED"
                    self.save_state(state)
                    self._notify_runner_exit(trade, "Cost/Breakeven Reversal", state["realized_day_pnl"])
                    return

            # B. Check Target 1 (When IN_TRADE)
            if status == "IN_TRADE":
                t1_hit = (ltp >= trade["target1_price"]) if direction == "LONG" else (ltp <= trade["target1_price"])
                if t1_hit:
                    t1_qty = trade["lot1_lots"] * lot_size
                    exit_tx = "SELL" if direction == "LONG" else "BUY"
                    self.order_manager.place_order(sym, exit_tx, t1_qty, trigger_price=ltp)
                    pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                    t1_pnl = pts * t1_qty
                    trade["lot1_status"] = "TARGET_HIT"
                    trade["lot1_exit_price"] = ltp
                    trade["lot1_pnl_rs"] = t1_pnl
                    state["realized_day_pnl"] += t1_pnl

                    # Trail runner SL to Cost
                    trade["sl_price"] = trade["entry_price"]
                    status = "TARGET1_HIT"
                    state["status"] = "TARGET1_HIT"
                    self.save_state(state)
                    self._notify_target1(trade, state["realized_day_pnl"])

            # C. Check Session Cutoff (15:15 IST)
            if current_time >= self.config.session_cutoff_time:
                rem_lots = trade["lot2_lots"] if status == "TARGET1_HIT" else trade["lots_total"]
                rem_qty = rem_lots * lot_size
                exit_tx = "SELL" if direction == "LONG" else "BUY"
                self.order_manager.place_order(sym, exit_tx, rem_qty, trigger_price=ltp)
                pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                runner_pnl = pts * (trade["lot2_lots"] * lot_size)
                trade["lot2_status"] = "CLOSED"
                trade["lot2_exit_price"] = ltp
                trade["lot2_pnl_rs"] = runner_pnl
                if status == "IN_TRADE":
                    trade["lot1_status"] = "CLOSED"
                    trade["lot1_exit_price"] = ltp
                    trade["lot1_pnl_rs"] = pts * (trade["lot1_lots"] * lot_size)
                    state["realized_day_pnl"] += trade["lot1_pnl_rs"]
                state["realized_day_pnl"] += runner_pnl
                state["status"] = "COMPLETED"
                self.save_state(state)
                self._notify_runner_exit(trade, "15:15 EOD Session Cutoff", state["realized_day_pnl"])
