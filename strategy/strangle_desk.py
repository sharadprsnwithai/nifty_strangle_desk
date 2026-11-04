import json
import logging
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Dict, Any, Optional, List
from kiteconnect import KiteConnect
from strategy.order_manager import OrderManager
from strategy.option_chain import find_strangle_strikes, resolve_target_expiry, _normalize_date
from strategy.holidays import is_trading_holiday
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("strategy.desk")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "trades_today.json"


class StrangleDesk:
    """Manages strategy state, persistence, recovery, and intraday execution."""

    def __init__(
        self,
        order_manager: OrderManager,
        state_file: Optional[Path] = None,
        notifier: Optional[TelegramNotifier] = None
    ):
        self.order_manager = order_manager
        self.state_file = state_file or DEFAULT_STATE_FILE
        self.notifier = notifier
        self.state: Optional[Dict[str, Any]] = None

    def save_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.state_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp_file.replace(self.state_file)

    def load_state(self) -> Optional[Dict[str, Any]]:
        if not self.state_file.exists():
            return None
        try:
            content = self.state_file.read_text(encoding="utf-8")
            self.state = json.loads(content)
            return self.state
        except Exception as e:
            logger.error("Failed to load state from %s: %s", self.state_file, e)
            return None

    def clear_state(self) -> None:
        if self.state_file.exists():
            self.state_file.unlink()
        self.state = None

    @staticmethod
    def _print_plan(strikes: Dict[str, Any], mode: str = "PAPER") -> None:
        ce_info = strikes["ce"]
        pe_info = strikes["pe"]
        print("\n" + "=" * 65)
        print(f"  [+] 10:00 AM NIFTY STRANGLE PLAN ({mode} MODE)")
        print("=" * 65)
        print(f"Target Expiry : {strikes['expiry']}")
        print(f"Order Sizing  : {strikes['total_lots']} lots ({strikes['total_quantity']} qty)")
        print(f"CALL Leg      : {ce_info['symbol']} @ Rs {ce_info['ltp']:.2f}")
        print(f"                SL: Rs {ce_info['sl_price']:.2f} (+50%) | Target: Rs {ce_info['target_price']:.2f} (80% decay)")
        print(f"PUT Leg       : {pe_info['symbol']} @ Rs {pe_info['ltp']:.2f}")
        print(f"                SL: Rs {pe_info['sl_price']:.2f} (+50%) | Target: Rs {pe_info['target_price']:.2f} (80% decay)")
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
        state: Dict[str, Any]
    ) -> bool:
        """
        Safely executes buy_to_cover for a single strangle leg.
        Catches broker exceptions, logs errors, alerts Telegram, and prevents loop crashes.
        """
        try:
            leg_product = (
                state.get(leg_name, {}).get("product")
                or state.get("product")
                or getattr(self.order_manager, "product", "MIS")
            )
            self.order_manager.buy_to_cover(
                symbol, qty, exit_price, reason=reason, order_type=order_type, buffer_pct=limit_buffer_pct,
                product=leg_product
            )
            state[leg_name]["status"] = "CLOSED"
            state[leg_name]["exit_price"] = exit_price
            state[leg_name]["exit_reason"] = reason
            self.save_state(state)
            return True
        except Exception as e:
            logger.error("Failed to execute %s exit for %s (%s): %s", reason, leg_name.upper(), symbol, e)
            if self.notifier:
                self.notifier.send_message(
                    f"🚨 <b>Order Exit Failed</b>: Failed to exit {leg_name.upper()} <code>{symbol}</code> ({reason}): {e}"
                )
            return False

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

    def _ensure_kite_session(self, kite: Optional[Any] = None, force_login: bool = False) -> Any:
        """
        Validates that the provided KiteConnect client has a live, valid session.
        If the access token has expired or is invalidated, triggers automated TOTP re-authentication.
        """
        if kite is None:
            kite = getattr(self.order_manager, "kite", None)

        if not force_login and kite is not None:
            try:
                kite.profile()
                return kite
            except Exception as e:
                err_str = str(e).lower()
                if "token" in err_str or "api_key" in err_str or "access_token" in err_str or "tokenexception" in type(e).__name__.lower():
                    logger.warning("Kite session token is expired or invalidated (%s). Initiating TOTP re-authentication...", e)
                else:
                    logger.warning("Kite profile check encountered unexpected error (%s). Proceeding to re-authenticate...", e)

        logger.info("Refreshing Kite session via automated TOTP login...")
        from kite.client import get_kite_client
        new_kite = get_kite_client(force_login=True)
        if hasattr(self.order_manager, "kite") and self.order_manager.kite is not None:
            self.order_manager.kite = new_kite
        return new_kite

    def perform_daily_morning_login(self, kite: Optional[Any] = None) -> Any:
        """
        Executes an automated headless TOTP login to obtain a fresh daily Zerodha Kite access token.
        Saves the token to .env, updates the OrderManager client, and notifies via Telegram.
        """
        logger.info("Executing daily morning TOTP login (08:00 AM IST schedule)...")
        from kite.client import get_kite_client
        new_kite = get_kite_client(force_login=True)
        if hasattr(self.order_manager, "kite"):
            self.order_manager.kite = new_kite

        user_desc = "Zerodha Account"
        try:
            profile = new_kite.profile()
            user_desc = f"{profile.get('user_name', 'Trader')} ({profile.get('user_id')})"
            logger.info("Daily morning TOTP login successful for %s.", user_desc)
        except Exception as e:
            logger.warning("Could not fetch profile after morning login: %s", e)

        if self.notifier:
            self.notifier.send_message(
                f"🔐 <b>Morning TOTP Login Successful</b>\n"
                f"Fresh Kite access token acquired for <b>{user_desc}</b> at {datetime.now().strftime('%H:%M:%S')} IST.\n"
                f"Credentials refreshed and ready for today's trading activity."
            )
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
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_reason"] = "BROKER_RECONCILED"
                    changed = True
                    logger.info("Reconciliation: CE leg %s is flat on Kite. Marked CLOSED.", ce_sym)

                if pe_closed and state.get("pe", {}).get("status") == "OPEN":
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_reason"] = "BROKER_RECONCILED"
                    changed = True
                    logger.info("Reconciliation: PE leg %s is flat on Kite. Marked CLOSED.", pe_sym)

                if ce_closed and pe_closed:
                    state["status"] = "COMPLETED"
                    changed = True
                    logger.warning("Reconciliation: Both strangle legs are closed on Kite. Synchronizing local state to COMPLETED.")

                if changed:
                    self.save_state(state)
                return state

            # 2. No active trade locally: check if broker has open short strangle positions
            if state is None or state.get("status") not in ("OPEN", "COMPLETED", "ABORTED"):
                # Fetch instruments to identify target expiry
                instruments = kite.instruments("NFO")
                if target_expiry is None:
                    target_expiry = resolve_target_expiry(instruments, underlying=underlying)

                # Filter contracts matching underlying and target weekly expiry
                inst_map = {
                    i["tradingsymbol"]: i
                    for i in instruments
                    if i.get("name") == underlying
                    and _normalize_date(i.get("expiry")) == target_expiry
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
                    ce_leg = open_ce[0]
                    pe_leg = open_pe[0]
                    qty = min(ce_leg["quantity"], pe_leg["quantity"])
                    lot_size = ce_leg["lot_size"]
                    total_lots = qty // lot_size

                    ce_entry = ce_leg["entry_price"]
                    pe_entry = pe_leg["entry_price"]

                    ce_sl = round(ce_entry * 1.50, 2)
                    ce_tgt = round(ce_entry * 0.20, 2)
                    pe_sl = round(pe_entry * 1.50, 2)
                    pe_tgt = round(pe_entry * 0.20, 2)

                    logger.info(
                        "Reconciliation: Found open broker strangle positions on Kite: "
                        "CE %s x %d @ Rs %.2f [%s] (SL: %.2f, Tgt: %.2f), "
                        "PE %s x %d @ Rs %.2f [%s] (SL: %.2f, Tgt: %.2f). Adopting into active state.",
                        ce_leg["symbol"], qty, ce_entry, ce_leg["product"], ce_sl, ce_tgt,
                        pe_leg["symbol"], qty, pe_entry, pe_leg["product"], pe_sl, pe_tgt
                    )

                    state = {
                        "status": "OPEN",
                        "date": today_str,
                        "underlying": underlying,
                        "expiry": str(target_expiry),
                        "lots": total_lots,
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

                    if self.notifier:
                        self.notifier.send_message(
                            f"🔄 <b>Broker Position Reconciled</b>\n"
                            f"Adopted active NIFTY Strangle from Zerodha Kite:\n"
                            f"• <b>CALL</b>: <code>{ce_leg['symbol']}</code> x {qty} @ Rs {ce_entry:.2f} [{ce_leg['product']}]\n"
                            f"  SL: Rs {ce_sl:.2f} | Target: Rs {ce_tgt:.2f}\n"
                            f"• <b>PUT</b>: <code>{pe_leg['symbol']}</code> x {qty} @ Rs {pe_entry:.2f} [{pe_leg['product']}]\n"
                            f"  SL: Rs {pe_sl:.2f} | Target: Rs {pe_tgt:.2f}\n"
                            f"Actively monitoring stop loss, decay target, and 15:00 square-off."
                        )

            return state
        except Exception as e:
            logger.warning("Broker position reconciliation failed: %s", e)
            return state

    def run_session(
        self,
        underlying: str = "NIFTY",
        lots: int = 10,
        poll_seconds: int = 5,
        dry_run: bool = False,
        execute_once: bool = False,
        kite: Optional[KiteConnect] = None,
        strategy_max_loss: Optional[float] = 10000.0,
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
        ignore_holidays: bool = False,
        order_timeout: int = 300,
        check_market_hours: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Executes and monitors the 10:00 AM Short Strangle intraday session.
        Handles holiday skip, strike discovery, dual-leg entry, live quote polling,
        independent stop-loss, profit-decay target, and 3:00 PM time square-off.
        """
        if not ignore_holidays:
            is_holiday, reason = is_trading_holiday()
            if is_holiday:
                logger.info("Exchange is closed today (%s). Skipping session.", reason)
                if self.notifier:
                    self.notifier.send_message(f"⏸ <b>Strangle Desk</b>: Exchange closed today ({reason}). Skipping session.")
                return {"status": "HOLIDAY", "reason": reason}

        # Market Hours Validation (prevent placing regular orders outside NSE trading hours)
        mode = getattr(self.order_manager, "mode", "PAPER")
        now_time = datetime.now().time()
        if mode == "LIVE" and not dry_run and not ignore_holidays and check_market_hours:
            if now_time < dt_time(9, 15) or now_time > dt_time(15, 30):
                err_msg = (
                    f"Cannot execute LIVE strangle orders: NSE market is closed. "
                    f"Current time: {now_time.strftime('%H:%M:%S')} IST. Regular trading hours are 09:15 - 15:30 IST."
                )
                logger.warning(err_msg)
                if self.notifier:
                    self.notifier.send_message(f"⚠️ <b>Strangle Desk Warning</b>: {err_msg}")
                return {"status": "MARKET_CLOSED", "reason": err_msg}

        state = self.load_state()
        today_str = datetime.now().strftime("%Y-%m-%d")

        if state is not None:
            if state.get("date") != today_str:
                logger.info("Existing state from prior date (%s != %s). Initializing fresh session.", state.get("date"), today_str)
                self.clear_state()
                state = None
            elif state.get("status") in ("COMPLETED", "ABORTED"):
                logger.info("Session for today (%s) is already %s.", today_str, state.get("status"))
                return state

        if kite is None:
            kite = getattr(self.order_manager, "kite", None)

        if not dry_run and kite is not None:
            kite = self._ensure_kite_session(kite)
            state = self.reconcile_with_broker(kite, state, underlying=underlying)

        if state is None:
            if kite is None:
                raise ValueError("KiteConnect instance required to discover strikes and fetch live quotes.")

            kite = self._ensure_kite_session(kite)

            logger.info("Scanning option chain for strikes with LTP >= Rs 30 (0 DTE -> Next Week)...")
            try:
                strikes = find_strangle_strikes(kite, underlying=underlying, target_price=30.0, lots=lots)
            except Exception as e:
                err_str = str(e).lower()
                if "token" in err_str or "api_key" in err_str or "access_token" in err_str or "tokenexception" in type(e).__name__.lower():
                    logger.warning("Token error during strike discovery (%s). Refreshing token and retrying...", e)
                    kite = self._ensure_kite_session(kite, force_login=True)
                    strikes = find_strangle_strikes(kite, underlying=underlying, target_price=30.0, lots=lots)
                else:
                    raise

            mode = getattr(self.order_manager, "mode", "PAPER")
            self._print_plan(strikes, mode=mode)

            if dry_run:
                logger.info("Dry-run complete. Strike plan displayed above. No orders submitted.")
                return {"status": "DRY_RUN", "strikes": strikes}

            ce_info = strikes["ce"]
            pe_info = strikes["pe"]
            total_qty = strikes["total_quantity"]

            ce_order = None
            pe_order = None
            try:
                ce_order = self.order_manager.sell(
                    ce_info["symbol"], total_qty, ce_info["ltp"], order_type=order_type, buffer_pct=limit_buffer_pct
                )
            except Exception as e:
                err_msg = f"Failed to place CE entry order ({ce_info['symbol']}): {e}"
                logger.error(err_msg)
                if self.notifier:
                    self.notifier.send_message(f"🚨 <b>Strangle Desk Error</b>: {err_msg}")
                return {"status": "ABORTED", "reason": err_msg}

            try:
                pe_order = self.order_manager.sell(
                    pe_info["symbol"], total_qty, pe_info["ltp"], order_type=order_type, buffer_pct=limit_buffer_pct
                )
            except Exception as e:
                err_msg = f"Failed to place PE entry order ({pe_info['symbol']}) after CE placed: {e}. Squaring off CE leg..."
                logger.error(err_msg)
                if self.notifier:
                    self.notifier.send_message(f"🚨 <b>Strangle Desk Error</b>: {err_msg}")
                ce_id = ce_order.get("order_id") if isinstance(ce_order, dict) else None
                if ce_id:
                    try:
                        self.order_manager.cancel_order(ce_id)
                        ce_status = self.wait_for_order_completion(ce_id, timeout_seconds=5, poll_seconds=1, execute_once=True)
                        ce_filled = int(ce_status.get("filled_quantity") or 0)
                        if ce_filled > 0:
                            logger.warning("CE leg was partially/fully filled (%d qty). Buying back to close exposure...", ce_filled)
                            self.order_manager.buy_to_cover(
                                symbol=ce_info["symbol"],
                                quantity=ce_filled,
                                price=ce_info["ltp"] * 1.05,
                                reason="ABORT_CLEANUP",
                                order_type=order_type
                            )
                    except Exception as clean_err:
                        logger.error("Failed to clean up CE leg after PE entry failure: %s", clean_err)
                return {"status": "ABORTED", "reason": err_msg}

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
                # 1. Cancel remaining open/pending order quantity on exchange
                if ce_status.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED") or ce_status.get("pending_quantity", 0) > 0:
                    self.order_manager.cancel_order(ce_id)
                if pe_status.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED") or pe_status.get("pending_quantity", 0) > 0:
                    self.order_manager.cancel_order(pe_id)

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

                # 3. Square off any filled positions immediately
                if ce_filled > 0:
                    ce_reason = "ABORT_PARTIAL_FILL" if ce_filled < total_qty else "ABORT_UNMATCHED_LEG"
                    logger.warning("Exiting %d filled shares of CE %s (%s)", ce_filled, ce_info['symbol'], ce_reason)
                    self.order_manager.buy_to_cover(
                        ce_info['symbol'], ce_filled, ce_exit_ltp, reason=ce_reason, order_type=order_type, buffer_pct=limit_buffer_pct
                    )

                if pe_filled > 0:
                    pe_reason = "ABORT_PARTIAL_FILL" if pe_filled < total_qty else "ABORT_UNMATCHED_LEG"
                    logger.warning("Exiting %d filled shares of PE %s (%s)", pe_filled, pe_info['symbol'], pe_reason)
                    self.order_manager.buy_to_cover(
                        pe_info['symbol'], pe_filled, pe_exit_ltp, reason=pe_reason, order_type=order_type, buffer_pct=limit_buffer_pct
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
                entry_msg = (
                    f"🚀 <b>NIFTY 10:00 AM Strangle Opened [{mode}]</b>\n"
                    f"Expiry: <code>{strikes['expiry']}</code> | Qty: {total_qty} ({strikes['total_lots']} lots)\n"
                    f"Order Type: <code>{order_type}</code> (Buffer: {limit_buffer_pct*100:.0f}%)\n"
                    f"• <b>CALL</b>: <code>{ce_info['symbol']}</code> @ Rs {ce_exec_price:.2f}\n"
                    f"  SL: Rs {ce_sl:.2f} | Target: Rs {ce_tgt:.2f}\n"
                    f"• <b>PUT</b>: <code>{pe_info['symbol']}</code> @ Rs {pe_exec_price:.2f}\n"
                    f"  SL: Rs {pe_sl:.2f} | Target: Rs {pe_tgt:.2f}\n"
                    f"• Risk Cap: {risk_cap_str}"
                )
                self.notifier.send_message(entry_msg)

            state = {
                "status": "OPEN",
                "date": today_str,
                "underlying": underlying,
                "expiry": str(strikes["expiry"]),
                "lots": strikes["total_lots"],
                "quantity": total_qty,
                "ce": {
                    "symbol": ce_info["symbol"],
                    "strike": ce_info["strike"],
                    "entry_price": ce_exec_price,
                    "sl_price": ce_sl,
                    "target_price": ce_tgt,
                    "status": "OPEN",
                    "order_id": ce_id,
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
                    "exit_price": None,
                    "exit_reason": None
                }
            }
            self.save_state(state)

        # Monitoring Loop
        if kite is None:
            kite = getattr(self.order_manager, "kite", None)

        ce_sym = state["ce"]["symbol"]
        pe_sym = state["pe"]["symbol"]
        qty = state.get("quantity", lots * 65)

        while state.get("status") == "OPEN":
            # Current time check: 3:00 PM cutoff
            if datetime.now().time() >= dt_time(15, 0):
                logger.info("Intraday 15:00 cutoff reached. Closing all open strangle legs.")
                quotes: Dict[str, Any] = {}
                if kite is not None:
                    try:
                        quotes = kite.ltp([f"NFO:{ce_sym}" if ":" not in ce_sym else ce_sym,
                                           f"NFO:{pe_sym}" if ":" not in pe_sym else pe_sym])
                    except Exception as e:
                        logger.warning("Could not fetch final quotes at cutoff: %s", e)

                ce_ltp = self._extract_ltp(quotes, ce_sym, default=state["ce"]["entry_price"])
                pe_ltp = self._extract_ltp(quotes, pe_sym, default=state["pe"]["entry_price"])

                if state["ce"].get("status") == "OPEN":
                    self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "TIME_EXIT", order_type, limit_buffer_pct, state)

                if state["pe"].get("status") == "OPEN":
                    self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "TIME_EXIT", order_type, limit_buffer_pct, state)

                if state["ce"].get("status") == "CLOSED" and state["pe"].get("status") == "CLOSED":
                    state["status"] = "COMPLETED"
                    self.save_state(state)
                    if self.notifier:
                        self.notifier.send_message(
                            f"🏁 <b>15:00 Cutoff Exit</b>: Closed open legs (CE @ Rs {ce_ltp:.2f}, PE @ Rs {pe_ltp:.2f})"
                        )
                    break

            # Fetch live quotes
            if kite is None:
                raise ValueError("KiteConnect instance required to poll live quotes.")

            try:
                symbols_to_query = [f"NFO:{ce_sym}" if ":" not in ce_sym else ce_sym,
                                    f"NFO:{pe_sym}" if ":" not in pe_sym else pe_sym]
                quotes = kite.ltp(symbols_to_query)
            except Exception as e:
                err_str = str(e).lower()
                if "token" in err_str or "api_key" in err_str or "access_token" in err_str or "tokenexception" in type(e).__name__.lower():
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

            ce_ltp = self._extract_ltp(quotes, ce_sym, default=state["ce"]["entry_price"])
            pe_ltp = self._extract_ltp(quotes, pe_sym, default=state["pe"]["entry_price"])

            ce_status = state["ce"].get("status")
            pe_status = state["pe"].get("status")
            ce_entry = float(state["ce"]["entry_price"])
            pe_entry = float(state["pe"]["entry_price"])
            ce_pnl = (ce_entry - ce_ltp) * qty if ce_status == "OPEN" else (ce_entry - float(state["ce"].get("exit_price") or ce_ltp)) * qty
            pe_pnl = (pe_entry - pe_ltp) * qty if pe_status == "OPEN" else (pe_entry - float(state["pe"].get("exit_price") or pe_ltp)) * qty
            total_strategy_pnl = ce_pnl + pe_pnl
            logger.info(
                "Tick | CE (%s): LTP=%.2f, P&L=Rs %.2f [%s] | PE (%s): LTP=%.2f, P&L=Rs %.2f [%s] | Total Strangle P&L=Rs %.2f",
                ce_sym, ce_ltp, ce_pnl, ce_status,
                pe_sym, pe_ltp, pe_pnl, pe_status,
                total_strategy_pnl
            )

            # Evaluate strategy-level portfolio stop loss
            if strategy_max_loss is not None and total_strategy_pnl <= -strategy_max_loss:
                logger.warning(
                    "Total strategy loss Rs %.2f >= limit Rs %.2f. Triggering immediate portfolio exit for all open legs.",
                    abs(total_strategy_pnl), strategy_max_loss
                )
                if state["ce"].get("status") == "OPEN":
                    self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "STRATEGY_MAX_LOSS_10K", order_type, limit_buffer_pct, state)
                if state["pe"].get("status") == "OPEN":
                    self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "STRATEGY_MAX_LOSS_10K", order_type, limit_buffer_pct, state)
                if self.notifier:
                    self.notifier.send_message(
                        f"🚨 <b>PORTFOLIO STOP LOSS HIT</b>: Total loss Rs {abs(total_strategy_pnl):,.2f} >= Rs {strategy_max_loss:,.2f}. Exited all open legs!"
                    )


            # Evaluate CE leg
            if state["ce"].get("status") == "OPEN":
                ce_sl = state["ce"]["sl_price"]
                ce_target = state["ce"]["target_price"]

                if ce_ltp >= ce_sl:
                    logger.warning("CE Stop-Loss hit! LTP: %.2f >= SL: %.2f", ce_ltp, ce_sl)
                    if self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "STOP_LOSS", order_type, limit_buffer_pct, state):
                        if self.notifier:
                            self.notifier.send_message(f"🛑 <b>STOP LOSS HIT</b>: CE <code>{ce_sym}</code> exited @ Rs {ce_ltp:.2f} (SL: Rs {ce_sl:.2f})")
                elif ce_ltp <= ce_target:
                    logger.info("CE Decay Target reached! LTP: %.2f <= Target: %.2f", ce_ltp, ce_target)
                    if self._safe_exit_leg("ce", ce_sym, qty, ce_ltp, "DECAY_TARGET", order_type, limit_buffer_pct, state):
                        if self.notifier:
                            self.notifier.send_message(f"🎯 <b>DECAY TARGET REACHED</b>: CE <code>{ce_sym}</code> exited @ Rs {ce_ltp:.2f} (Target: Rs {ce_target:.2f})")

            # Evaluate PE leg
            if state["pe"].get("status") == "OPEN":
                pe_sl = state["pe"]["sl_price"]
                pe_target = state["pe"]["target_price"]

                if pe_ltp >= pe_sl:
                    logger.warning("PE Stop-Loss hit! LTP: %.2f >= SL: %.2f", pe_ltp, pe_sl)
                    if self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "STOP_LOSS", order_type, limit_buffer_pct, state):
                        if self.notifier:
                            self.notifier.send_message(f"🛑 <b>STOP LOSS HIT</b>: PE <code>{pe_sym}</code> exited @ Rs {pe_ltp:.2f} (SL: Rs {pe_sl:.2f})")
                elif pe_ltp <= pe_target:
                    logger.info("PE Decay Target reached! LTP: %.2f <= Target: %.2f", pe_ltp, pe_target)
                    if self._safe_exit_leg("pe", pe_sym, qty, pe_ltp, "DECAY_TARGET", order_type, limit_buffer_pct, state):
                        if self.notifier:
                            self.notifier.send_message(f"🎯 <b>DECAY TARGET REACHED</b>: PE <code>{pe_sym}</code> exited @ Rs {pe_ltp:.2f} (Target: Rs {pe_target:.2f})")

            # If both legs closed, complete session
            if state["ce"].get("status") == "CLOSED" and state["pe"].get("status") == "CLOSED":
                logger.info("Both strangle legs closed. Marking session as COMPLETED.")
                state["status"] = "COMPLETED"
                if self.notifier:
                    ce_pnl = (state["ce"]["entry_price"] - (state["ce"]["exit_price"] or state["ce"]["entry_price"])) * qty
                    pe_pnl = (state["pe"]["entry_price"] - (state["pe"]["exit_price"] or state["pe"]["entry_price"])) * qty
                    gross_pnl = ce_pnl + pe_pnl
                    summary_msg = (
                        f"📊 <b>Strangle Session Completed</b>\n"
                        f"• CE ({state['ce']['exit_reason']}): Rs {ce_pnl:+,.2f}\n"
                        f"• PE ({state['pe']['exit_reason']}): Rs {pe_pnl:+,.2f}\n"
                        f"• <b>Total Gross P&L</b>: <b>Rs {gross_pnl:+,.2f}</b>"
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
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
        ignore_holidays: bool = False,
        order_timeout: int = 300,
        entry_time_str: str = "10:00",
        exit_time_str: str = "15:00",
        idle_sleep_seconds: int = 15,
        max_iterations: Optional[int] = None
    ) -> None:
        """
        Runs the automated daily daemon scheduler for the 10:00 AM Short Strangle Desk.
        - Continuously runs in background (ideal for Docker Compose and Cloud VPS)
        - Logs hourly heartbeat with market status
        - Skips weekends and official NSE trading holidays
        - If active trade exists for today: resumes monitoring until completion
        - At 10:00 AM IST: executes option chain scan, places dual-leg entry, and starts monitoring loop
        - At 15:00 PM IST: closes any remaining open positions
        - Outside market hours (e.g. evening 18:33 IST): sleeps peacefully and never submits orders
        """
        mode = getattr(self.order_manager, "mode", "PAPER")
        logger.info(
            "Starting 10:00 AM Strangle Desk Daemon [%s] (Lots: %d, Entry: %s IST, Cutoff: %s IST)...",
            mode, lots, entry_time_str, exit_time_str
        )
        if self.notifier:
            risk_desc = f"Portfolio Max Loss: Rs {strategy_max_loss:,.0f}" if strategy_max_loss is not None else "None"
            self.notifier.send_message(
                f"🤖 <b>10:00 AM Strangle Desk Daemon Started [{mode}]</b>\n"
                f"Schedule: <b>Daily Entry at {entry_time_str} IST</b> | Exit at {exit_time_str} IST\n"
                f"Lots: {lots} | {risk_desc}\n"
                f"Status: Waiting for market session..."
            )

        entry_h, entry_m = map(int, entry_time_str.split(":"))
        last_heartbeat_hour: int = -1
        last_holiday_date: Optional[date] = None
        last_totp_login_date: Optional[date] = None
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
                market_status = "OPEN" if dt_time(9, 15) <= current_t <= dt_time(15, 30) and now.weekday() < 5 else "CLOSED"
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
                    if self.notifier:
                        self.notifier.send_message(f"⚠️ <b>Morning TOTP Login Failed (08:00 IST)</b>: {e}")

            # Check today's state & reconcile with broker positions
            state = self.load_state()
            today_str = today_d.strftime("%Y-%m-%d")

            mode = getattr(self.order_manager, "mode", "PAPER")
            if mode == "LIVE" and kite is not None:
                kite = self._ensure_kite_session(kite)
                state = self.reconcile_with_broker(kite, state, underlying=underlying)

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
                        order_type=order_type,
                        limit_buffer_pct=limit_buffer_pct,
                        ignore_holidays=ignore_holidays,
                        order_timeout=order_timeout
                    )
                except Exception as e:
                    logger.exception("Error during active strangle session: %s", e)
                continue

            # 2. If today's session is already completed or aborted, wait for next trading day
            if state is not None and state.get("date") == today_str and state.get("status") in ("COMPLETED", "ABORTED"):
                time.sleep(idle_sleep_seconds)
                continue

            # 3. If before 10:00 AM (e.g. pre-market or 09:15-09:59), wait for entry time
            if current_t < dt_time(entry_h, entry_m):
                time.sleep(idle_sleep_seconds)
                continue

            # 4. If at 10:00 AM (entry window: 10:00 - 10:05 IST on trading days):
            if dt_time(entry_h, entry_m) <= current_t <= dt_time(entry_h, entry_m + 5):
                logger.info("Triggering 10:00 AM Strangle entry for %s...", today_str)
                try:
                    kite = self._ensure_kite_session(kite)
                    self.run_session(
                        underlying=underlying,
                        lots=lots,
                        poll_seconds=poll_seconds,
                        kite=kite,
                        strategy_max_loss=strategy_max_loss,
                        order_type=order_type,
                        limit_buffer_pct=limit_buffer_pct,
                        ignore_holidays=ignore_holidays,
                        order_timeout=order_timeout
                    )
                except Exception as e:
                    logger.exception("Error during 10:00 AM strangle session: %s", e)
                    if self.notifier:
                        self.notifier.send_message(f"⚠️ <b>Strangle Desk Error</b>: Session failed on {today_str}: {e}")
                continue

            # 5. Outside entry window & market closed (e.g. evening 18:33 IST): sleep
            time.sleep(idle_sleep_seconds)
