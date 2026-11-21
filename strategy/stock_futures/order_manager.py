import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional, List
from kiteconnect import KiteConnect

from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.models import TradeSignal, StockPosition, SignalType, calculate_futures_charges

logger = logging.getLogger("strategy.stock_futures.order")


def round_to_nse_tick(price: float, tick: float = 0.05) -> float:
    """Rounds a price to the nearest valid NSE tick size (0.05)."""
    return round(round(price / tick) * tick, 2)


class StockFuturesOrderManager:
    """Handles execution, state persistence, and position management for Stock Futures Desk."""

    def __init__(
        self,
        mode: str = "PAPER",
        kite: Optional[KiteConnect] = None,
        config: Optional[StockFuturesConfig] = None,
        state_file: Optional[Path] = None,
        product: str = "MIS",
    ):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}. Must be PAPER or LIVE.")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("KiteConnect instance required for LIVE mode.")

        self.config = config or StockFuturesConfig()
        self.state_file = state_file or self.config.state_file
        self.product = product.upper()

        self.positions: Dict[str, StockPosition] = {}
        self.trades_history: List[Dict[str, Any]] = []
        self._futures_symbol_cache: Dict[str, str] = {}
        self.load_state()

    def resolve_futures_symbol(self, symbol: str) -> str:
        """
        Resolves current near-month active futures tradingsymbol on NSE NFO.
        In PAPER mode or if Kite instruments are unavailable, generates standard format.
        """
        if symbol in self._futures_symbol_cache:
            return self._futures_symbol_cache[symbol]

        if self.mode == "LIVE" and self.kite:
            from strategy.holidays import is_market_open
            if not is_market_open()[0]:
                fallback = f"{symbol}_FUT"
                self._futures_symbol_cache[symbol] = fallback
                return fallback

            try:
                # Query NFO instruments
                instruments = self.kite.instruments("NFO")
                now = datetime.now()
                candidates = []
                for inst in instruments:
                    if inst.get("name") == symbol and inst.get("instrument_type") == "FUT":
                        expiry = inst.get("expiry")
                        if expiry and expiry >= now.date():
                            candidates.append((expiry, inst.get("tradingsymbol")))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    near_month_symbol = candidates[0][1]
                    self._futures_symbol_cache[symbol] = near_month_symbol
                    return near_month_symbol
            except Exception as e:
                logger.warning("Failed to query Kite instruments for %s: %s. Using fallback symbol.", symbol, e)

        # Fallback format: e.g. ABB_FUT
        fallback = f"{symbol}_FUT"
        self._futures_symbol_cache[symbol] = fallback
        return fallback

    def enter_position(self, signal: TradeSignal) -> Optional[StockPosition]:
        """Executes a new trade entry for a stock signal."""
        symbol = signal.symbol
        if symbol in self.positions and self.positions[symbol].status == "OPEN":
            logger.info("Position already open for %s. Skipping.", symbol)
            return None

        # Check max trades per day
        today_trades = [t for t in self.trades_history if t.get("symbol") == symbol and t.get("date") == str(date.today())]
        if len(today_trades) >= self.config.max_trades_per_stock_per_day:
            logger.info("Max daily trades reached for %s today. Skipping.", symbol)
            return None

        tradingsymbol = self.resolve_futures_symbol(symbol)
        quantity = self.config.get_unit_quantity(symbol)
        lots = self.config.lots_per_trade

        side_str = "LONG" if signal.side == SignalType.LONG else "SHORT"
        tx_type = "BUY" if signal.side == SignalType.LONG else "SELL"

        order_id = f"PAPER_{tx_type}_{symbol}_{int(datetime.now().timestamp())}"
        fill_price = signal.entry_price
        order_type_norm = (self.config.order_type or "LIMIT").upper()
        limit_price = None

        if order_type_norm == "LIMIT":
            buffer = fill_price * self.config.limit_buffer_pct
            limit_price = max(0.05, round_to_nse_tick(fill_price + buffer if tx_type == "BUY" else fill_price - buffer))

        if self.mode == "LIVE":
            try:
                prod = getattr(self.kite, f"PRODUCT_{self.product}", self.product)
                order_params = {
                    "variety": self.kite.VARIETY_REGULAR,
                    "exchange": self.kite.EXCHANGE_NFO,
                    "tradingsymbol": tradingsymbol,
                    "transaction_type": getattr(self.kite, f"TRANSACTION_TYPE_{tx_type}"),
                    "quantity": quantity,
                    "product": prod,
                    "validity": self.kite.VALIDITY_DAY,
                }
                if order_type_norm == "LIMIT":
                    order_params["order_type"] = self.kite.ORDER_TYPE_LIMIT
                    order_params["price"] = limit_price
                else:
                    order_params["order_type"] = self.kite.ORDER_TYPE_MARKET

                logger.info("[LIVE] Placing %s %s order for %s: %d qty @ limit %s", tx_type, order_type_norm, tradingsymbol, quantity, limit_price)
                order_id = str(self.kite.place_order(**order_params))
                logger.info("[LIVE] Placed order ID: %s", order_id)
            except Exception as e:
                logger.exception("[LIVE] Failed to place %s order for %s: %s", tx_type, tradingsymbol, e)
                return None

        position = StockPosition(
            symbol=symbol,
            tradingsymbol=tradingsymbol,
            side=side_str,
            quantity=quantity,
            lots=lots,
            entry_time=signal.trigger_time,
            entry_price=fill_price,
            sl_price=signal.sl_price,
            target_price=signal.target_price,
            current_price=fill_price,
            status="OPEN",
            order_id=order_id,
        )

        self.positions[symbol] = position
        self.save_state()
        logger.info(
            "[%s] Entered %s %s (%s) x %d (%d lots) @ Rs %.2f%s | SL: Rs %.2f | Target: Rs %.2f",
            self.mode, side_str, symbol, order_type_norm, quantity, lots, fill_price,
            f" (Limit: Rs {limit_price:.2f})" if limit_price else "", signal.sl_price, signal.target_price
        )
        return position

    def exit_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        exit_time: Optional[str] = None
    ) -> Optional[StockPosition]:
        """Closes an open position and records performance."""
        if symbol not in self.positions or self.positions[symbol].status != "OPEN":
            return None

        pos = self.positions[symbol]
        time_str = exit_time or datetime.now().strftime("%H:%M")
        tx_type = "SELL" if pos.side == "LONG" else "BUY"

        exit_order_id = f"PAPER_EXIT_{tx_type}_{symbol}_{int(datetime.now().timestamp())}"
        order_type_norm = (self.config.order_type or "LIMIT").upper()
        limit_price = None

        if order_type_norm == "LIMIT":
            buffer = exit_price * self.config.limit_buffer_pct
            limit_price = max(0.05, round_to_nse_tick(exit_price - buffer if tx_type == "SELL" else exit_price + buffer))

        if self.mode == "LIVE":
            try:
                prod = getattr(self.kite, f"PRODUCT_{self.product}", self.product)
                order_params = {
                    "variety": self.kite.VARIETY_REGULAR,
                    "exchange": self.kite.EXCHANGE_NFO,
                    "tradingsymbol": pos.tradingsymbol,
                    "transaction_type": getattr(self.kite, f"TRANSACTION_TYPE_{tx_type}"),
                    "quantity": pos.quantity,
                    "product": prod,
                    "validity": self.kite.VALIDITY_DAY,
                }
                if order_type_norm == "LIMIT":
                    order_params["order_type"] = self.kite.ORDER_TYPE_LIMIT
                    order_params["price"] = limit_price
                else:
                    order_params["order_type"] = self.kite.ORDER_TYPE_MARKET

                logger.info("[LIVE] Placing EXIT %s %s order for %s: %d qty @ limit %s", tx_type, order_type_norm, pos.tradingsymbol, pos.quantity, limit_price)
                exit_order_id = str(self.kite.place_order(**order_params))
                logger.info("[LIVE] Placed exit order ID: %s", exit_order_id)
            except Exception as e:
                logger.exception("[LIVE] Failed to place EXIT order for %s: %s", pos.tradingsymbol, e)
                return None

        pos.close(exit_price=exit_price, exit_time=time_str, exit_reason=exit_reason, exit_order_id=exit_order_id)

        # Record trade to history
        trade_record = pos.to_dict()
        trade_record["date"] = str(date.today())
        self.trades_history.append(trade_record)
        self.save_state()

        logger.info(
            "[%s] Closed %s %s @ Rs %.2f | Reason: %s | Net PnL: Rs %.2f",
            self.mode, pos.side, symbol, exit_price, exit_reason, pos.net_pnl
        )
        return pos

    def load_state(self) -> None:
        """Loads positions and trades history from state file."""
        if not self.state_file or not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                today_str = str(date.today())
                if data.get("date") == today_str:
                    for s, p_data in data.get("positions", {}).items():
                        self.positions[s] = StockPosition(**p_data)
                    self.trades_history = data.get("trades_history", [])
                    logger.info("Loaded state for %s: %d positions, %d trades", today_str, len(self.positions), len(self.trades_history))
        except Exception as e:
            logger.warning("Could not load state from %s: %s", self.state_file, e)

    def save_state(self) -> None:
        """Persists current state to JSON."""
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "date": str(date.today()),
                "updated_at": datetime.now().isoformat(),
                "positions": {s: p.to_dict() for s, p in self.positions.items()},
                "trades_history": self.trades_history,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Could not save state to %s: %s", self.state_file, e)
