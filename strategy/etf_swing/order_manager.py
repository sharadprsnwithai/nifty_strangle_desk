"""
Order Manager for Weekly ETF Swing Desk.
Handles order execution for CNC delivery in both PAPER and LIVE modes.
"""

from typing import Dict, Optional
import logging
import time
from kiteconnect import KiteConnect
from strategy.etf_swing.config import ETFSwingConfig

logger = logging.getLogger("etf_swing.orders")

class ETFOrderManager:
    def __init__(self, config: ETFSwingConfig, kite_client: Optional[KiteConnect] = None):
        self.config = config
        self.kite = kite_client

    def _poll_order_fill(self, order_id: str, max_wait_sec: int = 5) -> Dict:
        """
        Polls Kite order history to determine final execution state and filled quantity.
        If order is partially filled or unfilled after timeout, cancels any open remainder.
        """
        if not self.kite:
            return {"status": "COMPLETE", "filled_quantity": 0, "average_price": 0.0}

        for _ in range(max_wait_sec):
            time.sleep(1)
            try:
                history = self.kite.order_history(order_id)
                if not history:
                    continue
                latest = history[-1]
                status = latest.get("status")
                filled_qty = int(latest.get("filled_quantity") or 0)
                avg_price = float(latest.get("average_price") or 0.0)

                if status == "COMPLETE":
                    return {
                        "status": "COMPLETE",
                        "filled_quantity": filled_qty,
                        "average_price": avg_price
                    }
                elif status in ("REJECTED", "CANCELLED"):
                    return {
                        "status": "FAILED",
                        "reason": latest.get("status_message", f"Order {status}"),
                        "filled_quantity": filled_qty,
                        "average_price": avg_price
                    }
            except Exception as e:
                logger.warning(f"Error checking status for order {order_id}: {e}")

        # Timeout reached while still OPEN/TRIGGER PENDING
        try:
            history = self.kite.order_history(order_id)
            latest = history[-1] if history else {}
            filled_qty = int(latest.get("filled_quantity") or 0)
            avg_price = float(latest.get("average_price") or 0.0)

            # Cancel remaining pending quantity
            try:
                self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
                logger.info(f"Cancelled remaining unfilled quantity for order {order_id}")
            except Exception as cancel_e:
                logger.warning(f"Could not cancel order {order_id}: {cancel_e}")

            if filled_qty > 0:
                return {
                    "status": "COMPLETE",
                    "filled_quantity": filled_qty,
                    "average_price": avg_price
                }
            else:
                return {
                    "status": "FAILED",
                    "reason": f"Order {order_id} timed out without fills and was cancelled."
                }
        except Exception as e:
            return {"status": "FAILED", "reason": str(e)}

    def execute_sell(self, symbol: str, quantity: int, current_price: float) -> Dict:
        """
        Executes a delivery SELL order (CNC) to close an ETF position.
        """
        if quantity <= 0:
            return {"status": "FAILED", "reason": "Invalid quantity <= 0"}

        if self.config.mode == "PAPER" or not self.kite:
            logger.info(f"[PAPER SELL] Sold {quantity} units of {symbol} at Rs {current_price:.2f}")
            return {
                "status": "COMPLETE",
                "mode": "PAPER",
                "order_id": f"PAPER_SELL_{int(time.time())}",
                "symbol": symbol,
                "quantity": quantity,
                "price": current_price,
                "transaction_type": "SELL"
            }

        # Check actual Demat / T1 holdings to avoid Zerodha RMS rejection
        sell_qty = quantity
        try:
            holdings = self.kite.holdings()
            holding_item = next((h for h in holdings if h.get("tradingsymbol") == symbol), None)
            if holding_item:
                demat_qty = int(holding_item.get("quantity", 0) + holding_item.get("t1_quantity", 0))
                if demat_qty < sell_qty:
                    logger.warning(
                        f"Holding mismatch for {symbol}: state has {sell_qty}, Demat has {demat_qty}. Adjusting sell to {demat_qty}."
                    )
                    sell_qty = demat_qty
            else:
                logger.warning(f"No Demat holdings found for {symbol} on Kite. Proceeding with requested quantity {sell_qty}.")
        except Exception as e:
            logger.warning(f"Could not query Demat holdings from Kite: {e}")

        if sell_qty <= 0:
            return {"status": "FAILED", "reason": f"No available Demat holdings to sell for {symbol}"}

        # LIVE Execution via Kite Connect
        try:
            logger.info(f"[LIVE SELL] Placing CNC SELL order for {sell_qty} {symbol}...")
            
            # Use Marketable LIMIT order or MARKET
            if self.config.order_type == "LIMIT":
                # For sell, buffer downward to ensure fill
                limit_price = round(current_price * (1.0 - self.config.limit_buffer_pct), 1)
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=sell_qty,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=limit_price
                )
            else:
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=sell_qty,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
                
            logger.info(f"[LIVE SELL SUBMITTED] Order ID: {order_id}. Waiting for fill confirmation...")
            fill_info = self._poll_order_fill(order_id)
            if fill_info.get("status") == "COMPLETE":
                actual_qty = fill_info.get("filled_quantity", sell_qty)
                avg_price = fill_info.get("average_price") or current_price
                logger.info(f"[LIVE SELL COMPLETE] Sold {actual_qty}/{sell_qty} {symbol} @ Rs {avg_price:.2f}")
                return {
                    "status": "COMPLETE",
                    "mode": "LIVE",
                    "order_id": order_id,
                    "symbol": symbol,
                    "quantity": actual_qty,
                    "price": avg_price,
                    "transaction_type": "SELL"
                }
            else:
                reason = fill_info.get("reason", "Order not completed")
                logger.error(f"[LIVE SELL NOT COMPLETED] {symbol}: {reason}")
                return {"status": "FAILED", "reason": reason, "symbol": symbol}
        except Exception as e:
            logger.error(f"[LIVE SELL FAILED] {symbol}: {e}")
            return {"status": "FAILED", "reason": str(e), "symbol": symbol}

    def execute_buy(self, symbol: str, allocated_capital: float, current_price: float) -> Dict:
        """
        Executes a delivery BUY order (CNC) to enter a new ETF position.
        Calculates exact quantity based on allocated capital.
        """
        if current_price <= 0:
            return {"status": "FAILED", "reason": "Invalid price <= 0"}

        quantity = int(allocated_capital // current_price)
        if quantity < 1:
            return {
                "status": "FAILED", 
                "reason": f"Insufficient capital (Rs {allocated_capital:.0f}) for single share at Rs {current_price:.2f}"
            }

        actual_cost = quantity * current_price

        if self.config.mode == "PAPER" or not self.kite:
            logger.info(f"[PAPER BUY] Bought {quantity} units of {symbol} at Rs {current_price:.2f} (Total: Rs {actual_cost:,.2f})")
            return {
                "status": "COMPLETE",
                "mode": "PAPER",
                "order_id": f"PAPER_BUY_{int(time.time())}",
                "symbol": symbol,
                "quantity": quantity,
                "price": current_price,
                "cost": actual_cost,
                "transaction_type": "BUY"
            }

        # LIVE Execution via Kite Connect
        try:
            logger.info(f"[LIVE BUY] Placing CNC BUY order for {quantity} {symbol} at Rs {current_price:.2f}...")
            
            if self.config.order_type == "LIMIT":
                # For buy, buffer upward to ensure fill
                limit_price = round(current_price * (1.0 + self.config.limit_buffer_pct), 1)
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=quantity,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=limit_price
                )
            else:
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=quantity,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )

            logger.info(f"[LIVE BUY SUBMITTED] Order ID: {order_id}. Waiting for fill confirmation...")
            fill_info = self._poll_order_fill(order_id)
            if fill_info.get("status") == "COMPLETE":
                actual_qty = fill_info.get("filled_quantity", quantity)
                avg_price = fill_info.get("average_price") or current_price
                final_cost = actual_qty * avg_price
                logger.info(f"[LIVE BUY COMPLETE] Bought {actual_qty}/{quantity} {symbol} @ Rs {avg_price:.2f} (Cost: Rs {final_cost:,.2f})")
                return {
                    "status": "COMPLETE",
                    "mode": "LIVE",
                    "order_id": order_id,
                    "symbol": symbol,
                    "quantity": actual_qty,
                    "price": avg_price,
                    "cost": final_cost,
                    "transaction_type": "BUY"
                }
            else:
                reason = fill_info.get("reason", "Order not completed")
                logger.error(f"[LIVE BUY NOT COMPLETED] {symbol}: {reason}")
                return {"status": "FAILED", "reason": reason, "symbol": symbol}
        except Exception as e:
            logger.error(f"[LIVE BUY FAILED] {symbol}: {e}")
            return {"status": "FAILED", "reason": str(e), "symbol": symbol}
