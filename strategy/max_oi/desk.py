import html
import json
import logging
import time
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Any
from kiteconnect import KiteConnect

from notifications.telegram import TelegramNotifier
from strategy.holidays import is_trading_holiday, is_market_open
from strategy.max_oi.config import MaxOIConfig
from strategy.max_oi.models import MaxOICandidate, OptionBuySignal, OptionPosition
from strategy.max_oi.scanner import MaxOIScanner

logger = logging.getLogger("strategy.max_oi.desk")


class MaxOIDesk:
    """
    Automated Trading & Alerting Desk for Saurabh's Max OI Unwinding Option Buying Strategy.
    Runs daily in the 2nd half of monthly expiry (after 15th), screening ~200 F&O stocks,
    detecting >20% OI unwinding pressure, and dispatching actionable Telegram alerts.
    """

    def __init__(
        self,
        config: Optional[MaxOIConfig] = None,
        scanner: Optional[MaxOIScanner] = None,
        notifier: Optional[TelegramNotifier] = None,
        kite: Optional[KiteConnect] = None,
        ignore_holidays: bool = False,
    ):
        self.config = config or MaxOIConfig()
        self.scanner = scanner or MaxOIScanner(kite=kite, config=self.config)
        self.notifier = notifier
        self.kite = kite
        self.ignore_holidays = ignore_holidays
        self.positions: Dict[str, OptionPosition] = {}
        self._last_scan_date: Optional[str] = None
        self._last_holiday_log_date: Optional[str] = None
        self._last_premarket_log_date: Optional[str] = None
        self._last_postmarket_log_date: Optional[str] = None
        self._last_waiting_log_date: Optional[str] = None
        self.load_state()

    def load_state(self) -> None:
        """Loads active option buying positions from disk."""
        if self.config.state_file.exists():
            try:
                with open(self.config.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("positions", []):
                        pos = OptionPosition(**item)
                        if pos.status == "OPEN":
                            self.positions[pos.tradingsymbol] = pos
            except Exception as e:
                logger.warning("Failed to load Max OI state file: %s", e)

    def save_state(self) -> None:
        """Persists positions state to disk."""
        try:
            payload = {
                "updated_at": datetime.now().isoformat(),
                "positions": [pos.__dict__ for pos in self.positions.values()]
            }
            with open(self.config.state_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error("Failed to save Max OI state: %s", e)

    def format_candidate_summary_message(self, candidates: List[MaxOICandidate], target_expiry: str) -> str:
        """Builds a formatted Telegram briefing of stocks within 2% of Max OI."""
        lines = [
            "🔍 <b>MAX OI PROXIMITY WATCHLIST (POST-15TH)</b>",
            f"Monthly Expiry: <code>{target_expiry}</code>",
            f"Watchlist: <b>{len(candidates)} F&O Stocks within 2% of Max Strike</b>\n"
        ]
        for c in candidates[:10]:
            near_side = "CALL" if c.is_near_ce else "PUT"
            strike = c.max_ce_strike if c.is_near_ce else c.max_pe_strike
            dist = c.ce_dist_pct if c.is_near_ce else c.pe_dist_pct
            oi_chg = f" (OI: {c.ce_oi_change_pct:+.1f}%)" if c.ce_oi_change_pct is not None else ""
            lines.append(
                f"• <b>{c.symbol}</b> (Spot: Rs {c.spot:.2f}) → Near <b>{strike:.0f} {near_side}</b> ({dist:+.1f}%){oi_chg}"
            )
        if len(candidates) > 10:
            lines.append(f"\n<i>...and {len(candidates) - 10} more stocks under observation.</i>")

        lines.append("\n<i>Monitoring for >20% institutional unwinding and breakout trigger.</i>")
        return "\n".join(lines)

    def format_signal_message(self, signal: OptionBuySignal) -> str:
        """Builds high-priority alert for actionable breakout buy signal."""
        direction = "BULLISH CALL BUY 🚀" if signal.side == "CE" else "BEARISH PUT BUY 📉"
        approx_cost = signal.atm_ltp * signal.lot_size
        max_risk = (signal.atm_ltp - signal.sl_price) * signal.lot_size

        return (
            f"🎯 <b>MAX OI BREAKOUT SIGNAL: {direction}</b>\n\n"
            f"Stock: <b>{signal.symbol}</b> (Spot: Rs {signal.spot_price:.2f})\n"
            f"Breached Strike: <b>{signal.breached_strike:.0f} {signal.side}</b>\n"
            f"Panic Unwinding: <b>{signal.oi_unwinding_pct:.1f}%</b> (Writers Exiting!)\n\n"
            f"Contract: <code>{signal.atm_tradingsymbol}</code>\n"
            f"Entry Premium: <b>Rs {signal.atm_ltp:.2f}</b> (Lot Size: {signal.lot_size})\n"
            f"Approx Capital: Rs {approx_cost:,.2f} (Max Risk: <b>Rs {max_risk:,.2f}</b>)\n\n"
            f"🛡️ Stop Loss (50%): <b>Rs {signal.sl_price:.2f}</b>\n"
            f"🎯 Target (2x / 100%): <b>Rs {signal.target_price:.2f}</b>\n"
            f"⏳ Time Exit: <b>2 Days Max</b> (Cutoff: {signal.max_exit_date} EOD)\n"
            f"Expiry: <code>{signal.expiry_date}</code> | Time: <code>{signal.trigger_time} IST</code>"
        )

    def run_scan_cycle(self, ignore_date_filter: bool = False) -> Dict[str, Any]:
        """Runs single scan cycle and dispatches alerts."""
        result = self.scanner.scan(ignore_date_filter=ignore_date_filter, ignore_holidays=self.ignore_holidays)

        if result.get("status") == "MARKET_CLOSED":
            logger.info("[DESK] Market closed (%s). Skipping scan cycle.", result.get("reason"))
            return result
        if result.get("status") == "IDLE_PRE_15TH":
            logger.info("[DESK] Pre-15th filter active (day %d < 15th). Scanner idle.", result.get("day"))
            return result

        candidates = result.get("candidates", [])
        signals = result.get("signals", [])
        expiry = result.get("expiry", "")

        # 1. Send Signals Alerts (Highest Priority)
        if signals and self.notifier:
            for sig in signals:
                msg = self.format_signal_message(sig)
                self.notifier.send_message(msg)
                logger.info("[ALERT] Sent Telegram signal alert for %s %s", sig.symbol, sig.side)

        # 2. Send Daily Proximity Summary if candidates found
        elif candidates and self.notifier:
            summary_msg = self.format_candidate_summary_message(candidates, expiry)
            self.notifier.send_message(summary_msg)
            logger.info("[SUMMARY] Sent Telegram watchlist summary with %d stocks", len(candidates))

        return result

    def monitor_open_positions(self) -> List[OptionPosition]:
        """Monitors active option positions for 50% SL or 2-day holding time exit."""
        closed = []
        if not self.positions or not self.kite:
            return closed

        # Check market open
        market_open, _ = is_market_open(ignore_holidays=self.ignore_holidays)
        if not market_open:
            return closed

        today_str = date.today().strftime("%Y-%m-%d")
        now_time = datetime.now().strftime("%H:%M")

        quotes_to_fetch = [f"NFO:{pos.tradingsymbol}" for pos in self.positions.values() if pos.status == "OPEN"]
        quotes = {}
        try:
            quotes = self.kite.quote(quotes_to_fetch)
        except Exception as e:
            logger.warning("Error fetching quotes to monitor positions: %s", e)
            return closed

        for pos in list(self.positions.values()):
            if pos.status != "OPEN":
                continue

            q = quotes.get(f"NFO:{pos.tradingsymbol}", {})
            ltp = float(q.get("last_price") or 0.0)
            if ltp <= 0:
                continue

            pos.update_price(ltp)

            # 1. Check 50% Stop Loss
            if ltp <= pos.sl_price:
                pos.close(exit_price=ltp, exit_time=now_time, exit_date=today_str, exit_reason="SL_HIT")
                closed.append(pos)
                if self.notifier:
                    self.notifier.send_message(
                        f"🔴 <b>OPTION BUY SL HIT (50%)</b>\n"
                        f"Contract: <code>{pos.tradingsymbol}</code>\n"
                        f"Exit Price: Rs {ltp:.2f} (Entry: Rs {pos.entry_price:.2f})\n"
                        f"Net Loss: <b>Rs {pos.net_pnl:+,.2f}</b>\n"
                        f"Time: <code>{now_time}</code>"
                    )

            # 2. Check 2x Target
            elif ltp >= pos.target_price:
                pos.close(exit_price=ltp, exit_time=now_time, exit_date=today_str, exit_reason="TARGET_HIT")
                closed.append(pos)
                if self.notifier:
                    self.notifier.send_message(
                        f"🟢 <b>OPTION BUY TARGET HIT (2x / 100%)</b>\n"
                        f"Contract: <code>{pos.tradingsymbol}</code>\n"
                        f"Exit Price: Rs {ltp:.2f} (Entry: Rs {pos.entry_price:.2f})\n"
                        f"Net Profit: <b>Rs {pos.net_pnl:+,.2f} 🚀</b>\n"
                        f"Time: <code>{now_time}</code>"
                    )

            # 3. Check 2-Day Holding Cutoff
            elif today_str >= pos.max_exit_date and now_time >= "15:00":
                pos.close(exit_price=ltp, exit_time=now_time, exit_date=today_str, exit_reason="TIME_EXIT_2D")
                closed.append(pos)
                if self.notifier:
                    pnl_emoji = "🟢" if pos.net_pnl > 0 else "🔴"
                    self.notifier.send_message(
                        f"{pnl_emoji} <b>OPTION BUY 2-DAY TIME EXIT</b>\n"
                        f"Contract: <code>{pos.tradingsymbol}</code>\n"
                        f"Exit Price: Rs {ltp:.2f} (Entry: Rs {pos.entry_price:.2f})\n"
                        f"Holding: 2 Days Reached (Avoiding Theta Decay)\n"
                        f"Net P&L: <b>Rs {pos.net_pnl:+,.2f}</b>"
                    )

        if closed:
            self.save_state()

        return closed

    def run_loop(self, poll_interval: int = 60, max_iterations: Optional[int] = None) -> None:
        """Continuous scheduler loop running during market hours."""
        logger.info(
            "Starting Max OI Option Buying Desk Scheduler (Scan: %s IST, Cutoff: %s IST)...",
            self.config.scan_time, self.config.entry_cutoff_time
        )
        iteration = 0
        while True:
            if max_iterations is not None and iteration >= max_iterations:
                break
            iteration += 1

            try:
                now = datetime.now()
                today_d = now.date()
                today_str = today_d.strftime("%Y-%m-%d")
                now_time = now.strftime("%H:%M")

                # 1. Holiday & Weekend Guard
                if not self.ignore_holidays:
                    is_holiday, reason = is_trading_holiday(today_d)
                    if is_holiday:
                        if self._last_holiday_log_date != today_str:
                            logger.info("[SCHEDULER] Exchange closed today (%s: %s). Max OI Desk paused.", today_str, reason)
                            self._last_holiday_log_date = today_str
                        time.sleep(30)
                        continue

                # 2. Timing Guard: Only run in second half of the month (>= 15th)
                if self.config.post_15_only and today_d.day < 15:
                    if self._last_scan_date != today_str:
                        logger.info("[SCHEDULER] Today is %s (day %d < 15th). Strategy operates post-15th. Paused.", today_str, today_d.day)
                        self._last_scan_date = today_str
                    time.sleep(60)
                    continue

                # 3. Market Open Check
                market_open, market_reason = is_market_open(ignore_holidays=self.ignore_holidays)
                if not market_open:
                    if now_time < "09:15":
                        if self._last_premarket_log_date != today_str:
                            logger.info("[SCHEDULER] Pre-market hours (Current: %s IST). Regular trading opens at 09:15 IST. Waiting...", now_time)
                            self._last_premarket_log_date = today_str
                    elif now_time >= "15:30":
                        if self._last_postmarket_log_date != today_str:
                            logger.info("[SCHEDULER] Market closed for the day (Current: %s IST). Waiting for next session...", now_time)
                            self._last_postmarket_log_date = today_str
                    time.sleep(30)
                    continue

                # 4. Waiting for Daily Scan Time (09:30 IST)
                if now_time < self.config.scan_time:
                    if self._last_waiting_log_date != today_str:
                        logger.info("[SCHEDULER] Market OPEN. Waiting for scan trigger time (%s IST, current: %s IST)...", self.config.scan_time, now_time)
                        self._last_waiting_log_date = today_str
                    time.sleep(15)
                    continue

                # 5. Daily Morning Scan Trigger at scan_time (09:30 IST)
                if now_time >= self.config.scan_time and self._last_scan_date != today_str:
                    logger.info("[SCHEDULER] %s IST reached. Executing daily Max OI scan...", now_time)
                    self._last_scan_date = today_str
                    self.run_scan_cycle()

                # 6. Monitor Active Positions
                self.monitor_open_positions()

                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Max OI Desk stopped by user.")
                break
            except Exception as e:
                logger.exception("Error in Max OI Desk loop: %s", e)
                time.sleep(15)
