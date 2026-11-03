import logging
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.order")


def _round_to_nse_tick(price: float, tick: float = 0.05) -> float:
    """Rounds a price to the nearest valid NSE option tick size (0.05)."""
    return round(round(price / tick) * tick, 2)


class OrderManager:
    """Manages order execution for both PAPER simulation and LIVE trading."""

    def __init__(self, mode: str = "PAPER", kite: Optional[KiteConnect] = None):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("KiteConnect instance required for LIVE mode.")
        self._paper_orders: Dict[str, Dict[str, Any]] = {}

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05
    ) -> Dict[str, Any]:
        """
        Sells an option leg (entry).
        If order_type is LIMIT, places a marketable limit order below LTP (by buffer_pct)
        to prevent freak-trade execution while guaranteeing immediate fill against the bid.
        """
        order_type_norm = order_type.upper()
        limit_price = max(0.05, _round_to_nse_tick(price * (1.0 - buffer_pct))) if order_type_norm == "LIMIT" else None

        logger.info(
            "[%s] Executing SELL (%s): %s x %d @ Rs %.2f%s",
            self.mode,
            order_type_norm,
            symbol,
            quantity,
            price,
            f" (Limit: Rs {limit_price:.2f})" if limit_price else ""
        )

        if self.mode == "PAPER":
            order = {
                "order_id": f"PAPER_SELL_{symbol}",
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "filled_quantity": quantity,
                "pending_quantity": 0,
                "status": "COMPLETE"
            }
            self._paper_orders[order["order_id"]] = order.copy()
            return order
        
        # LIVE Mode
        params: Dict[str, Any] = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": self.kite.EXCHANGE_NFO,
            "tradingsymbol": symbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
            "quantity": quantity,
            "product": self.kite.PRODUCT_MIS,
        }

        if order_type_norm == "LIMIT":
            params["order_type"] = self.kite.ORDER_TYPE_LIMIT
            params["price"] = limit_price
        else:
            params["order_type"] = self.kite.ORDER_TYPE_MARKET

        order_id = self.kite.place_order(**params)
        return {
            "order_id": order_id,
            "mode": "LIVE",
            "symbol": symbol,
            "transaction_type": "SELL",
            "quantity": quantity,
            "order_type": order_type_norm,
            "limit_price": limit_price,
            "fill_price": price,
            "status": "SUBMITTED"
        }

    def buy_to_cover(
        self,
        symbol: str,
        quantity: int,
        price: float,
        reason: str = "EXIT",
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05
    ) -> Dict[str, Any]:
        """
        Buys back an open short option leg (exit on SL, target, or 3PM).
        If order_type is LIMIT, places a marketable limit order above LTP (by buffer_pct)
        to guarantee execution against the ask while preventing execution at extreme freak prices.
        """
        order_type_norm = order_type.upper()
        limit_price = max(0.05, _round_to_nse_tick(price * (1.0 + buffer_pct))) if order_type_norm == "LIMIT" else None

        logger.info(
            "[%s] Executing BUY TO COVER (%s / %s): %s x %d @ Rs %.2f%s",
            self.mode,
            reason,
            order_type_norm,
            symbol,
            quantity,
            price,
            f" (Limit: Rs {limit_price:.2f})" if limit_price else ""
        )

        if self.mode == "PAPER":
            order = {
                "order_id": f"PAPER_BUY_{symbol}",
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": "BUY",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "filled_quantity": quantity,
                "pending_quantity": 0,
                "reason": reason,
                "status": "COMPLETE"
            }
            self._paper_orders[order["order_id"]] = order.copy()
            return order

        params: Dict[str, Any] = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": self.kite.EXCHANGE_NFO,
            "tradingsymbol": symbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_BUY,
            "quantity": quantity,
            "product": self.kite.PRODUCT_MIS,
        }

        if order_type_norm == "LIMIT":
            params["order_type"] = self.kite.ORDER_TYPE_LIMIT
            params["price"] = limit_price
        else:
            params["order_type"] = self.kite.ORDER_TYPE_MARKET

        order_id = self.kite.place_order(**params)
        return {
            "order_id": order_id,
            "mode": "LIVE",
            "symbol": symbol,
            "transaction_type": "BUY",
            "quantity": quantity,
            "order_type": order_type_norm,
            "limit_price": limit_price,
            "fill_price": price,
            "reason": reason,
            "status": "SUBMITTED"
        }

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        Retrieves real-time status of an order.
        Returns:
            {
                "order_id": str,
                "status": "COMPLETE" | "OPEN" | "CANCELLED" | "REJECTED" | "UNKNOWN",
                "filled_quantity": int,
                "pending_quantity": int,
                "average_price": float,
                "status_message": Optional[str]
            }
        """
        if self.mode == "PAPER":
            order_info = self._paper_orders.get(order_id)
            if order_info:
                return {
                    "order_id": order_id,
                    "status": order_info.get("status", "COMPLETE"),
                    "filled_quantity": order_info.get("filled_quantity", order_info.get("quantity", 0)),
                    "pending_quantity": order_info.get("pending_quantity", 0),
                    "average_price": float(order_info.get("fill_price", 0.0)),
                    "status_message": order_info.get("status_message")
                }
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "filled_quantity": 0,
                "pending_quantity": 0,
                "average_price": 0.0,
                "status_message": None
            }

        try:
            history = self.kite.order_history(order_id)
            if not history:
                return {
                    "order_id": order_id,
                    "status": "UNKNOWN",
                    "filled_quantity": 0,
                    "pending_quantity": 0,
                    "average_price": 0.0,
                    "status_message": None
                }
            latest = history[-1]
            return {
                "order_id": order_id,
                "status": latest.get("status", "UNKNOWN"),
                "filled_quantity": int(latest.get("filled_quantity") or 0),
                "pending_quantity": int(latest.get("pending_quantity") or 0),
                "average_price": float(latest.get("average_price") or 0.0),
                "status_message": latest.get("status_message")
            }
        except Exception as e:
            logger.error("[LIVE] Error fetching order status for %s: %s", order_id, e)
            return {
                "order_id": order_id,
                "status": "ERROR",
                "filled_quantity": 0,
                "pending_quantity": 0,
                "average_price": 0.0,
                "status_message": str(e)
            }

    def cancel_order(self, order_id: str, variety: str = "regular") -> bool:
        """
        Cancels an open or pending order on the exchange.
        """
        logger.info("[%s] Cancelling order: %s", self.mode, order_id)
        if self.mode == "PAPER":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "CANCELLED"
                self._paper_orders[order_id]["pending_quantity"] = 0
            return True

        try:
            self.kite.cancel_order(variety=variety, order_id=order_id)
            logger.info("[LIVE] Cancel request sent successfully for order: %s", order_id)
            return True
        except Exception as e:
            logger.error("[LIVE] Failed to cancel order %s: %s", order_id, e)
            return False
