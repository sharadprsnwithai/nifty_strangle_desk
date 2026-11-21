import html
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, List, Any
from kiteconnect import KiteConnect

from notifications.telegram import TelegramNotifier
from strategy.holidays import is_trading_holiday, is_market_open
from strategy.techno_funda.config import TechnoFundaConfig
from strategy.techno_funda.models import TechnoFundaCandidate
from strategy.techno_funda.scanner import TechnoFundaScanner

logger = logging.getLogger("strategy.techno_funda.desk")


class TechnoFundaDesk:
    """
    Automated Daily Scanner and Alerting Desk for Milind Upasani's MTF Techno-Funda Strategy.
    Runs daily at 15:15 IST before market close, identifies high-growth operational breakout stocks,
    and dispatches high-probability swing trading setups to Telegram.
    """

    def __init__(
        self,
        config: Optional[TechnoFundaConfig] = None,
        scanner: Optional[TechnoFundaScanner] = None,
        notifier: Optional[TelegramNotifier] = None,
        kite: Optional[KiteConnect] = None,
        ignore_holidays: bool = False,
    ):
        self.config = config or TechnoFundaConfig()
        self.scanner = scanner or TechnoFundaScanner(config=self.config, kite=kite)
        self.notifier = notifier
        self.kite = kite
        self.ignore_holidays = ignore_holidays
        self._last_scan_date: Optional[str] = None
        self._last_holiday_log_date: Optional[str] = None
        self._last_waiting_log_date: Optional[str] = None

    def format_candidate_alert(self, c: TechnoFundaCandidate) -> str:
        """Formats actionable individual swing trade alert with technical & fundamental metrics."""
        ttm_growth = f"+{c.ttm_ebitda_growth_pct:.1f}%" if c.ttm_ebitda_growth_pct is not None else "N/A"
        q_growth = f"+{c.quarterly_ebitda_growth_pct:.1f}%" if c.quarterly_ebitda_growth_pct is not None else "N/A"
        de_str = f"{c.debt_to_equity:.2f}" if c.debt_to_equity is not None else "N/A"
        op_margin = f"{c.operating_margin_pct:.1f}%" if c.operating_margin_pct is not None else "N/A"

        trigger_badge = "Crossed 65 🚀" if c.rsi_cross_confirmed else f"{c.daily_rsi:.1f} (In Zone)"
        company_name = html.escape(str(c.company_name))
        sector = html.escape(str(c.sector))

        return (
            f"🎯 <b>TECHNO-FUNDA BREAKOUT ALERT</b>\n\n"
            f"Stock: <b>{company_name}</b> (<code>{c.symbol}</code>)\n"
            f"Sector: <i>{sector}</i> | LTP: <b>Rs {c.ltp:.2f}</b>\n"
            f"Pattern: <code>{c.chart_pattern}</code> (Vol: <b>{c.volume_ratio:.1f}x</b>)\n\n"
            f"📊 <b>Multi-Timeframe Momentum (Dow Theory)</b>\n"
            f"• Monthly RSI: <b>{c.monthly_rsi:.1f}</b> (Primary Bullish &gt; 60)\n"
            f"• Weekly RSI: <b>{c.weekly_rsi:.1f}</b> (Secondary Bullish &gt; 60)\n"
            f"• Daily RSI: <b>{c.daily_rsi:.1f}</b> (Trigger: {trigger_badge})\n\n"
            f"💎 <b>High-Growth Operational Fundamentals</b>\n"
            f"• TTM YoY EBITDA Growth: <b>{ttm_growth}</b> (&gt; 30%)\n"
            f"• Quarterly EBITDA Growth: <b>{q_growth}</b> (&gt; 30%)\n"
            f"• Debt-to-Equity: <b>{de_str}</b> (Clean Balance Sheet &lt; 1.0)\n"
            f"• Operating Margin: <b>{op_margin}</b> (Margin &gt;= 8%)\n\n"
            f"🛡️ Reference Swing Stop: <b>Rs {c.swing_sl:.2f}</b> (Base Support)\n"
            f"🎯 Target 1 (1.5R): <b>Rs {c.target_1:.2f}</b>\n"
            f"🎯 Target 2 (2.5R): <b>Rs {c.target_2:.2f}</b>\n"
            f"Scan Time: <code>{self.config.scan_time} IST</code> | Date: <code>{c.scan_date}</code>"
        )

    def format_summary_message(self, candidates: List[TechnoFundaCandidate]) -> str:
        """Formats multi-stock daily summary table for Telegram."""
        lines = [
            "🏆 <b>TECHNO-FUNDA DAILY WATCHLIST SUMMARY</b>",
            f"Scan Time: <code>{self.config.scan_time} IST</code>",
            f"Identified: <b>{len(candidates)} High-Probability Breakout Stocks</b>\n"
        ]
        for c in candidates[:10]:
            ttm_str = f"EBITDA: +{c.ttm_ebitda_growth_pct:.0f}%" if c.ttm_ebitda_growth_pct is not None else ""
            lines.append(
                f"• <b>{c.symbol}</b> (Rs {c.ltp:.2f}) → Daily RSI: <b>{c.daily_rsi:.1f}</b> | <code>{c.chart_pattern}</code> | {ttm_str}"
            )
        if len(candidates) > 10:
            lines.append(f"\n<i>...and {len(candidates) - 10} more stocks under observation.</i>")

        return "\n".join(lines)

    def run_scan_cycle(self, stocks: Optional[List[str]] = None, tech_only: bool = False) -> Dict[str, Any]:
        """Executes a single scan cycle and sends Telegram notifications."""
        result = self.scanner.scan(stocks=stocks, tech_only=tech_only, ignore_holidays=self.ignore_holidays)

        if result.get("status") == "MARKET_CLOSED":
            logger.info("[DESK] Market closed (%s). Skipping scan cycle.", result.get("reason"))
            return result

        candidates = result.get("candidates", [])
        if candidates and self.notifier:
            # 1. Send detailed breakdown alert for each high-conviction candidate
            for c in candidates[:5]:  # Top 5 to avoid message flooding
                msg = self.format_candidate_alert(c)
                self.notifier.send_message(msg)
                logger.info("[ALERT] Sent Techno-Funda alert for %s", c.symbol)

            # 2. If multiple candidates, send summary
            if len(candidates) > 1:
                summary = self.format_summary_message(candidates)
                self.notifier.send_message(summary)

        return result

    def run_loop(self, poll_interval: int = 30, max_iterations: Optional[int] = None) -> None:
        """Continuous daemon scheduler running daily and triggering scan strictly at 15:15 IST."""
        logger.info(
            "Starting MTF Techno-Funda Scanner Desk Scheduler (Trigger Time: %s IST daily)...",
            self.config.scan_time
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

                # 1. Holiday & Weekend Guard (Zero Kite API hits)
                if not self.ignore_holidays:
                    is_holiday, reason = is_trading_holiday(today_d)
                    if is_holiday:
                        if self._last_holiday_log_date != today_str:
                            logger.info("[SCHEDULER] Exchange closed today (%s: %s). Techno-Funda Desk paused.", today_str, reason)
                            self._last_holiday_log_date = today_str
                        time.sleep(30)
                        continue

                # 2. Market Open Check
                market_open, market_reason = is_market_open(ignore_holidays=self.ignore_holidays)
                if not market_open:
                    time.sleep(30)
                    continue

                # 3. Waiting for 15:15 IST Daily Trigger
                if now_time < self.config.scan_time:
                    if self._last_waiting_log_date != today_str:
                        logger.info("[SCHEDULER] Market OPEN. Waiting for scan trigger time (%s IST, current: %s IST)...", self.config.scan_time, now_time)
                        self._last_waiting_log_date = today_str
                    time.sleep(15)
                    continue

                # 4. Trigger Daily 15:15 IST Scan (Runs exactly once per trading day)
                if now_time >= self.config.scan_time and self._last_scan_date != today_str:
                    logger.info("[SCHEDULER] Daily trigger time %s IST reached. Executing MTF Techno-Funda Scan...", now_time)
                    self._last_scan_date = today_str
                    self.run_scan_cycle()

                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Techno-Funda Desk stopped by user.")
                break
            except Exception as e:
                logger.exception("Error in Techno-Funda Desk scheduler loop: %s", e)
                time.sleep(15)
