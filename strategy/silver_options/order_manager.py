import logging
import threading
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

from strategy.silver_options.config import SilverOptionsConfig

logger = logging.getLogger("strategy.silver.order")


def _round_to_mcx_tick(price: float, tick: float = 0.5) -> float:
    """Rounds a price to the nearest valid MCX Silver tick size (0.50)."""
    return round(round(price / tick) * tick, 2)


class SilverOrderManager:
    """Manages order execution for MCX Silver Options in PAPER and LIVE modes."""

    def __init__(
        self,
        mode: str = "PAPER",
        kite: Optional[KiteConnect] = None,
        config: Optional[SilverOptionsConfig] = None,
    ):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}. Must be 'PAPER' or 'LIVE'.")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("KiteConnect instance required for LIVE mode.")

        self.config = config or SilverOptionsConfig()
        self.product = self.config.product.upper()
        self._paper_orders: Dict[str, Dict[str, Any]] = {}
        self._kite_lock = threading.RLock()

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05,
        min_buffer_rs: float = 1.0,
        best_bid: Optional[float] = None,
        max_slippage_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Sells an option leg on MCX (Strangle entry).
        For LIMIT orders, anchors to live best_bid (when available) with tight max_slippage_pct
        to prevent thin-book slippage, falling back to price - buffer_pct otherwise.
        """
        if quantity <= 0:
            raise ValueError(f"Invalid MCX SELL quantity: {quantity}")
        if price <= 0:
            raise ValueError(f"Invalid MCX SELL price: {price}")
        order_type_norm = order_type.upper()
        tick = self.config.tick_size if self.config.tick_size and self.config.tick_size > 0 else 0.5
        anchor_price = best_bid if (best_bid is not None and best_bid > 0) else price
        effective_pct = max_slippage_pct if (best_bid is not None and best_bid > 0 and max_slippage_pct is not None) else buffer_pct
        buffer = max(anchor_price * effective_pct, min_buffer_rs)
        limit_price = max(0.5, _round_to_mcx_tick(anchor_price - buffer, tick)) if order_type_norm == "LIMIT" else None

        logger.info(
            "[%s] Executing MCX SELL (%s): %s x %d @ Rs %.2f%s",
            self.mode,
            order_type_norm,
            symbol,
            quantity,
            price,
            f" (Limit: Rs {limit_price:.2f})" if limit_price else "",
        )

        if self.mode == "PAPER":
            order = {
                "order_id": f"PAPER_SELL_{symbol}",
                "mode": "PAPER",
                "exchange": "MCX",
                "symbol": symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "filled_quantity": quantity,
                "pending_quantity": 0,
                "product": self.product,
                "status": "COMPLETE",
            }
            self._paper_orders[order["order_id"]] = order.copy()
            return order

        # LIVE Mode Execution on MCX
        prod_val = getattr(self.kite, f"PRODUCT_{self.product}", self.product)
        validity_val = getattr(self.kite, "VALIDITY_DAY", "DAY")
        params: Dict[str, Any] = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": self.kite.EXCHANGE_MCX,
            "tradingsymbol": symbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
            "quantity": quantity,
            "product": prod_val,
            "validity": validity_val,
        }

        if order_type_norm == "LIMIT":
            params["order_type"] = self.kite.ORDER_TYPE_LIMIT
            params["price"] = limit_price
        else:
            params["order_type"] = self.kite.ORDER_TYPE_MARKET

        try:
            logger.info("[LIVE] Submitting MCX SELL order: %s x %d (OrderType: %s, LimitPrice: %s)", symbol, quantity, order_type_norm, limit_price)
            order_id = self._call_kite_with_token_retry(
                f"MCX SELL ({symbol})",
                lambda: self.kite.place_order(**params),
            )
            logger.info("[LIVE] MCX SELL order placed successfully: %s x %d (Order ID: %s)", symbol, quantity, order_id)
            return {
                "order_id": order_id,
                "mode": "LIVE",
                "exchange": "MCX",
                "symbol": symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "status": "SUBMITTED",
            }
        except Exception as e:
            logger.error("[LIVE] MCX SELL order failed for %s: %s", symbol, e)
            raise e

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05,
        min_buffer_rs: float = 1.0,
        best_ask: Optional[float] = None,
        max_slippage_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Buys back an option leg on MCX (Square-off on SL, Target, or EOD).
        For LIMIT orders, anchors to live best_ask (when available) with tight max_slippage_pct
        to guarantee execution without sweeping an empty book.
        """
        if quantity <= 0:
            raise ValueError(f"Invalid MCX BUY quantity: {quantity}")
        if price <= 0:
            raise ValueError(f"Invalid MCX BUY price: {price}")
        order_type_norm = order_type.upper()
        tick = self.config.tick_size if self.config.tick_size and self.config.tick_size > 0 else 0.5
        anchor_price = best_ask if (best_ask is not None and best_ask > 0) else price
        effective_pct = max_slippage_pct if (best_ask is not None and best_ask > 0 and max_slippage_pct is not None) else buffer_pct
        buffer = max(anchor_price * effective_pct, min_buffer_rs)
        limit_price = max(0.5, _round_to_mcx_tick(anchor_price + buffer, tick)) if order_type_norm == "LIMIT" else None

        logger.info(
            "[%s] Executing MCX BUY square-off (%s): %s x %d @ Rs %.2f%s",
            self.mode,
            order_type_norm,
            symbol,
            quantity,
            price,
            f" (Limit: Rs {limit_price:.2f})" if limit_price else "",
        )

        if self.mode == "PAPER":
            order = {
                "order_id": f"PAPER_BUY_{symbol}",
                "mode": "PAPER",
                "exchange": "MCX",
                "symbol": symbol,
                "transaction_type": "BUY",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "filled_quantity": quantity,
                "pending_quantity": 0,
                "product": self.product,
                "status": "COMPLETE",
            }
            self._paper_orders[order["order_id"]] = order.copy()
            return order

        # LIVE Mode Execution on MCX
        prod_val = getattr(self.kite, f"PRODUCT_{self.product}", self.product)
        validity_val = getattr(self.kite, "VALIDITY_DAY", "DAY")
        params: Dict[str, Any] = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": self.kite.EXCHANGE_MCX,
            "tradingsymbol": symbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_BUY,
            "quantity": quantity,
            "product": prod_val,
            "validity": validity_val,
        }

        if order_type_norm == "LIMIT":
            params["order_type"] = self.kite.ORDER_TYPE_LIMIT
            params["price"] = limit_price
        else:
            params["order_type"] = self.kite.ORDER_TYPE_MARKET

        try:
            logger.info("[LIVE] Submitting MCX BUY order: %s x %d (OrderType: %s, LimitPrice: %s)", symbol, quantity, order_type_norm, limit_price)
            order_id = self._call_kite_with_token_retry(
                f"MCX BUY ({symbol})",
                lambda: self.kite.place_order(**params),
            )
            logger.info("[LIVE] MCX BUY order placed successfully: %s x %d (Order ID: %s)", symbol, quantity, order_id)
            return {
                "order_id": order_id,
                "mode": "LIVE",
                "exchange": "MCX",
                "symbol": symbol,
                "transaction_type": "BUY",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "status": "SUBMITTED",
            }
        except Exception as e:
            logger.error("[LIVE] MCX BUY order failed for %s: %s", symbol, e)
            raise e

    def _call_kite_with_token_retry(self, op_desc: str, fn):
        """Executes a Kite API call and thread-safely refreshes token once on TokenException."""
        try:
            return fn()
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if not is_token_error(e):
                raise
            with self._kite_lock:
                logger.warning("[LIVE] Token error on %s (%s). Refreshing session and retrying...", op_desc, e)
                self.kite = ensure_kite_session(self.kite, force_login=False)
                return fn()

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Retrieves real-time status of an MCX order."""
        if self.mode == "PAPER":
            order_info = self._paper_orders.get(order_id)
            if order_info:
                return {
                    "order_id": order_id,
                    "status": order_info.get("status", "COMPLETE"),
                    "filled_quantity": int(order_info.get("filled_quantity", order_info.get("quantity", 0))),
                    "pending_quantity": int(order_info.get("pending_quantity", 0)),
                    "average_price": float(order_info.get("fill_price", 0.0)),
                    "status_message": order_info.get("status_message"),
                }
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "filled_quantity": 0,
                "pending_quantity": 0,
                "average_price": 0.0,
                "status_message": None,
            }

        try:
            history = self._call_kite_with_token_retry(
                f"MCX order_history({order_id})",
                lambda: self.kite.order_history(order_id),
            )
            if not history:
                return {
                    "order_id": order_id,
                    "status": "UNKNOWN",
                    "filled_quantity": 0,
                    "pending_quantity": 0,
                    "average_price": 0.0,
                    "status_message": None,
                }
            latest = history[-1]
            return {
                "order_id": order_id,
                "status": latest.get("status", "UNKNOWN"),
                "filled_quantity": int(latest.get("filled_quantity") or 0),
                "pending_quantity": int(latest.get("pending_quantity") or 0),
                "average_price": float(latest.get("average_price") or 0.0),
                "status_message": latest.get("status_message"),
            }
        except Exception as e:
            logger.error("[LIVE] Error fetching MCX order status for %s: %s", order_id, e)
            return {
                "order_id": order_id,
                "status": "ERROR",
                "filled_quantity": 0,
                "pending_quantity": 0,
                "average_price": 0.0,
                "status_message": str(e),
            }

    def cancel_order(self, order_id: str, variety: str = "regular") -> bool:
        """Cancels an open or pending MCX order."""
        logger.info("[%s] Cancelling MCX order: %s", self.mode, order_id)
        if self.mode == "PAPER":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "CANCELLED"
                self._paper_orders[order_id]["pending_quantity"] = 0
            return True

        try:
            self._call_kite_with_token_retry(
                f"MCX cancel_order({order_id})",
                lambda: self.kite.cancel_order(variety=variety, order_id=order_id),
            )
            logger.info("[LIVE] Cancel request sent for MCX order: %s", order_id)
            return True
        except Exception as e:
            logger.error("[LIVE] Failed to cancel MCX order %s: %s", order_id, e)
            return False
