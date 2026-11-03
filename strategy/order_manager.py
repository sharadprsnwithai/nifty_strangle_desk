import logging
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.order")

class OrderManager:
    """Manages order execution for both PAPER simulation and LIVE trading."""

    def __init__(self, mode: str = "PAPER", kite: Optional[KiteConnect] = None):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("KiteConnect instance required for LIVE mode.")

    def sell(self, symbol: str, quantity: int, price: float) -> Dict[str, Any]:
        """Sells an option leg (entry)."""
        logger.info("[%s] Executing SELL: %s x %d @ Rs %.2f", self.mode, symbol, quantity, price)
        if self.mode == "PAPER":
            return {
                "order_id": f"PAPER_SELL_{symbol}",
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "fill_price": price,
                "status": "COMPLETE"
            }
        
        # LIVE Mode
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET
        )
        return {
            "order_id": order_id,
            "mode": "LIVE",
            "symbol": symbol,
            "transaction_type": "SELL",
            "quantity": quantity,
            "fill_price": price,
            "status": "SUBMITTED"
        }

    def buy_to_cover(self, symbol: str, quantity: int, price: float, reason: str = "EXIT") -> Dict[str, Any]:
        """Buys back an open short option leg (exit on SL, target, or 3PM)."""
        logger.info("[%s] Executing BUY TO COVER (%s): %s x %d @ Rs %.2f", self.mode, reason, symbol, quantity, price)
        if self.mode == "PAPER":
            return {
                "order_id": f"PAPER_BUY_{symbol}",
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": "BUY",
                "quantity": quantity,
                "fill_price": price,
                "reason": reason,
                "status": "COMPLETE"
            }

        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_BUY,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET
        )
        return {
            "order_id": order_id,
            "mode": "LIVE",
            "symbol": symbol,
            "transaction_type": "BUY",
            "quantity": quantity,
            "fill_price": price,
            "reason": reason,
            "status": "SUBMITTED"
        }
