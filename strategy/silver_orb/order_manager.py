import logging
import time
from typing import Any, Dict, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("silver_orb.order")


def _round_to_mcx_tick(price: float, tick: float = 1.0) -> float:
    """Rounds a price to the nearest valid MCX Silver tick size (1.0)."""
    return round(round(price / tick) * tick, 1)


class SilverOrderManager:
    """
    Manages order execution for both PAPER simulation and LIVE trading
    for the MCX Silver Mini (SILVERM) 18:00 US Session Breakout Strategy.
    """

    def __init__(
        self,
        mode: str = "PAPER",
        kite: Optional[KiteConnect] = None,
        exchange: str = "MCX",
        product: str = "MIS",
        order_type: str = "LIMIT",
        limit_buffer_pts: float = 15.0,
    ):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}. Must be 'PAPER' or 'LIVE'.")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("A valid KiteConnect instance is required for LIVE mode.")
        self.exchange = exchange.upper()
        self.product = product.upper()
        self.order_type = order_type.upper()
        self.limit_buffer_pts = float(limit_buffer_pts)

        self._paper_orders: Dict[str, Dict[str, Any]] = {}
        self._order_counter: int = 1

    def place_entry(
        self,
        symbol: str,
        direction: str,
        quantity_lots: int,
        entry_price: float,
        initial_sl: float,
        target_pts: float,
    ) -> Dict[str, Any]:
        """
        Executes a breakout entry order (BUY for LONG, SELL for SHORT).
        """
        transaction_type = "BUY" if direction.upper() == "LONG" else "SELL"
        order_price = _round_to_mcx_tick(entry_price)

        if self.mode == "PAPER":
            order_id = f"PAPER_SILVER_{self._order_counter:04d}"
            self._order_counter += 1
            order_record = {
                "order_id": order_id,
                "symbol": symbol,
                "exchange": self.exchange,
                "direction": direction.upper(),
                "transaction_type": transaction_type,
                "quantity": quantity_lots,
                "price": order_price,
                "status": "COMPLETE",
                "filled_quantity": quantity_lots,
                "average_price": order_price,
                "order_type": self.order_type,
                "product": self.product,
                "timestamp": time.time(),
            }
            self._paper_orders[order_id] = order_record
            logger.info(
                "[PAPER] Entry order filled: %s %d lots @ Rs %.1f (SL: Rs %.1f, Tgt: Rs %.1f)",
                transaction_type, quantity_lots, order_price, initial_sl, target_pts
            )
            return order_record

        # LIVE order execution via Kite
        try:
            kite_variety = self.kite.VARIETY_REGULAR
            kite_tx_type = self.kite.TRANSACTION_TYPE_BUY if transaction_type == "BUY" else self.kite.TRANSACTION_TYPE_SELL
            kite_product = self.kite.PRODUCT_MIS if self.product == "MIS" else self.kite.PRODUCT_NRML
            kite_order_type = self.kite.ORDER_TYPE_MARKET if self.order_type == "MARKET" else self.kite.ORDER_TYPE_LIMIT

            # Compute marketable limit price with safety buffer
            if self.order_type == "LIMIT":
                limit_p = _round_to_mcx_tick(order_price + self.limit_buffer_pts) if transaction_type == "BUY" else _round_to_mcx_tick(order_price - self.limit_buffer_pts)
            else:
                limit_p = None

            order_id = self.kite.place_order(
                variety=kite_variety,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=kite_tx_type,
                quantity=quantity_lots,
                product=kite_product,
                order_type=kite_order_type,
                price=limit_p,
            )
            logger.info("[LIVE] Submitted entry order %s for %s (%s, OrderType: %s, Price: %s)", order_id, symbol, transaction_type, self.order_type, limit_p)

            # Await and verify execution fill status
            avg_price = order_price
            order_status = "SUBMITTED"
            try:
                for _ in range(6):
                    time.sleep(0.5)
                    history = self.kite.order_history(order_id)
                    if history:
                        last_status = history[-1]
                        order_status = last_status.get("status")
                        if order_status == "COMPLETE":
                            avg_price = float(last_status.get("average_price") or order_price)
                            break
                        elif order_status in ("REJECTED", "CANCELLED"):
                            reject_reason = last_status.get("status_message", "Order rejected by broker")
                            logger.error("[LIVE] Entry order %s was %s: %s", order_id, order_status, reject_reason)
                            raise RuntimeError(f"Entry order {order_id} {order_status}: {reject_reason}")
            except RuntimeError:
                raise
            except Exception as hist_err:
                logger.warning("Could not fetch execution status for entry order %s (%s)", order_id, hist_err)

            return {
                "order_id": order_id,
                "symbol": symbol,
                "status": order_status,
                "average_price": avg_price,
                "transaction_type": transaction_type,
                "quantity": quantity_lots,
            }
        except Exception as e:
            logger.error("[LIVE] Failed to place entry order for %s: %s", symbol, e)
            raise

    def place_exit(
        self,
        symbol: str,
        direction: str,
        quantity_lots: int,
        exit_price: float,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Executes a partial or full position square-off (SELL for LONG, BUY for SHORT).
        """
        transaction_type = "SELL" if direction.upper() == "LONG" else "BUY"
        order_price = _round_to_mcx_tick(exit_price)

        if self.mode == "PAPER":
            order_id = f"PAPER_SILVER_EXIT_{self._order_counter:04d}"
            self._order_counter += 1
            order_record = {
                "order_id": order_id,
                "symbol": symbol,
                "exchange": self.exchange,
                "direction": direction.upper(),
                "transaction_type": transaction_type,
                "quantity": quantity_lots,
                "price": order_price,
                "status": "COMPLETE",
                "filled_quantity": quantity_lots,
                "average_price": order_price,
                "reason": reason,
                "timestamp": time.time(),
            }
            self._paper_orders[order_id] = order_record
            logger.info(
                "[PAPER] Exit filled (%s): %s %d lots of %s @ Rs %.1f",
                reason, transaction_type, quantity_lots, symbol, order_price
            )
            return order_record

        # LIVE order execution
        try:
            kite_variety = self.kite.VARIETY_REGULAR
            kite_tx_type = self.kite.TRANSACTION_TYPE_SELL if transaction_type == "SELL" else self.kite.TRANSACTION_TYPE_BUY
            kite_product = self.kite.PRODUCT_MIS if self.product == "MIS" else self.kite.PRODUCT_NRML

            # Compute marketable limit price with safety buffer for exit
            if self.order_type == "LIMIT":
                limit_p = _round_to_mcx_tick(order_price - self.limit_buffer_pts) if transaction_type == "SELL" else _round_to_mcx_tick(order_price + self.limit_buffer_pts)
                kite_order_type = self.kite.ORDER_TYPE_LIMIT
            else:
                limit_p = None
                kite_order_type = self.kite.ORDER_TYPE_MARKET

            order_id = self.kite.place_order(
                variety=kite_variety,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=kite_tx_type,
                quantity=quantity_lots,
                product=kite_product,
                order_type=kite_order_type,
                price=limit_p,
            )
            logger.info("[LIVE] Submitted exit order %s for %s (%s, Reason: %s)", order_id, symbol, transaction_type, reason)

            # Retrieve actual fill price if available from Kite order history
            avg_price = order_price
            try:
                for _ in range(5):
                    time.sleep(0.5)
                    history = self.kite.order_history(order_id)
                    if history:
                        last_status = history[-1]
                        if last_status.get("status") == "COMPLETE":
                            avg_price = float(last_status.get("average_price") or order_price)
                            break
            except Exception as hist_err:
                logger.warning("Could not fetch fill price for exit order %s (%s), defaulting to %.1f", order_id, hist_err, order_price)

            return {
                "order_id": order_id,
                "symbol": symbol,
                "status": "COMPLETE",
                "average_price": avg_price,
                "transaction_type": transaction_type,
                "quantity": quantity_lots,
                "reason": reason,
            }
        except Exception as e:
            logger.error("[LIVE] Failed to place exit order for %s: %s", symbol, e)
            raise
