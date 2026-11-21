import html
import logging
import time
from datetime import datetime, date
from typing import Optional, Dict, List, Any
import pandas as pd
from kiteconnect import KiteConnect

from notifications.telegram import TelegramNotifier
from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.models import StockPosition, TradeSignal
from strategy.stock_futures.engine import StockFuturesEngine
from strategy.stock_futures.order_manager import StockFuturesOrderManager

logger = logging.getLogger("strategy.stock_futures.desk")


class StockFuturesDesk:
    """Automated Trading Desk for Nifty 200 Momentum 30 Stock Futures."""

    def __init__(
        self,
        order_manager: StockFuturesOrderManager,
        engine: Optional[StockFuturesEngine] = None,
        config: Optional[StockFuturesConfig] = None,
        notifier: Optional[TelegramNotifier] = None,
        kite: Optional[KiteConnect] = None,
        ignore_holidays: bool = False,
    ):
        self.config = config or StockFuturesConfig()
        self.order_manager = order_manager
        self.engine = engine or StockFuturesEngine(self.config)
        self.notifier = notifier
        self.kite = kite
        self.ignore_holidays = ignore_holidays
        self._instrument_token_cache: Dict[str, int] = {}
        self._last_universe_mtime: float = 0.0
        self._last_daily_rehydrate_date: Optional[str] = None
        self._last_eod_summary_date: Optional[str] = None
        self._last_holiday_date: Optional[str] = None
        self._last_premarket_log_date: Optional[str] = None
        self._last_formation_log_date: Optional[str] = None
        self._last_active_scan_log_date: Optional[str] = None
        self._last_cutoff_log_date: Optional[str] = None
        if self.config.universe_file and self.config.universe_file.exists():
            try:
                self._last_universe_mtime = self.config.universe_file.stat().st_mtime
            except Exception:
                pass

    def perform_daily_rehydration(self, ignore_holidays: bool = False) -> bool:
        """Executes scheduled pre-market auto-rehydration check for universe and live lot sizes."""
        from strategy.stock_futures.rehydrator import UniverseRehydrator
        from strategy.holidays import is_trading_holiday

        today = datetime.now().date()
        if not ignore_holidays:
            is_holiday, reason = is_trading_holiday(today)
            if is_holiday:
                logger.info("[SCHEDULED] Today %s is a non-trading day (%s). Skipping pre-market rehydration.", today, reason)
                return False

        logger.info("[SCHEDULED] Running daily pre-market universe rehydration check...")
        try:
            rehydrator = UniverseRehydrator(kite=self.kite, universe_file=self.config.universe_file, ignore_holidays=ignore_holidays)
            result = rehydrator.auto_rehydrate(save=True)
            reloaded = self.check_and_rehydrate_universe()

            changes = result.get("changes", {})
            if not changes.get("is_changed") and self.notifier:
                # Send daily pre-market ready confirmation
                msg = (
                    "🌅 <b>STOCK FUTURES PRE-MARKET READY</b>\n"
                    f"Date: <code>{today}</code>\n"
                    f"Active Equities: <b>{len(self.config.symbols)} Momentum Stocks</b>\n"
                    f"Lot Sizing: <b>{self.config.lots_per_trade} Lots Verified</b> (Kite Connect Live)\n"
                    f"Status: Primed & Ready for 09:15 AM Open 🚀"
                )
                self.notifier.send_message(msg)
            return bool(reloaded or True)
        except Exception as e:
            logger.warning("[SCHEDULED] Error in daily universe rehydration: %s", e)
            return False

    def check_and_rehydrate_universe(self) -> bool:
        """
        Monitors universe_file for modifications.
        If file changed, dynamically rehydrates active symbols & lot sizes in-memory without restarting.
        """
        uf = self.config.universe_file
        if not uf or not uf.exists():
            return False

        try:
            mtime = uf.stat().st_mtime
            if mtime <= self._last_universe_mtime:
                return False

            old_symbols = set(self.config.symbols)
            reloaded = self.config.reload_universe()
            if not reloaded:
                return False

            self._last_universe_mtime = mtime
            new_symbols = set(self.config.symbols)
            added = sorted(list(new_symbols - old_symbols))
            removed = sorted(list(old_symbols - new_symbols))

            logger.info(
                "[REHYDRATE] Universe updated dynamically (%d stocks). Added: %s | Removed: %s",
                len(self.config.symbols), added or "None", removed or "None"
            )

            # Sync order manager config
            self.order_manager.config = self.config

            if self.notifier and (added or removed):
                msg = (
                    "🔄 <b>MOMENTUM 30 UNIVERSE REHYDRATED</b>\n"
                    f"Active Equities: <b>{len(self.config.symbols)}</b>\n"
                    f"Added: <code>{', '.join(added) if added else 'None'}</code>\n"
                    f"Removed: <code>{', '.join(removed) if removed else 'None'}</code>\n"
                    f"Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
                )
                self.notifier.send_message(msg)

            return True
        except Exception as e:
            logger.warning("Error checking universe rehydration: %s", e)
            return False

    def ensure_kite(self) -> Optional[KiteConnect]:
        """Ensures an active, authenticated KiteConnect session when market is active."""
        if self.kite is not None:
            return self.kite
        if self.order_manager.mode != "LIVE":
            return None

        from kite.client import get_kite_client
        logger.info("[KITE] Connecting to Kite Connect as market is active...")
        self.kite = get_kite_client(validate=True, check_market_hours=True, ignore_holidays=self.ignore_holidays)
        self.order_manager.kite = self.kite
        return self.kite

    def get_equity_token(self, symbol: str) -> Optional[int]:
        """Looks up Kite instrument token for NSE cash underlying to fetch intraday 15m candles."""
        if symbol in self._instrument_token_cache:
            return self._instrument_token_cache[symbol]

        if not self.kite:
            return None

        # Guard: Never query instruments if market is closed
        from strategy.holidays import is_market_open
        if not is_market_open(ignore_holidays=self.ignore_holidays)[0]:
            return None

        try:
            # Map ticker: GVT_D in data is GVT&D on NSE
            nse_symbol = "GVT&D" if symbol == "GVT_D" else symbol
            instruments = self.kite.instruments("NSE")
            for inst in instruments:
                if inst.get("tradingsymbol") == nse_symbol:
                    tok = inst.get("instrument_token")
                    self._instrument_token_cache[symbol] = tok
                    return tok
        except Exception as e:
            logger.warning("Could not lookup instrument token for %s: %s", symbol, e)
        return None

    def fetch_intraday_candles(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetches today's 15-minute candles for the symbol from Kite Connect."""
        if not self.kite:
            return None

        # Guard: Never query historical candle API if market is closed
        from strategy.holidays import is_market_open
        if not is_market_open(ignore_holidays=self.ignore_holidays)[0]:
            return None

        token = self.get_equity_token(symbol)
        if not token:
            return None

        today = date.today()
        try:
            records = self.kite.historical_data(
                instrument_token=token,
                from_date=today,
                to_date=today,
                interval="15minute"
            )
            if not records:
                return None
            df = pd.DataFrame(records)
            df['datetime'] = pd.to_datetime(df['date'])
            df['time'] = df['datetime'].dt.strftime('%H:%M')
            return df
        except Exception as e:
            logger.warning("Failed to fetch historical 15m data for %s: %s", symbol, e)
            return None

    def fetch_ltp(self, symbol: str) -> Optional[float]:
        """Fetches current LTP for underlying or future."""
        if not self.kite:
            return None

        # Guard: Never query quote API if market is closed
        from strategy.holidays import is_market_open
        if not is_market_open(ignore_holidays=self.ignore_holidays)[0]:
            return None

        try:
            nse_symbol = "GVT&D" if symbol == "GVT_D" else symbol
            quote_key = f"NSE:{nse_symbol}"
            quotes = self.kite.quote([quote_key])
            if quote_key in quotes:
                return quotes[quote_key].get("last_price")
        except Exception as e:
            logger.warning("Failed to fetch LTP for %s: %s", symbol, e)
        return None

    def scan_and_enter(self, candle_feed: Optional[Dict[str, pd.DataFrame]] = None) -> List[StockPosition]:
        """
        Scans all 30 momentum stocks for consolidation breakout signals.
        If candle_feed is provided (e.g. during testing/simulation), uses that;
        otherwise queries Kite API.
        """
        new_positions = []
        now_time = datetime.now().strftime("%H:%M")

        if now_time < self.config.entry_start_time:
            logger.info("Current time %s is before entry scan start (%s). Waiting.", now_time, self.config.entry_start_time)
            return new_positions

        if now_time > self.config.entry_cutoff_time:
            logger.info("Current time %s is past entry cutoff (%s). No new entries allowed.", now_time, self.config.entry_cutoff_time)
            return new_positions

        for symbol in self.config.symbols:
            # Skip if already in position
            if symbol in self.order_manager.positions and self.order_manager.positions[symbol].status == "OPEN":
                continue

            df = candle_feed.get(symbol) if candle_feed else self.fetch_intraday_candles(symbol)
            if df is None or len(df) == 0:
                continue

            signal = self.engine.evaluate_signal(symbol, df)
            if signal:
                logger.info("Found breakout signal on %s: %s @ Rs %.2f (Vol Ratio: %.2fx)", symbol, signal.side, signal.entry_price, signal.volume_ratio)
                pos = self.order_manager.enter_position(signal)
                if pos:
                    new_positions.append(pos)
                    if self.notifier:
                        msg = (
                            f"🚀 <b>STOCK FUTURES ENTRY</b>\n"
                            f"Symbol: <code>{symbol}</code> ({pos.side})\n"
                            f"Qty: <b>{pos.quantity}</b> ({pos.lots} Lots)\n"
                            f"Entry Price: Rs {pos.entry_price:.2f}\n"
                            f"Stop Loss: Rs {pos.sl_price:.2f} (-{self.config.sl_pct*100:.1f}%)\n"
                            f"Target: Rs {pos.target_price:.2f} (+{self.config.target_pct*100:.2f}%)\n"
                            f"Trigger Time: <code>{signal.trigger_time}</code>"
                        )
                        self.notifier.send_message(msg)
                else:
                    logger.error("[ENTRY FAILED] Failed to enter position for %s upon %s breakout", symbol, signal.side)
                    if self.notifier:
                        self.notifier.send_message(
                            f"⚠️ <b>STOCK FUTURES ENTRY FAILED</b>\n"
                            f"Failed to enter <code>{symbol}</code> ({signal.side}) @ Rs {signal.entry_price:.2f}!\n"
                            f"Check Kite Connect margin/broker connection."
                        )

        return new_positions

    def monitor_open_positions(self, prices: Optional[Dict[str, float]] = None) -> List[StockPosition]:
        """Monitors all active open positions against SL, Target, and EOD square-off."""
        closed_positions = []
        now_time = datetime.now().strftime("%H:%M")

        open_symbols = [s for s, p in self.order_manager.positions.items() if p.status == "OPEN"]
        for symbol in open_symbols:
            pos = self.order_manager.positions[symbol]
            current_p = prices.get(symbol) if prices else self.fetch_ltp(symbol)
            if current_p is None:
                continue

            pos.update_current_price(current_p)
            exit_decision = self.engine.check_exit(
                position=pos,
                current_price=current_p,
                current_time=now_time
            )

            if exit_decision:
                exit_price, reason = exit_decision
                closed_pos = self.order_manager.exit_position(
                    symbol=symbol,
                    exit_price=exit_price,
                    exit_reason=reason,
                    exit_time=now_time
                )
                if closed_pos:
                    closed_positions.append(closed_pos)
                    if self.notifier:
                        pnl_emoji = "🟢" if closed_pos.net_pnl > 0 else "🔴"
                        msg = (
                            f"{pnl_emoji} <b>STOCK FUTURES EXIT</b>\n"
                            f"Symbol: <code>{symbol}</code> ({closed_pos.side})\n"
                            f"Exit Price: Rs {closed_pos.exit_price:.2f}\n"
                            f"Reason: <code>{reason}</code>\n"
                            f"Net PnL: Rs {closed_pos.net_pnl:+,.2f}\n"
                            f"Time: <code>{now_time}</code>"
                        )
                        self.notifier.send_message(msg)
                else:
                    logger.critical(
                        "[EXIT FAILED] Broker order failed for %s (%s @ Rs %.2f). Position remains OPEN to retry.",
                        symbol, reason, exit_price
                    )
                    if self.notifier:
                        self.notifier.send_message(
                            f"🚨 <b>CRITICAL: EXIT ORDER FAILED</b>\n"
                            f"Failed to submit exit order to broker for <code>{symbol}</code> ({pos.side}) @ Rs {exit_price:.2f}!\n"
                            f"Reason: <code>{html.escape(reason)}</code>\n"
                            f"Position remains <b>OPEN</b>. Automatic retry scheduled on next cycle. Please verify Kite terminal!"
                        )

        return closed_positions

    def run_cycle(self, candle_feed: Optional[Dict[str, pd.DataFrame]] = None, prices: Optional[Dict[str, float]] = None) -> None:
        """Runs a single monitoring & scanning cycle with dynamic universe rehydration check."""
        self.check_and_rehydrate_universe()
        self.monitor_open_positions(prices=prices)
        self.scan_and_enter(candle_feed=candle_feed)

    def run_loop(
        self,
        poll_seconds: int = 15,
        ignore_holidays: bool = False,
        max_iterations: Optional[int] = None,
    ) -> None:
        """Runs automated polling loop continuously during market hours."""
        self.ignore_holidays = ignore_holidays
        logger.info(
            "Starting Stock Futures Desk daemon scheduler (Poll: %ds, Entry: %s IST, Cutoff: %s IST, Square-off: %s IST)...",
            poll_seconds, self.config.entry_start_time, self.config.entry_cutoff_time, self.config.squareoff_time
        )
        iteration = 0
        while True:
            if max_iterations is not None and iteration >= max_iterations:
                logger.info("Reached max iterations (%d). Stopping scheduler loop.", max_iterations)
                break
            iteration += 1

            try:
                now = datetime.now()
                now_time = now.strftime("%H:%M")
                today_str = now.strftime("%Y-%m-%d")

                # 1. Check weekends and official NSE trading holidays
                if not ignore_holidays:
                    from strategy.holidays import is_trading_holiday
                    is_holiday, holiday_reason = is_trading_holiday(now.date())
                    if is_holiday:
                        if self._last_holiday_date != today_str:
                            logger.info(
                                "[SCHEDULER] Exchange closed today (%s: %s). Scheduler paused. Waiting for next trading session...",
                                today_str, holiday_reason
                            )
                            self._last_holiday_date = today_str
                        time.sleep(30)
                        continue

                # Reset holiday date on trading days
                self._last_holiday_date = None

                # 2. Daily Pre-Market Auto-Rehydration Check (runs once every morning between 08:00 and 09:10 IST)
                if "08:00" <= now_time <= "09:10" and self._last_daily_rehydrate_date != today_str:
                    self._last_daily_rehydrate_date = today_str
                    self.perform_daily_rehydration(ignore_holidays=ignore_holidays)

                # 3. Before Market Hours (Pre-market wait until 09:15 IST)
                if now_time < "09:15":
                    if self._last_premarket_log_date != today_str:
                        logger.info(
                            "[SCHEDULER] Pre-market hours (Current: %s IST). Regular trading opens at 09:15 IST. Waiting...",
                            now_time
                        )
                        self._last_premarket_log_date = today_str
                    time.sleep(15)
                    continue

                # 4. Morning Formation Window (09:15 - 10:30 IST)
                # C1 (09:15-09:30) defines [L1, H1] range, and C2-C5 (09:30-10:15) form consolidation base.
                # Breakout entry scan starts strictly at entry_start_time (10:30 IST).
                if now_time < self.config.entry_start_time:
                    if self._last_formation_log_date != today_str:
                        logger.info(
                            "[SCHEDULER] Market opened (Current: %s IST). Range & Consolidation formation in progress (09:15–10:15). Breakout polling starts at %s IST.",
                            now_time, self.config.entry_start_time
                        )
                        self._last_formation_log_date = today_str

                    # Check universe file updates dynamically
                    self.check_and_rehydrate_universe()

                    # If open positions exist from earlier, monitor them
                    has_open_positions = any(p.status == "OPEN" for p in self.order_manager.positions.values())
                    if has_open_positions:
                        self.monitor_open_positions()
                        time.sleep(poll_seconds)
                    else:
                        time.sleep(15)
                    continue

                # 5. Market Closed / Post-Market (> 15:30 IST)
                if now_time >= "15:30":
                    if self._last_eod_summary_date != today_str:
                        self._last_eod_summary_date = today_str
                        today_trades = [t for t in self.order_manager.trades_history if t.get("date") == today_str]
                        total_pnl = sum(t.get("net_pnl", 0.0) for t in today_trades)
                        total_charges = sum(t.get("charges", 0.0) for t in today_trades)
                        wins = sum(1 for t in today_trades if t.get("net_pnl", 0.0) > 0)
                        losses = sum(1 for t in today_trades if t.get("net_pnl", 0.0) < 0)
                        logger.info(
                            "[SCHEDULER] Market closed for the day (%s IST). Total Trades Today: %d | Realized Net PnL: Rs %.2f. Waiting for next session.",
                            now_time, len(today_trades), total_pnl
                        )
                        if self.notifier:
                            summary_pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
                            self.notifier.send_message(
                                f"🏁 <b>STOCK FUTURES EOD SUMMARY</b>\n"
                                f"Date: <code>{today_str}</code>\n"
                                f"Trades: <b>{len(today_trades)}</b> (Wins: {wins}, Losses: {losses})\n"
                                f"Total Charges: Rs {total_charges:,.2f}\n"
                                f"{summary_pnl_emoji} <b>Net Realized P&L: Rs {total_pnl:+,.2f}</b>"
                            )

                    time.sleep(60)
                    continue

                # 6. EOD Square-off Window (15:00 - 15:30 IST)
                if now_time >= self.config.squareoff_time:
                    # Final check to square off any open positions
                    self.monitor_open_positions()

                    # Send daily EOD summary alert once after market close
                    if self._last_eod_summary_date != today_str:
                        self._last_eod_summary_date = today_str
                        today_trades = [t for t in self.order_manager.trades_history if t.get("date") == today_str]
                        total_pnl = sum(t.get("net_pnl", 0.0) for t in today_trades)
                        total_charges = sum(t.get("charges", 0.0) for t in today_trades)
                        wins = sum(1 for t in today_trades if t.get("net_pnl", 0.0) > 0)
                        losses = sum(1 for t in today_trades if t.get("net_pnl", 0.0) < 0)
                        logger.info(
                            "[SCHEDULER] Square-off complete (%s IST). Total Trades Today: %d | Realized Net PnL: Rs %.2f.",
                            now_time, len(today_trades), total_pnl
                        )
                        if self.notifier:
                            summary_pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
                            self.notifier.send_message(
                                f"🏁 <b>STOCK FUTURES EOD SUMMARY</b>\n"
                                f"Date: <code>{today_str}</code>\n"
                                f"Trades: <b>{len(today_trades)}</b> (Wins: {wins}, Losses: {losses})\n"
                                f"Total Charges: Rs {total_charges:,.2f}\n"
                                f"{summary_pnl_emoji} <b>Net Realized P&L: Rs {total_pnl:+,.2f}</b>"
                            )

                    time.sleep(60)
                    continue

                # 6. Entry Cutoff Window (13:00 - 15:00 IST)
                # No new entries allowed after 13:00 IST. Only monitor open positions.
                if now_time >= self.config.entry_cutoff_time:
                    if self._last_cutoff_log_date != today_str:
                        logger.info(
                            "[SCHEDULER] Entry cutoff (%s IST) reached. No new breakout entries allowed today. Monitoring active positions until %s square-off.",
                            self.config.entry_cutoff_time, self.config.squareoff_time
                        )
                        self._last_cutoff_log_date = today_str

                    self.check_and_rehydrate_universe()
                    self.monitor_open_positions()
                    time.sleep(poll_seconds)
                    continue

                # 7. Active Breakout Scanning & Trading Window (10:30 - 13:00 IST)
                if self._last_active_scan_log_date != today_str:
                    logger.info(
                        "[SCHEDULER] %s IST reached. Active breakout scanning engaged across %d Momentum Stocks (Poll: %ds).",
                        self.config.entry_start_time, len(self.config.symbols), poll_seconds
                    )
                    self._last_active_scan_log_date = today_str

                self.run_cycle()
                time.sleep(poll_seconds)
            except KeyboardInterrupt:
                logger.info("Stock Futures Desk stopped by user.")
                break
            except Exception as e:
                logger.exception("Error in Stock Futures Desk monitoring loop: %s", e)
                if self.notifier:
                    try:
                        self.notifier.send_message(
                            f"⚠️ <b>Stock Futures Desk Loop Error</b>: {html.escape(str(e))}\n"
                            f"Desk loop is attempting automatic recovery. Check container logs."
                        )
                    except Exception:
                        pass
                time.sleep(10)
