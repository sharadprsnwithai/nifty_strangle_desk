import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("nifty_orb.order_manager")


class NiftyOrderManager:
    """
    Executes and simulates orders for NIFTY 50 Futures ORB Desk.
    Supports both PAPER trading simulation and LIVE order routing via Kite Connect.
    """

    def __init__(
        self,
        mode: str = "PAPER",
        kite: Optional[Any] = None,
        exchange: str = "NFO",
        product: str = "MIS",
        order_type: str = "LIMIT",
        limit_buffer_pts: float = 5.0,
    ):
        self.mode = mode.upper()
        self.kite = kite
        self.exchange = exchange
        self.product = product
        self.order_type = order_type.upper()
        self.limit_buffer_pts = limit_buffer_pts

    def calculate_limit_price(self, transaction_type: str, ltp: float) -> float:
        """Calculates marketable limit price with slippage protection buffer."""
        tt = transaction_type.upper()
        if tt == "BUY":
            return round(ltp + self.limit_buffer_pts, 1)
        elif tt == "SELL":
            return round(ltp - self.limit_buffer_pts, 1)
        return round(ltp, 1)

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        trigger_price: Optional[float] = None,
        tag: str = "NIFTY_ORB",
    ) -> Dict[str, Any]:
        """Places a live or simulated paper order."""
        tt = transaction_type.upper()
        if self.mode == "PAPER":
            order_id = f"PAPER_{uuid.uuid4().hex[:8].upper()}"
            exec_price = trigger_price if trigger_price is not None else 0.0
            logger.info(
                "[PAPER] Order Executed: %s %d %s @ ₹%.2f (ID: %s)",
                tt, quantity, symbol, exec_price, order_id
            )
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "price": exec_price,
                "quantity": quantity,
                "symbol": symbol,
                "transaction_type": tt,
            }

        # LIVE Mode Execution
        if not self.kite:
            raise RuntimeError("Live mode enabled but KiteConnect client is None")

        exec_price = trigger_price or 0.0
        limit_p = self.calculate_limit_price(tt, exec_price) if self.order_type == "LIMIT" else None

        kite_order_type = self.kite.ORDER_TYPE_LIMIT if self.order_type == "LIMIT" else self.kite.ORDER_TYPE_MARKET
        price_arg = limit_p if self.order_type == "LIMIT" else None

        try:
            logger.info(
                "[LIVE] Placing %s %s order: %d %s (Limit: %s)",
                self.order_type, tt, quantity, symbol, price_arg
            )
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=tt,
                quantity=quantity,
                product=self.product,
                order_type=kite_order_type,
                price=price_arg,
                tag=tag[:20],
            )
            return {
                "order_id": str(order_id),
                "status": "COMPLETE",
                "price": exec_price,
                "quantity": quantity,
                "symbol": symbol,
                "transaction_type": tt,
            }
        except Exception as e:
            logger.error("Failed to place live order for %s: %s", symbol, e)
            raise
