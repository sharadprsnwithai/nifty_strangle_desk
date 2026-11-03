import json
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, Any, Optional, List
from kiteconnect import KiteConnect
from strategy.order_manager import OrderManager
from strategy.option_chain import find_strangle_strikes
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

    def run_session(
        self,
        underlying: str = "NIFTY",
        lots: int = 10,
        poll_seconds: int = 5,
        dry_run: bool = False,
        execute_once: bool = False,
        kite: Optional[KiteConnect] = None,
        max_loss_per_leg: float = 10000.0,
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
        ignore_holidays: bool = False,
        order_timeout: int = 300
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

        if state is None:
            if kite is None:
                raise ValueError("KiteConnect instance required to discover strikes and fetch live quotes.")

            logger.info("Scanning option chain for strikes with LTP >= Rs 30 (0 DTE -> Next Week)...")
            strikes = find_strangle_strikes(kite, underlying=underlying, target_price=30.0, lots=lots)
            mode = getattr(self.order_manager, "mode", "PAPER")
            self._print_plan(strikes, mode=mode)

            if dry_run:
                logger.info("Dry-run complete. Strike plan displayed above. No orders submitted.")
                return {"status": "DRY_RUN", "strikes": strikes}

            ce_info = strikes["ce"]
            pe_info = strikes["pe"]
            total_qty = strikes["total_quantity"]

            ce_order = self.order_manager.sell(
                ce_info["symbol"], total_qty, ce_info["ltp"], order_type=order_type, buffer_pct=limit_buffer_pct
            )
            pe_order = self.order_manager.sell(
                pe_info["symbol"], total_qty, pe_info["ltp"], order_type=order_type, buffer_pct=limit_buffer_pct
            )

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
                entry_msg = (
                    f"🚀 <b>NIFTY 10:00 AM Strangle Opened [{mode}]</b>\n"
                    f"Expiry: <code>{strikes['expiry']}</code> | Qty: {total_qty} ({strikes['total_lots']} lots)\n"
                    f"Order Type: <code>{order_type}</code> (Buffer: {limit_buffer_pct*100:.0f}%)\n"
                    f"• <b>CALL</b>: <code>{ce_info['symbol']}</code> @ Rs {ce_exec_price:.2f}\n"
                    f"  SL: Rs {ce_sl:.2f} | Target: Rs {ce_tgt:.2f}\n"
                    f"• <b>PUT</b>: <code>{pe_info['symbol']}</code> @ Rs {pe_exec_price:.2f}\n"
                    f"  SL: Rs {pe_sl:.2f} | Target: Rs {pe_tgt:.2f}\n"
                    f"• Risk Cap: Max Rs {max_loss_per_leg:,.0f} loss per leg"
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
                    self.order_manager.buy_to_cover(
                        ce_sym, qty, ce_ltp, reason="TIME_EXIT", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "TIME_EXIT"

                if state["pe"].get("status") == "OPEN":
                    self.order_manager.buy_to_cover(
                        pe_sym, qty, pe_ltp, reason="TIME_EXIT", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "TIME_EXIT"

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
                logger.error("Failed to fetch live quotes: %s", e)
                if execute_once:
                    break
                time.sleep(poll_seconds)
                continue

            ce_ltp = self._extract_ltp(quotes, ce_sym, default=state["ce"]["entry_price"])
            pe_ltp = self._extract_ltp(quotes, pe_sym, default=state["pe"]["entry_price"])

            # Evaluate CE leg
            if state["ce"].get("status") == "OPEN":
                ce_sl = state["ce"]["sl_price"]
                ce_target = state["ce"]["target_price"]
                ce_entry = state["ce"]["entry_price"]
                ce_loss = (ce_ltp - ce_entry) * qty

                if ce_loss >= max_loss_per_leg:
                    logger.warning("CE leg loss Rs %.2f >= max loss limit Rs %.2f. Triggering immediate exit.", ce_loss, max_loss_per_leg)
                    self.order_manager.buy_to_cover(
                        ce_sym, qty, ce_ltp, reason="MAX_LOSS_10K", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "MAX_LOSS_10K"
                    if self.notifier:
                        self.notifier.send_message(f"⚠️ <b>MAX LOSS HIT</b>: CE <code>{ce_sym}</code> exited @ Rs {ce_ltp:.2f} (Loss: Rs {ce_loss:,.2f})")
                elif ce_ltp >= ce_sl:
                    logger.warning("CE Stop-Loss hit! LTP: %.2f >= SL: %.2f", ce_ltp, ce_sl)
                    self.order_manager.buy_to_cover(
                        ce_sym, qty, ce_ltp, reason="STOP_LOSS", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "STOP_LOSS"
                    if self.notifier:
                        self.notifier.send_message(f"🛑 <b>STOP LOSS HIT</b>: CE <code>{ce_sym}</code> exited @ Rs {ce_ltp:.2f} (SL: Rs {ce_sl:.2f})")
                elif ce_ltp <= ce_target:
                    logger.info("CE Decay Target reached! LTP: %.2f <= Target: %.2f", ce_ltp, ce_target)
                    self.order_manager.buy_to_cover(
                        ce_sym, qty, ce_ltp, reason="DECAY_TARGET", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "DECAY_TARGET"
                    if self.notifier:
                        self.notifier.send_message(f"🎯 <b>DECAY TARGET REACHED</b>: CE <code>{ce_sym}</code> exited @ Rs {ce_ltp:.2f} (Target: Rs {ce_target:.2f})")

            # Evaluate PE leg
            if state["pe"].get("status") == "OPEN":
                pe_sl = state["pe"]["sl_price"]
                pe_target = state["pe"]["target_price"]
                pe_entry = state["pe"]["entry_price"]
                pe_loss = (pe_ltp - pe_entry) * qty

                if pe_loss >= max_loss_per_leg:
                    logger.warning("PE leg loss Rs %.2f >= max loss limit Rs %.2f. Triggering immediate exit.", pe_loss, max_loss_per_leg)
                    self.order_manager.buy_to_cover(
                        pe_sym, qty, pe_ltp, reason="MAX_LOSS_10K", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "MAX_LOSS_10K"
                    if self.notifier:
                        self.notifier.send_message(f"⚠️ <b>MAX LOSS HIT</b>: PE <code>{pe_sym}</code> exited @ Rs {pe_ltp:.2f} (Loss: Rs {pe_loss:,.2f})")
                elif pe_ltp >= pe_sl:
                    logger.warning("PE Stop-Loss hit! LTP: %.2f >= SL: %.2f", pe_ltp, pe_sl)
                    self.order_manager.buy_to_cover(
                        pe_sym, qty, pe_ltp, reason="STOP_LOSS", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "STOP_LOSS"
                    if self.notifier:
                        self.notifier.send_message(f"🛑 <b>STOP LOSS HIT</b>: PE <code>{pe_sym}</code> exited @ Rs {pe_ltp:.2f} (SL: Rs {pe_sl:.2f})")
                elif pe_ltp <= pe_target:
                    logger.info("PE Decay Target reached! LTP: %.2f <= Target: %.2f", pe_ltp, pe_target)
                    self.order_manager.buy_to_cover(
                        pe_sym, qty, pe_ltp, reason="DECAY_TARGET", order_type=order_type, buffer_pct=limit_buffer_pct
                    )
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "DECAY_TARGET"
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
