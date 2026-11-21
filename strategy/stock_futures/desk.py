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
    ):
        self.config = config or StockFuturesConfig()
        self.order_manager = order_manager
        self.engine = engine or StockFuturesEngine(self.config)
        self.notifier = notifier
        self.kite = kite
        self._instrument_token_cache: Dict[str, int] = {}
        self._last_universe_mtime: float = 0.0
        self._last_daily_rehydrate_date: Optional[str] = None
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
            rehydrator = UniverseRehydrator(kite=self.kite, universe_file=self.config.universe_file)
            result = rehydrator.auto_rehydrate(save=True)
            reloaded = self.check_and_rehydrate_universe()

            changes = result.get("changes", {})
            if not changes.get("is_changed") and self.notifier:
                # Send daily pre-market ready confirmation
                msg = (
                    "🌅 *STOCK FUTURES PRE-MARKET READY*\n"
                    f"Date: `{today}`\n"
                    f"Active Equities: {len(self.config.symbols)} Momentum Stocks\n"
                    f"Lot Sizing: 2 Lots Verified (Kite Connect Live)\n"
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
                    "🔄 *MOMENTUM 30 UNIVERSE REHYDRATED*\n"
                    f"Active Equities: {len(self.config.symbols)}\n"
                    f"Added: {', '.join(added) if added else 'None'}\n"
                    f"Removed: {', '.join(removed) if removed else 'None'}\n"
                    f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(msg)

            return True
        except Exception as e:
            logger.warning("Error checking universe rehydration: %s", e)
            return False

    def get_equity_token(self, symbol: str) -> Optional[int]:
        """Looks up Kite instrument token for NSE cash underlying to fetch intraday 15m candles."""
        if symbol in self._instrument_token_cache:
            return self._instrument_token_cache[symbol]

        if not self.kite:
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
                            f"🚀 *STOCK FUTURES ENTRY*\n"
                            f"Symbol: `{symbol}` ({pos.side})\n"
                            f"Qty: {pos.quantity} ({pos.lots} Lots)\n"
                            f"Entry Price: Rs {pos.entry_price:.2f}\n"
                            f"Stop Loss: Rs {pos.sl_price:.2f} (-{self.config.sl_pct*100:.1f}%)\n"
                            f"Target: Rs {pos.target_price:.2f} (+{self.config.target_pct*100:.1f}%)\n"
                            f"Time: {signal.trigger_time}"
                        )
                        self.notifier.send_message(msg)

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
                            f"{pnl_emoji} *STOCK FUTURES EXIT*\n"
                            f"Symbol: `{symbol}` ({closed_pos.side})\n"
                            f"Exit Price: Rs {closed_pos.exit_price:.2f}\n"
                            f"Reason: `{reason}`\n"
                            f"Net PnL: Rs {closed_pos.net_pnl:,.2f}\n"
                            f"Time: {now_time}"
                        )
                        self.notifier.send_message(msg)

        return closed_positions

    def run_cycle(self, candle_feed: Optional[Dict[str, pd.DataFrame]] = None, prices: Optional[Dict[str, float]] = None) -> None:
        """Runs a single monitoring & scanning cycle with dynamic universe rehydration check."""
        self.check_and_rehydrate_universe()
        self.monitor_open_positions(prices=prices)
        self.scan_and_enter(candle_feed=candle_feed)

    def run_loop(self, poll_seconds: int = 15) -> None:
        """Runs automated polling loop continuously during market hours."""
        logger.info("Starting Stock Futures Desk daemon loop (Poll interval: %ds)...", poll_seconds)
        while True:
            try:
                now = datetime.now()
                now_time = now.strftime("%H:%M")
                today_str = now.strftime("%Y-%m-%d")

                # Check weekends and official NSE trading holidays
                from strategy.holidays import is_trading_holiday
                is_holiday, holiday_reason = is_trading_holiday(now.date())
                if is_holiday:
                    logger.debug("Market closed today (%s: %s). Sleeping 5 minutes...", now.date(), holiday_reason)
                    time.sleep(300)
                    continue

                # Daily Pre-Market Auto-Rehydration Check (runs once every morning between 08:00 and 09:10 IST)
                if "08:00" <= now_time <= "09:10" and self._last_daily_rehydrate_date != today_str:
                    self._last_daily_rehydrate_date = today_str
                    self.perform_daily_rehydration()

                # Before market hours
                if now_time < "09:15":
                    logger.debug("Market closed (%s). Sleeping until 09:15...", now_time)
                    time.sleep(30)
                    continue

                # After square-off time (15:00 - 15:30)
                if now_time >= self.config.squareoff_time:
                    # Final check to square off any open positions
                    self.monitor_open_positions()
                    logger.info("Past square-off time (%s). Day complete.", now_time)
                    time.sleep(60)
                    continue

                self.run_cycle()
                time.sleep(poll_seconds)
            except KeyboardInterrupt:
                logger.info("Stock Futures Desk stopped by user.")
                break
            except Exception as e:
                logger.exception("Error in Stock Futures Desk monitoring loop: %s", e)
                time.sleep(10)
