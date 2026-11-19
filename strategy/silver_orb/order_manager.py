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
        limit_buffer_pts: float = 80.0,
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

            try:
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
            except Exception as e:
                from kite.client import is_token_error, ensure_kite_session
                if is_token_error(e):
                    logger.warning("[LIVE ENTRY] Token error for %s (%s). Reloading token from .env and retrying...", symbol, e)
                    self.kite = ensure_kite_session(self.kite, force_login=False)
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
                else:
                    raise
            logger.info("[LIVE] Submitted entry order %s for %s (%s, OrderType: %s, Price: %s)", order_id, symbol, transaction_type, self.order_type, limit_p or "MARKET")

            # Await and verify execution fill status
            avg_price = order_price
            order_status = "SUBMITTED"
            filled_qty = 0
            try:
                for _ in range(6):
                    time.sleep(0.5)
                    history = self.kite.order_history(order_id)
                    if history:
                        last_status = history[-1]
                        order_status = last_status.get("status")
                        filled_qty = int(last_status.get("filled_quantity", 0))
                        if order_status == "COMPLETE":
                            avg_price = float(last_status.get("average_price") or order_price)
                            break
                        elif order_status in ("REJECTED", "CANCELLED"):
                            reject_reason = last_status.get("status_message", "Order rejected by broker")
                            logger.error("[LIVE] Entry order %s was %s: %s", order_id, order_status, reject_reason)
                            if filled_qty > 0:
                                avg_price = float(last_status.get("average_price") or order_price)
                                order_status = "PARTIAL"
                                break
                            raise RuntimeError(f"Entry order {order_id} {order_status}: {reject_reason}")
            except RuntimeError:
                raise
            except Exception as hist_err:
                logger.warning("Could not fetch execution status for entry order %s (%s)", order_id, hist_err)

            if order_status != "COMPLETE" and order_status != "PARTIAL":
                if filled_qty > 0:
                    avg_price = float(last_status.get("average_price") or avg_price)
                    logger.warning("[LIVE] Entry order %s partially filled (%d/%d lots). Cancelling remainder...", order_id, filled_qty, quantity_lots)
                    try:
                        self.kite.cancel_order(variety=kite_variety, order_id=order_id)
                    except Exception as cancel_err:
                        logger.error("[LIVE] Error cancelling remainder for %s: %s", order_id, cancel_err)
                    order_status = "PARTIAL"
                else:
                    try:
                        logger.warning("[LIVE] Entry order %s not filled after timeout (status: %s). Cancelling order...", order_id, order_status)
                        self.kite.cancel_order(variety=kite_variety, order_id=order_id)
                    except Exception as cancel_err:
                        logger.error("[LIVE] Failed to cancel unfilled entry order %s: %s", order_id, cancel_err)
                    raise RuntimeError(f"Entry order {order_id} failed to fill within timeout (status: {order_status}). Cancelled.")

            actual_qty = filled_qty if filled_qty > 0 else quantity_lots
            return {
                "order_id": order_id,
                "symbol": symbol,
                "status": order_status,
                "average_price": avg_price,
                "transaction_type": transaction_type,
                "quantity": actual_qty,
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

            try:
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
            except Exception as e:
                from kite.client import is_token_error, ensure_kite_session
                if is_token_error(e):
                    logger.warning("[LIVE EXIT] Token error for %s (%s). Reloading token from .env and retrying...", symbol, e)
                    self.kite = ensure_kite_session(self.kite, force_login=False)
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
                else:
                    raise
            logger.info("[LIVE] Submitted exit order %s for %s (%s, OrderType: %s, Price: %s, Reason: %s)", order_id, symbol, transaction_type, self.order_type, limit_p or "MARKET", reason)

            # Retrieve actual fill price if available from Kite order history
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
                            logger.error("[LIVE] Exit order %s was %s: %s", order_id, order_status, reject_reason)
                            raise RuntimeError(f"Exit order {order_id} {order_status}: {reject_reason}")
            except RuntimeError:
                raise
            except Exception as hist_err:
                logger.warning("Could not fetch fill price for exit order %s (%s), defaulting to %.1f", order_id, hist_err, order_price)

            return {
                "order_id": order_id,
                "symbol": symbol,
                "status": order_status,
                "average_price": avg_price,
                "transaction_type": transaction_type,
                "quantity": quantity_lots,
                "reason": reason,
            }
        except Exception as e:
            logger.error("[LIVE] Failed to place exit order for %s: %s", symbol, e)
            raise

    def place_broker_sl(
        self,
        symbol: str,
        direction: str,
        quantity_lots: int,
        trigger_price: float,
        buffer_pts: float = 50.0,
    ) -> Optional[str]:
        """
        Submits a protective Stop-Loss Limit order on Kite (MCX).
        For LONG positions, places a SELL SL with trigger_price and limit_price = trigger_price - buffer.
        For SHORT positions, places a BUY SL with trigger_price and limit_price = trigger_price + buffer.
        Returns the broker order ID or None on failure.
        """
        transaction_type = "SELL" if direction.upper() == "LONG" else "BUY"
        trig_p = _round_to_mcx_tick(trigger_price)
        limit_p = _round_to_mcx_tick(trig_p - buffer_pts) if transaction_type == "SELL" else _round_to_mcx_tick(trig_p + buffer_pts)

        if self.mode == "PAPER":
            sl_id = f"PAPER_SL_{self._order_counter:04d}"
            self._order_counter += 1
            self._paper_orders[sl_id] = {
                "order_id": sl_id,
                "symbol": symbol,
                "direction": direction,
                "transaction_type": transaction_type,
                "quantity": quantity_lots,
                "trigger_price": trig_p,
                "price": limit_p,
                "status": "TRIGGER PENDING",
            }
            logger.info("[PAPER] Placed protective Stop Loss order %s: %s %d lots of %s (Trigger: Rs %.1f, Limit: Rs %.1f)", sl_id, transaction_type, quantity_lots, symbol, trig_p, limit_p)
            return sl_id

        try:
            kite_variety = self.kite.VARIETY_REGULAR
            kite_tx_type = self.kite.TRANSACTION_TYPE_SELL if transaction_type == "SELL" else self.kite.TRANSACTION_TYPE_BUY
            kite_product = self.kite.PRODUCT_MIS if self.product == "MIS" else self.kite.PRODUCT_NRML

            order_id = self.kite.place_order(
                variety=kite_variety,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=kite_tx_type,
                quantity=quantity_lots,
                product=kite_product,
                order_type=self.kite.ORDER_TYPE_SL,
                price=limit_p,
                trigger_price=trig_p,
            )
            logger.info("[LIVE] Placed broker-side Stop Loss order %s for %s (%s %d lots, Trigger: Rs %.1f, Limit: Rs %.1f)", order_id, symbol, transaction_type, quantity_lots, trig_p, limit_p)
            return str(order_id)
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                logger.warning("[LIVE SL] Token error for %s (%s). Reloading token from .env and retrying...", symbol, e)
                try:
                    self.kite = ensure_kite_session(self.kite, force_login=False)
                    order_id = self.kite.place_order(
                        variety=kite_variety,
                        exchange=self.exchange,
                        tradingsymbol=symbol,
                        transaction_type=kite_tx_type,
                        quantity=quantity_lots,
                        product=kite_product,
                        order_type=self.kite.ORDER_TYPE_SL,
                        price=limit_p,
                        trigger_price=trig_p,
                    )
                    logger.info("[LIVE SL] Stop Loss order succeeded after session refresh: %s for %s", order_id, symbol)
                    return str(order_id)
                except Exception as retry_e:
                    logger.critical("[LIVE SL] Failed to place broker-side Stop Loss order after session refresh: %s", retry_e, exc_info=True)
                    return None
            logger.critical("[LIVE] Failed to place broker-side Stop Loss order for %s: %s", symbol, e, exc_info=True)
            return None

    def modify_broker_sl(
        self,
        order_id: str,
        quantity_lots: int,
        trigger_price: float,
        buffer_pts: float = 50.0,
    ) -> bool:
        """Modifies quantity or trigger price of an existing broker Stop Loss order."""
        if not order_id:
            return False
        trig_p = _round_to_mcx_tick(trigger_price)

        if self.mode == "PAPER":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["quantity"] = quantity_lots
                self._paper_orders[order_id]["trigger_price"] = trig_p
            logger.info("[PAPER] Modified Stop Loss %s -> %d lots @ Trigger Rs %.1f", order_id, quantity_lots, trig_p)
            return True

        try:
            history = self.kite.order_history(order_id)
            if not history:
                return False
            tx_type = history[-1].get("transaction_type", "BUY")
            limit_p = _round_to_mcx_tick(trig_p - buffer_pts) if tx_type == "SELL" else _round_to_mcx_tick(trig_p + buffer_pts)

            self.kite.modify_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
                quantity=quantity_lots,
                price=limit_p,
                trigger_price=trig_p,
                order_type=self.kite.ORDER_TYPE_SL,
            )
            logger.info("[LIVE] Modified broker Stop Loss %s: %d lots @ Trigger Rs %.1f (Limit Rs %.1f)", order_id, quantity_lots, trig_p, limit_p)
            return True
        except Exception as e:
            logger.error("[LIVE] Failed to modify broker Stop Loss %s: %s", order_id, e)
            return False

    def cancel_broker_sl(self, order_id: str) -> bool:
        """Cancels a broker Stop Loss order."""
        if not order_id:
            return False
        if self.mode == "PAPER":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "CANCELLED"
            logger.info("[PAPER] Cancelled protective Stop Loss %s", order_id)
            return True
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
            logger.info("[LIVE] Cancelled broker Stop Loss %s", order_id)
            return True
        except Exception as e:
            logger.warning("[LIVE] Error cancelling broker Stop Loss %s (may already be triggered or cancelled): %s", order_id, e)
            return False

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Retrieves latest order status from broker or paper tracking."""
        if self.mode == "PAPER":
            rec = self._paper_orders.get(order_id, {})
            return {
                "status": rec.get("status", "COMPLETE"),
                "average_price": rec.get("average_price", rec.get("price", 0.0)),
                "filled_quantity": rec.get("filled_quantity", rec.get("quantity", 0)),
            }
        try:
            history = self.kite.order_history(order_id)
            if history:
                last_rec = history[-1]
                return {
                    "status": last_rec.get("status", "UNKNOWN"),
                    "average_price": float(last_rec.get("average_price") or 0.0),
                    "filled_quantity": int(last_rec.get("filled_quantity") or 0),
                }
        except Exception as e:
            logger.warning("Could not query status for order %s: %s", order_id, e)
        return {"status": "UNKNOWN", "average_price": 0.0, "filled_quantity": 0}
