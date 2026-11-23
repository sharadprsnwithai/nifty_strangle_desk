import logging
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

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05,
        min_buffer_rs: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Sells an option leg on MCX (Strangle entry).
        For LIMIT orders, places a marketable limit order below LTP to prevent freak fills.
        """
        order_type_norm = order_type.upper()
        buffer = max(price * buffer_pct, min_buffer_rs)
        limit_price = max(0.5, _round_to_mcx_tick(price - buffer, self.config.tick_size)) if order_type_norm == "LIMIT" else None

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
            order_id = self.kite.place_order(**params)
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
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("[LIVE] Token error on MCX SELL order (%s). Refreshing session and retrying...", e)
                try:
                    self.kite = ensure_kite_session(self.kite, force_login=False)
                    order_id = self.kite.place_order(**params)
                    logger.info("[LIVE] MCX SELL order succeeded after token refresh: %s x %d (Order ID: %s)", symbol, quantity, order_id)
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
                except Exception as retry_err:
                    logger.error("[LIVE] MCX SELL order retry failed: %s", retry_err)
                    raise retry_err
            raise e

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05,
        min_buffer_rs: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Buys back an option leg on MCX (Square-off on SL, Target, or EOD).
        For LIMIT orders, places a marketable limit order above LTP to guarantee execution.
        """
        order_type_norm = order_type.upper()
        buffer = max(price * buffer_pct, min_buffer_rs)
        limit_price = max(0.5, _round_to_mcx_tick(price + buffer, self.config.tick_size)) if order_type_norm == "LIMIT" else None

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
            order_id = self.kite.place_order(**params)
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
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("[LIVE] Token error on MCX BUY order (%s). Refreshing session and retrying...", e)
                try:
                    self.kite = ensure_kite_session(self.kite, force_login=False)
                    order_id = self.kite.place_order(**params)
                    logger.info("[LIVE] MCX BUY order succeeded after token refresh: %s x %d (Order ID: %s)", symbol, quantity, order_id)
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
                except Exception as retry_err:
                    logger.error("[LIVE] MCX BUY order retry failed: %s", retry_err)
                    raise retry_err
            raise e
