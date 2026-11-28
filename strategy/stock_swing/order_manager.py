"""CNC Order Manager for FP-ALL Stock Swing Desk (Paper & Live Kite execution)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from strategy.stock_swing.config import StockSwingConfig

logger = logging.getLogger("stock_swing.order_manager")


class StockOrderManager:
    """Executes delivery (CNC) BUY and SELL orders via Kite Connect or Paper simulation."""

    def __init__(self, config: StockSwingConfig, kite_client: Any = None):
        self.config = config
        self.kite = kite_client

    @staticmethod
    def _round_tick(price: float, tick: float = 0.05) -> float:
        """Round price to NSE equity tick size (Rs 0.05)."""
        return round(round(price / tick) * tick, 2)

    def _poll_order_fill(self, order_id: str, max_wait_sec: int = 5) -> Dict[str, Any]:
        start = time.time()
        while time.time() - start < max_wait_sec:
            try:
                orders = self.kite.orders()
                for o in orders:
                    if str(o.get("order_id")) == str(order_id):
                        status = o.get("status")
                        if status == "COMPLETE":
                            return {
                                "status": "COMPLETE",
                                "order_id": order_id,
                                "filled_qty": int(o.get("filled_quantity", 0)),
                                "avg_price": float(o.get("average_price", 0.0)),
                            }
                        if status in ("REJECTED", "CANCELLED"):
                            return {
                                "status": "FAILED",
                                "reason": o.get("status_message", f"Order {status}"),
                            }
            except Exception as e:
                logger.warning("Order poll error for %s: %s", order_id, e)
            time.sleep(0.5)

        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
        except Exception:
            pass
        return {"status": "FAILED", "reason": f"Order {order_id} timed out and was cancelled."}

    def execute_buy(self, symbol: str, quantity: int, current_price: float) -> Dict[str, Any]:
        """Place a CNC BUY order for `quantity` shares."""
        if quantity < 1 or current_price <= 0:
            return {"status": "FAILED", "reason": "Invalid quantity or price"}

        if self.config.mode == "PAPER" or not self.kite:
            logger.info("[PAPER BUY] %s x %d @ Rs %.2f", symbol, quantity, current_price)
            return {
                "status": "COMPLETE",
                "mode": "PAPER",
                "order_id": f"PAPER_BUY_{symbol}_{int(time.time())}",
                "symbol": symbol,
                "quantity": quantity,
                "price": current_price,
            }

        try:
            if self.config.order_type == "LIMIT":
                limit_px = self._round_tick(current_price * (1.0 + self.config.limit_buffer_pct))
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=quantity,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=limit_px,
                )
            else:
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=quantity,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                )
            fill = self._poll_order_fill(order_id)
            if fill["status"] == "COMPLETE":
                return {
                    "status": "COMPLETE",
                    "mode": "LIVE",
                    "order_id": order_id,
                    "symbol": symbol,
                    "quantity": fill["filled_qty"] or quantity,
                    "price": fill["avg_price"] or current_price,
                }
            return fill
        except Exception as e:
            logger.error("[LIVE BUY FAILED] %s: %s", symbol, e)
            return {"status": "FAILED", "reason": str(e), "symbol": symbol}

    def execute_sell(self, symbol: str, quantity: int, current_price: float) -> Dict[str, Any]:
        """Place a CNC SELL order for `quantity` shares, verifying Demat/T1 holdings."""
        if quantity < 1 or current_price <= 0:
            return {"status": "FAILED", "reason": "Invalid quantity or price"}

        if self.config.mode == "PAPER" or not self.kite:
            logger.info("[PAPER SELL] %s x %d @ Rs %.2f", symbol, quantity, current_price)
            return {
                "status": "COMPLETE",
                "mode": "PAPER",
                "order_id": f"PAPER_SELL_{symbol}_{int(time.time())}",
                "symbol": symbol,
                "quantity": quantity,
                "price": current_price,
            }

        sell_qty = quantity
        try:
            holdings = self.kite.holdings()
            h_item = next((h for h in holdings if h.get("tradingsymbol") == symbol), None)
            if h_item:
                demat_qty = int(h_item.get("quantity", 0) + h_item.get("t1_quantity", 0))
                if demat_qty < sell_qty:
                    logger.warning("Adjusting %s sell qty from %d to Demat qty %d", symbol, sell_qty, demat_qty)
                    sell_qty = demat_qty
        except Exception as e:
            logger.warning("Could not verify Kite holdings for %s: %s", symbol, e)

        if sell_qty <= 0:
            return {"status": "FAILED", "reason": f"No Demat holdings available for {symbol}"}

        try:
            if self.config.order_type == "LIMIT":
                limit_px = self._round_tick(current_price * (1.0 - self.config.limit_buffer_pct))
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=sell_qty,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=limit_px,
                )
            else:
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=sell_qty,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                )
            fill = self._poll_order_fill(order_id)
            if fill["status"] == "COMPLETE":
                return {
                    "status": "COMPLETE",
                    "mode": "LIVE",
                    "order_id": order_id,
                    "symbol": symbol,
                    "quantity": fill["filled_qty"] or sell_qty,
                    "price": fill["avg_price"] or current_price,
                }
            return fill
        except Exception as e:
            logger.error("[LIVE SELL FAILED] %s: %s", symbol, e)
            return {"status": "FAILED", "reason": str(e), "symbol": symbol}
