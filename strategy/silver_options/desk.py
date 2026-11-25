import html
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.models import SilverStrangleLeg, SilverStranglePosition, calculate_silver_mcx_costs
from strategy.silver_options.chain import find_silver_strangle_strikes
from strategy.silver_options.order_manager import SilverOrderManager
from strategy.holidays import is_mcx_trading_holiday
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("strategy.silver.desk")


class SilverStrangleDesk:
    """Automated Execution & Lifecycle Engine for MCX SILVERM Evening Intraday Strangle."""

    def __init__(
        self,
        order_manager: SilverOrderManager,
        config: Optional[SilverOptionsConfig] = None,
        state_file: Optional[Path] = None,
        notifier: Optional[TelegramNotifier] = None,
        kite_client: Optional[KiteConnect] = None,
    ):
        self.config = config or SilverOptionsConfig()
        self.order_manager = order_manager
        self.kite = kite_client or getattr(order_manager, "kite", None)
        self.notifier = notifier
        self.state_file = Path(state_file or self.config.state_file)
        self.position: Optional[SilverStranglePosition] = None
        self.load_state()

    def load_state(self, today_str: Optional[str] = None) -> Optional[SilverStranglePosition]:
        if not self.state_file.exists():
            self.position = None
            return None
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            pos = SilverStranglePosition.from_dict(data)
            today = today_str or datetime.now().strftime("%Y-%m-%d")
            if pos.trade_date != today:
                logger.info(
                    "State file contains prior date (%s != %s). Clearing stale state for fresh session.",
                    pos.trade_date, today
                )
                self.clear_state()
                return None
            self.position = pos
            logger.info("Loaded existing Silver Strangle position for date: %s (Status: %s)", self.position.trade_date, self.position.status)
            return self.position
        except Exception as e:
            logger.warning("Could not read Silver state file %s: %s", self.state_file, e)
            self.position = None
            return None

    def save_state(self):
        if self.position is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self.position.to_dict(), f, indent=2)
        except Exception as e:
            logger.error("Failed to save Silver state to %s: %s", self.state_file, e)

    def clear_state(self):
        if self.state_file.exists():
            try:
                self.state_file.unlink()
                logger.info("Cleared state file %s", self.state_file)
            except Exception as e:
                logger.error("Failed to remove state file %s: %s", self.state_file, e)
        self.position = None

    def _save_aborted_state(self, now: datetime, reason: str, spot: float = 0.0):
        """Persists an ABORTED terminal state for today so restarts never retry entry."""
        today_str = now.strftime("%Y-%m-%d")
        logger.error("Silver Strangle Entry ABORTED (%s): %s", today_str, reason)
        self.position = SilverStranglePosition(
            trade_date=today_str,
            underlying_spot=spot,
            lots=self.config.lots,
            multiplier=self.config.multiplier,
            max_loss_cap=self.config.max_portfolio_loss,
            ce_leg=None,
            pe_leg=None,
            status="ABORTED",
            created_at=now.isoformat(),
            closed_at=now.isoformat(),
            abort_reason=reason,
        )
        self.save_state()
        if self.notifier:
            try:
                self.notifier.send_message(
                    f"🚨 <b>MCX SILVERM ENTRY ABORTED</b>\n"
                    f"• <b>Date</b>: {today_str}\n"
                    f"• <b>Reason</b>: {html.escape(reason)}\n"
                    f"• Entry halted for today to prevent duplicate/unhedged orders."
                )
            except Exception as e:
                logger.error("Failed to send Telegram abort alert: %s", e)

    def _has_open_broker_silver_positions(self) -> Optional[str]:
        """
        Checks Kite net MCX positions for any already-open SILVERM option legs.
        Returns a descriptive reason string if open positions exist or broker check fails,
        or None if safe to enter.
        """
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode != "LIVE" or self.kite is None:
            return None

        try:
            raw_pos = self.kite.positions()
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                try:
                    self.kite = ensure_kite_session(self.kite, force_login=False)
                    self.order_manager.kite = self.kite
                    raw_pos = self.kite.positions()
                except Exception as retry_e:
                    return f"Failed to verify existing MCX broker positions after token refresh: {retry_e}"
            else:
                return f"Failed to verify existing MCX broker positions before entry: {e}"

        net_positions = raw_pos.get("net", []) if isinstance(raw_pos, dict) else []
        open_silver_opts = [
            p for p in net_positions
            if p.get("exchange") == self.config.exchange
            and str(p.get("tradingsymbol", "")).startswith(self.config.underlying)
            and str(p.get("tradingsymbol", "")).endswith(("CE", "PE"))
            and int(p.get("quantity") or 0) != 0
        ]
        if open_silver_opts:
            summary = ", ".join(f"{p.get('tradingsymbol')} (qty={p.get('quantity')})" for p in open_silver_opts)
            return f"Existing open MCX {self.config.underlying} option position(s) found on broker: {summary}"
        return None

    def _rollback_submitted_leg(self, order_info: Optional[Dict[str, Any]], symbol: str, ref_ltp: float):
        """Cancels any pending order and buys back any filled quantity for a single leg on abort."""
        if not isinstance(order_info, dict):
            return
        order_id = order_info.get("order_id")
        if not order_id:
            return
        try:
            self.order_manager.cancel_order(order_id)
            status_info = self.order_manager.get_order_status(order_id)
            filled_qty = int(status_info.get("filled_quantity") or 0)
            if filled_qty > 0:
                logger.warning("Rolling back filled leg %s (%d lots) via BUY square-off...", symbol, filled_qty)
                self.order_manager.buy(
                    symbol=symbol,
                    quantity=filled_qty,
                    price=ref_ltp * 1.05,
                    order_type=self.config.order_type,
                    buffer_pct=self.config.limit_buffer_pct,
                )
        except Exception as clean_err:
            logger.error("CRITICAL: Failed to roll back %s leg (%s): %s", symbol, order_id, clean_err)

    def _wait_for_entry_orders(
        self,
        ce_id: Optional[str],
        pe_id: Optional[str],
        timeout_seconds: int = 20,
        poll_seconds: int = 1,
    ) -> Dict[str, Dict[str, Any]]:
        """Polls both entry orders until terminal status or timeout_seconds."""
        order_ids = [oid for oid in (ce_id, pe_id) if oid]
        terminal_statuses = {"COMPLETE", "REJECTED", "CANCELLED"}
        results: Dict[str, Dict[str, Any]] = {}
        start_ts = time.time()

        while True:
            all_terminal = True
            for oid in order_ids:
                if oid not in results or results[oid].get("status") not in terminal_statuses:
                    results[oid] = self.order_manager.get_order_status(oid)
                if results[oid].get("status") not in terminal_statuses:
                    all_terminal = False

            if all_terminal:
                return results
            elapsed = time.time() - start_ts
            if elapsed >= timeout_seconds:
                return results
            time.sleep(min(poll_seconds, max(0.2, timeout_seconds - elapsed)))

    def enter_strangle(self, now: Optional[datetime] = None) -> bool:
        """
        Executes atomic entry into the evening short strangle at 20:00 IST.
        Sells OTM CE and OTM PE legs concurrently, verifies both fills, rolls back
        any partial/unhedged leg on failure, and persists ABORTED state so restarts never duplicate orders.
        """
        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        if self.position and self.position.trade_date == today_str:
            if self.position.status == "OPEN":
                logger.info("Silver Strangle already active today (%s). Resuming existing position.", today_str)
                return True
            logger.info("Silver Strangle for today (%s) is already %s. Skipping entry.", today_str, self.position.status)
            return False

        mode = getattr(self.order_manager, "mode", "PAPER")

        # Guard: Never enter if open SILVERM positions already exist on Zerodha
        broker_conflict = self._has_open_broker_silver_positions()
        if broker_conflict:
            self._save_aborted_state(now, broker_conflict)
            return False

        logger.info("Executing 20:00 IST Entry for %s (%d lots = %.1f kg)...", self.config.underlying, self.config.lots, self.config.multiplier)

        # 1. Resolve Strikes (refuse hardcoded fallback prices in LIVE mode)
        try:
            strikes_info = find_silver_strangle_strikes(
                kite=self.kite,
                underlying=self.config.underlying,
                strike_otm_pct=self.config.strike_otm_pct,
                exchange=self.config.exchange,
                allow_fallback=(mode != "LIVE"),
                min_oi=self.config.min_option_oi,
                max_spread_pct=self.config.max_bid_ask_spread_pct,
                scan_steps=self.config.strike_scan_steps,
            )
        except Exception as e:
            self._save_aborted_state(now, f"Strike resolution failed: {e}")
            return False

        ce_sym = strikes_info["ce_symbol"]
        pe_sym = strikes_info["pe_symbol"]
        ce_ltp = strikes_info["ce_ltp"]
        pe_ltp = strikes_info["pe_ltp"]
        ce_bid = strikes_info.get("ce_bid")
        pe_bid = strikes_info.get("pe_bid")
        spot = strikes_info["spot_price"]

        if ce_ltp <= 0.5 or pe_ltp <= 0.5:
            self._save_aborted_state(now, f"Invalid option LTP (CE={ce_ltp:.2f}, PE={pe_ltp:.2f})", spot=spot)
            return False

        # 2. Execute CE and PE SELL orders concurrently (anchored to live best bid when available)
        def _place_leg(sym: str, ltp: float, best_bid: Optional[float]) -> Dict[str, Any]:
            return self.order_manager.sell(
                symbol=sym,
                quantity=self.config.lots,
                price=ltp,
                order_type=self.config.order_type,
                buffer_pct=self.config.limit_buffer_pct,
                best_bid=best_bid,
                max_slippage_pct=self.config.max_limit_slippage_pct,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_ce = executor.submit(_place_leg, ce_sym, ce_ltp, ce_bid)
            future_pe = executor.submit(_place_leg, pe_sym, pe_ltp, pe_bid)

        ce_order = None
        pe_order = None
        ce_err = None
        pe_err = None

        try:
            ce_order = future_ce.result()
        except Exception as e:
            ce_err = e

        try:
            pe_order = future_pe.result()
        except Exception as e:
            pe_err = e

        if ce_err or pe_err:
            if not ce_err and ce_order:
                self._rollback_submitted_leg(ce_order, ce_sym, ce_ltp)
            if not pe_err and pe_order:
                self._rollback_submitted_leg(pe_order, pe_sym, pe_ltp)
            err_msg = f"Entry order placement failed (CE err={ce_err}, PE err={pe_err})"
            self._save_aborted_state(now, err_msg, spot=spot)
            return False

        ce_id = ce_order.get("order_id") if isinstance(ce_order, dict) else None
        pe_id = pe_order.get("order_id") if isinstance(pe_order, dict) else None

        # 3. Verify both orders actually COMPLETE in LIVE mode (catch async RMS/OI rejections)
        ce_fill = float(ce_order.get("fill_price", ce_ltp))
        pe_fill = float(pe_order.get("fill_price", pe_ltp))

        if mode == "LIVE":
            statuses = self._wait_for_entry_orders(ce_id, pe_id, timeout_seconds=20, poll_seconds=1)
            ce_st = statuses.get(ce_id, {})
            pe_st = statuses.get(pe_id, {})
            ce_filled = int(ce_st.get("filled_quantity") or 0)
            pe_filled = int(pe_st.get("filled_quantity") or 0)
            ce_ok = ce_st.get("status") == "COMPLETE" and ce_filled == self.config.lots
            pe_ok = pe_st.get("status") == "COMPLETE" and pe_filled == self.config.lots

            if not (ce_ok and pe_ok):
                self._rollback_submitted_leg(ce_order, ce_sym, ce_ltp)
                self._rollback_submitted_leg(pe_order, pe_sym, pe_ltp)
                reason = (
                    f"Entry verification failed: CE({ce_sym}: {ce_st.get('status')}, "
                    f"filled={ce_filled}/{self.config.lots}, msg={ce_st.get('status_message')}); "
                    f"PE({pe_sym}: {pe_st.get('status')}, "
                    f"filled={pe_filled}/{self.config.lots}, msg={pe_st.get('status_message')})"
                )
                self._save_aborted_state(now, reason, spot=spot)
                return False

            if float(ce_st.get("average_price") or 0) > 0:
                ce_fill = float(ce_st["average_price"])
            if float(pe_st.get("average_price") or 0) > 0:
                pe_fill = float(pe_st["average_price"])

        # 4. Calculate SL for each leg
        ce_sl = round(ce_fill * (1.0 + self.config.sl_pct), 2)
        pe_sl = round(pe_fill * (1.0 + self.config.sl_pct), 2)

        entry_time_str = now.strftime("%H:%M:%S")

        ce_leg = SilverStrangleLeg(
            symbol=ce_sym,
            strike=strikes_info["ce_strike"],
            option_type="CE",
            entry_price=ce_fill,
            entry_time=entry_time_str,
            stop_loss_price=ce_sl,
            current_price=ce_fill,
            order_id=ce_id,
        )

        pe_leg = SilverStrangleLeg(
            symbol=pe_sym,
            strike=strikes_info["pe_strike"],
            option_type="PE",
            entry_price=pe_fill,
            entry_time=entry_time_str,
            stop_loss_price=pe_sl,
            current_price=pe_fill,
            order_id=pe_id,
        )

        self.position = SilverStranglePosition(
            trade_date=today_str,
            underlying_spot=spot,
            lots=self.config.lots,
            multiplier=self.config.multiplier,
            max_loss_cap=self.config.max_portfolio_loss,
            ce_leg=ce_leg,
            pe_leg=pe_leg,
            status="OPEN",
            created_at=now.isoformat(),
        )

        self.save_state()

        logger.info(
            "Silver Strangle Entry Complete! CE: %s @ Rs %.2f (SL: Rs %.2f) | PE: %s @ Rs %.2f (SL: Rs %.2f) | Max Risk: Rs %.2f",
            ce_sym, ce_fill, ce_sl, pe_sym, pe_fill, pe_sl, self.config.max_portfolio_loss
        )

        if self.notifier:
            try:
                msg = (
                    f"🥈 <b>MCX SILVERM STRANGLE ENTERED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Lots</b>: {self.config.lots} ({self.config.multiplier:.0f} kg)\n"
                    f"• <b>Spot</b>: ₹{spot:,.1f}\n"
                    f"• <b>Call</b>: {ce_sym} @ ₹{ce_fill:,.1f} (SL: ₹{ce_sl:,.1f})\n"
                    f"• <b>Put</b>: {pe_sym} @ ₹{pe_fill:,.1f} (SL: ₹{pe_sl:,.1f})\n"
                    f"• <b>Combined Premium</b>: ₹{ce_fill + pe_fill:,.1f}\n"
                    f"• <b>Max Risk Cap</b>: ₹{self.config.max_portfolio_loss:,.2f} (Calculated for {self.config.lots} lots)\n"
                    f"• <b>Exit Time</b>: {self.config.exit_time} IST"
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error("Failed to send Telegram entry alert: %s", e)

        return True

    def monitor_cycle(self, now: Optional[datetime] = None) -> bool:
        """
        Executes a single monitoring evaluation pass:
        - Updates LTPs
        - Enforces single-leg 40% SL and trails surviving leg to cost
        - Checks target decay (70%)
        - Enforces portfolio max loss cap
        - Enforces mandatory 23:15 IST EOD exit
        Returns True if position is still active, False if fully closed.
        """
        if not self.position or self.position.status in ("CLOSED", "ABORTED"):
            return False
        if self.position.ce_leg is None or self.position.pe_leg is None:
            return False

        now = now or datetime.now()
        curr_time_str = now.strftime("%H:%M")

        # 1. Update live quotes
        ce_sym = self.position.ce_leg.symbol
        pe_sym = self.position.pe_leg.symbol

        ce_ltp = self.position.ce_leg.current_price
        pe_ltp = self.position.pe_leg.current_price
        ce_ask: Optional[float] = None
        pe_ask: Optional[float] = None

        if self.kite is not None:
            try:
                quotes = self.kite.quote([f"{self.config.exchange}:{ce_sym}", f"{self.config.exchange}:{pe_sym}"])
                ce_key = f"{self.config.exchange}:{ce_sym}"
                pe_key = f"{self.config.exchange}:{pe_sym}"
                if ce_key in quotes and quotes[ce_key].get("last_price"):
                    ce_ltp = float(quotes[ce_key]["last_price"])
                    ce_sell_depth = (quotes[ce_key].get("depth") or {}).get("sell") or []
                    if ce_sell_depth and ce_sell_depth[0].get("price"):
                        ce_ask = float(ce_sell_depth[0]["price"])
                if pe_key in quotes and quotes[pe_key].get("last_price"):
                    pe_ltp = float(quotes[pe_key]["last_price"])
                    pe_sell_depth = (quotes[pe_key].get("depth") or {}).get("sell") or []
                    if pe_sell_depth and pe_sell_depth[0].get("price"):
                        pe_ask = float(pe_sell_depth[0]["price"])
            except Exception as e:
                logger.warning("Failed to refresh quotes during monitor cycle: %s", e)

        if self.position.ce_leg.status == "OPEN":
            self.position.ce_leg.current_price = ce_ltp
        if self.position.pe_leg.status == "OPEN":
            self.position.pe_leg.current_price = pe_ltp

        # 2. Check EOD 23:15 IST Mandatory Square-Off
        if curr_time_str >= self.config.exit_time:
            logger.info("23:15 IST EOD Cutoff reached. Squaring off all open legs.")
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="EOD", best_ask=ce_ask)
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="EOD", best_ask=pe_ask)
            self._finalize_close(now, reason="EOD")
            return False

        # 3. Check Portfolio Max Loss Cap (derived from number of lots)
        current_pnl = self.position.total_pnl
        if current_pnl <= -abs(self.position.max_loss_cap):
            logger.warning("Portfolio max loss limit breached (P&L: Rs %.2f <= -Rs %.2f). Emergency square-off!", current_pnl, self.position.max_loss_cap)
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="MAX_PORTFOLIO_LOSS", best_ask=ce_ask)
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="MAX_PORTFOLIO_LOSS", best_ask=pe_ask)
            self._finalize_close(now, reason="MAX_PORTFOLIO_LOSS")
            return False

        # 4. Check Single-Leg Stop Losses
        ce_stopped = False
        pe_stopped = False

        if self.position.ce_leg.status == "OPEN" and ce_ltp >= self.position.ce_leg.stop_loss_price:
            logger.warning("CE Leg hit Stop Loss (LTP: Rs %.2f >= SL: Rs %.2f)", ce_ltp, self.position.ce_leg.stop_loss_price)
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="SL", best_ask=ce_ask)
            ce_stopped = True

        if self.position.pe_leg.status == "OPEN" and pe_ltp >= self.position.pe_leg.stop_loss_price:
            logger.warning("PE Leg hit Stop Loss (LTP: Rs %.2f >= SL: Rs %.2f)", pe_ltp, self.position.pe_leg.stop_loss_price)
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="SL", best_ask=pe_ask)
            pe_stopped = True

        # 5. Trail to Cost Logic
        if self.config.trail_to_cost:
            # If CE stopped out, trail PE to cost
            if ce_stopped and self.position.pe_leg.status == "OPEN":
                old_sl = self.position.pe_leg.stop_loss_price
                self.position.pe_leg.stop_loss_price = self.position.pe_leg.entry_price
                logger.info("Trailed PE SL to cost: Rs %.2f -> Rs %.2f", old_sl, self.position.pe_leg.stop_loss_price)

            # If PE stopped out, trail CE to cost
            if pe_stopped and self.position.ce_leg.status == "OPEN":
                old_sl = self.position.ce_leg.stop_loss_price
                self.position.ce_leg.stop_loss_price = self.position.ce_leg.entry_price
                logger.info("Trailed CE SL to cost: Rs %.2f -> Rs %.2f", old_sl, self.position.ce_leg.stop_loss_price)

            # Proactive trailing: if winning leg decayed by 30%+, lock other leg to cost
            if self.position.ce_leg.status == "OPEN" and self.position.pe_leg.status == "OPEN":
                ce_decay = (self.position.ce_leg.entry_price - ce_ltp) / self.position.ce_leg.entry_price
                pe_decay = (self.position.pe_leg.entry_price - pe_ltp) / self.position.pe_leg.entry_price
                if ce_decay >= self.config.trail_activation_decay_pct and self.position.pe_leg.stop_loss_price > self.position.pe_leg.entry_price:
                    self.position.pe_leg.stop_loss_price = self.position.pe_leg.entry_price
                    logger.info("CE decayed %.1f%% -> Trailed PE SL to cost (Rs %.2f)", ce_decay * 100, self.position.pe_leg.stop_loss_price)
                if pe_decay >= self.config.trail_activation_decay_pct and self.position.ce_leg.stop_loss_price > self.position.ce_leg.entry_price:
                    self.position.ce_leg.stop_loss_price = self.position.ce_leg.entry_price
                    logger.info("PE decayed %.1f%% -> Trailed CE SL to cost (Rs %.2f)", pe_decay * 100, self.position.ce_leg.stop_loss_price)

        # 6. Check Target Combined Decay (70%)
        if self.position.ce_leg.status == "OPEN" and self.position.pe_leg.status == "OPEN":
            total_entry = self.position.ce_leg.entry_price + self.position.pe_leg.entry_price
            total_curr = ce_ltp + pe_ltp
            combined_decay = (total_entry - total_curr) / total_entry
            if combined_decay >= self.config.target_decay_pct:
                logger.info("Target Decay achieved (%.1f%% >= %.1f%%). Taking profit on both legs!", combined_decay * 100, self.config.target_decay_pct * 100)
                self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="TARGET_DECAY", best_ask=ce_ask)
                self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="TARGET_DECAY", best_ask=pe_ask)
                self._finalize_close(now, reason="TARGET_DECAY")
                return False

        if self.position.is_fully_closed():
            self._finalize_close(now, reason="BOTH_LEGS_CLOSED")
            return False

        self.save_state()
        return True

    def _square_off_leg(
        self,
        leg: SilverStrangleLeg,
        ltp: float,
        now: datetime,
        reason: str,
        best_ask: Optional[float] = None,
    ):
        if leg.status != "OPEN":
            return
        logger.info("Squaring off %s leg %s @ Rs %.2f (Reason: %s)", leg.option_type, leg.symbol, ltp, reason)
        buy_order = self.order_manager.buy(
            symbol=leg.symbol,
            quantity=self.config.lots,
            price=ltp,
            order_type=self.config.order_type,
            buffer_pct=self.config.limit_buffer_pct,
            best_ask=best_ask,
            max_slippage_pct=self.config.max_limit_slippage_pct,
        )
        fill_price = float(buy_order.get("fill_price", ltp))
        leg.exit_price = fill_price
        leg.exit_time = now.strftime("%H:%M:%S")
        leg.exit_reason = reason
        leg.status = reason
        leg.exit_order_id = buy_order.get("order_id")

        if self.notifier:
            try:
                pts = leg.points_captured
                pnl = leg.pnl(self.config.multiplier)
                icon = "🟢" if pts >= 0 else "🔴"
                msg = (
                    f"{icon} <b>MCX SILVERM LEG SQUARED OFF</b>\n"
                    f"• <b>Symbol</b>: {leg.symbol} ({leg.option_type})\n"
                    f"• <b>Exit Price</b>: ₹{fill_price:,.1f} (Entry: ₹{leg.entry_price:,.1f})\n"
                    f"• <b>Points</b>: {pts:+,.1f} pts\n"
                    f"• <b>P&L</b>: ₹{pnl:+,.2f}\n"
                    f"• <b>Reason</b>: {reason}"
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error("Failed to send Telegram leg square-off alert: %s", e)

    def _finalize_close(self, now: datetime, reason: str):
        if not self.position:
            return
        self.position.status = "CLOSED"
        self.position.closed_at = now.isoformat()
        self.save_state()

        pts = self.position.total_points
        pnl = self.position.total_pnl
        icon = "🎉" if pnl >= 0 else "⚠️"
        logger.info("Silver Strangle Session Finalized! Combined Points: %+.2f pts | Net P&L: Rs %+.2f (Reason: %s)", pts, pnl, reason)

        if self.notifier:
            try:
                msg = (
                    f"{icon} <b>MCX SILVERM DAILY SESSION COMPLETED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Date</b>: {self.position.trade_date}\n"
                    f"• <b>Lots</b>: {self.config.lots} ({self.config.multiplier:.0f} kg)\n"
                    f"• <b>Call Result</b>: {self.position.ce_leg.points_captured:+,.1f} pts ({self.position.ce_leg.exit_reason})\n"
                    f"• <b>Put Result</b>: {self.position.pe_leg.points_captured:+,.1f} pts ({self.position.pe_leg.exit_reason})\n"
                    f"• <b>Total Points</b>: {pts:+,.1f} pts\n"
                    f"• <b>Realized Gross P&L</b>: ₹{pnl:+,.2f}\n"
                    f"• <b>Final Reason</b>: {reason}"
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error("Failed to send Telegram session completion alert: %s", e)

    def run_session(self, poll_interval: Optional[int] = None, execute_once: bool = False, now: Optional[datetime] = None):
        """Runs the active evening trading session until completion."""
        poll_secs = poll_interval or self.config.poll_seconds
        logger.info("Starting Silver Strangle session execution loop (Poll: %ds)...", poll_secs)

        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        curr_time = now.strftime("%H:%M")

        if self.position and self.position.trade_date != today_str:
            logger.info("Clearing prior day position (%s != %s) before session start.", self.position.trade_date, today_str)
            self.clear_state()

        # Enter if not yet entered today
        if curr_time >= self.config.entry_time:
            if not self.position or self.position.trade_date != today_str:
                if not self.enter_strangle(now):
                    return
            elif self.position.status in ("CLOSED", "ABORTED"):
                logger.info("Silver Strangle for %s is already %s. Skipping session.", today_str, self.position.status)
                return

        loop_now = now
        while True:
            if not execute_once:
                loop_now = datetime.now()
            is_active = self.monitor_cycle(loop_now)
            if not is_active or execute_once:
                break
            time.sleep(poll_secs)

    def run_scheduler(self):
        """
        Runs the automated 24/7 daemon loop for MCX Silver:
        - Detects MCX holidays and weekends
        - Idles until 20:00 IST
        - Enters and monitors Strangle until 23:15 IST
        - Sleeps overnight until next trading day
        """
        logger.info("=" * 80)
        logger.info("  MCX SILVERM EVENING INTRADAY STRANGLE DAEMON STARTED")
        logger.info("  Lots: %d (%d kg) | Entry: %s IST | Exit: %s IST | SL Per Lot: Rs %.2f (Total SL: Rs %.2f)",
                    self.config.lots, self.config.multiplier, self.config.entry_time, self.config.exit_time,
                    self.config.sl_per_lot, self.config.max_portfolio_loss)
        logger.info("=" * 80)

        while True:
            try:
                now = datetime.now()
                today = now.date()
                today_str = now.strftime("%Y-%m-%d")

                # 0. Daily Date Boundary Check: Clear prior day's state from memory & file
                if self.position and self.position.trade_date != today_str:
                    logger.info("Clearing state from prior date (%s != %s). Starting fresh session for today.", self.position.trade_date, today_str)
                    self.clear_state()

                # 1. Holiday & Weekend Check for MCX Evening Session
                is_holiday, reason = is_mcx_trading_holiday(today, session="EVENING")
                if is_holiday:
                    logger.info("MCX Evening Session is closed today (%s). Sleeping for 30 minutes...", reason)
                    time.sleep(1800)
                    continue

                curr_time = now.strftime("%H:%M")

                # 2. Before 20:00 IST: Idle and wait
                if curr_time < self.config.entry_time:
                    time.sleep(30)
                    continue

                # 3. Active Window: 20:00 to 23:15 IST
                if self.config.entry_time <= curr_time <= self.config.exit_time:
                    if not self.position or self.position.trade_date != today_str:
                        entered = self.enter_strangle(now)
                        if not entered:
                            time.sleep(60)
                            continue
                    if self.position and self.position.trade_date == today_str and self.position.status == "OPEN":
                        while True:
                            active = self.monitor_cycle()
                            if not active:
                                break
                            time.sleep(self.config.poll_seconds)
                    else:
                        # Position already CLOSED or ABORTED today, wait until post-market cutoff
                        time.sleep(60)
                        continue

                # 4. Post 23:15 IST: Clear completed/aborted state from memory & file for tomorrow, then sleep
                elif curr_time > self.config.exit_time:
                    if self.position or self.state_file.exists():
                        logger.info("Post-%s IST session ended (%s). Clearing today's state in memory & file for tomorrow.", self.config.exit_time, today)
                        self.clear_state()
                    else:
                        logger.info("MCX evening trading concluded for today (%s). Sleeping until tomorrow...", today)
                    time.sleep(1800)
            except Exception as e:
                logger.exception("Unhandled error in Silver Strangle scheduler loop: %s. Retrying in 60s...", e)
                time.sleep(60)
