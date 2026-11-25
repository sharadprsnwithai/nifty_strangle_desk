import logging
import threading
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.order")

NSE_NIFTY_FREEZE_QTY = 1755


def _round_to_nse_tick(price: float, tick: float = 0.05) -> float:
    """Rounds a price to the nearest valid NSE option tick size (0.05)."""
    return round(round(price / tick) * tick, 2)


class OrderManager:
    """Manages order execution for both PAPER simulation and LIVE trading."""

    def __init__(self, mode: str = "PAPER", kite: Optional[KiteConnect] = None, product: str = "MIS"):
        self.mode = mode.upper()
        if self.mode not in ("PAPER", "LIVE"):
            raise ValueError(f"Invalid mode: {self.mode}")
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("KiteConnect instance required for LIVE mode.")
        self.product = product.upper()
        if self.product not in ("MIS", "NRML", "CNC"):
            raise ValueError(f"Invalid product type: {self.product}. Must be MIS, NRML, or CNC.")
        self._paper_orders: Dict[str, Dict[str, Any]] = {}
        self._sliced_orders: Dict[str, list] = {}
        self._kite_lock = threading.RLock()

    @staticmethod
    def _split_freeze_quantities(quantity: int, symbol: str, lot_size: int = 65) -> list:
        """Splits NIFTY orders exceeding NSE_NIFTY_FREEZE_QTY into exchange-valid slices."""
        if quantity <= NSE_NIFTY_FREEZE_QTY or not symbol.upper().startswith("NIFTY"):
            return [quantity]
        max_slice = (NSE_NIFTY_FREEZE_QTY // max(1, lot_size)) * max(1, lot_size)
        if max_slice <= 0:
            max_slice = NSE_NIFTY_FREEZE_QTY
        chunks = []
        rem = quantity
        while rem > 0:
            part = min(rem, max_slice)
            chunks.append(part)
            rem -= part
        return chunks

    def _call_kite_with_token_retry(self, op_desc: str, fn):
        """Executes a Kite API call and thread-safely refreshes token once on TokenException."""
        try:
            return fn()
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if not is_token_error(e):
                raise
            with self._kite_lock:
                logger.warning("[LIVE] Token error on %s (%s). Refreshing session from .env and retrying...", op_desc, e)
                self.kite = ensure_kite_session(self.kite, force_login=False)
                return fn()

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05,
        min_buffer_rs: float = 0.0,
        product: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sells an option leg (entry).
        If order_type is LIMIT, places a marketable limit order below LTP (by buffer_pct / min_buffer_rs)
        to prevent freak-trade execution while guaranteeing immediate fill against the bid.
        """
        order_type_norm = order_type.upper()
        buffer = max(price * buffer_pct, min_buffer_rs)
        limit_price = max(0.05, _round_to_nse_tick(price - buffer)) if order_type_norm == "LIMIT" else None

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
                "product": product or self.product,
                "status": "COMPLETE"
            }
            self._paper_orders[order["order_id"]] = order.copy()
            return order
        
        # LIVE Mode
        prod = (product or self.product).upper()
        prod_val = getattr(self.kite, f"PRODUCT_{prod}", prod)
        validity_val = getattr(self.kite, "VALIDITY_DAY", "DAY")
        params: Dict[str, Any] = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": self.kite.EXCHANGE_NFO,
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

        slices = self._split_freeze_quantities(quantity, symbol)
        order_ids = []
        try:
            for idx, slice_qty in enumerate(slices):
                slice_params = dict(params, quantity=slice_qty)
                logger.info(
                    "[LIVE] Submitting SELL order%s: %s x %d (OrderType: %s, LimitPrice: %s)",
                    f" slice {idx + 1}/{len(slices)}" if len(slices) > 1 else "",
                    symbol, slice_qty, order_type_norm, limit_price,
                )
                oid = self._call_kite_with_token_retry(
                    f"SELL ({symbol})",
                    lambda p=slice_params: self.kite.place_order(**p),
                )
                order_ids.append(oid)
                logger.info("[LIVE] SELL order placed successfully: %s x %d (Order ID: %s)", symbol, slice_qty, oid)
            primary_id = order_ids[0]
            if len(order_ids) > 1:
                with self._kite_lock:
                    self._sliced_orders[primary_id] = list(order_ids)
            res = {
                "order_id": primary_id,
                "mode": "LIVE",
                "symbol": symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "fill_price": price,
                "status": "SUBMITTED"
            }
            if len(order_ids) > 1:
                res["order_ids"] = order_ids
            return res
        except Exception as e:
            if order_ids:
                logger.error("[LIVE] Partial freeze-slice failure on SELL %s (%d/%d slices placed). Rolling back %s...", symbol, len(order_ids), len(slices), order_ids)
                for placed_oid in order_ids:
                    try:
                        self.cancel_order(placed_oid)
                        st = self.get_order_status(placed_oid)
                        filled = int(st.get("filled_quantity") or 0)
                        if filled > 0:
                            logger.warning("[LIVE] Slice %s filled %d qty before slice failure. Squaring off via MARKET...", placed_oid, filled)
                            self.buy_to_cover(symbol, filled, price, reason="ABORT_SLICE_ROLLBACK", order_type="MARKET", product=prod)
                    except Exception as rb_err:
                        logger.error("[LIVE] Rollback failed for slice %s: %s", placed_oid, rb_err)
            logger.exception("[LIVE] Failed to place SELL order for %s (qty: %d, type: %s): %s", symbol, quantity, order_type_norm, e)
            raise

    def buy_to_cover(
        self,
        symbol: str,
        quantity: int,
        price: float,
        reason: str = "EXIT",
        order_type: str = "LIMIT",
        buffer_pct: float = 0.05,
        min_buffer_rs: float = 0.0,
        product: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Buys back an open short option leg (exit on SL, target, or 3PM).
        If order_type is LIMIT, places a marketable limit order above LTP (by buffer_pct / min_buffer_rs)
        to guarantee execution against the ask while preventing execution at extreme freak prices.
        """
        order_type_norm = order_type.upper()
        buffer = max(price * buffer_pct, min_buffer_rs)
        limit_price = max(0.05, _round_to_nse_tick(price + buffer)) if order_type_norm == "LIMIT" else None

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
                "product": product or self.product,
                "status": "COMPLETE"
            }
            self._paper_orders[order["order_id"]] = order.copy()
            return order

        prod = (product or self.product).upper()
        prod_val = getattr(self.kite, f"PRODUCT_{prod}", prod)
        validity_val = getattr(self.kite, "VALIDITY_DAY", "DAY")
        params: Dict[str, Any] = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": self.kite.EXCHANGE_NFO,
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

        slices = self._split_freeze_quantities(quantity, symbol)
        order_ids = []
        try:
            for idx, slice_qty in enumerate(slices):
                slice_params = dict(params, quantity=slice_qty)
                logger.info(
                    "[LIVE] Submitting BUY-TO-COVER order%s: %s x %d (OrderType: %s, LimitPrice: %s, Reason: %s)",
                    f" slice {idx + 1}/{len(slices)}" if len(slices) > 1 else "",
                    symbol, slice_qty, order_type_norm, limit_price, reason,
                )
                oid = self._call_kite_with_token_retry(
                    f"BUY_TO_COVER ({symbol})",
                    lambda p=slice_params: self.kite.place_order(**p),
                )
                order_ids.append(oid)
                logger.info("[LIVE] BUY-TO-COVER order placed successfully: %s x %d (Order ID: %s, Reason: %s)", symbol, slice_qty, oid, reason)
            primary_id = order_ids[0]
            if len(order_ids) > 1:
                with self._kite_lock:
                    self._sliced_orders[primary_id] = list(order_ids)
            res = {
                "order_id": primary_id,
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
            if len(order_ids) > 1:
                res["order_ids"] = order_ids
            return res
        except Exception as e:
            if order_ids:
                logger.error("[LIVE] Partial freeze-slice failure on BUY-TO-COVER %s (%d/%d slices placed). Partial order IDs: %s", symbol, len(order_ids), len(slices), order_ids)
                e.placed_order_ids = list(order_ids)
            logger.exception("[LIVE] Failed to place BUY-TO-COVER order for %s (qty: %d, reason: %s): %s", symbol, quantity, reason, e)
            raise

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

        with self._kite_lock:
            child_ids = list(self._sliced_orders.get(order_id, []))
        if len(child_ids) > 1:
            child_statuses = [self._get_single_order_status(cid) for cid in child_ids]
            total_filled = sum(s["filled_quantity"] for s in child_statuses)
            total_pending = sum(s["pending_quantity"] for s in child_statuses)
            weighted_val = sum(s["filled_quantity"] * s["average_price"] for s in child_statuses)
            avg_px = round(weighted_val / total_filled, 2) if total_filled > 0 else 0.0
            raw_states = [s["status"] for s in child_statuses]
            if all(st == "COMPLETE" for st in raw_states):
                agg_status = "COMPLETE"
            elif any(st in ("OPEN", "TRIGGER PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED") for st in raw_states):
                agg_status = "OPEN"
            elif "ERROR" in raw_states:
                agg_status = "ERROR"
            elif any(st == "REJECTED" for st in raw_states):
                agg_status = "REJECTED"
            elif any(st == "CANCELLED" for st in raw_states):
                agg_status = "CANCELLED"
            else:
                agg_status = raw_states[0]
            msgs = [s["status_message"] for s in child_statuses if s.get("status_message")]
            return {
                "order_id": order_id,
                "status": agg_status,
                "filled_quantity": total_filled,
                "pending_quantity": total_pending,
                "average_price": avg_px,
                "status_message": "; ".join(msgs) if msgs else None,
            }
        return self._get_single_order_status(order_id)

    def _get_single_order_status(self, order_id: str) -> Dict[str, Any]:
        try:
            history = self._call_kite_with_token_retry(
                f"order_history({order_id})",
                lambda: self.kite.order_history(order_id),
            )
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
            logger.exception("[LIVE] Error fetching order status for %s: %s", order_id, e)
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
        Cancels an open or pending order (and any freeze-limit slices) on the exchange.
        """
        logger.info("[%s] Cancelling order: %s", self.mode, order_id)
        if self.mode == "PAPER":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "CANCELLED"
                self._paper_orders[order_id]["pending_quantity"] = 0
            return True

        with self._kite_lock:
            targets = list(self._sliced_orders.get(order_id, [order_id]))
        all_ok = True
        for oid in targets:
            try:
                self._call_kite_with_token_retry(
                    f"cancel_order({oid})",
                    lambda target_oid=oid: self.kite.cancel_order(variety=variety, order_id=target_oid),
                )
                logger.info("[LIVE] Cancel request sent successfully for order: %s", oid)
            except Exception as e:
                logger.exception("[LIVE] Failed to cancel order %s: %s", oid, e)
                all_ok = False
        return all_ok
