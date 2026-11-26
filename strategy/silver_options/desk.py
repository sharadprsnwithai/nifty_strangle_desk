import html
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.models import SilverStrangleLeg, SilverStranglePosition, calculate_silver_mcx_costs
from strategy.silver_options.chain import find_silver_strangle_strikes
from strategy.silver_options.order_manager import SilverOrderManager
from strategy.holidays import is_mcx_trading_holiday
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("strategy.silver.desk")


class SilverStrangleDesk:
    """Automated Execution & Lifecycle Engine for MCX SILVERM Evening Intraday Strangle."""

    def __init__(
        self,
        order_manager: SilverOrderManager,
        config: Optional[SilverOptionsConfig] = None,
        state_file: Optional[Path] = None,
        notifier: Optional[TelegramNotifier] = None,
        kite_client: Optional[KiteConnect] = None,
    ):
        self.config = config or SilverOptionsConfig()
        self.order_manager = order_manager
        self.kite = kite_client or getattr(order_manager, "kite", None)
        self.notifier = notifier
        om_mode = getattr(order_manager, "mode", "LIVE")
        if state_file is not None:
            self.state_file = Path(state_file)
        elif om_mode == "PAPER" and not os.getenv("SILVER_STATE_FILE") and self.config.state_file == "data/silver_trades_today.json":
            self.state_file = Path("data/silver_paper_trades_today.json")
        else:
            self.state_file = Path(self.config.state_file)
        self.position: Optional[SilverStranglePosition] = None
        self._entry_timeout_count: int = 0
        self._entry_timeout_date: Optional[str] = None
        self._last_reconcile_ts: float = time.time()
        self._vwap_state: Dict[str, Any] = {}
        self.load_state()

    def load_state(self, today_str: Optional[str] = None) -> Optional[SilverStranglePosition]:
        if not self.state_file.exists():
            self.position = None
            self._vwap_state = {}
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            om_mode = getattr(self.order_manager, "mode", "LIVE")
            if "mode" in data and data["mode"] != om_mode:
                logger.warning("Silver state file mode mismatch (%s != %s). Ignoring.", data["mode"], om_mode)
                self.position = None
                self._vwap_state = {}
                return None
            pos = SilverStranglePosition.from_dict(data)
            today = today_str or datetime.now().strftime("%Y-%m-%d")
            if pos.trade_date != today:
                logger.info(
                    "State file contains prior date (%s != %s). Clearing stale state for fresh session.",
                    pos.trade_date, today
                )
                self.clear_state()
                return None
            self.position = pos
            logger.info("Loaded existing Silver Strangle position for date: %s (Status: %s)", self.position.trade_date, self.position.status)
            return self.position
        except Exception as e:
            logger.warning("Could not read Silver state file %s: %s", self.state_file, e)
            self.position = None
            self._vwap_state = {}
            return None

    def save_state(self):
        if self.position is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.position.to_dict(), f, indent=2)
            tmp_file.replace(self.state_file)
        except Exception as e:
            logger.error("Failed to save Silver state to %s: %s", self.state_file, e)

    def clear_state(self):
        if self.state_file.exists():
            try:
                self.state_file.unlink()
                logger.info("Cleared state file %s", self.state_file)
            except Exception as e:
                logger.error("Failed to remove state file %s: %s", self.state_file, e)
        self.position = None
        self._vwap_state = {}

    def _save_skipped_state(self, now: datetime, reason: str, spot: float = 0.0):
        """Persists a SKIPPED terminal state for today (e.g. DTE gate or VWAP cutoff) without alerting an error."""
        today_str = now.strftime("%Y-%m-%d")
        logger.info("Silver Strangle Entry SKIPPED (%s): %s", today_str, reason)
        self.position = SilverStranglePosition(
            trade_date=today_str,
            underlying_spot=spot,
            lots=self.config.lots,
            multiplier=self.config.multiplier,
            max_loss_cap=self.config.max_portfolio_loss,
            ce_leg=None,
            pe_leg=None,
            status="SKIPPED",
            mode=getattr(self.order_manager, "mode", "LIVE"),
            created_at=now.isoformat(),
            closed_at=now.isoformat(),
            abort_reason=reason,
        )
        self.save_state()

    def _save_aborted_state(self, now: datetime, reason: str, spot: float = 0.0):
        """Persists an ABORTED terminal state for today so restarts never retry entry."""
        today_str = now.strftime("%Y-%m-%d")
        logger.error("Silver Strangle Entry ABORTED (%s): %s", today_str, reason)
        self.position = SilverStranglePosition(
            trade_date=today_str,
            underlying_spot=spot,
            lots=self.config.lots,
            multiplier=self.config.multiplier,
            max_loss_cap=self.config.max_portfolio_loss,
            ce_leg=None,
            pe_leg=None,
            status="ABORTED",
            mode=getattr(self.order_manager, "mode", "LIVE"),
            created_at=now.isoformat(),
            closed_at=now.isoformat(),
            abort_reason=reason,
        )
        self.save_state()
        if self.notifier:
            try:
                self.notifier.send_message(
                    f"🚨 <b>MCX SILVERM ENTRY ABORTED</b>\n"
                    f"• <b>Date</b>: {today_str}\n"
                    f"• <b>Reason</b>: {html.escape(reason)}\n"
                    f"• Entry halted for today to prevent duplicate/unhedged orders."
                )
            except Exception as e:
                logger.error("Failed to send Telegram abort alert: %s", e)

    def _has_open_broker_silver_positions(self, ignore_symbols: Optional[set] = None) -> Optional[str]:
        """
        Checks Kite net MCX positions for any already-open SILVERM option legs.
        Returns a descriptive reason string if open positions exist or broker check fails,
        or None if safe to enter. Symbols in `ignore_symbols` (e.g. active Directional Buyer legs)
        are excluded from conflict detection.
        """
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode != "LIVE" or self.kite is None:
            return None

        try:
            raw_pos = self.kite.positions()
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                try:
                    self.kite = ensure_kite_session(self.kite, force_login=False)
                    self.order_manager.kite = self.kite
                    raw_pos = self.kite.positions()
                except Exception as retry_e:
                    return f"Failed to verify existing MCX broker positions after token refresh: {retry_e}"
            else:
                return f"Failed to verify existing MCX broker positions before entry: {e}"

        ignored = ignore_symbols or set()
        net_positions = raw_pos.get("net", []) if isinstance(raw_pos, dict) else []
        open_silver_opts = [
            p for p in net_positions
            if p.get("exchange") == self.config.exchange
            and str(p.get("tradingsymbol", "")).startswith(self.config.underlying)
            and str(p.get("tradingsymbol", "")).endswith(("CE", "PE"))
            and str(p.get("tradingsymbol", "")) not in ignored
            and int(p.get("quantity") or 0) != 0
        ]
        if open_silver_opts:
            summary = ", ".join(f"{p.get('tradingsymbol')} (qty={p.get('quantity')})" for p in open_silver_opts)
            return f"Existing open MCX {self.config.underlying} option position(s) found on broker: {summary}"
        return None

    def _sync_kite(self, kite_client: Optional[KiteConnect]):
        """Keeps Desk and OrderManager synchronized on the latest KiteConnect session."""
        if kite_client is not None:
            self.kite = kite_client
            self.order_manager.kite = kite_client

    def _ensure_active_kite(self):
        """Refreshes expired MCX Kite session if needed and syncs Desk + OrderManager."""
        if self.kite is None:
            return
        try:
            from kite.client import ensure_kite_session
            refreshed = ensure_kite_session(self.kite, force_login=False)
            self._sync_kite(refreshed)
        except Exception as e:
            logger.warning("Silver session validation/refresh skipped (%s)", e)

    def reconcile_with_broker(self, now: Optional[datetime] = None) -> None:
        """
        Reconciles OPEN Silver Strangle legs against Kite net MCX positions.
        If a tracked leg was manually closed on Kite (`quantity == 0`), marks it CLOSED (`BROKER_RECONCILED`)
        so monitor_cycle() does not fire duplicate BUY orders and flip net LONG.
        """
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode != "LIVE" or self.kite is None or self.position is None or self.position.status != "OPEN":
            return
        if self.position.ce_leg is None or self.position.pe_leg is None:
            return
        now = now or datetime.now()
        try:
            raw_pos = self.kite.positions()
        except Exception as e:
            from kite.client import is_token_error
            if is_token_error(e):
                self._ensure_active_kite()
                try:
                    raw_pos = self.kite.positions()
                except Exception as retry_e:
                    logger.warning("Silver reconciliation retry failed: %s", retry_e)
                    return
            else:
                logger.warning("Silver broker reconciliation failed: %s", e)
                return

        net_positions = raw_pos.get("net", []) if isinstance(raw_pos, dict) else []
        net_map = {
            p.get("tradingsymbol", ""): p
            for p in net_positions
            if p.get("exchange") == self.config.exchange
        }
        changed = False
        for leg in (self.position.ce_leg, self.position.pe_leg):
            if leg.status != "OPEN":
                continue
            pos_row = net_map.get(leg.symbol, {})
            if int(pos_row.get("quantity") or 0) == 0:
                broker_buy_px = float(pos_row.get("buy_price") or 0.0)
                leg.exit_price = broker_buy_px if broker_buy_px > 0 else float(leg.current_price or leg.entry_price)
                leg.exit_filled_lots = self.config.lots
                leg.exit_time = now.strftime("%H:%M:%S")
                leg.exit_reason = "BROKER_RECONCILED"
                leg.status = "BROKER_RECONCILED"
                changed = True
                logger.warning("Silver Reconciliation: %s leg %s is flat on MCX. Marked CLOSED (@ %.2f).", leg.option_type, leg.symbol, leg.exit_price)

        if self.position.is_fully_closed():
            self._finalize_close(now, reason="BROKER_RECONCILED")
        elif changed:
            self.save_state()

    def _wait_for_orders(
        self,
        order_ids: list,
        timeout_seconds: int = 20,
        poll_seconds: int = 1,
    ) -> Dict[str, Dict[str, Any]]:
        """Polls submitted order IDs until terminal status or timeout_seconds."""
        clean_ids = [oid for oid in order_ids if oid]
        if not clean_ids:
            return {}
        terminal_statuses = {"COMPLETE", "REJECTED", "CANCELLED"}
        results: Dict[str, Dict[str, Any]] = {}
        start_ts = time.time()

        while True:
            all_terminal = True
            for oid in clean_ids:
                if oid not in results or results[oid].get("status") not in terminal_statuses:
                    results[oid] = self.order_manager.get_order_status(oid)
                if results[oid].get("status") not in terminal_statuses:
                    all_terminal = False

            if all_terminal:
                return results
            elapsed = time.time() - start_ts
            if elapsed >= timeout_seconds:
                return results
            time.sleep(min(poll_seconds, max(0.2, timeout_seconds - elapsed)))

    def _wait_for_entry_orders(
        self,
        ce_id: Optional[str],
        pe_id: Optional[str],
        timeout_seconds: int = 20,
        poll_seconds: int = 1,
    ) -> Dict[str, Dict[str, Any]]:
        """Polls both entry orders until terminal status or timeout_seconds."""
        return self._wait_for_orders([ce_id, pe_id], timeout_seconds=timeout_seconds, poll_seconds=poll_seconds)

    def _execute_live_buy_with_escalation(
        self,
        symbol: str,
        qty_needed: int,
        ltp: float,
        best_ask: Optional[float] = None,
        allow_market_escalation: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes a LIVE MCX BUY order and verifies fill completion.
        If the initial LIMIT order does not fill within 10s, cancels remaining open qty
        and (when allow_market_escalation=True) escalates the unfilled balance via MARKET order.
        """
        initial_order = self.order_manager.buy(
            symbol=symbol,
            quantity=qty_needed,
            price=ltp,
            order_type=self.config.order_type,
            buffer_pct=self.config.limit_buffer_pct,
            best_ask=best_ask,
            max_slippage_pct=self.config.max_limit_slippage_pct,
        )
        self._sync_kite(getattr(self.order_manager, "kite", None))
        oid = initial_order.get("order_id")
        if not oid:
            return {"filled_qty": 0, "avg_price": ltp, "last_order_id": None}

        statuses = self._wait_for_orders([oid], timeout_seconds=10, poll_seconds=1)
        st = statuses.get(oid, {})
        filled_qty = int(st.get("filled_quantity") or 0)
        avg_px = float(st.get("average_price") or 0.0) or ltp
        last_oid = oid

        if st.get("status") == "COMPLETE" and filled_qty >= qty_needed:
            return {"filled_qty": filled_qty, "avg_price": avg_px, "last_order_id": last_oid}

        # Cancel any still-open remainder before computing exact balance to re-submit
        if st.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED"):
            self.order_manager.cancel_order(oid)
            post_cancel = self._wait_for_orders([oid], timeout_seconds=5, poll_seconds=1).get(oid, {})
            filled_qty = int(post_cancel.get("filled_quantity") or filled_qty)
            if float(post_cancel.get("average_price") or 0.0) > 0:
                avg_px = float(post_cancel["average_price"])

        remaining = max(0, qty_needed - filled_qty)
        if remaining == 0:
            return {"filled_qty": filled_qty, "avg_price": avg_px, "last_order_id": last_oid}

        if allow_market_escalation and self.config.order_type.upper() != "MARKET":
            logger.warning(
                "Square-off LIMIT order %s for %s filled %d/%d. Escalating %d lot(s) via MARKET...",
                oid, symbol, filled_qty, qty_needed, remaining,
            )
            esc_order = self.order_manager.buy(
                symbol=symbol,
                quantity=remaining,
                price=ltp,
                order_type="MARKET",
            )
            self._sync_kite(getattr(self.order_manager, "kite", None))
            esc_id = esc_order.get("order_id")
            if esc_id:
                last_oid = esc_id
                esc_st = self._wait_for_orders([esc_id], timeout_seconds=10, poll_seconds=1).get(esc_id, {})
                esc_filled = int(esc_st.get("filled_quantity") or 0)
                esc_px = float(esc_st.get("average_price") or 0.0) or ltp
                if esc_filled > 0:
                    total_qty = filled_qty + esc_filled
                    avg_px = round(((filled_qty * avg_px) + (esc_filled * esc_px)) / total_qty, 2)
                    filled_qty = total_qty

        return {"filled_qty": filled_qty, "avg_price": avg_px, "last_order_id": last_oid}

    def _rollback_submitted_leg(self, order_info: Optional[Dict[str, Any]], symbol: str, ref_ltp: float) -> bool:
        """Cancels pending entry order, waits for cancel settlement, and squares off any filled qty."""
        if not isinstance(order_info, dict):
            return True
        order_id = order_info.get("order_id")
        if not order_id:
            return True
        mode = getattr(self.order_manager, "mode", "PAPER")
        try:
            self.order_manager.cancel_order(order_id)
            if mode == "LIVE":
                status_info = self._wait_for_orders([order_id], timeout_seconds=5, poll_seconds=1).get(order_id, {})
            else:
                status_info = self.order_manager.get_order_status(order_id)
            filled_qty = int(status_info.get("filled_quantity") or 0)
            if filled_qty > 0:
                logger.warning("Rolling back filled leg %s (%d lots) via BUY square-off...", symbol, filled_qty)
                if mode == "LIVE":
                    res = self._execute_live_buy_with_escalation(symbol, filled_qty, ref_ltp)
                    if res["filled_qty"] < filled_qty:
                        logger.error(
                            "CRITICAL: Rollback for %s only filled %d/%d lots!",
                            symbol, res["filled_qty"], filled_qty,
                        )
                        return False
                else:
                    self.order_manager.buy(
                        symbol=symbol,
                        quantity=filled_qty,
                        price=ref_ltp,
                        order_type=self.config.order_type,
                        buffer_pct=self.config.limit_buffer_pct,
                    )
            elif mode == "LIVE" and status_info.get("status") not in ("CANCELLED", "REJECTED", "COMPLETE"):
                logger.error("CRITICAL: Rollback cancel for %s (%s) left non-terminal status=%s", symbol, order_id, status_info.get("status"))
                return False
            return True
        except Exception as clean_err:
            logger.error("CRITICAL: Failed to roll back %s leg (%s): %s", symbol, order_id, clean_err)
            return False

    def _evaluate_vwap_entry(self, now: datetime, strikes_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Tracks a 20:00-anchored running VWAP of combined option premium (CE + PE)
        on locked strikes and returns refreshed strike/quote info once `Comb < VWAP`
        is observed (requiring >= 2 samples). Returns None while waiting.
        """
        today_str = now.strftime("%Y-%m-%d")
        curr_time = now.strftime("%H:%M")

        if self._vwap_state.get("date") != today_str:
            self._vwap_state = {
                "date": today_str,
                "strikes": dict(strikes_info),
                "cum_pv": 0.0,
                "cum_vol": 0.0,
                "prev_ce_vol": None,
                "prev_pe_vol": None,
                "samples": 0,
            }

        state = self._vwap_state
        locked = state["strikes"]
        ce_sym = locked["ce_symbol"]
        pe_sym = locked["pe_symbol"]
        ce_ltp = float(locked.get("ce_ltp") or 0.0)
        pe_ltp = float(locked.get("pe_ltp") or 0.0)
        ce_bid = locked.get("ce_bid")
        pe_bid = locked.get("pe_bid")
        ce_ask = locked.get("ce_ask")
        pe_ask = locked.get("pe_ask")
        ce_vol_tot = None
        pe_vol_tot = None

        # On subsequent cycles, refresh quotes for the locked strikes
        quote_ok = True
        if state["samples"] > 0 and self.kite is not None:
            try:
                ce_key = f"{self.config.exchange}:{ce_sym}"
                pe_key = f"{self.config.exchange}:{pe_sym}"
                try:
                    quotes = self.kite.quote([ce_key, pe_key]) or {}
                except Exception as q_err:
                    from kite.client import is_token_error, ensure_kite_session
                    if is_token_error(q_err):
                        refreshed = ensure_kite_session(self.kite, force_login=False)
                        self._sync_kite(refreshed)
                        quotes = self.kite.quote([ce_key, pe_key]) or {}
                    else:
                        raise
                if ce_key in quotes and quotes[ce_key].get("last_price"):
                    ce_ltp = float(quotes[ce_key]["last_price"])
                    ce_vol_tot = quotes[ce_key].get("volume") or quotes[ce_key].get("volume_traded")
                    depth_ce = quotes[ce_key].get("depth") or {}
                    b = depth_ce.get("buy") or []
                    s = depth_ce.get("sell") or []
                    ce_bid = float(b[0]["price"]) if (b and b[0].get("price")) else None
                    ce_ask = float(s[0]["price"]) if (s and s[0].get("price")) else None
                if pe_key in quotes and quotes[pe_key].get("last_price"):
                    pe_ltp = float(quotes[pe_key]["last_price"])
                    pe_vol_tot = quotes[pe_key].get("volume") or quotes[pe_key].get("volume_traded")
                    depth_pe = quotes[pe_key].get("depth") or {}
                    b = depth_pe.get("buy") or []
                    s = depth_pe.get("sell") or []
                    pe_bid = float(b[0]["price"]) if (b and b[0].get("price")) else None
                    pe_ask = float(s[0]["price"]) if (s and s[0].get("price")) else None
                locked["ce_ltp"] = ce_ltp
                locked["pe_ltp"] = pe_ltp
                locked["ce_bid"] = ce_bid
                locked["pe_bid"] = pe_bid
                locked["ce_ask"] = ce_ask
                locked["pe_ask"] = pe_ask
            except Exception as e:
                quote_ok = False
                logger.warning("VWAP tracker quote refresh failed (%s); skipping sample update.", e)

        if not quote_ok:
            if curr_time >= self.config.vwap_entry_cutoff:
                self._save_skipped_state(
                    now,
                    f"VWAP quote unavailable through cutoff {self.config.vwap_entry_cutoff}",
                    spot=float(locked.get("spot_price") or 0.0),
                )
            return None

        if ce_ltp <= 0 or pe_ltp <= 0:
            return None

        comb_ltp = ce_ltp + pe_ltp
        delta_vol = 0.0
        if ce_vol_tot is not None and state["prev_ce_vol"] is not None:
            delta_vol += max(0.0, float(ce_vol_tot) - float(state["prev_ce_vol"]))
        if pe_vol_tot is not None and state["prev_pe_vol"] is not None:
            delta_vol += max(0.0, float(pe_vol_tot) - float(state["prev_pe_vol"]))
        if ce_vol_tot is not None:
            state["prev_ce_vol"] = float(ce_vol_tot)
        if pe_vol_tot is not None:
            state["prev_pe_vol"] = float(pe_vol_tot)

        weight = delta_vol if delta_vol > 0 else 1.0
        state["cum_pv"] += comb_ltp * weight
        state["cum_vol"] += weight
        state["samples"] += 1
        comb_vwap = state["cum_pv"] / state["cum_vol"]

        if state["samples"] >= 2 and comb_ltp < comb_vwap:
            spread_ok = True
            max_spread = self.config.max_bid_ask_spread_pct
            if max_spread > 0:
                for leg_lbl, b_px, a_px in (("CE", ce_bid, ce_ask), ("PE", pe_bid, pe_ask)):
                    if b_px is not None and a_px is not None and b_px > 0 and a_px >= b_px:
                        mid = (a_px + b_px) / 2.0
                        spread_pct = (a_px - b_px) / mid if mid > 0 else 0.0
                        if spread_pct > max_spread:
                            logger.warning(
                                "VWAP trigger delayed (%s): %s spread %.1f%% > max %.1f%% (bid=%.2f, ask=%.2f)",
                                curr_time, leg_lbl, spread_pct * 100, max_spread * 100, b_px, a_px,
                            )
                            spread_ok = False
                            break
            if spread_ok:
                logger.info(
                    "Silver Combined Premium VWAP trigger met (%s): Comb=Rs %.2f < VWAP=Rs %.2f (samples=%d)",
                    curr_time, comb_ltp, comb_vwap, state["samples"],
                )
                return locked

        if curr_time >= self.config.vwap_entry_cutoff:
            self._save_skipped_state(
                now,
                f"Combined premium ({comb_ltp:.2f}) stayed >= VWAP ({comb_vwap:.2f}) through cutoff {self.config.vwap_entry_cutoff}",
                spot=float(locked.get("spot_price") or 0.0),
            )
            return None

        logger.info(
            "Waiting for Silver Comb < VWAP (%s): Comb=Rs %.2f vs VWAP=Rs %.2f (samples=%d, cutoff=%s)",
            curr_time, comb_ltp, comb_vwap, state["samples"], self.config.vwap_entry_cutoff,
        )
        return None

    def enter_strangle(self, now: Optional[datetime] = None, ignore_symbols: Optional[set] = None) -> bool:
        """
        Executes atomic entry into the evening short strangle at 20:00 IST (or on Comb < VWAP).
        Sells OTM CE and OTM PE legs concurrently, verifies both fills, rolls back
        any partial/unhedged leg on failure, and persists ABORTED state so restarts never duplicate orders.
        """
        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        if self.position and self.position.trade_date == today_str:
            if self.position.status == "OPEN":
                logger.info("Silver Strangle already active today (%s). Resuming existing position.", today_str)
                return True
            logger.info("Silver Strangle for today (%s) is already %s. Skipping entry.", today_str, self.position.status)
            return False

        if now.strftime("%H:%M") >= self.config.exit_time:
            self._save_skipped_state(
                now,
                f"Current time ({now.strftime('%H:%M')}) is at/after exit_time={self.config.exit_time}",
            )
            return False

        mode = getattr(self.order_manager, "mode", "PAPER")

        # Guard: Never enter if open SILVERM positions already exist on Zerodha
        broker_conflict = self._has_open_broker_silver_positions(ignore_symbols=ignore_symbols)
        if broker_conflict:
            self._save_aborted_state(now, broker_conflict)
            return False

        logger.info("Executing %s IST Entry check for %s (%d lots = %.1f kg)...", self.config.entry_time, self.config.underlying, self.config.lots, self.config.multiplier)

        # 1. Resolve Strikes (using locked VWAP strikes if already waiting today)
        vwap_locked = (
            self.config.vwap_entry_enabled
            and self._vwap_state.get("date") == today_str
            and self._vwap_state.get("strikes")
        )
        if vwap_locked:
            strikes_info = self._vwap_state["strikes"]
        else:
            try:
                strikes_info = find_silver_strangle_strikes(
                    kite=self.kite,
                    underlying=self.config.underlying,
                    strike_otm_pct=self.config.strike_otm_pct,
                    exchange=self.config.exchange,
                    allow_fallback=(mode != "LIVE"),
                    min_oi=self.config.min_option_oi,
                    max_spread_pct=self.config.max_bid_ask_spread_pct,
                    scan_steps=self.config.strike_scan_steps,
                    skip_0dte=self.config.skip_0dte,
                    strike_mode=self.config.strike_mode,
                    atr_multiplier=self.config.atr_multiplier,
                    atr_period=self.config.atr_period,
                    atr_min_otm_pct=self.config.atr_min_otm_pct,
                    exclude_symbols=ignore_symbols,
                )
            except Exception as e:
                self._save_aborted_state(now, f"Strike resolution failed: {e}")
                return False

        # Optional DTE gate (e.g. skip >10 DTE when theta/spread ratio is unfavorable)
        dte_days = int(strikes_info.get("dte_days") or 0)
        if self.config.max_dte_days > 0 and dte_days > self.config.max_dte_days:
            self._save_skipped_state(
                now,
                f"DTE {dte_days} exceeds configured max_dte_days={self.config.max_dte_days}",
                spot=float(strikes_info.get("spot_price") or 0.0),
            )
            return False

        # Optional 20:00-anchored Combined Premium VWAP confirmation
        if self.config.vwap_entry_enabled:
            confirmed = self._evaluate_vwap_entry(now, strikes_info)
            if confirmed is None:
                return False
            strikes_info = confirmed

        ce_sym = strikes_info["ce_symbol"]
        pe_sym = strikes_info["pe_symbol"]
        ce_ltp = strikes_info["ce_ltp"]
        pe_ltp = strikes_info["pe_ltp"]
        ce_bid = strikes_info.get("ce_bid")
        pe_bid = strikes_info.get("pe_bid")
        spot = strikes_info["spot_price"]

        if ce_ltp <= 0.5 or pe_ltp <= 0.5:
            self._save_aborted_state(now, f"Invalid option LTP (CE={ce_ltp:.2f}, PE={pe_ltp:.2f})", spot=spot)
            return False

        # 2. Execute CE and PE SELL orders concurrently (anchored to live best bid when available)
        def _place_leg(sym: str, ltp: float, best_bid: Optional[float]) -> Dict[str, Any]:
            return self.order_manager.sell(
                symbol=sym,
                quantity=self.config.lots,
                price=ltp,
                order_type=self.config.order_type,
                buffer_pct=self.config.limit_buffer_pct,
                best_bid=best_bid,
                max_slippage_pct=self.config.max_limit_slippage_pct,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_ce = executor.submit(_place_leg, ce_sym, ce_ltp, ce_bid)
            future_pe = executor.submit(_place_leg, pe_sym, pe_ltp, pe_bid)

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

        self._sync_kite(getattr(self.order_manager, "kite", None))

        if ce_err or pe_err:
            if not ce_err and ce_order:
                self._rollback_submitted_leg(ce_order, ce_sym, ce_ltp)
            if not pe_err and pe_order:
                self._rollback_submitted_leg(pe_order, pe_sym, pe_ltp)
            err_msg = f"Entry order placement failed (CE err={ce_err}, PE err={pe_err})"
            self._save_aborted_state(now, err_msg, spot=spot)
            return False

        ce_id = ce_order.get("order_id") if isinstance(ce_order, dict) else None
        pe_id = pe_order.get("order_id") if isinstance(pe_order, dict) else None

        # 3. Verify both orders actually COMPLETE in LIVE mode (catch async RMS/OI rejections)
        ce_fill = float(ce_order.get("fill_price", ce_ltp))
        pe_fill = float(pe_order.get("fill_price", pe_ltp))

        if mode == "LIVE":
            statuses = self._wait_for_entry_orders(ce_id, pe_id, timeout_seconds=20, poll_seconds=1)
            ce_st = statuses.get(ce_id, {})
            pe_st = statuses.get(pe_id, {})
            ce_filled = int(ce_st.get("filled_quantity") or 0)
            pe_filled = int(pe_st.get("filled_quantity") or 0)
            ce_ok = ce_st.get("status") == "COMPLETE" and ce_filled == self.config.lots
            pe_ok = pe_st.get("status") == "COMPLETE" and pe_filled == self.config.lots

            if not (ce_ok and pe_ok):
                rb_ce = self._rollback_submitted_leg(ce_order, ce_sym, ce_ltp)
                rb_pe = self._rollback_submitted_leg(pe_order, pe_sym, pe_ltp)
                reason = (
                    f"Entry verification failed: CE({ce_sym}: {ce_st.get('status')}, "
                    f"filled={ce_filled}/{self.config.lots}, msg={ce_st.get('status_message')}); "
                    f"PE({pe_sym}: {pe_st.get('status')}, "
                    f"filled={pe_filled}/{self.config.lots}, msg={pe_st.get('status_message')})"
                )
                is_pure_timeout = (
                    ce_filled == 0
                    and pe_filled == 0
                    and ce_st.get("status") != "REJECTED"
                    and pe_st.get("status") != "REJECTED"
                    and rb_ce
                    and rb_pe
                )
                if self._entry_timeout_date != today_str:
                    self._entry_timeout_date = today_str
                    self._entry_timeout_count = 0
                if is_pure_timeout and self._entry_timeout_count < 1:
                    self._entry_timeout_count += 1
                    logger.warning("Entry LIMIT orders timed out with 0 fills (attempt %d). Will retry next cycle: %s", self._entry_timeout_count, reason)
                    return False

                self._save_aborted_state(now, reason, spot=spot)
                return False

            if float(ce_st.get("average_price") or 0) > 0:
                ce_fill = float(ce_st["average_price"])
            if float(pe_st.get("average_price") or 0) > 0:
                pe_fill = float(pe_st["average_price"])

        # 4. Calculate SL and effective portfolio max loss cap
        ce_sl = round(ce_fill * (1.0 + self.config.sl_pct), 2)
        pe_sl = round(pe_fill * (1.0 + self.config.sl_pct), 2)
        max_loss_cap = self.config.effective_max_portfolio_loss(ce_fill + pe_fill)

        entry_time_str = now.strftime("%H:%M:%S")

        ce_leg = SilverStrangleLeg(
            symbol=ce_sym,
            strike=strikes_info["ce_strike"],
            option_type="CE",
            entry_price=ce_fill,
            entry_time=entry_time_str,
            stop_loss_price=ce_sl,
            current_price=ce_fill,
            order_id=ce_id,
        )

        pe_leg = SilverStrangleLeg(
            symbol=pe_sym,
            strike=strikes_info["pe_strike"],
            option_type="PE",
            entry_price=pe_fill,
            entry_time=entry_time_str,
            stop_loss_price=pe_sl,
            current_price=pe_fill,
            order_id=pe_id,
        )

        self.position = SilverStranglePosition(
            trade_date=today_str,
            underlying_spot=spot,
            lots=self.config.lots,
            multiplier=self.config.multiplier,
            max_loss_cap=max_loss_cap,
            ce_leg=ce_leg,
            pe_leg=pe_leg,
            status="OPEN",
            mode=mode,
            created_at=now.isoformat(),
        )

        self.save_state()

        logger.info(
            "Silver Strangle Entry Complete! CE: %s @ Rs %.2f (SL: Rs %.2f) | PE: %s @ Rs %.2f (SL: Rs %.2f) | Max Risk: Rs %.2f",
            ce_sym, ce_fill, ce_sl, pe_sym, pe_fill, pe_sl, max_loss_cap
        )

        if self.notifier:
            try:
                msg = (
                    f"🥈 <b>MCX SILVERM STRANGLE ENTERED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Lots</b>: {self.config.lots} ({self.config.multiplier:.0f} kg)\n"
                    f"• <b>Spot</b>: ₹{spot:,.1f}\n"
                    f"• <b>Call</b>: {ce_sym} @ ₹{ce_fill:,.1f} (SL: ₹{ce_sl:,.1f})\n"
                    f"• <b>Put</b>: {pe_sym} @ ₹{pe_fill:,.1f} (SL: ₹{pe_sl:,.1f})\n"
                    f"• <b>Combined Premium</b>: ₹{ce_fill + pe_fill:,.1f}\n"
                    f"• <b>Max Risk Cap</b>: ₹{max_loss_cap:,.2f} (Calculated for {self.config.lots} lots)\n"
                    f"• <b>Exit Time</b>: {self.config.exit_time} IST"
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error("Failed to send Telegram entry alert: %s", e)

        return True

    def monitor_cycle(self, now: Optional[datetime] = None) -> bool:
        """
        Executes a single monitoring evaluation pass:
        - Updates LTPs
        - Enforces single-leg 40% SL and trails surviving leg to cost
        - Checks target decay (70%)
        - Enforces portfolio max loss cap
        - Enforces mandatory 23:15 IST EOD exit
        Returns True if position is still active, False if fully closed.
        """
        if not self.position or self.position.status != "OPEN":
            return False
        if self.position.ce_leg is None or self.position.pe_leg is None:
            return False

        now = now or datetime.now()
        curr_time_str = now.strftime("%H:%M")

        # Periodic LIVE broker reconciliation every >= 60s so manual exits aren't double-bought
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode == "LIVE" and self.kite is not None and (time.time() - self._last_reconcile_ts) >= 60.0:
            self.reconcile_with_broker(now=now)
            self._last_reconcile_ts = time.time()
            if not self.position or self.position.status != "OPEN":
                return False

        # 1. Update live quotes
        ce_sym = self.position.ce_leg.symbol
        pe_sym = self.position.pe_leg.symbol

        ce_ltp = self.position.ce_leg.current_price
        pe_ltp = self.position.pe_leg.current_price
        ce_ask: Optional[float] = None
        pe_ask: Optional[float] = None

        if self.kite is not None:
            try:
                q_keys = [f"{self.config.exchange}:{ce_sym}", f"{self.config.exchange}:{pe_sym}"]
                try:
                    quotes = self.kite.quote(q_keys)
                except Exception as q_err:
                    from kite.client import is_token_error, ensure_kite_session
                    if is_token_error(q_err):
                        logger.warning("Token error during monitor quote (%s). Refreshing session...", q_err)
                        refreshed = ensure_kite_session(self.kite, force_login=False)
                        self._sync_kite(refreshed)
                        quotes = self.kite.quote(q_keys)
                    else:
                        raise
                ce_key = f"{self.config.exchange}:{ce_sym}"
                pe_key = f"{self.config.exchange}:{pe_sym}"
                if ce_key in quotes and quotes[ce_key].get("last_price"):
                    ce_ltp = float(quotes[ce_key]["last_price"])
                    ce_sell_depth = (quotes[ce_key].get("depth") or {}).get("sell") or []
                    if ce_sell_depth and ce_sell_depth[0].get("price"):
                        ce_ask = float(ce_sell_depth[0]["price"])
                if pe_key in quotes and quotes[pe_key].get("last_price"):
                    pe_ltp = float(quotes[pe_key]["last_price"])
                    pe_sell_depth = (quotes[pe_key].get("depth") or {}).get("sell") or []
                    if pe_sell_depth and pe_sell_depth[0].get("price"):
                        pe_ask = float(pe_sell_depth[0]["price"])
            except Exception as e:
                logger.warning("Failed to refresh quotes during monitor cycle: %s", e)

        if self.position.ce_leg.status == "OPEN":
            self.position.ce_leg.current_price = ce_ltp
        if self.position.pe_leg.status == "OPEN":
            self.position.pe_leg.current_price = pe_ltp

        # 2. Check EOD Mandatory Square-Off
        if curr_time_str >= self.config.exit_time:
            logger.info("%s IST EOD Cutoff reached. Squaring off all open legs.", self.config.exit_time)
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="EOD", best_ask=ce_ask)
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="EOD", best_ask=pe_ask)
            if self.position.is_fully_closed():
                self._finalize_close(now, reason="EOD")
                return False
            self.save_state()
            return True

        # 3. Check Portfolio Max Loss Cap (derived from number of lots)
        current_pnl = self.position.total_pnl
        if current_pnl <= -abs(self.position.max_loss_cap):
            logger.warning("Portfolio max loss limit breached (P&L: Rs %.2f <= -Rs %.2f). Emergency square-off!", current_pnl, self.position.max_loss_cap)
            self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="MAX_PORTFOLIO_LOSS", best_ask=ce_ask)
            self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="MAX_PORTFOLIO_LOSS", best_ask=pe_ask)
            if self.position.is_fully_closed():
                self._finalize_close(now, reason="MAX_PORTFOLIO_LOSS")
                return False
            self.save_state()
            return True

        # 4. Resume any incomplete partial exits FIRST, then check Single-Leg Stop Losses
        ce_stopped = False
        pe_stopped = False

        if self.position.ce_leg.status == "OPEN" and 0 < self.position.ce_leg.exit_filled_lots < self.config.lots:
            resume_reason = self.position.ce_leg.exit_reason or "SL"
            logger.warning("Resuming incomplete CE partial exit (%d/%d lots filled, reason=%s).", self.position.ce_leg.exit_filled_lots, self.config.lots, resume_reason)
            ce_stopped = self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason=resume_reason, best_ask=ce_ask)
            if ce_stopped and resume_reason == "SL" and not self.config.single_leg_exit and self.position.pe_leg.status == "OPEN":
                self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="SL_CO_EXIT", best_ask=pe_ask)

        if self.position.pe_leg.status == "OPEN" and 0 < self.position.pe_leg.exit_filled_lots < self.config.lots:
            resume_reason = self.position.pe_leg.exit_reason or "SL"
            logger.warning("Resuming incomplete PE partial exit (%d/%d lots filled, reason=%s).", self.position.pe_leg.exit_filled_lots, self.config.lots, resume_reason)
            pe_stopped = self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason=resume_reason, best_ask=pe_ask)
            if pe_stopped and resume_reason == "SL" and not self.config.single_leg_exit and self.position.ce_leg.status == "OPEN":
                self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="SL_CO_EXIT", best_ask=ce_ask)

        if self.position.ce_leg.status == "OPEN" and ce_ltp >= self.position.ce_leg.stop_loss_price:
            logger.warning("CE Leg hit Stop Loss (LTP: Rs %.2f >= SL: Rs %.2f)", ce_ltp, self.position.ce_leg.stop_loss_price)
            ce_stopped = self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="SL", best_ask=ce_ask)
            if ce_stopped and not self.config.single_leg_exit and self.position.pe_leg.status == "OPEN":
                self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="SL_CO_EXIT", best_ask=pe_ask)

        if self.position.pe_leg.status == "OPEN" and pe_ltp >= self.position.pe_leg.stop_loss_price:
            logger.warning("PE Leg hit Stop Loss (LTP: Rs %.2f >= SL: Rs %.2f)", pe_ltp, self.position.pe_leg.stop_loss_price)
            pe_stopped = self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="SL", best_ask=pe_ask)
            if pe_stopped and not self.config.single_leg_exit and self.position.ce_leg.status == "OPEN":
                self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="SL_CO_EXIT", best_ask=ce_ask)

        # 5. Trail to Cost Logic (arms pending trail, only locks cost once surviving leg is < entry_price)
        if self.config.trail_to_cost and self.config.single_leg_exit:
            if ce_stopped and self.position.pe_leg.status == "OPEN":
                self.position.pe_leg.cost_trail_pending = True

            if pe_stopped and self.position.ce_leg.status == "OPEN":
                self.position.ce_leg.cost_trail_pending = True

            if self.position.ce_leg.status == "OPEN" and self.position.pe_leg.status == "OPEN":
                ce_decay = (self.position.ce_leg.entry_price - ce_ltp) / self.position.ce_leg.entry_price
                pe_decay = (self.position.pe_leg.entry_price - pe_ltp) / self.position.pe_leg.entry_price
                if ce_decay >= self.config.trail_activation_decay_pct:
                    self.position.pe_leg.cost_trail_pending = True
                if pe_decay >= self.config.trail_activation_decay_pct:
                    self.position.ce_leg.cost_trail_pending = True

            for leg in (self.position.ce_leg, self.position.pe_leg):
                if leg.status == "OPEN" and leg.cost_trail_pending and leg.stop_loss_price > leg.entry_price:
                    if leg.current_price < leg.entry_price:
                        old_sl = leg.stop_loss_price
                        leg.stop_loss_price = leg.entry_price
                        leg.cost_trail_pending = False
                        logger.info("Trailed %s SL to cost: Rs %.2f -> Rs %.2f", leg.option_type, old_sl, leg.stop_loss_price)

        # 6. Check Target Decay (70%) — both combined when dual-open AND single surviving leg when one closed
        if self.position.ce_leg.status == "OPEN" and self.position.pe_leg.status == "OPEN":
            total_entry = self.position.ce_leg.entry_price + self.position.pe_leg.entry_price
            total_curr = ce_ltp + pe_ltp
            combined_decay = (total_entry - total_curr) / total_entry
            if combined_decay >= self.config.target_decay_pct:
                logger.info("Target Decay achieved (%.1f%% >= %.1f%%). Taking profit on both legs!", combined_decay * 100, self.config.target_decay_pct * 100)
                self._square_off_leg(self.position.ce_leg, ce_ltp, now, reason="TARGET_DECAY", best_ask=ce_ask)
                self._square_off_leg(self.position.pe_leg, pe_ltp, now, reason="TARGET_DECAY", best_ask=pe_ask)
                if self.position.is_fully_closed():
                    self._finalize_close(now, reason="TARGET_DECAY")
                    return False
                self.save_state()
                return True
        else:
            for leg, ltp_val, ask_val in (
                (self.position.ce_leg, ce_ltp, ce_ask),
                (self.position.pe_leg, pe_ltp, pe_ask),
            ):
                if leg.status == "OPEN" and leg.entry_price > 0:
                    single_decay = (leg.entry_price - ltp_val) / leg.entry_price
                    if single_decay >= self.config.target_decay_pct:
                        logger.info(
                            "Surviving %s leg hit Target Decay (%.1f%% >= %.1f%%). Taking profit!",
                            leg.option_type, single_decay * 100, self.config.target_decay_pct * 100,
                        )
                        self._square_off_leg(leg, ltp_val, now, reason="TARGET_DECAY", best_ask=ask_val)

        if self.position.is_fully_closed():
            self._finalize_close(now, reason="BOTH_LEGS_CLOSED")
            return False

        self.save_state()
        return True

    def _square_off_leg(
        self,
        leg: SilverStrangleLeg,
        ltp: float,
        now: datetime,
        reason: str,
        best_ask: Optional[float] = None,
    ) -> bool:
        if leg.status != "OPEN":
            return True
        remaining_lots = max(0, self.config.lots - leg.exit_filled_lots)
        if remaining_lots == 0:
            leg.exit_time = leg.exit_time or now.strftime("%H:%M:%S")
            leg.exit_reason = reason
            leg.status = reason
            return True

        logger.info("Squaring off %s leg %s x %d/%d @ Rs %.2f (Reason: %s)", leg.option_type, leg.symbol, remaining_lots, self.config.lots, ltp, reason)
        mode = getattr(self.order_manager, "mode", "PAPER")

        try:
            if mode == "LIVE":
                res = self._execute_live_buy_with_escalation(
                    leg.symbol,
                    remaining_lots,
                    ltp,
                    best_ask=best_ask,
                    allow_market_escalation=(reason != "TARGET_DECAY"),
                )
                new_filled = int(res["filled_qty"] or 0)
                if new_filled > 0:
                    prev_qty = leg.exit_filled_lots
                    prev_px = float(leg.exit_price or 0.0)
                    total_qty = prev_qty + new_filled
                    leg.exit_price = round(((prev_qty * prev_px) + (new_filled * float(res["avg_price"]))) / total_qty, 2)
                    leg.exit_filled_lots = total_qty
                    leg.exit_order_id = res["last_order_id"]

                if leg.exit_filled_lots < self.config.lots:
                    if leg.exit_filled_lots > 0:
                        leg.exit_reason = leg.exit_reason or reason
                    err_msg = (
                        f"Square-off for {leg.symbol} ({reason}) filled {leg.exit_filled_lots}/{self.config.lots} lots; "
                        f"keeping leg OPEN to retry {self.config.lots - leg.exit_filled_lots} remaining lot(s)!"
                    )
                    logger.error("CRITICAL: %s", err_msg)
                    if self.notifier:
                        try:
                            self.notifier.send_message(f"🚨 <b>MCX SILVERM SQUARE-OFF UNFILLED</b>\n• {html.escape(err_msg)}")
                        except Exception:
                            pass
                    return False
                fill_price = float(leg.exit_price)
            else:
                buy_order = self.order_manager.buy(
                    symbol=leg.symbol,
                    quantity=remaining_lots,
                    price=ltp,
                    order_type=self.config.order_type,
                    buffer_pct=self.config.limit_buffer_pct,
                    best_ask=best_ask,
                    max_slippage_pct=self.config.max_limit_slippage_pct,
                )
                prev_qty = leg.exit_filled_lots
                prev_px = float(leg.exit_price or 0.0)
                new_fill_px = float(buy_order.get("fill_price", ltp))
                total_qty = prev_qty + remaining_lots
                fill_price = round(((prev_qty * prev_px) + (remaining_lots * new_fill_px)) / total_qty, 2) if total_qty > 0 else new_fill_px
                leg.exit_price = fill_price
                leg.exit_filled_lots = self.config.lots
                leg.exit_order_id = buy_order.get("order_id")
        except Exception as e:
            logger.error("CRITICAL: Square-off order error for %s (%s): %s", leg.symbol, reason, e)
            if self.notifier:
                try:
                    self.notifier.send_message(
                        f"🚨 <b>MCX SILVERM SQUARE-OFF ERROR</b>\n• {leg.symbol} ({reason}): {html.escape(str(e))}"
                    )
                except Exception:
                    pass
            return False

        leg.exit_time = now.strftime("%H:%M:%S")
        leg.exit_reason = reason
        leg.status = reason

        if self.notifier:
            try:
                pts = leg.points_captured
                pnl = leg.pnl(self.config.multiplier)
                icon = "🟢" if pts >= 0 else "🔴"
                msg = (
                    f"{icon} <b>MCX SILVERM LEG SQUARED OFF</b>\n"
                    f"• <b>Symbol</b>: {leg.symbol} ({leg.option_type})\n"
                    f"• <b>Exit Price</b>: ₹{fill_price:,.1f} (Entry: ₹{leg.entry_price:,.1f})\n"
                    f"• <b>Points</b>: {pts:+,.1f} pts\n"
                    f"• <b>P&L</b>: ₹{pnl:+,.2f}\n"
                    f"• <b>Reason</b>: {reason}"
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error("Failed to send Telegram leg square-off alert: %s", e)
        return True

    def _finalize_close(self, now: datetime, reason: str):
        if not self.position:
            return
        self.position.status = "CLOSED"
        self.position.closed_at = now.isoformat()
        self.save_state()

        pts = self.position.total_points
        pnl = self.position.total_pnl
        est_costs = self.position.total_estimated_costs
        est_net = self.position.estimated_net_pnl
        icon = "🎉" if est_net >= 0 else "⚠️"
        logger.info(
            "Silver Strangle Session Finalized! Combined Points: %+.2f pts | Gross P&L: Rs %+.2f | Est. Charges: Rs %.2f | Est. Net P&L: Rs %+.2f (Reason: %s)",
            pts, pnl, est_costs, est_net, reason,
        )

        if self.notifier:
            try:
                msg = (
                    f"{icon} <b>MCX SILVERM DAILY SESSION COMPLETED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Date</b>: {self.position.trade_date}\n"
                    f"• <b>Lots</b>: {self.config.lots} ({self.config.multiplier:.0f} kg)\n"
                    f"• <b>Call Result</b>: {self.position.ce_leg.points_captured:+,.1f} pts ({self.position.ce_leg.exit_reason})\n"
                    f"• <b>Put Result</b>: {self.position.pe_leg.points_captured:+,.1f} pts ({self.position.pe_leg.exit_reason})\n"
                    f"• <b>Total Points</b>: {pts:+,.1f} pts\n"
                    f"• <b>Realized Gross P&L</b>: ₹{pnl:+,.2f}\n"
                    f"• <b>Est. Net P&L (after taxes)</b>: ₹{est_net:+,.2f} (~₹{est_costs:,.2f} charges)\n"
                    f"• <b>Final Reason</b>: {reason}"
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error("Failed to send Telegram session completion alert: %s", e)

    def run_session(self, poll_interval: Optional[int] = None, execute_once: bool = False, now: Optional[datetime] = None):
        """Runs the active evening trading session until completion."""
        poll_secs = poll_interval or self.config.poll_seconds
        logger.info("Starting Silver Strangle session execution loop (Poll: %ds)...", poll_secs)

        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        curr_time = now.strftime("%H:%M")

        if self.position and self.position.trade_date != today_str:
            logger.info("Clearing prior day position (%s != %s) before session start.", self.position.trade_date, today_str)
            self.clear_state()

        # Enter only before exit cutoff if not yet entered today
        if self.config.entry_time <= curr_time < self.config.exit_time:
            if not self.position or self.position.trade_date != today_str:
                attempt_now = now
                while True:
                    if self.enter_strangle(attempt_now):
                        break
                    if execute_once or (self.position and self.position.status in ("CLOSED", "ABORTED", "SKIPPED")):
                        return
                    if not self.config.vwap_entry_enabled:
                        return
                    attempt_now = datetime.now()
                    if attempt_now.strftime("%H:%M") >= self.config.exit_time:
                        return
                    time.sleep(poll_secs)
            elif self.position.status in ("CLOSED", "ABORTED", "SKIPPED"):
                logger.info("Silver Strangle for %s is already %s. Skipping session.", today_str, self.position.status)
                return
        elif not self.position or self.position.trade_date != today_str:
            logger.info("Current time %s is outside active entry window [%s, %s).", curr_time, self.config.entry_time, self.config.exit_time)
            return

        loop_now = now
        while True:
            if not execute_once:
                loop_now = datetime.now()
            is_active = self.monitor_cycle(loop_now)
            if not is_active or execute_once:
                break
            time.sleep(poll_secs)

    def run_scheduler(self, ignore_holidays: bool = False):
        """
        Runs the automated 24/7 daemon loop for MCX Silver:
        - Detects MCX holidays and weekends
        - Idles until 20:00 IST
        - Enters and monitors Strangle until 23:15 IST
        - Sleeps overnight until next trading day
        """
        logger.info("=" * 80)
        logger.info("  MCX SILVERM EVENING INTRADAY STRANGLE DAEMON STARTED [%s MODE]", getattr(self.order_manager, "mode", "LIVE"))
        logger.info("  Lots: %d (%d kg) | Entry: %s IST (VWAP<%s: %s) | Exit: %s IST | Strike: %s (ATR x%.2f) | MaxDTE: %d | PortSL: %.0f%%",
                    self.config.lots, self.config.multiplier, self.config.entry_time, self.config.vwap_entry_cutoff,
                    self.config.vwap_entry_enabled, self.config.exit_time, self.config.strike_mode,
                    self.config.atr_multiplier, self.config.max_dte_days, self.config.portfolio_sl_pct * 100)
        logger.info("=" * 80)

        last_session_refresh_date: Optional[str] = None
        while True:
            try:
                now = datetime.now()
                today = now.date()
                today_str = now.strftime("%Y-%m-%d")

                # 0. Daily Date Boundary Check: Clear prior day's state from memory & file
                if self.position and self.position.trade_date != today_str:
                    logger.info("Clearing state from prior date (%s != %s). Starting fresh session for today.", self.position.trade_date, today_str)
                    self.clear_state()

                # 1. Holiday & Weekend Check for MCX Evening Session
                if not ignore_holidays:
                    is_holiday, reason = is_mcx_trading_holiday(today, session="EVENING")
                    if is_holiday:
                        logger.info("MCX Evening Session is closed today (%s). Sleeping for 30 minutes...", reason)
                        time.sleep(1800)
                        continue

                curr_time = now.strftime("%H:%M")

                # Pre-entry daily session validation/renewal at >= 19:45 IST (once per trading day)
                if getattr(self.order_manager, "mode", "PAPER") == "LIVE" and curr_time >= "19:45" and last_session_refresh_date != today_str:
                    self._ensure_active_kite()
                    last_session_refresh_date = today_str

                # 2. Before 20:00 IST: Idle and wait
                if curr_time < self.config.entry_time:
                    time.sleep(30)
                    continue

                # 3. Active Window: [entry_time, exit_time) IST (never open fresh positions at/after cutoff)
                if self.config.entry_time <= curr_time <= self.config.exit_time:
                    if not self.position or self.position.trade_date != today_str:
                        entered = self.enter_strangle(now)
                        if not entered:
                            wait_secs = self.config.poll_seconds if (self.config.vwap_entry_enabled and not self.position) else 60
                            time.sleep(wait_secs)
                            continue
                    if self.position and self.position.trade_date == today_str and self.position.status == "OPEN":
                        while True:
                            active = self.monitor_cycle()
                            if not active:
                                break
                            time.sleep(self.config.poll_seconds)
                    else:
                        # Position already CLOSED, ABORTED, or SKIPPED today, wait until post-market cutoff
                        time.sleep(60)
                        continue

                # 4. Post 23:15 IST: Force square-off if an OPEN position survived a late restart before clearing
                elif curr_time > self.config.exit_time:
                    if self.position and self.position.trade_date == today_str and self.position.status == "OPEN":
                        logger.warning(
                            "Post-%s IST restart found still-OPEN position (%s). Executing mandatory EOD square-off!",
                            self.config.exit_time, today_str,
                        )
                        self.monitor_cycle(now)
                        if self.position and self.position.status == "OPEN":
                            time.sleep(self.config.poll_seconds)
                            continue
                    if self.position or self.state_file.exists():
                        logger.info("Post-%s IST session ended (%s). Clearing today's state in memory & file for tomorrow.", self.config.exit_time, today)
                        self.clear_state()
                    else:
                        logger.info("MCX evening trading concluded for today (%s). Sleeping until tomorrow...", today)
                    time.sleep(1800)
            except Exception as e:
                logger.exception("Unhandled error in Silver Strangle scheduler loop: %s. Retrying in 60s...", e)
                time.sleep(60)
