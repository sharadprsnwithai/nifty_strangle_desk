import json
import logging
import os
import time
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
                    "State file contains prior date (%s != %s). Initializing fresh state for today.",
                    pos.trade_date, today
                )
                self.position = None
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

    def enter_strangle(self, now: Optional[datetime] = None) -> bool:
        """
        Executes entry into the evening short strangle at 20:00 IST.
        Sells OTM CE and OTM PE legs with the configured number of lots.
        """
        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        if self.position and self.position.trade_date == today_str and self.position.status != "CLOSED":
            logger.info("Silver Strangle already active today (%s). Resuming existing position.", today_str)
            return True

        logger.info("Executing 20:00 IST Entry for %s (%d lots = %.1f kg)...", self.config.underlying, self.config.lots, self.config.multiplier)

        # 1. Resolve Strikes
        strikes_info = find_silver_strangle_strikes(
            kite=self.kite,
            underlying=self.config.underlying,
            strike_otm_pct=self.config.strike_otm_pct,
            exchange=self.config.exchange,
        )

        ce_sym = strikes_info["ce_symbol"]
        pe_sym = strikes_info["pe_symbol"]
        ce_ltp = strikes_info["ce_ltp"]
        pe_ltp = strikes_info["pe_ltp"]
        spot = strikes_info["spot_price"]

        if ce_ltp <= 0.5 or pe_ltp <= 0.5:
            logger.error("Invalid option LTP: CE: %.2f | PE: %.2f. Aborting entry.", ce_ltp, pe_ltp)
            return False

        # 2. Execute SELL orders
        ce_order = self.order_manager.sell(
            symbol=ce_sym,
            quantity=self.config.lots,
            price=ce_ltp,
            order_type=self.config.order_type,
            buffer_pct=self.config.limit_buffer_pct,
        )

        pe_order = self.order_manager.sell(
            symbol=pe_sym,
            quantity=self.config.lots,
            price=pe_ltp,
            order_type=self.config.order_type,
            buffer_pct=self.config.limit_buffer_pct,
        )

        ce_fill = float(ce_order.get("fill_price", ce_ltp))
        pe_fill = float(pe_order.get("fill_price", pe_ltp))

        # 3. Calculate SL for each leg
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
            order_id=ce_order.get("order_id"),
        )

        pe_leg = SilverStrangleLeg(
            symbol=pe_sym,
            strike=strikes_info["pe_strike"],
            option_type="PE",
            entry_price=pe_fill,
            entry_time=entry_time_str,
            stop_loss_price=pe_sl,
            current_price=pe_fill,
            order_id=pe_order.get("order_id"),
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
        if not self.position or self.position.status == "CLOSED":
            return False

        now = now or datetime.now()
        curr_time_str = now.strftime("%H:%M")

        # 1. Update live quotes
        ce_sym = self.position.ce_leg.symbol
        pe_sym = self.position.pe_leg.symbol

        ce_ltp = self.position.ce_leg.current_price
        pe_ltp = self.position.pe_leg.current_price

        if self.kite is not None:
            try:
                quotes = self.kite.quote([f"{self.config.exchange}:{ce_sym}", f"{self.config.exchange}:{pe_sym}"])
                ce_key = f"{self.config.exchange}:{ce_sym}"
                pe_key = f"{self.config.exchange}:{pe_sym}"
                if ce_key in quotes and quotes[ce_key].get("last_price"):
                    ce_ltp = float(quotes[ce_key]["last_price"])
                if pe_key in quotes and quotes[pe_key].get("last_price"):
                    pe_ltp = float(quotes[pe_key]["last_price"])
            except Exception as e:
                logger.warning("Failed to refresh quotes during monitor cycle: %s", e)

        if self.position.ce_leg.status == "OPEN":
            self.position.ce_leg.current_price = ce_ltp
        if self.position.pe_leg.status == "OPEN":
            self.position.pe_leg.current_price = pe_ltp

        # 2. Check EOD 23:15 IST Mandatory Square-Off
        if curr_time_str >= self.config.exit_time:
            logger.info("23:15 IST EOD Cutoff reached. Squaring off all open legs.")
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="EOD")
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="EOD")
            self._finalize_close(now, reason="EOD")
            return False

        # 3. Check Portfolio Max Loss Cap (derived from number of lots)
        current_pnl = self.position.total_pnl
        if current_pnl <= -abs(self.position.max_loss_cap):
            logger.warning("Portfolio max loss limit breached (P&L: Rs %.2f <= -Rs %.2f). Emergency square-off!", current_pnl, self.position.max_loss_cap)
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="MAX_PORTFOLIO_LOSS")
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="MAX_PORTFOLIO_LOSS")
            self._finalize_close(now, reason="MAX_PORTFOLIO_LOSS")
            return False

        # 4. Check Single-Leg Stop Losses
        ce_stopped = False
        pe_stopped = False

        if self.position.ce_leg.status == "OPEN" and ce_ltp >= self.position.ce_leg.stop_loss_price:
            logger.warning("CE Leg hit Stop Loss (LTP: Rs %.2f >= SL: Rs %.2f)", ce_ltp, self.position.ce_leg.stop_loss_price)
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="SL")
            ce_stopped = True

        if self.position.pe_leg.status == "OPEN" and pe_ltp >= self.position.pe_leg.stop_loss_price:
            logger.warning("PE Leg hit Stop Loss (LTP: Rs %.2f >= SL: Rs %.2f)", pe_ltp, self.position.pe_leg.stop_loss_price)
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="SL")
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
                self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="TARGET_DECAY")
                self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="TARGET_DECAY")
                self._finalize_close(now, reason="TARGET_DECAY")
                return False

        if self.position.is_fully_closed():
            self._finalize_close(now, reason="BOTH_LEGS_CLOSED")
            return False

        self.save_state()
        return True

    def _square_off_leg(self, leg: SilverStrangleLeg, ltp: float, now: datetime, reason: str):
        if leg.status != "OPEN":
            return
        logger.info("Squaring off %s leg %s @ Rs %.2f (Reason: %s)", leg.option_type, leg.symbol, ltp, reason)
        buy_order = self.order_manager.buy(
            symbol=leg.symbol,
            quantity=self.config.lots,
            price=ltp,
            order_type=self.config.order_type,
            buffer_pct=self.config.limit_buffer_pct,
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
            self.position = None

        # Enter if not yet entered
        if curr_time >= self.config.entry_time and (
            not self.position or (self.position.trade_date == today_str and self.position.status != "CLOSED")
        ):
            self.enter_strangle(now)

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
            now = datetime.now()
            today = now.date()
            today_str = now.strftime("%Y-%m-%d")

            # 0. Daily Date Boundary Check: Clear prior day's state
            if self.position and self.position.trade_date != today_str:
                logger.info("Archiving state from prior date (%s != %s). Starting fresh session for today.", self.position.trade_date, today_str)
                self.position = None

            # 1. Holiday & Weekend Check for MCX Evening Session
            is_holiday, reason = is_mcx_trading_holiday(today, session="EVENING")
            if is_holiday:
                logger.info("MCX Evening Session is closed today (%s). Sleeping for 30 minutes...", reason)
                time.sleep(1800)
                continue

            curr_time = now.strftime("%H:%M")

            # 2. Before 20:00 IST: Idle and wait
            if curr_time < self.config.entry_time:
                # Sleep in 30s intervals until 20:00 IST
                time.sleep(30)
                continue

            # 3. Active Window: 20:00 to 23:15 IST
            if self.config.entry_time <= curr_time <= self.config.exit_time:
                if not self.position or (self.position.trade_date == today_str and self.position.status != "CLOSED"):
                    self.enter_strangle(now)
                    while True:
                        active = self.monitor_cycle()
                        if not active:
                            break
                        time.sleep(self.config.poll_seconds)
                else:
                    # Position already completed today, wait until post-market cutoff
                    time.sleep(60)
                    continue

            # 4. Post 23:15 IST: Session closed, sleep until tomorrow
            elif curr_time > self.config.exit_time:
                logger.info("MCX evening trading concluded for today (%s). Sleeping until tomorrow...", today)
                time.sleep(1800)
