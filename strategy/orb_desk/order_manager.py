import logging
import time
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("orb.order")


def _round_to_nse_tick(price: float, tick: float = 0.05) -> float:
    """Rounds a price to the nearest valid NSE tick size (0.05)."""
    return round(round(price / tick) * tick, 2)


class ORBOrderManager:
    """
    Manages order execution for both PAPER simulation and LIVE trading
    for the 15-minute Opening Range Breakout (ORB) strategy.
    Supports Cash Equity (NSE:MIS) and Futures (NFO:MIS).
    """

    def __init__(
        self,
        mode: str = "PAPER",
        kite: Optional[KiteConnect] = None,
        exchange: str = "NSE",
        product: str = "MIS"
    ):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}. Must be 'PAPER' or 'LIVE'.")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("A valid KiteConnect instance is required for LIVE mode.")
        self.exchange = exchange.upper()
        self.product = product.upper()

        self._paper_orders: Dict[str, Dict[str, Any]] = {}
        self._order_counter: int = 1

    def place_entry(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        initial_sl: float,
        target_2_5r: float
    ) -> Dict[str, Any]:
        """
        Executes a breakout entry order (BUY for LONG, SELL for SHORT).
        """
        direction_upper = direction.upper()
        txn_type = "BUY" if direction_upper == "LONG" else "SELL"
        entry_price = _round_to_nse_tick(entry_price)

        logger.info(
            "[%s ENTRY] %s %s x %d @ Rs %.2f (SL: Rs %.2f, Target: Rs %.2f)",
            self.mode,
            txn_type,
            symbol,
            quantity,
            entry_price,
            initial_sl,
            target_2_5r
        )

        if self.mode == "PAPER":
            order_id = f"PAPER_ENTRY_{self._order_counter}_{symbol}"
            self._order_counter += 1
            order = {
                "order_id": order_id,
                "mode": "PAPER",
                "symbol": symbol,
                "direction": direction_upper,
                "transaction_type": txn_type,
                "quantity": quantity,
                "fill_price": entry_price,
                "initial_sl": initial_sl,
                "target_2_5r": target_2_5r,
                "timestamp": time.time(),
                "status": "COMPLETE"
            }
            self._paper_orders[order_id] = order
            return order

        # LIVE Mode via Kite API
        try:
            params = {
                "variety": self.kite.VARIETY_REGULAR,
                "exchange": self.exchange,
                "tradingsymbol": symbol,
                "transaction_type": getattr(self.kite, f"TRANSACTION_TYPE_{txn_type}"),
                "quantity": quantity,
                "product": getattr(self.kite, f"PRODUCT_{self.product}"),
                "order_type": self.kite.ORDER_TYPE_MARKET,
                "validity": self.kite.VALIDITY_DAY
            }
            live_order_id = self.kite.place_order(**params)
            logger.info("[LIVE ENTRY SUCCESS] Order ID: %s for %s %s x %d", live_order_id, txn_type, symbol, quantity)
            return {
                "order_id": live_order_id,
                "mode": "LIVE",
                "symbol": symbol,
                "direction": direction_upper,
                "transaction_type": txn_type,
                "quantity": quantity,
                "fill_price": entry_price,
                "initial_sl": initial_sl,
                "target_2_5r": target_2_5r,
                "timestamp": time.time(),
                "status": "SUBMITTED"
            }
        except Exception as e:
            logger.error("[LIVE ENTRY FAILED] Failed to place order for %s: %s", symbol, e)
            raise

    def place_exit(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        exit_price: float,
        reason: str
    ) -> Dict[str, Any]:
        """
        Executes an exit order (SELL for LONG, BUY for SHORT).
        """
        direction_upper = direction.upper()
        # Closing a LONG means SELLING; closing a SHORT means BUYING
        txn_type = "SELL" if direction_upper == "LONG" else "BUY"
        exit_price = _round_to_nse_tick(exit_price)

        logger.info(
            "[%s EXIT - %s] %s %s x %d @ Rs %.2f",
            self.mode,
            reason,
            txn_type,
            symbol,
            quantity,
            exit_price
        )

        if self.mode == "PAPER":
            order_id = f"PAPER_EXIT_{self._order_counter}_{symbol}"
            self._order_counter += 1
            order = {
                "order_id": order_id,
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": txn_type,
                "quantity": quantity,
                "fill_price": exit_price,
                "reason": reason,
                "timestamp": time.time(),
                "status": "COMPLETE"
            }
            self._paper_orders[order_id] = order
            return order

        # LIVE Mode
        try:
            params = {
                "variety": self.kite.VARIETY_REGULAR,
                "exchange": self.exchange,
                "tradingsymbol": symbol,
                "transaction_type": getattr(self.kite, f"TRANSACTION_TYPE_{txn_type}"),
                "quantity": quantity,
                "product": getattr(self.kite, f"PRODUCT_{self.product}"),
                "order_type": self.kite.ORDER_TYPE_MARKET,
                "validity": self.kite.VALIDITY_DAY
            }
            live_order_id = self.kite.place_order(**params)
            logger.info("[LIVE EXIT SUCCESS] Order ID: %s for %s %s x %d (%s)", live_order_id, txn_type, symbol, quantity, reason)
            return {
                "order_id": live_order_id,
                "mode": "LIVE",
                "symbol": symbol,
                "transaction_type": txn_type,
                "quantity": quantity,
                "fill_price": exit_price,
                "reason": reason,
                "timestamp": time.time(),
                "status": "SUBMITTED"
            }
        except Exception as e:
            logger.error("[LIVE EXIT FAILED] Failed to exit %s (%s): %s", symbol, reason, e)
            raise
