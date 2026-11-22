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

        # LIVE Execution via Kite Connect
        try:
            logger.info(f"[LIVE SELL] Placing CNC SELL order for {quantity} {symbol}...")
            
            # Use Marketable LIMIT order or MARKET
            if self.config.order_type == "LIMIT":
                # For sell, buffer downward to ensure fill
                limit_price = round(current_price * (1.0 - self.config.limit_buffer_pct), 1)
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
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
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=quantity,
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
                
            logger.info(f"[LIVE SELL SUCCESS] Order ID: {order_id}")
            return {
                "status": "COMPLETE",
                "mode": "LIVE",
                "order_id": order_id,
                "symbol": symbol,
                "quantity": quantity,
                "price": current_price,
                "transaction_type": "SELL"
            }
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

            logger.info(f"[LIVE BUY SUCCESS] Order ID: {order_id}")
            return {
                "status": "COMPLETE",
                "mode": "LIVE",
                "order_id": order_id,
                "symbol": symbol,
                "quantity": quantity,
                "price": current_price,
                "cost": actual_cost,
                "transaction_type": "BUY"
            }
        except Exception as e:
            logger.error(f"[LIVE BUY FAILED] {symbol}: {e}")
            return {"status": "FAILED", "reason": str(e), "symbol": symbol}
