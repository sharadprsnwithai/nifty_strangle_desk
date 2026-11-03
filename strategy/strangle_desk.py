import json
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect
from strategy.order_manager import OrderManager
from strategy.option_chain import find_strangle_strikes

logger = logging.getLogger("strategy.desk")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "trades_today.json"


class StrangleDesk:
    """Manages strategy state, persistence, recovery, and intraday execution."""

    def __init__(self, order_manager: OrderManager, state_file: Optional[Path] = None):
        self.order_manager = order_manager
        self.state_file = state_file or DEFAULT_STATE_FILE
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

    def run_session(
        self,
        underlying: str = "NIFTY",
        lots: int = 10,
        poll_seconds: int = 5,
        dry_run: bool = False,
        execute_once: bool = False,
        kite: Optional[KiteConnect] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Executes and monitors the 10:00 AM Short Strangle intraday session.
        Handles strike discovery, dual-leg entry, live quote polling,
        independent stop-loss, profit-decay target, and 3:00 PM time square-off.
        """
        state = self.load_state()
        today_str = datetime.now().strftime("%Y-%m-%d")

        if state is not None:
            if state.get("date") != today_str:
                logger.info("Existing state from prior date (%s != %s). Initializing fresh session.", state.get("date"), today_str)
                self.clear_state()
                state = None
            elif state.get("status") == "COMPLETED":
                logger.info("Session for today (%s) is already COMPLETED.", today_str)
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

            ce_order = self.order_manager.sell(ce_info["symbol"], total_qty, ce_info["ltp"])
            pe_order = self.order_manager.sell(pe_info["symbol"], total_qty, pe_info["ltp"])

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
                    "entry_price": ce_info["ltp"],
                    "sl_price": ce_info["sl_price"],
                    "target_price": ce_info["target_price"],
                    "status": "OPEN",
                    "order_id": ce_order.get("order_id"),
                    "exit_price": None,
                    "exit_reason": None
                },
                "pe": {
                    "symbol": pe_info["symbol"],
                    "strike": pe_info["strike"],
                    "entry_price": pe_info["ltp"],
                    "sl_price": pe_info["sl_price"],
                    "target_price": pe_info["target_price"],
                    "status": "OPEN",
                    "order_id": pe_order.get("order_id"),
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
        qty = state.get("quantity", lots * 75)

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
                    self.order_manager.buy_to_cover(ce_sym, qty, ce_ltp, reason="TIME_EXIT")
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "TIME_EXIT"

                if state["pe"].get("status") == "OPEN":
                    self.order_manager.buy_to_cover(pe_sym, qty, pe_ltp, reason="TIME_EXIT")
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "TIME_EXIT"

                state["status"] = "COMPLETED"
                self.save_state(state)
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
                if ce_ltp >= ce_sl:
                    logger.warning("CE Stop-Loss hit! LTP: %.2f >= SL: %.2f", ce_ltp, ce_sl)
                    self.order_manager.buy_to_cover(ce_sym, qty, ce_ltp, reason="STOP_LOSS")
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "STOP_LOSS"
                elif ce_ltp <= ce_target:
                    logger.info("CE Decay Target reached! LTP: %.2f <= Target: %.2f", ce_ltp, ce_target)
                    self.order_manager.buy_to_cover(ce_sym, qty, ce_ltp, reason="DECAY_TARGET")
                    state["ce"]["status"] = "CLOSED"
                    state["ce"]["exit_price"] = ce_ltp
                    state["ce"]["exit_reason"] = "DECAY_TARGET"

            # Evaluate PE leg
            if state["pe"].get("status") == "OPEN":
                pe_sl = state["pe"]["sl_price"]
                pe_target = state["pe"]["target_price"]
                if pe_ltp >= pe_sl:
                    logger.warning("PE Stop-Loss hit! LTP: %.2f >= SL: %.2f", pe_ltp, pe_sl)
                    self.order_manager.buy_to_cover(pe_sym, qty, pe_ltp, reason="STOP_LOSS")
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "STOP_LOSS"
                elif pe_ltp <= pe_target:
                    logger.info("PE Decay Target reached! LTP: %.2f <= Target: %.2f", pe_ltp, pe_target)
                    self.order_manager.buy_to_cover(pe_sym, qty, pe_ltp, reason="DECAY_TARGET")
                    state["pe"]["status"] = "CLOSED"
                    state["pe"]["exit_price"] = pe_ltp
                    state["pe"]["exit_reason"] = "DECAY_TARGET"

            # If both legs closed, complete session
            if state["ce"].get("status") == "CLOSED" and state["pe"].get("status") == "CLOSED":
                logger.info("Both strangle legs closed. Marking session as COMPLETED.")
                state["status"] = "COMPLETED"

            self.save_state(state)

            if execute_once:
                logger.info("execute_once=True: exiting monitoring loop after single evaluation.")
                break

            if state.get("status") == "COMPLETED":
                break

            time.sleep(poll_seconds)

        return state
