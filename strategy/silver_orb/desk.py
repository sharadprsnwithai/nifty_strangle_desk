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

from notifications.telegram import TelegramNotifier
from strategy.holidays import is_mcx_trading_holiday
from .config import SilverORBConfig
from .contract_resolver import resolve_active_silverm_contract
from .order_manager import SilverOrderManager

logger = logging.getLogger("silver_orb.desk")


class SilverORBDesk:
    """
    Automated trading desk for MCX Silver Mini (SILVERM) 18:00 US Session Breakout Strategy.
    Executes the 2-lot tranche scaling methodology with strict Telegram notifications
    on Entry, Stop Loss, and Exit along with cumulative Total Day P&L.
    """

    def __init__(
        self,
        config: Optional[SilverORBConfig] = None,
        kite: Optional[KiteConnect] = None,
        order_manager: Optional[SilverOrderManager] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.config = config or SilverORBConfig()
        self.kite = kite
        self.order_manager = order_manager or SilverOrderManager(
            mode=self.config.mode,
            kite=self.kite,
            exchange=self.config.exchange,
            product=self.config.product,
        )
        self.notifier = notifier
        self.state_file = Path(self.config.state_file).resolve()
        self.state: Optional[Dict[str, Any]] = None

    def _ensure_kite_session(self, force_login: bool = False) -> Any:
        """Validates or refreshes Kite session on token expiration without crashing daemon."""
        if self.kite is None:
            return None
        from kite.client import ensure_kite_session
        try:
            self.kite = ensure_kite_session(self.kite, force_login=force_login)
            if hasattr(self.order_manager, "kite"):
                self.order_manager.kite = self.kite
            return self.kite
        except Exception as e:
            logger.error("Failed to re-authenticate Kite session for Silver ORB Desk: %s", e)
            return self.kite

    def _get_fallback_file(self) -> Path:
        fallback_dir = Path(tempfile.gettempdir()) / "trading_state"
        return fallback_dir / self.state_file.name

    def save_state(self, state: Dict[str, Any]) -> None:
        """Atomically saves silver state to disk with fallback resilience."""
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
                logger.warning("Silver state saved to fallback path: %s", fallback_file)
            except Exception as fb_err:
                logger.critical("All state save attempts failed: %s (Fallback: %s)", e, fb_err)

    def load_state(self) -> Optional[Dict[str, Any]]:
        target_file = self.state_file
        if not target_file.exists():
            fallback_file = self._get_fallback_file()
            if fallback_file.exists():
                logger.info("Primary state file not found; loading from fallback: %s", fallback_file)
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
        existing_state = self.load_state()

        if existing_state and existing_state.get("date") == today_str:
            logger.info("Resuming existing Silver ORB state for %s", today_str)
            return existing_state

        # Resolve active near-month contract
        contract = resolve_active_silverm_contract(self.kite)

        new_state = {
            "date": today_str,
            "contract": {
                "tradingsymbol": contract["tradingsymbol"],
                "instrument_token": contract["instrument_token"],
                "expiry": str(contract["expiry"]),
            },
            "status": "IDLE",  # IDLE -> WAITING_TRIGGER -> ACTIVE_TRADE -> COMPLETED
            "ref_bar": None,
            "trade": None,
            "realized_pnl_rs": 0.0,
            "trades_count": 0,
        }
        self.save_state(new_state)
        logger.info("Initialized new daily state for %s with contract %s", today_str, contract["tradingsymbol"])
        return new_state

    # -------------------------------------------------------------------------
    # Telegram Notifications (Strictly Entry, SL, and Exit with Total Day PnL)
    # -------------------------------------------------------------------------
    def _notify_entry(self, trade: Dict[str, Any]) -> None:
        if not self.notifier:
            return
        mode = self.config.mode
        sym = trade["symbol"]
        direction = trade["direction"]
        lots = trade["lots_total"]
        entry = trade["entry_price"]
        sl = trade["initial_sl"]
        tgt1 = trade["target1_price"]
        sl_pts = self.config.initial_sl_pts
        tgt_pts = self.config.target1_pts
        max_risk = sl_pts * self.config.point_value_per_lot * lots
        tgt1_gain = tgt_pts * self.config.point_value_per_lot * 1  # 1 lot profit

        msg = (
            f"🚀 <b>SILVER MINI BREAKOUT ENTERED [{mode}]</b>\n\n"
            f"Contract: <code>{sym}</code> (<b>{direction}</b>)\n"
            f"Quantity: <b>{lots} Lots (10 kg)</b>\n"
            f"Entry Price: <b>₹{entry:,.1f}</b>\n"
            f"Initial SL: <b>₹{sl:,.1f}</b> (-{sl_pts:.0f} pts | Risk: ₹{max_risk:,.0f})\n"
            f"Target 1: <b>₹{tgt1:,.1f}</b> (+{tgt_pts:.0f} pts | Gain: ₹{tgt1_gain:,.0f})\n\n"
            f"<i>Plan: Book Lot 1 @ Target 1 & Trail Lot 2 SL to Cost.</i>"
        )
        self.notifier.send_message(msg)

    def _notify_target1(self, trade: Dict[str, Any], total_day_pnl: float) -> None:
        if not self.notifier:
            return
        mode = self.config.mode
        sym = trade["symbol"]
        direction = trade["direction"]
        exit_p = trade["lot1_exit_price"]
        lot1_gain = trade["lot1_pnl_rs"]
        entry = trade["entry_price"]

        msg = (
            f"🎯 <b>SILVER MINI TARGET 1 HIT [{mode}]</b>\n\n"
            f"Contract: <code>{sym}</code> ({direction})\n"
            f"Lot 1 Exited: <b>1 Lot @ ₹{exit_p:,.1f}</b> (+{self.config.target1_pts:.0f} pts)\n"
            f"Booked P&L: <b>+₹{lot1_gain:+,.2f}</b>\n\n"
            f"🛡️ <b>Action: Lot 2 Stop Loss Trailed to Cost (₹{entry:,.1f})!</b>\n"
            f"Runner Lot 2 is now 100% Risk-Free.\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_sl(self, trade: Dict[str, Any], exit_price: float, trade_pnl: float, total_day_pnl: float) -> None:
        if not self.notifier:
            return
        mode = self.config.mode
        sym = trade["symbol"]
        direction = trade["direction"]

        msg = (
            f"🛑 <b>SILVER MINI STOP LOSS HIT [{mode}]</b>\n\n"
            f"Contract: <code>{sym}</code> ({direction})\n"
            f"Exited: <b>{trade['lots_total']} Lots @ ₹{exit_price:,.1f}</b>\n"
            f"Trade P&L: <b>₹{trade_pnl:+,.2f}</b>\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_runner_exit(self, trade: Dict[str, Any], reason: str, total_day_pnl: float) -> None:
        if not self.notifier:
            return
        mode = self.config.mode
        sym = trade["symbol"]
        direction = trade["direction"]
        exit_p = trade["lot2_exit_price"]
        lot2_gain = trade["lot2_pnl_rs"]

        msg = (
            f"🏁 <b>SILVER MINI RUNNER EXITED [{mode}]</b>\n\n"
            f"Contract: <code>{sym}</code> ({direction})\n"
            f"Lot 2 Exited: <b>1 Lot @ ₹{exit_p:,.1f}</b>\n"
            f"Exit Reason: <b>{reason}</b>\n"
            f"Runner P&L: <b>₹{lot2_gain:+,.2f}</b>\n"
            f"Trade Total P&L: <b>₹{trade['lot1_pnl_rs'] + lot2_gain:+,.2f}</b>\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    # -------------------------------------------------------------------------
    # Market Data & Signal Processing
    # -------------------------------------------------------------------------
    def fetch_recent_candles(self, token: int, num_candles: int = 70) -> Optional[pd.DataFrame]:
        """Fetches recent 15m candles from Kite Connect."""
        if not self.kite:
            return None
        try:
            to_d = datetime.now()
            from_d = to_d - timedelta(days=6)
            records = self.kite.historical_data(token, from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d"), "15minute")
            if not records:
                return None
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df["time"] = df["date"].dt.time
            df["day"] = df["date"].dt.date
            return df
        except Exception as e:
            err_str = str(e).lower()
            if "token" in err_str or "api_key" in err_str or "access_token" in err_str or "tokenexception" in type(e).__name__.lower():
                logger.warning("Token error while fetching 15m candles (%s). Re-authenticating...", e)
                self._ensure_kite_session(force_login=True)
                try:
                    records = self.kite.historical_data(token, from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d"), "15minute")
                    if not records:
                        return None
                    df = pd.DataFrame(records)
                    df["date"] = pd.to_datetime(df["date"])
                    df["time"] = df["date"].dt.time
                    df["day"] = df["date"].dt.date
                    return df
                except Exception as retry_err:
                    logger.error("Failed to fetch 15m candles after re-auth: %s", retry_err)
                    return None
            logger.error("Failed to fetch recent 15m candles for token %s: %s", token, e)
            return None

    def evaluate_breakout_signal(
        self,
        df: pd.DataFrame,
        ref_high: float,
        ref_low: float,
        current_time: dt_time,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates whether current price forms a valid 18:00 breakout
        confirmed by 50 EMA trend filter and 1.2x volume average.
        """
        if len(df) < 50:
            return None

        df["vol_sma20"] = df["volume"].rolling(20).mean().shift(1)
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

        latest_bar = df.iloc[-1]
        b_high = float(latest_bar["high"])
        b_low = float(latest_bar["low"])
        b_close = float(latest_bar["close"])
        b_open = float(latest_bar["open"])
        b_vol = float(latest_bar["volume"])
        vol_avg = float(latest_bar["vol_sma20"]) if not np.isnan(latest_bar["vol_sma20"]) else 100.0
        ema50_val = float(latest_bar["ema50"])

        vol_ok = b_vol >= self.config.vol_multiplier * vol_avg

        # LONG Breakout: Close > ref_high, above 50 EMA, with volume confirmation
        long_ok = (b_high > ref_high) and (b_close > ref_high) and vol_ok
        if self.config.use_trend_filter:
            long_ok = long_ok and (b_close > ema50_val)

        # SHORT Breakout: Close < ref_low, below 50 EMA, with volume confirmation
        short_ok = (b_low < ref_low) and (b_close < ref_low) and vol_ok
        if self.config.use_trend_filter:
            short_ok = short_ok and (b_close < ema50_val)

        if long_ok:
            return {
                "direction": "LONG",
                "trigger_price": ref_high + 5.0,
                "ref_high": ref_high,
                "ref_low": ref_low,
                "candle_time": latest_bar["time"],
            }
        elif short_ok:
            return {
                "direction": "SHORT",
                "trigger_price": ref_low - 5.0,
                "ref_high": ref_high,
                "ref_low": ref_low,
                "candle_time": latest_bar["time"],
            }
        return None

    # -------------------------------------------------------------------------
    # Position Lifecycle & 2-Lot Tranche Execution
    # -------------------------------------------------------------------------
    def monitor_active_trade(self, current_ltp: float, current_time: dt_time) -> None:
        """
        Monitors active 2-lot trade:
        - Lot 1 exits at Target 1 (+800 pts) and moves Lot 2 SL to Cost.
        - Stop loss triggers if price violates SL before Target 1.
        - Lot 2 exits at 22:45 IST session close or stops out at Cost.
        """
        state = self.state
        if not state or state.get("status") != "ACTIVE_TRADE":
            return

        trade = state["trade"]
        if not trade or trade.get("status") != "OPEN":
            return

        direction = trade["direction"]
        entry = float(trade["entry_price"])
        current_sl = float(trade["current_sl"])
        tgt1 = float(trade["target1_price"])
        sym = trade["symbol"]

        # Case 1: Lot 1 Target 1 Hit (+800 pts)
        if not trade["lot1_closed"]:
            hit_tgt1 = (direction == "LONG" and current_ltp >= tgt1) or (direction == "SHORT" and current_ltp <= tgt1)
            hit_sl_early = (direction == "LONG" and current_ltp <= current_sl) or (direction == "SHORT" and current_ltp >= current_sl)

            if hit_tgt1:
                exit_price = tgt1
                exit_res = self.order_manager.place_exit(sym, direction, 1, exit_price, reason="TARGET_1_HIT")
                fill_p = float(exit_res.get("average_price") or exit_price) if isinstance(exit_res, dict) else exit_price
                pts = (fill_p - entry) if direction == "LONG" else (entry - fill_p)
                lot1_pnl = pts * self.config.point_value_per_lot
                trade["lot1_closed"] = True
                trade["lot1_exit_price"] = fill_p
                trade["lot1_pnl_rs"] = lot1_pnl
                trade["lots_open"] = 1
                trade["current_sl"] = entry  # Trailed to Breakeven
                trade["be_moved"] = True
                state["realized_pnl_rs"] += lot1_pnl
                self.save_state(state)
                logger.info("Lot 1 Target 1 HIT: +Rs %.2f. Lot 2 SL moved to Breakeven @ Rs %.1f", lot1_pnl, entry)
                self._notify_target1(trade, state["realized_pnl_rs"])
                return

            elif hit_sl_early:
                # Both lots stopped out at initial SL
                exit_price = current_sl
                exit_res = self.order_manager.place_exit(sym, direction, trade["lots_total"], exit_price, reason="INITIAL_STOP_LOSS")
                fill_p = float(exit_res.get("average_price") or exit_price) if isinstance(exit_res, dict) else exit_price
                loss_pts = (entry - fill_p) if direction == "LONG" else (fill_p - entry)
                trade_loss = -loss_pts * self.config.point_value_per_lot * trade["lots_total"]
                trade["status"] = "CLOSED"
                trade["lot1_closed"] = True
                trade["lot2_closed"] = True
                trade["lot1_exit_price"] = fill_p
                trade["lot2_exit_price"] = fill_p
                trade["lot1_pnl_rs"] = -loss_pts * self.config.point_value_per_lot
                trade["lot2_pnl_rs"] = -loss_pts * self.config.point_value_per_lot
                trade["lots_open"] = 0
                state["status"] = "COMPLETED"
                state["realized_pnl_rs"] += trade_loss
                self.save_state(state)
                logger.warning("Stop loss hit on both lots @ Rs %.1f (Loss: Rs %.2f)", fill_p, trade_loss)
                self._notify_sl(trade, fill_p, trade_loss, state["realized_pnl_rs"])
                return

        # Case 2: Manage Lot 2 (Runner) after Lot 1 is banked
        if trade["lot1_closed"] and not trade["lot2_closed"]:
            # Check if stopped out at Breakeven
            hit_be_sl = (direction == "LONG" and current_ltp <= current_sl) or (direction == "SHORT" and current_ltp >= current_sl)
            # Check 22:45 session cutoff
            hit_eod = current_time >= self.config.session_cutoff_time

            if hit_be_sl:
                exit_price = current_sl
                lot2_pnl = 0.0  # Stopped out flat at cost
                self.order_manager.place_exit(sym, direction, 1, exit_price, reason="TRAILED_BREAKEVEN_EXIT")
                trade["status"] = "CLOSED"
                trade["lot2_closed"] = True
                trade["lot2_exit_price"] = exit_price
                trade["lot2_pnl_rs"] = lot2_pnl
                trade["lots_open"] = 0
                state["status"] = "COMPLETED"
                self.save_state(state)
                logger.info("Runner Lot 2 stopped out at Breakeven (Rs %.1f). Trade completed.", exit_price)
                self._notify_runner_exit(trade, "TRAILED_BREAKEVEN_EXIT", state["realized_pnl_rs"])
                return

            elif hit_eod:
                exit_price = current_ltp
                exit_res = self.order_manager.place_exit(sym, direction, 1, exit_price, reason="SESSION_CUTOFF_EXIT")
                fill_p = float(exit_res.get("average_price") or exit_price) if isinstance(exit_res, dict) else exit_price
                pts = (fill_p - entry) if direction == "LONG" else (entry - fill_p)
                lot2_pnl = pts * self.config.point_value_per_lot
                trade["status"] = "CLOSED"
                trade["lot2_closed"] = True
                trade["lot2_exit_price"] = fill_p
                trade["lot2_pnl_rs"] = lot2_pnl
                trade["lots_open"] = 0
                state["status"] = "COMPLETED"
                state["realized_pnl_rs"] += lot2_pnl
                self.save_state(state)
                logger.info("Runner Lot 2 squared off at EOD (22:45 IST) @ Rs %.1f (P&L: Rs %.2f)", fill_p, lot2_pnl)
                self._notify_runner_exit(trade, "SESSION_CUTOFF_EXIT", state["realized_pnl_rs"])
                return

    def run_session_cycle(self, current_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Executes a single evaluation cycle of the Silver ORB strategy.
        Can be polled in a loop or tested chronologically.
        """
        now = current_dt or datetime.now()
        today = now.date()
        current_t = now.time()

        # Holiday check for MCX Evening Session
        is_hol, reason = is_mcx_trading_holiday(check_date=today, session="EVENING")
        if is_hol:
            logger.debug("MCX evening session closed today (%s). Skipping cycle.", reason)
            return {"status": "HOLIDAY", "reason": reason}

        state = self.initialize_day(today)

        # If today is already completed (1 trade took place and finished), exit peacefully
        if state.get("status") == "COMPLETED":
            return state

        contract = state["contract"]
        sym = contract["tradingsymbol"]
        token = int(contract["instrument_token"])

        # Step 1: Between 17:45 and 18:00, wait for 17:45 candle close
        if current_t < self.config.trade_window_start:
            logger.debug("Pre-US window (%s). Waiting for 18:00 IST trigger window.", current_t.strftime("%H:%M:%S"))
            return state

        # Step 2: At or after 18:00 IST, establish reference bar if not already cached
        if state.get("ref_bar") is None:
            candles = self.fetch_recent_candles(token)
            if candles is not None and not candles.empty:
                # Find the 17:45 candle for today
                today_candles = candles[candles["day"] == today]
                ref_candles = today_candles[today_candles["time"] == self.config.ref_candle_time]
                if not ref_candles.empty:
                    ref_c = ref_candles.iloc[-1]
                    state["ref_bar"] = {
                        "high": float(ref_c["high"]),
                        "low": float(ref_c["low"]),
                        "close": float(ref_c["close"]),
                        "open": float(ref_c["open"]),
                    }
                    state["status"] = "WAITING_TRIGGER"
                    self.save_state(state)
                    logger.info("Established 17:45 reference bar for %s: High=%.1f, Low=%.1f", sym, state["ref_bar"]["high"], state["ref_bar"]["low"])

        # Step 3: If reference bar exists and we are in trade window (18:00 - 19:30), evaluate entry trigger
        if state.get("status") == "WAITING_TRIGGER" and state.get("trade") is None:
            if current_t <= self.config.trade_window_end:
                # Optimize API calls: evaluate candle-close breakout only on new 15m intervals or initial check
                candle_slot = f"{current_t.hour}:{current_t.minute // 15}"
                should_eval = state.get("last_eval_slot") != candle_slot or current_dt is not None
                candles = self.fetch_recent_candles(token) if should_eval else None
                if should_eval:
                    state["last_eval_slot"] = candle_slot
                    self.save_state(state)
                if candles is not None and not candles.empty:
                    ref_h = state["ref_bar"]["high"]
                    ref_l = state["ref_bar"]["low"]
                    sig = self.evaluate_breakout_signal(candles, ref_h, ref_l, current_t)
                    if sig:
                        direction = sig["direction"]
                        entry_p = sig["trigger_price"]
                        sl_p = entry_p - self.config.initial_sl_pts if direction == "LONG" else entry_p + self.config.initial_sl_pts
                        tgt1_p = entry_p + self.config.target1_pts if direction == "LONG" else entry_p - self.config.target1_pts

                        # Submit 2-lot entry order
                        self.order_manager.place_entry(
                            symbol=sym,
                            direction=direction,
                            quantity_lots=self.config.lots,
                            entry_price=entry_p,
                            initial_sl=sl_p,
                            target_pts=self.config.target1_pts,
                        )

                        trade_record = {
                            "symbol": sym,
                            "direction": direction,
                            "entry_price": entry_p,
                            "initial_sl": sl_p,
                            "current_sl": sl_p,
                            "target1_price": tgt1_p,
                            "lots_total": self.config.lots,
                            "lots_open": self.config.lots,
                            "lot1_closed": False,
                            "lot1_exit_price": None,
                            "lot1_pnl_rs": 0.0,
                            "lot2_closed": False,
                            "lot2_exit_price": None,
                            "lot2_pnl_rs": 0.0,
                            "be_moved": False,
                            "status": "OPEN",
                            "entry_time": current_t.strftime("%H:%M:%S"),
                        }
                        state["trade"] = trade_record
                        state["status"] = "ACTIVE_TRADE"
                        state["trades_count"] += 1
                        self.save_state(state)
                        self._notify_entry(trade_record)
            else:
                # Past 19:30 with no trigger: day closes peacefully
                logger.info("Trade window closed (19:30 IST) without breakout trigger. Session completed.")
                state["status"] = "COMPLETED"
                self.save_state(state)
                return state

        # Step 4: If active trade exists, monitor for Target 1, SL, or 22:45 cutoff
        if state.get("status") == "ACTIVE_TRADE" and state.get("trade"):
            # Fetch live quote for symbol
            try:
                full_symbol = f"MCX:{sym}"
                quote = self.kite.quote([full_symbol])
                if full_symbol in quote:
                    ltp = float(quote[full_symbol]["last_price"])
                    self.monitor_active_trade(ltp, current_t)
            except Exception as e:
                err_str = str(e).lower()
                if "token" in err_str or "api_key" in err_str or "access_token" in err_str or "tokenexception" in type(e).__name__.lower():
                    logger.warning("Token error while querying MCX quote (%s). Refreshing session...", e)
                    self._ensure_kite_session(force_login=True)
                else:
                    logger.error("Failed to fetch quote for %s during active trade monitoring: %s", sym, e)

        return self.state or state

    def run_daemon(self) -> None:
        """Automated background daemon loop polling every poll_interval_seconds."""
        logger.info(
            "Starting MCX Silver Mini 18:00 US Session Desk Daemon [%s] (Lots: %d, SL: %.0f pts, Tgt1: %.0f pts)...",
            self.config.mode, self.config.lots, self.config.initial_sl_pts, self.config.target1_pts
        )
        while True:
            try:
                self.run_session_cycle()
            except Exception as e:
                logger.exception("Unexpected error in Silver ORB session cycle: %s", e)
            time.sleep(self.config.poll_interval_seconds)
