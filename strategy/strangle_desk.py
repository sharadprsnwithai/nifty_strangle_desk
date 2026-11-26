import os
import json
import logging
import time
import tempfile
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from kiteconnect import KiteConnect
from kite.client import is_token_error
from strategy.order_manager import OrderManager
from strategy.option_chain import find_strangle_strikes, resolve_target_expiry, _normalize_date
from strategy.holidays import is_trading_holiday
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("strategy.desk")


def get_default_strangle_state_file(mode: str = "LIVE") -> Path:
    env_file = os.getenv("STRANGLE_STATE_FILE")
    if env_file:
        return Path(env_file).resolve()
    filename = "paper_trades_today.json" if str(mode).upper() == "PAPER" else "trades_today.json"
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return Path(data_dir).resolve() / filename
    return Path(__file__).resolve().parent.parent / "data" / filename


DEFAULT_STATE_FILE = get_default_strangle_state_file()


class StrangleDesk:
    """Manages strategy state, persistence, recovery, and intraday execution."""

    def __init__(
        self,
        order_manager: OrderManager,
        state_file: Optional[Path] = None,
        notifier: Optional[TelegramNotifier] = None
    ):
        self.order_manager = order_manager
        om_mode = getattr(order_manager, "mode", "LIVE") if order_manager is not None else "LIVE"
        self.state_file = Path(state_file).resolve() if state_file else get_default_strangle_state_file(mode=om_mode)
        self.notifier = notifier
        self.state: Optional[Dict[str, Any]] = None

    def _get_fallback_file(self) -> Path:
        fallback_dir = Path(tempfile.gettempdir()) / "trading_state"
        return fallback_dir / self.state_file.name

    def save_state(self, state: Dict[str, Any]) -> None:
        """Atomically saves strangle state to disk with fallback resilience against filesystem/mount errors."""
        self.state = state
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error("Primary state save failed at %s (%s). Attempting fallback...", self.state_file, e)
            try:
                fallback_file = self._get_fallback_file()
                fallback_file.parent.mkdir(parents=True, exist_ok=True)
                temp_fallback = fallback_file.with_suffix(".tmp")
                temp_fallback.write_text(json.dumps(state, indent=2), encoding="utf-8")
                temp_fallback.replace(fallback_file)
                logger.warning("Strangle state saved to fallback path: %s", fallback_file)
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>State Warning</b>: Primary state save failed at <code>{self.state_file}</code> ({e}). "
                        f"State saved to fallback <code>{fallback_file}</code>."
                    )
            except Exception as fb_err:
                logger.critical("All state save attempts failed: %s (Fallback error: %s). Preserving state in memory.", e, fb_err)

    def load_state(self) -> Optional[Dict[str, Any]]:
        target_file = self.state_file
        if not target_file.exists():
            fallback_file = self._get_fallback_file()
            if fallback_file.exists():
                logger.info("Primary state file not found; loading from fallback: %s", fallback_file)
                target_file = fallback_file
            else:
                return None
        try:
            content = target_file.read_text(encoding="utf-8")
            self.state = json.loads(content)
            return self.state
        except Exception as e:
            logger.error("Failed to load state from %s: %s", target_file, e)
            return None

    def clear_state(self) -> None:
        for f in (self.state_file, self._get_fallback_file()):
            if f.exists():
                try:
                    f.unlink()
                except Exception as e:
                    logger.warning("Could not delete state file %s: %s", f, e)
        self.state = None

    @staticmethod
    def _print_plan(strikes: Dict[str, Any], mode: str = "PAPER", exit_time_str: str = "14:45") -> None:
        ce_info = strikes["ce"]
        pe_info = strikes["pe"]
        print("\n" + "=" * 65)
        print(f"  [+] 10:00 AM NIFTY STRANGLE PLAN ({mode} MODE)")
        print("=" * 65)
        print(f"Target Expiry : {strikes['expiry']}")
        print(f"Order Sizing  : {strikes['total_lots']} lots ({strikes['total_quantity']} qty)")
        print(f"CALL Leg      : {ce_info['symbol']} @ Rs {ce_info['ltp']:.2f} | Target: Rs {ce_info['target_price']:.2f} (80% decay)")
        print(f"PUT Leg       : {pe_info['symbol']} @ Rs {pe_info['ltp']:.2f} | Target: Rs {pe_info['target_price']:.2f} (80% decay)")
        print(f"Exit Rules    : Single-Leg 50% SL (winner stays open & trails to cost) | Target: 80% decay | Cutoff: {exit_time_str} IST")
        print("=" * 65 + "\n")

    @staticmethod
    def _extract_ltp(quotes: Dict[str, Any], sym: str, default: float = 0.0) -> float:
        data = quotes.get(f"NFO:{sym}") or quotes.get(sym) or {}
        if isinstance(data, dict):
            return float(data.get("last_price", default))
        elif isinstance(data, (int, float)):
            return float(data)
        return default

    def _safe_exit_leg(
        self,
        leg_name: str,
        symbol: str,
        qty: int,
        exit_price: float,
        reason: str,
        order_type: str,
        limit_buffer_pct: float,
        state: Dict[str, Any],
        min_buffer_rs: float = 1.5,
        escalate_to_market: bool = False,
    ) -> bool:
        """
        Safely executes buy_to_cover for a single strangle leg.
        Tracks partial exit fills (`exit_filled_qty`) so retries never over-buy,
        verifies order completion on Kite before marking CLOSED, and optionally
        escalates unfilled exits to MARKET.
        """
        try:
            leg_state = state.setdefault(leg_name, {})
            prev_filled = int(leg_state.get("exit_filled_qty") or 0)
            prev_exit_px = float(leg_state["exit_price"]) if leg_state.get("exit_price") is not None else 0.0
            remaining_qty = max(0, qty - prev_filled)
            if remaining_qty == 0:
                leg_state["status"] = "CLOSED"
                leg_state["exit_reason"] = reason
                self.save_state(state)
                return True

            leg_product = (
                leg_state.get("product")
                or state.get("product")
                or getattr(self.order_manager, "product", "MIS")
            )
            btc_kwargs: Dict[str, Any] = {
                "reason": reason,
                "order_type": order_type,
                "buffer_pct": limit_buffer_pct,
                "product": leg_product,
            }
            if min_buffer_rs > 0.0:
                btc_kwargs["min_buffer_rs"] = min_buffer_rs

            order_res = self.order_manager.buy_to_cover(
                symbol, remaining_qty, exit_price, **btc_kwargs
            )
            mode = getattr(self.order_manager, "mode", "PAPER")
            order_id = order_res.get("order_id") if isinstance(order_res, dict) else None

            actual_exit_price = exit_price
            if mode == "LIVE" and order_id:
                status_res = self.wait_for_order_completion(order_id, timeout_seconds=10, poll_seconds=1)
                order_status = status_res.get("status")
                if order_status == "COMPLETE":
                    fill_px = float(status_res["average_price"]) if status_res.get("average_price") is not None and float(status_res.get("average_price")) > 0 else exit_price
                    total_done = prev_filled + remaining_qty
                    actual_exit_price = round(((prev_filled * prev_exit_px) + (remaining_qty * fill_px)) / total_done, 2) if total_done > 0 else fill_px
                    leg_state["exit_filled_qty"] = total_done
                elif order_status in ("REJECTED", "CANCELLED"):
                    err_msg = status_res.get("status_message") or f"Order ended in {order_status}"
                    part_filled = int(status_res.get("filled_quantity") or 0)
                    total_done = prev_filled + part_filled
                    if part_filled > 0:
                        part_px = float(status_res["average_price"]) if status_res.get("average_price") is not None and float(status_res.get("average_price")) > 0 else exit_price
                        actual_exit_price = round(((prev_filled * prev_exit_px) + (part_filled * part_px)) / total_done, 2) if total_done > 0 else part_px
                        leg_state["exit_price"] = actual_exit_price
                        leg_state["exit_filled_qty"] = total_done
                        self.save_state(state)
                    still_left = max(0, qty - total_done)
                    if still_left == 0:
                        actual_exit_price = float(leg_state["exit_price"]) if leg_state.get("exit_price") is not None else exit_price
                    elif escalate_to_market and order_type.upper() != "MARKET":
                        logger.warning("Exit order %s (%s) %s (%d/%d filled). Escalating %d qty to MARKET...", order_id, symbol, order_status, total_done, qty, still_left)
                        mkt_res = self.order_manager.buy_to_cover(
                            symbol, still_left, exit_price, reason=reason, order_type="MARKET", product=leg_product
                        )
                        mkt_id = mkt_res.get("order_id") if isinstance(mkt_res, dict) else None
                        if mkt_id:
                            mkt_st = self.wait_for_order_completion(mkt_id, timeout_seconds=10, poll_seconds=1)
                            mkt_filled = int(mkt_st.get("filled_quantity") or 0)
                            if mkt_st.get("status") == "COMPLETE" or mkt_filled >= still_left:
                                fill_px = float(mkt_st["average_price"]) if mkt_st.get("average_price") is not None and float(mkt_st.get("average_price")) > 0 else exit_price
                                new_total = total_done + (still_left if mkt_st.get("status") == "COMPLETE" else mkt_filled)
                                prev_sum_px = float(leg_state["exit_price"]) if leg_state.get("exit_price") is not None else prev_exit_px
                                actual_exit_price = round(((total_done * prev_sum_px) + ((new_total - total_done) * fill_px)) / new_total, 2) if new_total > 0 else fill_px
                                leg_state["exit_filled_qty"] = new_total
                                order_id = mkt_id
                            else:
                                logger.error("MARKET escalation failed for %s (%s): %s", symbol, reason, mkt_st)
                                return False
                        else:
                            return False
                    else:
                        logger.error("Exit order %s for %s (%s) was %s: %s", order_id, leg_name.upper(), symbol, order_status, err_msg)
                        if self.notifier:
                            self.notifier.send_message(
                                f"🚨 <b>Exit Order {order_status}</b>: Failed to exit {leg_name.upper()} <code>{symbol}</code> ({reason}): {err_msg}"
                            )
                        return False
                else:
                    logger.warning("Exit order %s for %s still pending (%s) after 10s. Cancelling resting order...", order_id, symbol, order_status)
                    self.order_manager.cancel_order(order_id)
                    cancel_res = self.wait_for_order_completion(order_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                    filled_qty = int(cancel_res.get("filled_quantity") or 0)
                    total_done = prev_filled + filled_qty
                    if filled_qty > 0:
                        part_px = float(cancel_res["average_price"]) if cancel_res.get("average_price") is not None and float(cancel_res.get("average_price")) > 0 else exit_price
                        actual_exit_price = round(((prev_filled * prev_exit_px) + (filled_qty * part_px)) / total_done, 2) if total_done > 0 else part_px
                        leg_state["exit_price"] = actual_exit_price
                        leg_state["exit_filled_qty"] = total_done
                        self.save_state(state)
                    if total_done >= qty:
                        actual_exit_price = float(leg_state["exit_price"]) if leg_state.get("exit_price") is not None else exit_price
                    elif escalate_to_market and order_type.upper() != "MARKET":
                        still_left = max(0, qty - total_done)
                        logger.warning("Escalating remaining %d/%d of %s (%s) via MARKET...", still_left, qty, symbol, reason)
                        mkt_res = self.order_manager.buy_to_cover(
                            symbol, still_left, exit_price, reason=reason, order_type="MARKET", product=leg_product
                        )
                        mkt_id = mkt_res.get("order_id") if isinstance(mkt_res, dict) else None
                        if mkt_id:
                            mkt_st = self.wait_for_order_completion(mkt_id, timeout_seconds=10, poll_seconds=1)
                            mkt_filled = int(mkt_st.get("filled_quantity") or 0)
                            if mkt_filled > 0:
                                mkt_px = float(mkt_st["average_price"]) if mkt_st.get("average_price") is not None and float(mkt_st.get("average_price")) > 0 else exit_price
                                new_total = total_done + mkt_filled
                                prev_sum_px = float(leg_state["exit_price"]) if leg_state.get("exit_price") is not None else 0.0
                                actual_exit_price = round(((total_done * prev_sum_px) + (mkt_filled * mkt_px)) / new_total, 2) if new_total > 0 else mkt_px
                                leg_state["exit_price"] = actual_exit_price
                                leg_state["exit_filled_qty"] = new_total
                                order_id = mkt_id
                                self.save_state(state)
                            if int(leg_state.get("exit_filled_qty") or 0) < qty:
                                if self.notifier:
                                    self.notifier.send_message(
                                        f"🚨 <b>Incomplete Escalated Exit</b>: {leg_name.upper()} <code>{symbol}</code> filled {leg_state.get('exit_filled_qty', 0)}/{qty}. Retrying next tick!"
                                    )
                                return False
                        else:
                            return False
                    elif filled_qty > 0:
                        logger.warning("Exit order %s partially filled (%d/%d).", order_id, filled_qty, qty)
                        if self.notifier:
                            self.notifier.send_message(
                                f"⚠️ <b>Partial Exit Fill</b>: {leg_name.upper()} <code>{symbol}</code> filled {total_done}/{qty}. Remaining {qty - total_done} will retry next tick."
                            )
                        return False
                    else:
                        if self.notifier:
                            self.notifier.send_message(
                                f"⚠️ <b>Exit Order Timeout</b>: Limit order for {leg_name.upper()} <code>{symbol}</code> did not fill within 10s. Order cancelled. Retrying next tick."
                            )
                        return False
            else:
                leg_state["exit_filled_qty"] = qty

            leg_state["status"] = "CLOSED"
            leg_state["exit_price"] = actual_exit_price
            leg_state["exit_reason"] = reason
            if order_id:
                leg_state["exit_order_id"] = order_id
            self.save_state(state)
            return True
        except Exception as e:
            logger.error("Failed to execute %s exit for %s (%s): %s", reason, leg_name.upper(), symbol, e)
            if self.notifier:
                self.notifier.send_message(
                    f"🚨 <b>Order Exit Failed</b>: Failed to exit {leg_name.upper()} <code>{symbol}</code> ({reason}): {e}"
                )
            return False

    @staticmethod
    def _get_current_day_pnl(state: Dict[str, Any], qty: int) -> float:
        """Computes current realized day P&L from closed strangle legs."""
        ce_pnl = 0.0
        pe_pnl = 0.0
        ce = state.get("ce", {})
        pe = state.get("pe", {})
        if ce and ce.get("status") == "CLOSED" and ce.get("exit_price") is not None:
            ce_pnl = (float(ce["entry_price"]) - float(ce["exit_price"])) * qty
        if pe and pe.get("status") == "CLOSED" and pe.get("exit_price") is not None:
            pe_pnl = (float(pe["entry_price"]) - float(pe["exit_price"])) * qty
        return ce_pnl + pe_pnl

    def wait_for_orders_completion(
        self,
        order_ids: List[str],
        timeout_seconds: int = 300,
        poll_seconds: int = 5,
        execute_once: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """
        Polls order status for multiple order IDs concurrently until all reach
        terminal status (COMPLETE, REJECTED, CANCELLED) or timeout_seconds elapses.
        """
        start_time = time.time()
        terminal_statuses = {"COMPLETE", "REJECTED", "CANCELLED"}
        results: Dict[str, Dict[str, Any]] = {}

        while True:
            all_terminal = True
            for oid in order_ids:
                if oid not in results or results[oid].get("status") not in terminal_statuses:
                    results[oid] = self.order_manager.get_order_status(oid)
                if results[oid].get("status") not in terminal_statuses:
                    all_terminal = False

            if all_terminal:
                return results

            elapsed = time.time() - start_time
            if elapsed >= timeout_seconds or execute_once:
                logger.warning(
                    "Order completion wait reached timeout (%d s). Non-terminal order statuses: %s",
                    timeout_seconds,
                    {oid: results[oid].get("status") for oid in order_ids if results[oid].get("status") not in terminal_statuses}
                )
                return results

            sleep_dur = min(poll_seconds, max(0.5, timeout_seconds - elapsed))
            time.sleep(sleep_dur)

    def wait_for_order_completion(
        self,
        order_id: str,
        timeout_seconds: int = 300,
        poll_seconds: int = 5,
        execute_once: bool = False
    ) -> Dict[str, Any]:
        """
        Convenience wrapper to wait for a single order's completion.
        """
        res = self.wait_for_orders_completion(
            [order_id],
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            execute_once=execute_once
        )
        return res.get(order_id, {})

    def _ensure_kite_session(
        self,
        kite: Optional[Any] = None,
        force_login: bool = False,
        check_market_hours: bool = False,
        ignore_holidays: bool = False,
    ) -> Any:
        """
        Validates that the provided KiteConnect client has a live, valid session.
        If the access token has expired or is invalidated, triggers automated TOTP re-authentication.
        """
        if kite is None:
            kite = getattr(self.order_manager, "kite", None)

        from kite.client import ensure_kite_session
        try:
            active_kite = ensure_kite_session(
                kite,
                force_login=force_login,
                check_market_hours=check_market_hours,
                ignore_holidays=ignore_holidays,
            )
            if active_kite and hasattr(self, "order_manager") and self.order_manager is not None:
                self.order_manager.kite = active_kite
            return active_kite or kite
        except Exception as e:
            logger.error("Failed to re-authenticate Kite session for Strangle Desk: %s", e)
            return kite

    def perform_daily_morning_login(self, kite: Optional[Any] = None) -> Any:
        """
        Ensures a fresh daily Zerodha Kite access token at/after 08:00 AM IST.
        When an existing Kite client is supplied (e.g. scheduler restart mid-day),
        validates / adopts any already-valid .env token first via ensure_kite_session()
        so peer strategy containers are not logged out. When called with kite=None,
        performs an explicit fresh TOTP login.
        """
        logger.info("Executing daily morning TOTP login (08:00 AM IST schedule)...")
        if kite is not None:
            active_kite = self._ensure_kite_session(kite, force_login=False)
            if active_kite is not None:
                try:
                    profile = active_kite.profile()
                    logger.info(
                        "Daily session check OK for %s (%s).",
                        profile.get("user_name", "Trader"),
                        profile.get("user_id"),
                    )
                    return active_kite
                except Exception as prof_err:
                    err_s = str(prof_err).lower()
                    if not is_token_error(prof_err) and ("429" in err_s or "too many" in err_s or "timeout" in err_s or "connection" in err_s):
                        logger.warning("Transient error during morning profile check (%s); preserving active client.", prof_err)
                        return active_kite

        from kite.client import get_kite_client
        new_kite = get_kite_client(force_login=True)
        if hasattr(self.order_manager, "kite"):
            self.order_manager.kite = new_kite

        try:
            profile = new_kite.profile()
            user_desc = f"{profile.get('user_name', 'Trader')} ({profile.get('user_id')})"
            logger.info("Daily morning TOTP login successful for %s.", user_desc)
        except Exception as e:
            logger.warning("Could not fetch profile after morning login: %s", e)

        return new_kite

    def reconcile_with_broker(
        self,
        kite: Optional[KiteConnect] = None,
        state: Optional[Dict[str, Any]] = None,
        target_expiry: Optional[date] = None,
        underlying: str = "NIFTY"
    ) -> Optional[Dict[str, Any]]:
        """
        Reconciles local trade state with actual open net positions on Zerodha Kite.
        - If state records an active strangle but both legs are closed on Kite,
          marks the session COMPLETED locally.
        - If state has an active strangle and one leg is closed, updates that leg to CLOSED.
        - If state is None (or IDLE) but open short NIFTY CE & PE options matching target weekly expiry
          exist on Kite, reconstructs and adopts them into state (including exact entry prices, product,
          +50% SL, and 80% decay targets).
        """
        if kite is None:
            kite = getattr(self.order_manager, "kite", None)
        if kite is None:
            return state

        if state is None:
            state = self.load_state()

        try:
            raw_pos = kite.positions()
            net_positions = raw_pos.get("net", []) if isinstance(raw_pos, dict) else []
            net_map = {
                p.get("tradingsymbol", ""): p
                for p in net_positions
                if p.get("exchange") == "NFO"
            }

            today_str = datetime.now().strftime("%Y-%m-%d")

            # 1. Existing open trade: check if broker has closed legs
            if state is not None and state.get("date") == today_str and state.get("status") == "OPEN":
                ce_sym = state.get("ce", {}).get("symbol")
                pe_sym = state.get("pe", {}).get("symbol")
                ce_qty = net_map.get(ce_sym, {}).get("quantity", 0)
                pe_qty = net_map.get(pe_sym, {}).get("quantity", 0)

                ce_closed = (ce_qty == 0)
                pe_closed = (pe_qty == 0)

                changed = False
                if ce_closed and state.get("ce", {}).get("status") == "OPEN":
                    ce_pos = net_map.get(ce_sym, {})
                    broker_buy_px = float(ce_pos.get("buy_price") or 0.0)
                    reconciled_ce_px = (
                        broker_buy_px
                        or float(ce_pos.get("last_price") or 0.0)
                        or float(state["ce"].get("last_ltp") or 0.0)
                        or float(state["ce"].get("entry_price") or 0.0)
                    )
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_reason"] = "BROKER_RECONCILED"
                    has_partial_exit = int(state["ce"].get("exit_filled_qty") or 0) > 0
                    if (state["ce"].get("exit_price") is None or (has_partial_exit and broker_buy_px > 0)) and reconciled_ce_px > 0:
                        state["ce"]["exit_price"] = reconciled_ce_px
                    changed = True
                    logger.info("Reconciliation: CE leg %s is flat on Kite. Marked CLOSED (@ %.2f).", ce_sym, state["ce"].get("exit_price") or 0.0)

                if pe_closed and state.get("pe", {}).get("status") == "OPEN":
                    pe_pos = net_map.get(pe_sym, {})
                    broker_buy_px = float(pe_pos.get("buy_price") or 0.0)
                    reconciled_pe_px = (
                        broker_buy_px
                        or float(pe_pos.get("last_price") or 0.0)
                        or float(state["pe"].get("last_ltp") or 0.0)
                        or float(state["pe"].get("entry_price") or 0.0)
                    )
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_reason"] = "BROKER_RECONCILED"
                    has_partial_exit = int(state["pe"].get("exit_filled_qty") or 0) > 0
                    if (state["pe"].get("exit_price") is None or (has_partial_exit and broker_buy_px > 0)) and reconciled_pe_px > 0:
                        state["pe"]["exit_price"] = reconciled_pe_px
                    changed = True
                    logger.info("Reconciliation: PE leg %s is flat on Kite. Marked CLOSED (@ %.2f).", pe_sym, state["pe"].get("exit_price") or 0.0)

                if ce_closed and pe_closed:
                    state["status"] = "COMPLETED"
                    qty = int(state.get("quantity") or 0)
                    if qty > 0:
                        state["realized_pnl"] = self._get_current_day_pnl(state, qty)
                    changed = True
                    logger.warning("Reconciliation: Both strangle legs are closed on Kite. Synchronizing local state to COMPLETED.")

                if changed:
                    self.save_state(state)
                return state

            # 2. No active trade locally: check if broker has open short strangle positions
            if state is None or state.get("date") != today_str or state.get("status") not in ("OPEN", "COMPLETED", "ABORTED", "SKIPPED_TREND_DAY"):
                # Short-circuit: If broker has no short NFO positions at all, avoid fetching instruments
                has_candidate_positions = any(
                    p.get("exchange") == "NFO" and int(p.get("quantity", 0)) < 0
                    for p in net_positions
                )
                if not has_candidate_positions:
                    return state

                # Fetch instruments to identify target expiry (check 0-DTE too if an open expiry position exists today)
                instruments = kite.instruments("NFO")
                if target_expiry is not None:
                    candidate_expiries = [target_expiry]
                else:
                    default_exp = resolve_target_expiry(instruments, underlying=underlying)
                    today_exp = datetime.now().date()
                    candidate_expiries = [today_exp, default_exp] if today_exp != default_exp else [default_exp]

                adopted = False
                for scan_expiry in candidate_expiries:
                    inst_map = {
                        i["tradingsymbol"]: i
                        for i in instruments
                        if i.get("name") == underlying
                        and _normalize_date(i.get("expiry")) == scan_expiry
                    }

                    open_ce = []
                    open_pe = []

                    for p in net_positions:
                        sym = p.get("tradingsymbol", "")
                        qty = p.get("quantity", 0)
                        if p.get("exchange") == "NFO" and sym in inst_map and qty < 0:
                            inst = inst_map[sym]
                            itype = inst.get("instrument_type")
                            leg_info = {
                                "symbol": sym,
                                "strike": float(inst.get("strike", 0.0)),
                                "quantity": abs(qty),
                                "product": p.get("product", "MIS"),
                                "entry_price": float(p.get("average_price") or p.get("sell_price") or 0.0),
                                "lot_size": int(inst.get("lot_size", 65))
                            }
                            if itype == "CE":
                                open_ce.append(leg_info)
                            elif itype == "PE":
                                open_pe.append(leg_info)

                    if open_ce and open_pe:
                        if len(open_ce) > 1 or len(open_pe) > 1 or open_ce[0]["quantity"] != open_pe[0]["quantity"]:
                            logger.warning(
                                "Reconciliation: Skipping non-strangle/mismatched short positions for expiry %s "
                                "(CE legs=%d qty=%s, PE legs=%d qty=%s).",
                                scan_expiry, len(open_ce), [x["quantity"] for x in open_ce],
                                len(open_pe), [x["quantity"] for x in open_pe]
                            )
                            continue

                        ce_leg = open_ce[0]
                        pe_leg = open_pe[0]
                        qty = ce_leg["quantity"]
                        lot_size = ce_leg["lot_size"]
                        total_lots = qty // lot_size

                        ce_entry = ce_leg["entry_price"]
                        pe_entry = pe_leg["entry_price"]

                        ce_sl = round(ce_entry * 1.50, 2)
                        ce_tgt = round(ce_entry * 0.20, 2)
                        pe_sl = round(pe_entry * 1.50, 2)
                        pe_tgt = round(pe_entry * 0.20, 2)

                        logger.info(
                            "Reconciliation: Found open broker strangle positions on Kite (%s): "
                            "CE %s x %d @ Rs %.2f [%s] (SL: %.2f, Tgt: %.2f), "
                            "PE %s x %d @ Rs %.2f [%s] (SL: %.2f, Tgt: %.2f). Adopting into active state.",
                            scan_expiry,
                            ce_leg["symbol"], qty, ce_entry, ce_leg["product"], ce_sl, ce_tgt,
                            pe_leg["symbol"], qty, pe_entry, pe_leg["product"], pe_sl, pe_tgt
                        )

                        state = {
                            "status": "OPEN",
                            "mode": getattr(self.order_manager, "mode", "LIVE"),
                            "date": today_str,
                            "underlying": underlying,
                            "expiry": str(scan_expiry),
                            "lots": total_lots,
                            "lot_size": lot_size,
                            "quantity": qty,
                            "product": ce_leg["product"],
                            "ce": {
                                "symbol": ce_leg["symbol"],
                                "strike": ce_leg["strike"],
                                "entry_price": ce_entry,
                                "sl_price": ce_sl,
                                "target_price": ce_tgt,
                                "status": "OPEN",
                                "product": ce_leg["product"],
                                "order_id": None,
                                "exit_price": None,
                                "exit_reason": None
                            },
                            "pe": {
                                "symbol": pe_leg["symbol"],
                                "strike": pe_leg["strike"],
                                "entry_price": pe_entry,
                                "sl_price": pe_sl,
                                "target_price": pe_tgt,
                                "status": "OPEN",
                                "product": pe_leg["product"],
                                "order_id": None,
                                "exit_price": None,
                                "exit_reason": None
                            }
                        }
                        self.save_state(state)
                        adopted = True
                        break

            return state
        except Exception as e:
            err_str = str(e).lower()
            if "too many requests" in err_str or "429" in err_str:
                logger.warning("Broker position reconciliation rate-limited by Kite (Too many requests). Backing off.")
            else:
                logger.warning("Broker position reconciliation failed: %s", e)
            return state

    def check_available_equity_margin(self) -> Dict[str, Any]:
        """
        Queries available equity / F&O margin from Kite Connect.
        Returns status ('OK', 'DISABLED', 'ERROR', or 'UNKNOWN') and available cash.
        """
        kite = getattr(self.order_manager, "kite", None)
        if not kite:
            return {"status": "UNKNOWN", "available_cash": 0.0}
        try:
            margins = kite.margins("equity")
            if not isinstance(margins, dict):
                return {"status": "UNKNOWN", "available_cash": 0.0}
            available = float(
                margins.get("available", {}).get("live_balance", 0.0)
                or margins.get("available", {}).get("cash", 0.0)
                or margins.get("net", 0.0)
                or 0.0
            )
            enabled = bool(margins.get("enabled", True))
            return {
                "status": "OK" if enabled else "DISABLED",
                "available_cash": available,
                "raw": margins
            }
        except Exception as e:
            logger.warning("Failed to query equity margins from Kite: %s", e)
            return {"status": "ERROR", "error": str(e), "available_cash": 0.0}

    def check_morning_trend_filter(
        self,
        kite: Any,
        underlying: str = "NIFTY",
        threshold_pct: float = 0.60,
        enforce_quote_window: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Inspects 09:15 to 10:00 morning price action for underlying.
        Returns (is_trend_day, details_dict).
        If (high - low) / open >= threshold_pct, triggers trend day filter to skip session.
        """
        token = 256265 if underlying == "NIFTY" else (260105 if underlying == "BANKNIFTY" else None)
        now_dt = datetime.now()
        today = now_dt.date()
        from_dt = datetime.combine(today, dt_time(9, 15))
        to_dt = datetime.combine(today, dt_time(10, 0))

        try:
            if token and hasattr(kite, "historical_data"):
                candles = kite.historical_data(
                    token,
                    from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    interval="5minute"
                )
                if candles:
                    parsed_candles = []
                    for c in candles:
                        c_dt = c.get("date")
                        if isinstance(c_dt, str):
                            try:
                                c_dt = datetime.fromisoformat(c_dt)
                            except Exception:
                                c_dt = None
                        parsed_candles.append((c_dt, c))
                    valid_candles = [c for c_dt, c in parsed_candles if isinstance(c_dt, datetime) and c_dt.time() < dt_time(10, 0)]
                    if not valid_candles:
                        valid_candles = [c for c_dt, c in parsed_candles if isinstance(c_dt, datetime) and c_dt.time() <= dt_time(10, 5)]
                    if not valid_candles:
                        valid_candles = candles

                    open_price = float(valid_candles[0]["open"])
                    high_price = max(float(c["high"]) for c in valid_candles)
                    low_price = min(float(c["low"]) for c in valid_candles)
                    range_pts = high_price - low_price
                    range_pct = (range_pts / open_price) * 100.0 if open_price > 0 else 0.0
                    is_triggered = range_pct >= threshold_pct
                    details = {
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "range_pts": round(range_pts, 2),
                        "range_pct": round(range_pct, 2),
                        "threshold_pct": threshold_pct,
                        "triggered": is_triggered,
                        "source": "historical_5m"
                    }
                    logger.info(
                        "Morning Trend Filter Check: Open=%.1f, High=%.1f, Low=%.1f => Range=%.1f pts (%.2f%%) vs Threshold=%.2f%% (Triggered: %s)",
                        open_price, high_price, low_price, range_pts, range_pct, threshold_pct, is_triggered
                    )
                    return is_triggered, details
        except Exception as e:
            if is_token_error(e):
                raise
            logger.warning("Failed to fetch 5m candles for morning filter (%s); checking quote OHLC...", e)

        # Fallback to quote/OHLC only near the morning entry window when enforce_quote_window=True
        try:
            sym = "NSE:NIFTY 50" if underlying == "NIFTY" else ("NSE:NIFTY BANK" if underlying == "BANKNIFTY" else f"NSE:{underlying}")
            if hasattr(kite, "quote") and (not enforce_quote_window or now_dt.time() <= dt_time(10, 15)):
                q_data = kite.quote([sym]).get(sym, {})
                ohlc = q_data.get("ohlc", {})
                open_price = float(ohlc.get("open") or 0.0)
                high_price = float(ohlc.get("high") or 0.0)
                low_price = float(ohlc.get("low") or 0.0)
                if open_price > 0.0 and high_price > 0.0 and low_price > 0.0:
                    range_pts = high_price - low_price
                    range_pct = (range_pts / open_price) * 100.0
                    is_triggered = range_pct >= threshold_pct
                    details = {
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "range_pts": round(range_pts, 2),
                        "range_pct": round(range_pct, 2),
                        "threshold_pct": threshold_pct,
                        "triggered": is_triggered,
                        "source": "quote_ohlc"
                    }
                    logger.info(
                        "Morning Trend Filter (Quote OHLC): Open=%.1f, High=%.1f, Low=%.1f => Range=%.1f pts (%.2f%%) vs Threshold=%.2f%% (Triggered: %s)",
                        open_price, high_price, low_price, range_pts, range_pct, threshold_pct, is_triggered
                    )
                    return is_triggered, details
        except Exception as e:
            if is_token_error(e):
                raise
            logger.warning("Quote OHLC lookup failed for morning filter: %s", e)

        return False, {"triggered": False, "reason": "No morning OHLC available"}

    def run_session(
        self,
        underlying: str = "NIFTY",
        lots: int = 10,
        poll_seconds: int = 5,
        dry_run: bool = False,
        execute_once: bool = False,
        kite: Optional[KiteConnect] = None,
        strategy_max_loss: Optional[float] = 10000.0,
        use_leg_sl: bool = False,
        trail_to_cost: bool = False,
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
        ignore_holidays: bool = False,
        order_timeout: int = 300,
        check_market_hours: bool = False,
        morning_filter: bool = True,
        morning_filter_pct: float = 0.60,
        strike_mode: str = "ATR",
        atr_multiplier: float = 1.25,
        target_price: float = 30.0,
        exit_time_str: str = "14:45",
    ) -> Optional[Dict[str, Any]]:
        """
        Executes and monitors the 10:00 AM Short Strangle intraday session.
        Handles holiday skip, morning trend filter skip (>0.60% range), dynamic ATR strike discovery,
        dual-leg entry, live quote polling, single-leg stop loss (50% SL / ₹10k exit on losing leg only),
        breakeven trailing on winning leg, decay target, and 2:45 PM square-off.
        """
        if not ignore_holidays:
            is_holiday, reason = is_trading_holiday()
            if is_holiday:
                logger.info("Exchange is closed today (%s). Skipping session.", reason)
                # Exchange closed notification removed per policy
                return {"status": "HOLIDAY", "reason": reason}

        # Market Hours & Cutoff Validation (prevent placing regular orders outside NSE trading hours / after cutoff)
        mode = getattr(self.order_manager, "mode", "PAPER")
        now_time = datetime.now().time()
        exit_h, exit_m = map(int, exit_time_str.split(":"))
        cutoff_time = dt_time(exit_h, exit_m)
        if mode == "LIVE" and not dry_run and not ignore_holidays and check_market_hours:
            if now_time < dt_time(9, 15) or now_time > dt_time(15, 30):
                err_msg = (
                    f"Cannot execute LIVE strangle orders: NSE market is closed. "
                    f"Current time: {now_time.strftime('%H:%M:%S')} IST. Regular trading hours are 09:15 - 15:30 IST."
                )
                logger.warning(err_msg)
                # Market closed warning removed per policy
                return {"status": "MARKET_CLOSED", "reason": err_msg}

        state = self.load_state()
        today_str = datetime.now().strftime("%Y-%m-%d")

        if state is not None:
            if state.get("mode") and state.get("mode") != mode:
                logger.warning(
                    "Loaded state belongs to %s mode (%s != %s). Ignoring mismatched state file.",
                    state.get("mode"), state.get("mode"), mode
                )
                state = None
            elif state.get("date") != today_str:
                logger.info("Existing state from prior date (%s != %s). Initializing fresh session.", state.get("date"), today_str)
                self.clear_state()
                state = None
            elif state.get("status") in ("COMPLETED", "ABORTED", "SKIPPED_TREND_DAY"):
                logger.info("Session for today (%s) is already %s.", today_str, state.get("status"))
                return state

        if state is None and check_market_hours and not dry_run and now_time >= cutoff_time:
            err_msg = (
                f"Cannot enter new strangle session at {now_time.strftime('%H:%M:%S')} IST "
                f"(at or after configured cutoff {exit_time_str} IST)."
            )
            logger.warning(err_msg)
            return {"status": "AFTER_CUTOFF", "reason": err_msg}

        if kite is None:
            kite = getattr(self.order_manager, "kite", None)

        if mode == "LIVE" and not dry_run and kite is not None:
            kite = self._ensure_kite_session(kite)
            state = self.reconcile_with_broker(kite, state, underlying=underlying)

        if state is None:
            if kite is None:
                raise ValueError("KiteConnect instance required to discover strikes and fetch live quotes.")

            kite = self._ensure_kite_session(kite)

            # Evaluate Morning Range Trend Filter (skips violent morning moves >= 0.60%)
            if morning_filter and not dry_run:
                filter_kwargs: Dict[str, Any] = {"underlying": underlying, "threshold_pct": morning_filter_pct}
                if check_market_hours:
                    filter_kwargs["enforce_quote_window"] = True
                is_trend_day, filter_details = self.check_morning_trend_filter(
                    kite, **filter_kwargs
                )
                if is_trend_day:
                    err_msg = (
                        f"Morning Trend Filter triggered: NIFTY 09:15-10:00 range was "
                        f"{filter_details.get('range_pts')} pts ({filter_details.get('range_pct')}%) "
                        f">= threshold {morning_filter_pct}%. Skipping session today to preserve capital."
                    )
                    logger.warning(err_msg)
                    if self.notifier:
                        self.notifier.send_message(
                            f"⚠️ <b>Morning Trend Filter Triggered</b>: NIFTY 09:15–10:00 range was "
                            f"<b>{filter_details.get('range_pts')} pts ({filter_details.get('range_pct')}%)</b> "
                            f">= threshold {morning_filter_pct}%. Strangle entry skipped today to avoid trending momentum."
                        )
                    skipped_state = {
                        "status": "SKIPPED_TREND_DAY",
                        "reason": err_msg,
                        "date": today_str,
                        "underlying": underlying,
                        "filter_details": filter_details
                    }
                    self.save_state(skipped_state)
                    return skipped_state

            logger.info("Discovering strikes (Mode: %s, ATR Mult: %.2fx, Target Price: Rs %.1f)...", strike_mode, atr_multiplier, target_price)
            try:
                strikes = find_strangle_strikes(
                    kite,
                    underlying=underlying,
                    target_price=target_price,
                    strike_mode=strike_mode,
                    atr_multiplier=atr_multiplier,
                    lots=lots
                )
            except Exception as e:
                if is_token_error(e):
                    logger.warning("Token error during strike discovery (%s). Refreshing token and retrying...", e)
                    kite = self._ensure_kite_session(kite, force_login=True)
                    strikes = find_strangle_strikes(
                        kite,
                        underlying=underlying,
                        target_price=target_price,
                        strike_mode=strike_mode,
                        atr_multiplier=atr_multiplier,
                        lots=lots
                    )
                else:
                    raise

            mode = getattr(self.order_manager, "mode", "PAPER")
            self._print_plan(strikes, mode=mode, exit_time_str=exit_time_str)

            if dry_run:
                logger.info("Dry-run complete. Strike plan displayed above. No orders submitted.")
                return {"status": "DRY_RUN", "strikes": strikes}

            ce_info = strikes["ce"]
            pe_info = strikes["pe"]
            total_qty = strikes["total_quantity"]

            # Place CE and PE entry orders concurrently to eliminate execution latency & unhedged delta
            from concurrent.futures import ThreadPoolExecutor

            def _place_leg_order(leg_sym: str, leg_ltp: float) -> Dict[str, Any]:
                return self.order_manager.sell(
                    leg_sym, total_qty, leg_ltp, order_type=order_type, buffer_pct=limit_buffer_pct
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                future_ce = executor.submit(_place_leg_order, ce_info["symbol"], ce_info["ltp"])
                future_pe = executor.submit(_place_leg_order, pe_info["symbol"], pe_info["ltp"])

            ce_order = None
            pe_order = None
            ce_err = None
            pe_err = None

            try:
                ce_order = future_ce.result()
            except Exception as e:
                ce_err = e

            try:
                pe_order = future_pe.result()
            except Exception as e:
                pe_err = e

            if getattr(self.order_manager, "kite", None) is not None:
                kite = self.order_manager.kite

            # Handle failures
            if ce_err and pe_err:
                err_msg = f"Failed to place CE entry order ({ce_info['symbol']}): {ce_err} (PE also failed: {pe_err})"
                logger.error(err_msg)
                if self.notifier:
                    import html
                    self.notifier.send_message(f"🚨 <b>Strangle Desk Error</b>: {html.escape(err_msg)}")
                aborted_state = {
                    "status": "ABORTED",
                    "reason": err_msg,
                    "date": today_str,
                    "underlying": underlying,
                    "expiry": str(strikes.get("expiry")) if isinstance(strikes, dict) else None,
                }
                self.save_state(aborted_state)
                return aborted_state

            if ce_err and not pe_err:
                err_msg = f"Failed to place CE entry order ({ce_info['symbol']}): {ce_err}. Squaring off PE leg..."
                logger.error(err_msg)
                if self.notifier:
                    import html
                    self.notifier.send_message(f"🚨 <b>Strangle Desk Error</b>: {html.escape(err_msg)}")
                pe_id = pe_order.get("order_id") if isinstance(pe_order, dict) else None
                if pe_id:
                    try:
                        self.order_manager.cancel_order(pe_id)
                        pe_status = self.wait_for_order_completion(pe_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                        pe_filled = int(pe_status.get("filled_quantity") or 0)
                        if pe_filled > 0:
                            logger.warning("PE leg was partially/fully filled (%d qty). Buying back to close exposure...", pe_filled)
                            leg_prod = getattr(self.order_manager, "product", "MIS")
                            rb_res = self.order_manager.buy_to_cover(
                                symbol=pe_info["symbol"],
                                quantity=pe_filled,
                                price=pe_info["ltp"],
                                reason="ABORT_CLEANUP",
                                order_type=order_type,
                                buffer_pct=limit_buffer_pct,
                                product=leg_prod,
                            )
                            rb_id = rb_res.get("order_id") if isinstance(rb_res, dict) else None
                            if mode == "LIVE" and isinstance(rb_id, str):
                                rb_st = self.wait_for_order_completion(rb_id, timeout_seconds=10, poll_seconds=1)
                                if rb_st.get("status") != "COMPLETE" and order_type.upper() != "MARKET":
                                    self.order_manager.cancel_order(rb_id)
                                    rb_post = self.wait_for_order_completion(rb_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                                    rem_rb = max(0, pe_filled - int(rb_post.get("filled_quantity") or 0))
                                    if rem_rb > 0:
                                        self.order_manager.buy_to_cover(
                                            symbol=pe_info["symbol"],
                                            quantity=rem_rb,
                                            price=pe_info["ltp"],
                                            reason="ABORT_CLEANUP",
                                            order_type="MARKET",
                                            product=leg_prod,
                                        )
                    except Exception as clean_err:
                        logger.error("Failed to clean up PE leg after CE entry failure: %s", clean_err)
                aborted_state = {
                    "status": "ABORTED",
                    "reason": err_msg,
                    "date": today_str,
                    "underlying": underlying,
                    "expiry": str(strikes.get("expiry")) if isinstance(strikes, dict) else None,
                }
                self.save_state(aborted_state)
                return aborted_state

            if pe_err and not ce_err:
                err_msg = f"Failed to place PE entry order ({pe_info['symbol']}) after CE placed: {pe_err}. Squaring off CE leg..."
                logger.error(err_msg)
                if self.notifier:
                    import html
                    self.notifier.send_message(f"🚨 <b>Strangle Desk Error</b>: {html.escape(err_msg)}")
                ce_id = ce_order.get("order_id") if isinstance(ce_order, dict) else None
                if ce_id:
                    try:
                        self.order_manager.cancel_order(ce_id)
                        ce_status = self.wait_for_order_completion(ce_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                        ce_filled = int(ce_status.get("filled_quantity") or 0)
                        if ce_filled > 0:
                            logger.warning("CE leg was partially/fully filled (%d qty). Buying back to close exposure...", ce_filled)
                            leg_prod = getattr(self.order_manager, "product", "MIS")
                            rb_res = self.order_manager.buy_to_cover(
                                symbol=ce_info["symbol"],
                                quantity=ce_filled,
                                price=ce_info["ltp"],
                                reason="ABORT_CLEANUP",
                                order_type=order_type,
                                buffer_pct=limit_buffer_pct,
                                product=leg_prod,
                            )
                            rb_id = rb_res.get("order_id") if isinstance(rb_res, dict) else None
                            if mode == "LIVE" and isinstance(rb_id, str):
                                rb_st = self.wait_for_order_completion(rb_id, timeout_seconds=10, poll_seconds=1)
                                if rb_st.get("status") != "COMPLETE" and order_type.upper() != "MARKET":
                                    self.order_manager.cancel_order(rb_id)
                                    rb_post = self.wait_for_order_completion(rb_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                                    rem_rb = max(0, ce_filled - int(rb_post.get("filled_quantity") or 0))
                                    if rem_rb > 0:
                                        self.order_manager.buy_to_cover(
                                            symbol=ce_info["symbol"],
                                            quantity=rem_rb,
                                            price=ce_info["ltp"],
                                            reason="ABORT_CLEANUP",
                                            order_type="MARKET",
                                            product=leg_prod,
                                        )
                    except Exception as clean_err:
                        logger.error("Failed to clean up CE leg after PE entry failure: %s", clean_err)
                aborted_state = {
                    "status": "ABORTED",
                    "reason": err_msg,
                    "date": today_str,
                    "underlying": underlying,
                    "expiry": str(strikes.get("expiry")) if isinstance(strikes, dict) else None,
                }
                self.save_state(aborted_state)
                return aborted_state

            ce_id = ce_order.get("order_id")
            pe_id = pe_order.get("order_id")
            logger.info("Placed entry orders (CE: %s, PE: %s). Waiting up to %d s for execution...", ce_id, pe_id, order_timeout)

            statuses = self.wait_for_orders_completion(
                [ce_id, pe_id],
                timeout_seconds=order_timeout,
                poll_seconds=poll_seconds,
                execute_once=execute_once
            )
            ce_status = statuses.get(ce_id, {})
            pe_status = statuses.get(pe_id, {})

            ce_filled = int(ce_status.get("filled_quantity") or 0)
            pe_filled = int(pe_status.get("filled_quantity") or 0)
            ce_done = (ce_status.get("status") == "COMPLETE") and (ce_filled == total_qty)
            pe_done = (pe_status.get("status") == "COMPLETE") and (pe_filled == total_qty)

            if not (ce_done and pe_done):
                logger.warning(
                    "Entry orders failed to fully complete within %d s (CE: status=%s, filled=%d/%d; PE: status=%s, filled=%d/%d). Aborting session.",
                    order_timeout,
                    ce_status.get("status"),
                    ce_filled,
                    total_qty,
                    pe_status.get("status"),
                    pe_filled,
                    total_qty
                )
                # 1. Cancel remaining open/pending order quantity on exchange and re-read settled fill count
                if ce_status.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED") or ce_status.get("pending_quantity", 0) > 0:
                    self.order_manager.cancel_order(ce_id)
                    ce_post = self.wait_for_order_completion(ce_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                    ce_filled = max(ce_filled, int(ce_post.get("filled_quantity") or 0))
                if pe_status.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED") or pe_status.get("pending_quantity", 0) > 0:
                    self.order_manager.cancel_order(pe_id)
                    pe_post = self.wait_for_order_completion(pe_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                    pe_filled = max(pe_filled, int(pe_post.get("filled_quantity") or 0))

                # 2. Fetch latest quotes to exit any filled quantities
                exit_quotes: Dict[str, Any] = {}
                if kite is not None:
                    try:
                        exit_quotes = kite.ltp([
                            f"NFO:{ce_info['symbol']}" if ":" not in ce_info['symbol'] else ce_info['symbol'],
                            f"NFO:{pe_info['symbol']}" if ":" not in pe_info['symbol'] else pe_info['symbol']
                        ])
                    except Exception as e:
                        logger.warning("Could not fetch latest quotes for abort square-off: %s", e)

                ce_exit_ltp = self._extract_ltp(exit_quotes, ce_info['symbol'], default=ce_info['ltp'])
                pe_exit_ltp = self._extract_ltp(exit_quotes, pe_info['symbol'], default=pe_info['ltp'])

                # 3. Square off any filled positions immediately (and verify completion / escalate in LIVE)
                for leg_sym, leg_filled, leg_exit_ltp, leg_side_lbl in (
                    (ce_info["symbol"], ce_filled, ce_exit_ltp, "CE"),
                    (pe_info["symbol"], pe_filled, pe_exit_ltp, "PE"),
                ):
                    if leg_filled <= 0:
                        continue
                    abort_reason = "ABORT_PARTIAL_FILL" if leg_filled < total_qty else "ABORT_UNMATCHED_LEG"
                    logger.warning("Exiting %d filled shares of %s %s (%s)", leg_filled, leg_side_lbl, leg_sym, abort_reason)
                    rb_res = self.order_manager.buy_to_cover(
                        leg_sym, leg_filled, leg_exit_ltp, reason=abort_reason, order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    rb_id = rb_res.get("order_id") if isinstance(rb_res, dict) else None
                    if mode == "LIVE" and isinstance(rb_id, str):
                        rb_st = self.wait_for_order_completion(rb_id, timeout_seconds=10, poll_seconds=1)
                        if rb_st.get("status") != "COMPLETE" and order_type.upper() != "MARKET":
                            self.order_manager.cancel_order(rb_id)
                            rb_post = self.wait_for_order_completion(rb_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                            rem_rb = max(0, leg_filled - int(rb_post.get("filled_quantity") or 0))
                            if rem_rb > 0:
                                self.order_manager.buy_to_cover(
                                    leg_sym, rem_rb, leg_exit_ltp, reason=abort_reason, order_type="MARKET"
                                )

                # 4. Telegram alert
                if self.notifier:
                    abort_msg = (
                        f"⚠️ <b>Entry Order Abort / Timeout [{mode}]</b>\n"
                        f"Entry orders failed to complete within {order_timeout}s.\n"
                        f"• <b>CALL ({ce_info['symbol']})</b>: Status=<code>{ce_status.get('status')}</code>, Filled={ce_filled}/{total_qty}\n"
                        f"• <b>PUT ({pe_info['symbol']})</b>: Status=<code>{pe_status.get('status')}</code>, Filled={pe_filled}/{total_qty}\n"
                        f"All pending orders cancelled. Any filled shares squared off immediately."
                    )
                    self.notifier.send_message(abort_msg)

                aborted_state = {
                    "status": "ABORTED",
                    "reason": "ENTRY_ORDER_TIMEOUT",
                    "date": today_str,
                    "underlying": underlying,
                    "expiry": str(strikes["expiry"]),
                    "ce": {
                        "symbol": ce_info["symbol"],
                        "order_id": ce_id,
                        "status": ce_status.get("status"),
                        "filled_quantity": ce_filled,
                        "total_quantity": total_qty
                    },
                    "pe": {
                        "symbol": pe_info["symbol"],
                        "order_id": pe_id,
                        "status": pe_status.get("status"),
                        "filled_quantity": pe_filled,
                        "total_quantity": total_qty
                    }
                }
                self.save_state(aborted_state)
                return aborted_state

            # Both legs fully completed
            ce_exec_price = float(ce_status.get("average_price") or ce_info["ltp"])
            pe_exec_price = float(pe_status.get("average_price") or pe_info["ltp"])
            if ce_exec_price <= 0:
                ce_exec_price = ce_info["ltp"]
            if pe_exec_price <= 0:
                pe_exec_price = pe_info["ltp"]

            ce_sl = round(ce_exec_price * 1.50, 2)
            ce_tgt = round(ce_exec_price * 0.20, 2)
            pe_sl = round(pe_exec_price * 1.50, 2)
            pe_tgt = round(pe_exec_price * 0.20, 2)

            if self.notifier:
                risk_cap_str = f"Max Rs {strategy_max_loss:,.0f} portfolio loss" if strategy_max_loss is not None else "None"
                exit_rule_desc = (
                    f"Single-Leg 50% SL (winner stays open & trails to cost) | Target: 80% decay | Cutoff: {exit_time_str} IST"
                    if use_leg_sl
                    else f"Portfolio SL: {risk_cap_str} | Target: 80% decay (both exit) | Cutoff: {exit_time_str} IST"
                )
                entry_msg = (
                    f"🚀 <b>NIFTY 10:00 AM Strangle Opened [{mode}]</b>\n"
                    f"Expiry: <code>{strikes['expiry']}</code> | Qty: {total_qty} ({strikes['total_lots']} lots)\n"
                    f"Order Type: <code>{order_type}</code> (Buffer: {limit_buffer_pct*100:.0f}%)\n"
                    f"• <b>CALL</b>: <code>{ce_info['symbol']}</code> @ Rs {ce_exec_price:.2f} | Target: Rs {ce_tgt:.2f}\n"
                    f"• <b>PUT</b>: <code>{pe_info['symbol']}</code> @ Rs {pe_exec_price:.2f} | Target: Rs {pe_tgt:.2f}\n"
                    f"• <b>Exit Rules</b>: {exit_rule_desc}"
                )
                self.notifier.send_message(entry_msg)

            state = {
                "status": "OPEN",
                "mode": mode,
                "date": today_str,
                "underlying": underlying,
                "expiry": str(strikes["expiry"]),
                "lots": strikes["total_lots"],
                "lot_size": strikes.get("lot_size", 65),
                "quantity": total_qty,
                "ce": {
                    "symbol": ce_info["symbol"],
                    "strike": ce_info["strike"],
                    "entry_price": ce_exec_price,
                    "sl_price": ce_sl,
                    "target_price": ce_tgt,
                    "status": "OPEN",
                    "order_id": ce_id,
                    **({"order_ids": ce_order["order_ids"]} if isinstance(ce_order, dict) and ce_order.get("order_ids") else {}),
                    "exit_price": None,
                    "exit_reason": None
                },
                "pe": {
                    "symbol": pe_info["symbol"],
                    "strike": pe_info["strike"],
                    "entry_price": pe_exec_price,
                    "sl_price": pe_sl,
                    "target_price": pe_tgt,
                    "status": "OPEN",
                    "order_id": pe_id,
                    **({"order_ids": pe_order["order_ids"]} if isinstance(pe_order, dict) and pe_order.get("order_ids") else {}),
                    "exit_price": None,
                    "exit_reason": None
                }
            }
            self.save_state(state)

        # Monitoring Loop
        if kite is None:
            kite = getattr(self.order_manager, "kite", None)

        exit_h, exit_m = map(int, exit_time_str.split(":"))
        cutoff_time = dt_time(exit_h, exit_m)
        last_intraday_reconcile_ts = time.time()

        ce_sym = state["ce"]["symbol"]
        pe_sym = state["pe"]["symbol"]
        qty = int(state.get("quantity") or (lots * int(state.get("lot_size") or 65)))

        while state.get("status") == "OPEN":
            # Periodic intraday broker reconciliation (LIVE) so manual Kite exits aren't re-entered
            if mode == "LIVE" and kite is not None and not execute_once and (time.time() - last_intraday_reconcile_ts) >= 60.0:
                reconciled = self.reconcile_with_broker(kite, state, underlying=underlying)
                if isinstance(reconciled, dict):
                    state = reconciled
                last_intraday_reconcile_ts = time.time()
                if state.get("status") != "OPEN":
                    break

            # Current time check: configured intraday cutoff (default 14:45 IST)
            if datetime.now().time() >= cutoff_time:
                logger.info("Intraday %s cutoff reached. Closing all open strangle legs.", exit_time_str)
                quotes: Dict[str, Any] = {}
                if kite is not None:
                    try:
                        quotes = kite.ltp([f"NFO:{ce_sym}" if ":" not in ce_sym else ce_sym,
                                           f"NFO:{pe_sym}" if ":" not in pe_sym else pe_sym])
                    except Exception as e:
                        logger.warning("Could not fetch final quotes at cutoff: %s", e)

                ce_fallback = state["ce"].get("last_ltp") or state["ce"]["entry_price"]
                pe_fallback = state["pe"].get("last_ltp") or state["pe"]["entry_price"]
                ce_ltp = self._extract_ltp(quotes, ce_sym, default=ce_fallback)
                pe_ltp = self._extract_ltp(quotes, pe_sym, default=pe_fallback)

                if state["ce"].get("status") == "OPEN":
                    self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "TIME_EXIT", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True)

                if state["pe"].get("status") == "OPEN":
                    self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "TIME_EXIT", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True)

                if state["ce"].get("status") == "CLOSED" and state["pe"].get("status") == "CLOSED":
                    total_day_pnl = self._get_current_day_pnl(state, qty)
                    state["status"] = "COMPLETED"
                    state["realized_pnl"] = total_day_pnl
                    self.save_state(state)
                    if self.notifier:
                        ce_fin = float(state["ce"]["exit_price"]) if state["ce"].get("exit_price") is not None else ce_ltp
                        pe_fin = float(state["pe"]["exit_price"]) if state["pe"].get("exit_price") is not None else pe_ltp
                        self.notifier.send_message(
                            f"🏁 <b>{exit_time_str} Cutoff Exit</b>: Closed open legs (CE @ ₹{ce_fin:.2f}, PE @ ₹{pe_fin:.2f})\n\n"
                            f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
                        )
                    break

                if execute_once:
                    break
                time.sleep(poll_seconds)
                continue

            # Fetch live quotes
            if kite is None:
                raise ValueError("KiteConnect instance required to poll live quotes.")

            try:
                symbols_to_query = [f"NFO:{ce_sym}" if ":" not in ce_sym else ce_sym,
                                    f"NFO:{pe_sym}" if ":" not in pe_sym else pe_sym]
                quotes = kite.ltp(symbols_to_query)
            except Exception as e:
                if is_token_error(e):
                    logger.warning("Token error during live quote polling (%s). Refreshing session...", e)
                    try:
                        kite = self._ensure_kite_session(kite, force_login=True)
                        quotes = kite.ltp(symbols_to_query)
                    except Exception as retry_err:
                        logger.error("Failed to fetch live quotes after re-authentication: %s", retry_err)
                        if execute_once:
                            break
                        time.sleep(poll_seconds)
                        continue
                else:
                    logger.error("Failed to fetch live quotes: %s", e)
                    if execute_once:
                        break
                    time.sleep(poll_seconds)
                    continue

            ce_ltp = self._extract_ltp(quotes, ce_sym, default=state["ce"].get("last_ltp") or state["ce"]["entry_price"])
            pe_ltp = self._extract_ltp(quotes, pe_sym, default=state["pe"].get("last_ltp") or state["pe"]["entry_price"])
            state["ce"]["last_ltp"] = ce_ltp
            state["pe"]["last_ltp"] = pe_ltp

            ce_status = state["ce"].get("status")
            pe_status = state["pe"].get("status")
            ce_entry = float(state["ce"]["entry_price"])
            pe_entry = float(state["pe"]["entry_price"])
            ce_done_qty = int(state["ce"].get("exit_filled_qty") or 0)
            pe_done_qty = int(state["pe"].get("exit_filled_qty") or 0)
            ce_exit_val = float(state["ce"]["exit_price"]) if state["ce"].get("exit_price") is not None else ce_ltp
            pe_exit_val = float(state["pe"]["exit_price"]) if state["pe"].get("exit_price") is not None else pe_ltp
            ce_pnl = (
                ((ce_entry - ce_exit_val) * ce_done_qty + (ce_entry - ce_ltp) * max(0, qty - ce_done_qty))
                if ce_status == "OPEN"
                else (ce_entry - ce_exit_val) * qty
            )
            pe_pnl = (
                ((pe_entry - pe_exit_val) * pe_done_qty + (pe_entry - pe_ltp) * max(0, qty - pe_done_qty))
                if pe_status == "OPEN"
                else (pe_entry - pe_exit_val) * qty
            )
            total_strategy_pnl = ce_pnl + pe_pnl
            logger.info(
                "Tick | CE (%s): LTP=%.2f, P&L=Rs %.2f [%s] | PE (%s): LTP=%.2f, P&L=Rs %.2f [%s] | Total Strangle P&L=Rs %.2f",
                ce_sym, ce_ltp, ce_pnl, ce_status,
                pe_sym, pe_ltp, pe_pnl, pe_status,
                total_strategy_pnl
            )

            # 1. Evaluate strategy-level portfolio stop loss (circuit breaker)
            effective_max_loss = strategy_max_loss if strategy_max_loss is not None else 10000.0
            single_leg_exited_this_tick = False
            attempted_exit_legs = set()
            if total_strategy_pnl <= -effective_max_loss:
                if use_leg_sl:
                    # In single-leg mode, exit ONLY the losing leg and arm cost trail (never cascade in the same tick!)
                    if state["ce"].get("status") == "OPEN" and state["pe"].get("status") == "OPEN":
                        if ce_pnl < pe_pnl:
                            logger.warning(
                                "Combined loss Rs %.2f >= limit Rs %.2f. Exiting losing CE leg (PnL: Rs %.2f). PE remains OPEN.",
                                abs(total_strategy_pnl), effective_max_loss, ce_pnl
                            )
                            attempted_exit_legs.add("ce")
                            if self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "SINGLE_LEG_10K_EXIT", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True):
                                single_leg_exited_this_tick = True
                                if trail_to_cost and state["pe"].get("status") == "OPEN":
                                    state["pe"]["cost_trail_pending"] = True
                                    if pe_ltp < pe_entry:
                                        state["pe"]["sl_price"] = pe_entry
                                        state["pe"]["cost_trail_pending"] = False
                                    self.save_state(state)
                                if self.notifier:
                                    total_day_pnl = self._get_current_day_pnl(state, qty)
                                    ce_fin = float(state["ce"]["exit_price"]) if state["ce"].get("exit_price") is not None else ce_ltp
                                    ce_realized_pnl = (ce_entry - ce_fin) * qty
                                    self.notifier.send_message(
                                        f"🚨 <b>PORTFOLIO 10K THRESHOLD HIT</b>: Total loss ₹{abs(total_strategy_pnl):,.2f} >= ₹{effective_max_loss:,.2f}.\n"
                                        f"Exited losing CE leg @ ₹{ce_fin:.2f} (Loss: ₹{ce_realized_pnl:+,.2f}).\n"
                                        f"PE leg remains <b>OPEN</b> (cost trail armed at ₹{pe_entry:.2f}) to harvest decay!\n\n"
                                        f"📊 <b>Current Day P&L: ₹{total_day_pnl:+,.2f}</b>"
                                    )
                        else:
                            logger.warning(
                                "Combined loss Rs %.2f >= limit Rs %.2f. Exiting losing PE leg (PnL: Rs %.2f). CE remains OPEN.",
                                abs(total_strategy_pnl), effective_max_loss, pe_pnl
                            )
                            attempted_exit_legs.add("pe")
                            if self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "SINGLE_LEG_10K_EXIT", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True):
                                single_leg_exited_this_tick = True
                                if trail_to_cost and state["ce"].get("status") == "OPEN":
                                    state["ce"]["cost_trail_pending"] = True
                                    if ce_ltp < ce_entry:
                                        state["ce"]["sl_price"] = ce_entry
                                        state["ce"]["cost_trail_pending"] = False
                                    self.save_state(state)
                                if self.notifier:
                                    total_day_pnl = self._get_current_day_pnl(state, qty)
                                    pe_fin = float(state["pe"]["exit_price"]) if state["pe"].get("exit_price") is not None else pe_ltp
                                    pe_realized_pnl = (pe_entry - pe_fin) * qty
                                    self.notifier.send_message(
                                        f"🚨 <b>PORTFOLIO 10K THRESHOLD HIT</b>: Total loss ₹{abs(total_strategy_pnl):,.2f} >= ₹{effective_max_loss:,.2f}.\n"
                                        f"Exited losing PE leg @ ₹{pe_fin:.2f} (Loss: ₹{pe_realized_pnl:+,.2f}).\n"
                                        f"CE leg remains <b>OPEN</b> (cost trail armed at ₹{ce_entry:.2f}) to harvest decay!\n\n"
                                        f"📊 <b>Current Day P&L: ₹{total_day_pnl:+,.2f}</b>"
                                    )
                    elif state["ce"].get("status") == "OPEN":
                        attempted_exit_legs.add("ce")
                        if self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "STRATEGY_MAX_LOSS_10K", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True):
                            if self.notifier:
                                total_day_pnl = self._get_current_day_pnl(state, qty)
                                self.notifier.send_message(
                                    f"🚨 <b>PORTFOLIO STOP LOSS HIT</b>: Total loss ₹{abs(total_strategy_pnl):,.2f} >= ₹{effective_max_loss:,.2f}. Exited remaining CE leg!\n\n"
                                    f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
                                )
                    elif state["pe"].get("status") == "OPEN":
                        attempted_exit_legs.add("pe")
                        if self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "STRATEGY_MAX_LOSS_10K", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True):
                            if self.notifier:
                                total_day_pnl = self._get_current_day_pnl(state, qty)
                                self.notifier.send_message(
                                    f"🚨 <b>PORTFOLIO STOP LOSS HIT</b>: Total loss ₹{abs(total_strategy_pnl):,.2f} >= ₹{effective_max_loss:,.2f}. Exited remaining PE leg!\n\n"
                                    f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
                                )
                else:
                    logger.warning(
                        "Total strategy loss Rs %.2f >= limit Rs %.2f. Triggering immediate portfolio exit for all open legs simultaneously.",
                        abs(total_strategy_pnl), effective_max_loss
                    )
                    ce_closed_ok = True
                    pe_closed_ok = True
                    if state["ce"].get("status") == "OPEN":
                        attempted_exit_legs.add("ce")
                        ce_closed_ok = self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "STRATEGY_MAX_LOSS_10K", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True)
                    if state["pe"].get("status") == "OPEN":
                        attempted_exit_legs.add("pe")
                        pe_closed_ok = self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "STRATEGY_MAX_LOSS_10K", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=True)
                    if self.notifier and ce_closed_ok and pe_closed_ok:
                        total_day_pnl = self._get_current_day_pnl(state, qty)
                        self.notifier.send_message(
                            f"🚨 <b>PORTFOLIO STOP LOSS HIT</b>: Total loss ₹{abs(total_strategy_pnl):,.2f} >= ₹{effective_max_loss:,.2f}. Exited all open legs!\n\n"
                            f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
                        )

            # Skip leg SL check on same tick after SINGLE_LEG_10K_EXIT to prevent instant cascade double-exit
            if not single_leg_exited_this_tick:
                # 2. Apply any pending cost trail once surviving leg moves below entry
                for l_name, l_ltp, l_entry in (("ce", ce_ltp, ce_entry), ("pe", pe_ltp, pe_entry)):
                    leg_d = state[l_name]
                    cur_sl = float(leg_d["sl_price"]) if leg_d.get("sl_price") is not None else l_entry
                    if leg_d.get("status") == "OPEN" and leg_d.get("cost_trail_pending") and cur_sl > l_entry:
                        if l_ltp < l_entry:
                            leg_d["sl_price"] = l_entry
                            leg_d["cost_trail_pending"] = False
                            self.save_state(state)
                            logger.info("Locked pending cost trail for %s @ Rs %.2f", l_name.upper(), l_entry)

                # 3. Evaluate individual CE leg
                if state["ce"].get("status") == "OPEN" and "ce" not in attempted_exit_legs:
                    ce_sl = state["ce"].get("sl_price")
                    ce_target = state["ce"].get("target_price")

                    if use_leg_sl and ce_sl is not None and ce_ltp >= ce_sl:
                        logger.warning("CE Stop-Loss hit! LTP: %.2f >= SL: %.2f. Exiting CE leg only.", ce_ltp, ce_sl)
                        sl_retry = int(state["ce"].get("sl_retry_count") or 0)
                        if self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "STOP_LOSS", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=(sl_retry >= 1)):
                            if trail_to_cost and state["pe"].get("status") == "OPEN":
                                state["pe"]["cost_trail_pending"] = True
                                if pe_ltp < pe_entry:
                                    state["pe"]["sl_price"] = pe_entry
                                    state["pe"]["cost_trail_pending"] = False
                                    logger.info("Trailed PE stop-loss to cost/breakeven: %.2f", pe_entry)
                                self.save_state(state)
                            if self.notifier:
                                ce_fin = float(state["ce"]["exit_price"]) if state["ce"].get("exit_price") is not None else ce_ltp
                                leg_pnl = (ce_entry - ce_fin) * qty
                                total_day_pnl = self._get_current_day_pnl(state, qty)
                                trail_note = f"\nPE leg stop loss trailed to cost (₹{pe_entry:.2f})." if (trail_to_cost and state['pe'].get('status') == 'OPEN' and state['pe'].get('sl_price') == pe_entry) else ""
                                self.notifier.send_message(
                                    f"🛑 <b>STOP LOSS HIT</b>: CE <code>{ce_sym}</code> exited @ ₹{ce_fin:.2f} (SL: ₹{ce_sl:.2f})\n"
                                    f"PE leg remains <b>OPEN</b> to harvest theta decay!{trail_note}\n"
                                    f"Leg Realized P&L: <b>₹{leg_pnl:+,.2f}</b>\n\n"
                                    f"📊 <b>Current Day P&L: ₹{total_day_pnl:+,.2f}</b>"
                                )
                        else:
                            state["ce"]["sl_retry_count"] = sl_retry + 1
                            self.save_state(state)

                    elif ce_target is not None and ce_ltp <= ce_target:
                        logger.info("CE Decay Target reached! LTP: %.2f <= Target: %.2f.", ce_ltp, ce_target)
                        if self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "DECAY_TARGET", order_type, limit_buffer_pct, state, min_buffer_rs=1.5):
                            if not use_leg_sl and state["pe"].get("status") == "OPEN":
                                self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "CO_TARGET_EXIT", order_type, limit_buffer_pct, state, min_buffer_rs=1.5)
                            if self.notifier:
                                ce_entry = float(state["ce"]["entry_price"])
                                ce_fin = float(state["ce"]["exit_price"]) if state["ce"].get("exit_price") is not None else ce_ltp
                                leg_pnl = (ce_entry - ce_fin) * qty
                                total_day_pnl = self._get_current_day_pnl(state, qty)
                                self.notifier.send_message(
                                    f"🎯 <b>DECAY TARGET REACHED</b>: CE <code>{ce_sym}</code> exited @ ₹{ce_fin:.2f} (Target: ₹{ce_target:.2f})\n"
                                    f"Leg Realized P&L: <b>₹{leg_pnl:+,.2f}</b>\n\n"
                                    f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
                                )

                # 4. Evaluate individual PE leg
                if state["pe"].get("status") == "OPEN" and "pe" not in attempted_exit_legs:
                    pe_sl = state["pe"].get("sl_price")
                    pe_target = state["pe"].get("target_price")

                    if use_leg_sl and pe_sl is not None and pe_ltp >= pe_sl:
                        logger.warning("PE Stop-Loss hit! LTP: %.2f >= SL: %.2f. Exiting PE leg only.", pe_ltp, pe_sl)
                        sl_retry = int(state["pe"].get("sl_retry_count") or 0)
                        if self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "STOP_LOSS", order_type, limit_buffer_pct, state, min_buffer_rs=1.5, escalate_to_market=(sl_retry >= 1)):
                            if trail_to_cost and state["ce"].get("status") == "OPEN":
                                state["ce"]["cost_trail_pending"] = True
                                if ce_ltp < ce_entry:
                                    state["ce"]["sl_price"] = ce_entry
                                    state["ce"]["cost_trail_pending"] = False
                                    logger.info("Trailed CE stop-loss to cost/breakeven: %.2f", ce_entry)
                                self.save_state(state)
                            if self.notifier:
                                pe_fin = float(state["pe"]["exit_price"]) if state["pe"].get("exit_price") is not None else pe_ltp
                                leg_pnl = (pe_entry - pe_fin) * qty
                                total_day_pnl = self._get_current_day_pnl(state, qty)
                                trail_note = f"\nCE leg stop loss trailed to cost (₹{ce_entry:.2f})." if (trail_to_cost and state['ce'].get('status') == 'OPEN' and state['ce'].get('sl_price') == ce_entry) else ""
                                self.notifier.send_message(
                                    f"🛑 <b>STOP LOSS HIT</b>: PE <code>{pe_sym}</code> exited @ ₹{pe_fin:.2f} (SL: ₹{pe_sl:.2f})\n"
                                    f"CE leg remains <b>OPEN</b> to harvest theta decay!{trail_note}\n"
                                    f"Leg Realized P&L: <b>₹{leg_pnl:+,.2f}</b>\n\n"
                                    f"📊 <b>Current Day P&L: ₹{total_day_pnl:+,.2f}</b>"
                                )
                        else:
                            state["pe"]["sl_retry_count"] = sl_retry + 1
                            self.save_state(state)

                    elif pe_target is not None and pe_ltp <= pe_target:
                        logger.info("PE Decay Target reached! LTP: %.2f <= Target: %.2f.", pe_ltp, pe_target)
                        if self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "DECAY_TARGET", order_type, limit_buffer_pct, state, min_buffer_rs=1.5):
                            if not use_leg_sl and state["ce"].get("status") == "OPEN":
                                self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "CO_TARGET_EXIT", order_type, limit_buffer_pct, state, min_buffer_rs=1.5)
                            if self.notifier:
                                pe_entry = float(state["pe"]["entry_price"])
                                pe_fin = float(state["pe"]["exit_price"]) if state["pe"].get("exit_price") is not None else pe_ltp
                                leg_pnl = (pe_entry - pe_fin) * qty
                                total_day_pnl = self._get_current_day_pnl(state, qty)
                                self.notifier.send_message(
                                    f"🎯 <b>DECAY TARGET REACHED</b>: PE <code>{pe_sym}</code> exited @ ₹{pe_fin:.2f} (Target: ₹{pe_target:.2f})\n"
                                    f"Leg Realized P&L: <b>₹{leg_pnl:+,.2f}</b>\n\n"
                                    f"📊 <b>Total Day Realized P&L: ₹{total_day_pnl:+,.2f}</b>"
                                )

            # If both legs closed, complete session
            if state["ce"].get("status") == "CLOSED" and state["pe"].get("status") == "CLOSED":
                logger.info("Both strangle legs closed. Marking session as COMPLETED.")
                state["status"] = "COMPLETED"
                gross_pnl = self._get_current_day_pnl(state, qty)
                state["realized_pnl"] = gross_pnl
                if self.notifier:
                    ce_fin = float(state["ce"]["exit_price"]) if state["ce"].get("exit_price") is not None else float(state["ce"]["entry_price"])
                    pe_fin = float(state["pe"]["exit_price"]) if state["pe"].get("exit_price") is not None else float(state["pe"]["entry_price"])
                    ce_pnl = (float(state["ce"]["entry_price"]) - ce_fin) * qty
                    pe_pnl = (float(state["pe"]["entry_price"]) - pe_fin) * qty
                    summary_msg = (
                        f"📊 <b>Strangle Session Completed</b>\n\n"
                        f"• CE ({state['ce']['exit_reason']}): ₹{ce_pnl:+,.2f}\n"
                        f"• PE ({state['pe']['exit_reason']}): ₹{pe_pnl:+,.2f}\n\n"
                        f"📊 <b>Total Day Realized P&L: ₹{gross_pnl:+,.2f}</b>"
                    )
                    self.notifier.send_message(summary_msg)

            self.save_state(state)

            if execute_once:
                logger.info("execute_once=True: exiting monitoring loop after single evaluation.")
                break

            if state.get("status") == "COMPLETED":
                break

            time.sleep(poll_seconds)

        return state

    def run_scheduler(
        self,
        underlying: str = "NIFTY",
        lots: int = 10,
        poll_seconds: int = 5,
        kite: Optional[KiteConnect] = None,
        strategy_max_loss: Optional[float] = 10000.0,
        use_leg_sl: bool = False,
        trail_to_cost: bool = False,
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
        ignore_holidays: bool = False,
        order_timeout: int = 300,
        entry_time_str: str = "10:00",
        exit_time_str: str = "14:45",
        idle_sleep_seconds: int = 15,
        max_iterations: Optional[int] = None,
        morning_filter: bool = True,
        morning_filter_pct: float = 0.60,
        strike_mode: str = "ATR",
        atr_multiplier: float = 1.25,
        target_price: float = 30.0
    ) -> None:
        """
        Runs the automated daily daemon scheduler for the 10:00 AM Short Strangle Desk.
        - Continuously runs in background (ideal for Docker Compose and Cloud VPS)
        - Logs hourly heartbeat with market status
        - Skips weekends and official NSE trading holidays
        - If active trade exists for today: resumes monitoring until completion
        - At 10:00 AM IST: executes option chain scan, places dual-leg entry, and starts monitoring loop
        - At 14:45 PM IST: closes any remaining open positions (avoids late gamma spikes)
        - Outside market hours (e.g. evening 18:33 IST): sleeps peacefully and never submits orders
        """
        mode = getattr(self.order_manager, "mode", "PAPER")
        logger.info(
            "Starting 10:00 AM Strangle Desk Daemon [%s] (Lots: %d, Entry: %s IST, Cutoff: %s IST)...",
            mode, lots, entry_time_str, exit_time_str
        )
        # Daemon started notification removed per policy (alerts only on entry, exit, sl)

        entry_h, entry_m = map(int, entry_time_str.split(":"))
        exit_h, exit_m = map(int, exit_time_str.split(":"))
        cutoff_time = dt_time(exit_h, exit_m)
        last_heartbeat_hour: int = -1
        last_holiday_date: Optional[date] = None
        last_totp_login_date: Optional[date] = None
        last_idle_reconcile_time: float = 0.0
        iteration = 0

        while True:
            if max_iterations is not None and iteration >= max_iterations:
                logger.info("Reached max iterations (%d). Stopping scheduler.", max_iterations)
                break
            iteration += 1

            now = datetime.now()
            today_d = now.date()
            current_t = now.time()

            # Heartbeat once per hour
            if now.hour != last_heartbeat_hour:
                last_heartbeat_hour = now.hour
                state = self.load_state() or {}
                hol_closed, _ = is_trading_holiday(check_date=today_d) if not ignore_holidays else (False, None)
                market_status = "OPEN" if (dt_time(9, 15) <= current_t <= dt_time(15, 30) and not hol_closed) else "CLOSED"
                logger.info(
                    "Strangle scheduler heartbeat [%s] | Market: %s | Today State: %s | Realized P&L: Rs %.2f",
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    market_status,
                    state.get("status", "IDLE"),
                    float(state.get("realized_pnl", 0.0))
                )

            # Check weekends and holidays
            if not ignore_holidays:
                is_holiday, reason = is_trading_holiday(check_date=today_d)
                if is_holiday:
                    if last_holiday_date != today_d:
                        logger.info("Exchange is closed today (%s: %s). Scheduler waiting for next trading day.", today_d, reason)
                        last_holiday_date = today_d
                    time.sleep(idle_sleep_seconds)
                    continue

            # Morning 08:00 AM IST TOTP Login Renewal:
            # Renew Zerodha access token once per day at or after 08:00 AM IST on trading days.
            # Ensures all daily activities (10:00 AM entry, order fills, quotes) use freshly minted credentials.
            if current_t >= dt_time(8, 0) and last_totp_login_date != today_d:
                try:
                    kite = self.perform_daily_morning_login(kite)
                    last_totp_login_date = today_d
                except Exception as e:
                    logger.error("Failed daily 08:00 AM morning TOTP login: %s", e, exc_info=True)
                    pass  # Non-trade error alert removed per policy

            # Check today's state
            state = self.load_state()
            today_str = today_d.strftime("%Y-%m-%d")

            # 1. If an active trade already exists for today, resume monitoring it!
            if state is not None and state.get("date") == today_str and state.get("status") == "OPEN":
                logger.info("Resuming active session from today (%s)...", today_str)
                try:
                    self.run_session(
                        underlying=underlying,
                        lots=lots,
                        poll_seconds=poll_seconds,
                        kite=kite,
                        strategy_max_loss=strategy_max_loss,
                        use_leg_sl=use_leg_sl,
                        trail_to_cost=trail_to_cost,
                        order_type=order_type,
                        limit_buffer_pct=limit_buffer_pct,
                        ignore_holidays=ignore_holidays,
                        order_timeout=order_timeout,
                        morning_filter=morning_filter,
                        morning_filter_pct=morning_filter_pct,
                        strike_mode=strike_mode,
                        atr_multiplier=atr_multiplier,
                        target_price=target_price,
                        exit_time_str=exit_time_str,
                    )
                except Exception as e:
                    logger.exception("Error during active strangle session: %s", e)
                time.sleep(idle_sleep_seconds)
                continue

            # 2. If today's session is already completed, aborted, or skipped by morning filter, wait for next trading day
            if state is not None and state.get("date") == today_str and state.get("status") in ("COMPLETED", "ABORTED", "SKIPPED_TREND_DAY"):
                time.sleep(idle_sleep_seconds)
                continue

            # 3. Market Hours Guard: Outside regular NSE trading hours (09:15 - 15:30 IST) or on weekends, sleep peacefully
            is_market_hours = (dt_time(9, 15) <= current_t <= dt_time(15, 30)) and (now.weekday() < 5 or ignore_holidays)
            if not is_market_hours:
                time.sleep(idle_sleep_seconds)
                continue

            # 4. During Market Hours: Reconcile with broker positions in LIVE mode
            mode = getattr(self.order_manager, "mode", "PAPER")
            if mode == "LIVE" and kite is not None:
                # If an active trade exists locally, reconcile every loop to detect external broker exits.
                # If state is None / idle, throttle queries to at most once per 60s to conserve API limits.
                should_reconcile = True
                if state is None or state.get("status") not in ("OPEN",):
                    if last_idle_reconcile_time and (time.time() - last_idle_reconcile_time) < 60.0:
                        should_reconcile = False

                if should_reconcile:
                    kite = self._ensure_kite_session(kite, check_market_hours=True, ignore_holidays=ignore_holidays)
                    state = self.reconcile_with_broker(kite, state, underlying=underlying)
                    last_idle_reconcile_time = time.time()
                    if state is not None and state.get("date") == today_str and state.get("status") == "OPEN":
                        continue

            # 5. If before 10:00 AM (e.g. 09:15-09:59 IST), wait for entry time
            if current_t < dt_time(entry_h, entry_m):
                time.sleep(idle_sleep_seconds)
                continue

            # 6. If within 5-minute entry window on trading days (safe across hour boundaries and before cutoff):
            entry_window_start = dt_time(entry_h, entry_m)
            end_total_m = min(entry_h * 60 + entry_m + 5, 23 * 60 + 59)
            entry_window_end = dt_time(end_total_m // 60, end_total_m % 60)
            if entry_window_start <= current_t <= entry_window_end and current_t < cutoff_time:
                logger.info("Triggering 10:00 AM Strangle entry for %s...", today_str)
                try:
                    kite = self._ensure_kite_session(kite, check_market_hours=True, ignore_holidays=ignore_holidays)
                    session_res = self.run_session(
                        underlying=underlying,
                        lots=lots,
                        poll_seconds=poll_seconds,
                        kite=kite,
                        strategy_max_loss=strategy_max_loss,
                        use_leg_sl=use_leg_sl,
                        trail_to_cost=trail_to_cost,
                        order_type=order_type,
                        limit_buffer_pct=limit_buffer_pct,
                        ignore_holidays=ignore_holidays,
                        order_timeout=order_timeout,
                        morning_filter=morning_filter,
                        morning_filter_pct=morning_filter_pct,
                        strike_mode=strike_mode,
                        atr_multiplier=atr_multiplier,
                        target_price=target_price,
                        exit_time_str=exit_time_str,
                    )
                    if isinstance(session_res, dict) and session_res.get("status") in ("ABORTED", "COMPLETED", "SKIPPED_TREND_DAY"):
                        logger.info("Strangle session returned %s. Pausing before next scheduler cycle.", session_res.get("status"))
                except Exception as e:
                    logger.exception("Error during 10:00 AM strangle session: %s", e)
                    pass  # Non-trade error alert removed per policy
                time.sleep(idle_sleep_seconds)
                continue

            # 5. Outside entry window & market closed (e.g. evening 18:33 IST): sleep
            time.sleep(idle_sleep_seconds)
