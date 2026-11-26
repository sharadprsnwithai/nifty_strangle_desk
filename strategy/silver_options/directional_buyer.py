import html
import json
import logging
import os
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from kiteconnect import KiteConnect

from notifications.telegram import TelegramNotifier
from strategy.holidays import is_mcx_trading_holiday
from strategy.silver_options.chain import find_silver_atm_option, resolve_silver_context
from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.desk import SilverStrangleDesk
from strategy.silver_options.models import SilverDirectionalPosition
from strategy.silver_options.order_manager import SilverOrderManager

logger = logging.getLogger("strategy.silver.buyer")


def evaluate_vwap_volume_signal(
    bars: List[Dict[str, Any]],
    vol_mult: float = 1.3,
    max_vwap_dist_pct: float = 0.012,
    ema_fast: int = 9,
    ema_slow: int = 21,
    vol_window: int = 12,
) -> Optional[Dict[str, Any]]:
    """
    Pure-Python evaluation of intraday 5m VWAP + EMA(9/21) + Volume Surge signal
    (identical to backtest Combo 2A, requiring zero numpy/pandas in runtime Docker image).

    Returns a dict `{"direction": "CE"|"PE", "close": ..., "vwap": ..., ...}` or None.
    """
    if not bars:
        return None

    cum_pv = 0.0
    cum_v = 0.0
    k_fast = 2.0 / (ema_fast + 1.0)
    k_slow = 2.0 / (ema_slow + 1.0)
    e_fast: Optional[float] = None
    e_slow: Optional[float] = None
    vols: List[float] = []

    latest_c = 0.0
    latest_vwap = 0.0
    latest_vol = 0.0
    latest_vma = 0.0

    for bar in bars:
        h = float(bar.get("high") or 0.0)
        l = float(bar.get("low") or 0.0)
        c = float(bar.get("close") or 0.0)
        raw_v = float(bar.get("volume") or 0.0)
        if c <= 0 or h <= 0 or l <= 0:
            continue

        typ = (h + l + c) / 3.0
        v_wt = raw_v if raw_v > 0 else 1.0
        cum_pv += typ * v_wt
        cum_v += v_wt
        latest_vwap = cum_pv / cum_v if cum_v > 0 else c

        e_fast = c if e_fast is None else (c * k_fast + e_fast * (1.0 - k_fast))
        e_slow = c if e_slow is None else (c * k_slow + e_slow * (1.0 - k_slow))

        vols.append(raw_v)
        window_slice = vols[-vol_window:]
        latest_vma = sum(window_slice) / len(window_slice)
        latest_c = c
        latest_vol = raw_v

    if latest_c <= 0 or latest_vwap <= 0 or e_fast is None or e_slow is None or latest_vma <= 0:
        return None

    dist_up = (latest_c - latest_vwap) / latest_vwap
    dist_dn = (latest_vwap - latest_c) / latest_vwap

    direction: Optional[str] = None
    if latest_c > latest_vwap and e_fast > e_slow and latest_vol > vol_mult * latest_vma and dist_up < max_vwap_dist_pct:
        direction = "CE"
    elif latest_c < latest_vwap and e_fast < e_slow and latest_vol > vol_mult * latest_vma and dist_dn < max_vwap_dist_pct:
        direction = "PE"

    if direction is None:
        return None

    return {
        "direction": direction,
        "close": latest_c,
        "vwap": round(latest_vwap, 2),
        "ema9": round(e_fast, 2),
        "ema21": round(e_slow, 2),
        "volume": latest_vol,
        "vol_ma": round(latest_vma, 2),
    }


class SilverDirectionalBuyer:
    """
    18:00-20:00 IST Directional Long ATM Option Buyer for MCX SILVERM (Combo 2A leg 1).
    Gates entries by `(DTE <= buyer_max_dte_days) OR (Daily ATR% >= buyer_min_atr_pct)`.
    Enforces hard rupee loss cap + 30% SL + 25% breakeven trail + 60% target + 23:10 EOD exit.
    """

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
        elif om_mode == "PAPER" and not os.getenv("SILVER_BUYER_STATE_FILE") and self.config.buyer_state_file == "data/silver_buyer_trades_today.json":
            self.state_file = Path("data/silver_buyer_paper_trades_today.json")
        else:
            self.state_file = Path(self.config.buyer_state_file)

        self.position: Optional[SilverDirectionalPosition] = None
        self._daily_ctx: Optional[Dict[str, Any]] = None
        self._daily_ctx_date: Optional[str] = None
        self._last_signal_poll_ts: float = 0.0
        self._last_reconcile_ts: float = time.time()
        self.load_state()

    def _sync_kite(self, kite_client: Optional[KiteConnect]):
        if kite_client is not None:
            self.kite = kite_client
            self.order_manager.kite = kite_client

    def _ensure_active_kite(self, force_login: bool = False):
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode != "LIVE":
            return
        try:
            from kite.client import ensure_kite_session
            refreshed = ensure_kite_session(self.kite, force_login=force_login)
            self._sync_kite(refreshed)
        except Exception as e:
            logger.error("Failed to ensure active Kite session for Silver Buyer: %s", e)

    @staticmethod
    def _to_naive_ist(dt_val: Any) -> Optional[datetime]:
        if isinstance(dt_val, str):
            try:
                dt_val = datetime.fromisoformat(dt_val)
            except ValueError:
                return None
        if not isinstance(dt_val, datetime):
            return None
        if dt_val.tzinfo is not None:
            ist = timezone(timedelta(hours=5, minutes=30))
            return dt_val.astimezone(ist).replace(tzinfo=None)
        return dt_val

    def load_state(self, today_str: Optional[str] = None) -> Optional[SilverDirectionalPosition]:
        if not self.state_file.exists():
            self.position = None
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            om_mode = getattr(self.order_manager, "mode", "LIVE")
            if "mode" in data and data["mode"] != om_mode:
                logger.warning("Buyer state file mode mismatch (%s != %s). Ignoring.", data["mode"], om_mode)
                self.position = None
                return None
            pos = SilverDirectionalPosition.from_dict(data)
            today = today_str or datetime.now().strftime("%Y-%m-%d")
            if pos.trade_date != today:
                self.clear_state()
                return None
            self.position = pos
            return self.position
        except Exception as e:
            logger.warning("Could not read Buyer state file %s: %s", self.state_file, e)
            self.position = None
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
            logger.error("Failed to save Buyer state to %s: %s", self.state_file, e)

    def clear_state(self):
        if self.state_file.exists():
            try:
                self.state_file.unlink()
            except Exception as e:
                logger.error("Failed to remove Buyer state file %s: %s", self.state_file, e)
        self.position = None
        self._daily_ctx = None
        self._daily_ctx_date = None

    def _save_terminal_state(self, now: datetime, status: str, reason: str, spot: float = 0.0):
        today_str = now.strftime("%Y-%m-%d")
        self.position = SilverDirectionalPosition(
            trade_date=today_str,
            underlying_spot=spot,
            lots=self.config.lots,
            multiplier=self.config.multiplier,
            max_loss_cap=self.config.buyer_max_loss_cap,
            status=status,
            mode=getattr(self.order_manager, "mode", "LIVE"),
            abort_reason=reason,
            created_at=now.isoformat(),
            closed_at=now.isoformat(),
        )
        self.save_state()

    def _wait_for_order(self, order_id: Optional[str], timeout_seconds: int = 15, poll_seconds: int = 1) -> Dict[str, Any]:
        if not order_id:
            return {}
        terminal = {"COMPLETE", "REJECTED", "CANCELLED"}
        start = time.time()
        st: Dict[str, Any] = {}
        while True:
            st = self.order_manager.get_order_status(order_id)
            if st.get("status") in terminal or (time.time() - start) >= timeout_seconds:
                return st
            time.sleep(min(poll_seconds, max(0.2, timeout_seconds - (time.time() - start))))

    def _has_open_broker_conflict(self, ignore_symbols: Optional[set] = None) -> Optional[str]:
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode != "LIVE" or self.kite is None:
            return None
        try:
            raw_pos = self.kite.positions()
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                try:
                    self._sync_kite(ensure_kite_session(self.kite, force_login=False))
                    raw_pos = self.kite.positions()
                except Exception as retry_e:
                    return f"Failed to verify MCX positions before Buyer entry: {retry_e}"
            else:
                return f"Failed to verify MCX positions before Buyer entry: {e}"
        ignored = ignore_symbols or set()
        net = raw_pos.get("net", []) if isinstance(raw_pos, dict) else []
        conflicts = [
            p for p in net
            if p.get("exchange") == self.config.exchange
            and str(p.get("tradingsymbol", "")).startswith(self.config.underlying)
            and str(p.get("tradingsymbol", "")).endswith(("CE", "PE"))
            and str(p.get("tradingsymbol", "")) not in ignored
            and int(p.get("quantity") or 0) != 0
        ]
        if conflicts:
            summary = ", ".join(f"{p.get('tradingsymbol')} (qty={p.get('quantity')})" for p in conflicts)
            return f"Existing open MCX {self.config.underlying} position(s) on broker: {summary}"
        return None

    def _get_daily_context(self, now: datetime) -> Dict[str, Any]:
        today_str = now.strftime("%Y-%m-%d")
        if self._daily_ctx is not None and self._daily_ctx_date == today_str:
            return self._daily_ctx
        mode = getattr(self.order_manager, "mode", "PAPER")
        ctx = resolve_silver_context(
            kite=self.kite,
            underlying=self.config.underlying,
            exchange=self.config.exchange,
            allow_fallback=(mode != "LIVE"),
            skip_0dte=self.config.skip_0dte,
            atr_period=self.config.atr_period,
            as_of_date=now.date(),
        )
        self._sync_kite(ctx.get("kite"))
        self._daily_ctx = ctx
        self._daily_ctx_date = today_str
        return ctx

    def _fetch_today_5m_bars(self, fut_token: Optional[int], now: datetime) -> List[Dict[str, Any]]:
        if self.kite is None or not fut_token:
            return []
        day_start = datetime.combine(now.date(), dt_time(9, 0))
        try:
            bars = self.kite.historical_data(fut_token, day_start, now, "5minute") or []
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                try:
                    self._sync_kite(ensure_kite_session(self.kite, force_login=False))
                    bars = self.kite.historical_data(fut_token, day_start, now, "5minute") or []
                except Exception as retry_e:
                    logger.warning("Buyer 5m bar retry failed: %s", retry_e)
                    return []
            else:
                logger.warning("Buyer 5m bar fetch failed: %s", e)
                return []

        now_naive = self._to_naive_ist(now) or now
        completed_bars: List[Dict[str, Any]] = []
        for bar in bars:
            bar_dt = self._to_naive_ist(bar.get("date"))
            if bar_dt is None or (bar_dt + timedelta(minutes=5)) <= now_naive:
                completed_bars.append(bar)
        return completed_bars

    def evaluate_entry(
        self,
        now: Optional[datetime] = None,
        bars_5m: Optional[List[Dict[str, Any]]] = None,
        ignore_symbols: Optional[set] = None,
        min_poll_interval_s: float = 15.0,
    ) -> bool:
        """
        Evaluates Combo 2A Directional Buyer entry between `buyer_entry_start` (18:00)
        and `buyer_entry_cutoff` (20:00). Returns True if position opened.
        """
        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        curr_time = now.strftime("%H:%M")

        if self.position and self.position.trade_date == today_str:
            return self.position.status == "OPEN"

        if curr_time < self.config.buyer_entry_start:
            return False
        if curr_time > self.config.buyer_entry_cutoff:
            self._save_terminal_state(now, "SKIPPED", f"No VWAP+Volume Surge signal before {self.config.buyer_entry_cutoff} IST")
            return False

        mode = getattr(self.order_manager, "mode", "PAPER")
        try:
            ctx = self._get_daily_context(now)
        except Exception as e:
            self._save_terminal_state(now, "ABORTED", f"Context resolution failed: {e}")
            return False

        dte_days = int(ctx.get("dte_days") or 0)
        atr_pct = ctx.get("atr_pct")
        dte_ok = self.config.buyer_max_dte_days <= 0 or dte_days <= self.config.buyer_max_dte_days
        atr_ok = atr_pct is not None and float(atr_pct) >= self.config.buyer_min_atr_pct
        if not (dte_ok or atr_ok):
            self._save_terminal_state(
                now,
                "SKIPPED",
                f"Combo 2A gate skipped (DTE {dte_days} > {self.config.buyer_max_dte_days} and ATR% {atr_pct} < {self.config.buyer_min_atr_pct}%)",
                spot=float(ctx.get("spot_price") or 0.0),
            )
            return False

        if bars_5m is None:
            now_ts = time.time()
            if (now_ts - self._last_signal_poll_ts) < min_poll_interval_s:
                return False
            self._last_signal_poll_ts = now_ts
            bars_5m = self._fetch_today_5m_bars(ctx.get("fut_token"), now)

        sig = evaluate_vwap_volume_signal(
            bars_5m,
            vol_mult=self.config.buyer_vol_mult,
            max_vwap_dist_pct=self.config.buyer_max_vwap_dist_pct,
        )
        if sig is None:
            return False

        broker_conflict = self._has_open_broker_conflict(ignore_symbols=ignore_symbols)
        if broker_conflict:
            self._save_terminal_state(now, "ABORTED", broker_conflict, spot=float(sig["close"]))
            return False

        try:
            opt_info = find_silver_atm_option(
                kite=self.kite,
                option_type=sig["direction"],
                underlying=self.config.underlying,
                exchange=self.config.exchange,
                allow_fallback=(mode != "LIVE"),
                min_oi=self.config.min_option_oi,
                max_spread_pct=self.config.max_bid_ask_spread_pct,
                scan_steps=self.config.strike_scan_steps,
                skip_0dte=self.config.skip_0dte,
                atr_period=self.config.atr_period,
                context=ctx,
            )
        except Exception as e:
            logger.warning("Directional Buyer ATM option resolution failed (%s): %s", sig["direction"], e)
            return False

        sym = opt_info["symbol"]
        ltp = float(opt_info["ltp"])
        ask = opt_info.get("ask")
        spot = float(opt_info["spot_price"])
        if ltp <= 20.0:
            logger.info("Skipping Buyer entry: ATM %s premium Rs %.2f <= 20.0", sym, ltp)
            return False

        try:
            buy_order = self.order_manager.buy(
                symbol=sym,
                quantity=self.config.lots,
                price=ltp,
                order_type=self.config.order_type,
                buffer_pct=self.config.limit_buffer_pct,
                best_ask=ask,
                max_slippage_pct=self.config.max_limit_slippage_pct,
            )
            self._sync_kite(getattr(self.order_manager, "kite", None))
        except Exception as e:
            self._save_terminal_state(now, "ABORTED", f"Buyer order failed for {sym}: {e}", spot=spot)
            return False

        oid = buy_order.get("order_id") if isinstance(buy_order, dict) else None
        fill_px = float(buy_order.get("fill_price", ltp))
        filled_lots = self.config.lots

        if mode == "LIVE":
            st = self._wait_for_order(oid, timeout_seconds=15, poll_seconds=1)
            filled_lots = int(st.get("filled_quantity") or 0)
            final_status = str(st.get("status") or "").upper()
            if final_status != "COMPLETE" or filled_lots <= 0:
                if oid and final_status not in ("COMPLETE", "REJECTED", "CANCELLED"):
                    cancel_ok = self.order_manager.cancel_order(oid)
                    post_c = self._wait_for_order(oid, timeout_seconds=5, poll_seconds=1)
                    filled_lots = max(filled_lots, int(post_c.get("filled_quantity") or 0))
                    if float(post_c.get("average_price") or 0.0) > 0:
                        fill_px = float(post_c["average_price"])
                    if post_c.get("status"):
                        final_status = str(post_c["status"]).upper()
                    if not cancel_ok and filled_lots <= 0 and final_status not in ("COMPLETE", "REJECTED", "CANCELLED"):
                        logger.error("CRITICAL: Failed to cancel unfilled Buyer BUY order %s (status=%s).", oid, final_status)
                if filled_lots <= 0:
                    logger.warning("Buyer LIMIT order %s for %s unfilled (status=%s).", oid, sym, final_status)
                    if final_status != "CANCELLED":
                        self._save_terminal_state(
                            now,
                            "ABORTED",
                            f"Buyer order ended without clean cancellation (order_id={oid}, status={final_status}): {st.get('status_message')}",
                            spot=spot,
                        )
                    return False
            elif float(st.get("average_price") or 0.0) > 0:
                fill_px = float(st["average_price"])

        mult = float(filled_lots * self.config.lot_size_kg)
        max_loss_cap = round(self.config.buyer_sl_per_lot * filled_lots, 2)
        pct_sl = fill_px * (1.0 - self.config.buyer_sl_pct) if self.config.buyer_sl_pct > 0 else 0.0
        hard_floor = fill_px - (max_loss_cap / mult) if (max_loss_cap > 0 and mult > 0) else 0.0
        init_sl = round(max(0.5, pct_sl, hard_floor), 2)
        tgt_px = round(fill_px * (1.0 + self.config.buyer_target_pct), 2)

        self.position = SilverDirectionalPosition(
            trade_date=today_str,
            underlying_spot=spot,
            symbol=sym,
            strike=float(opt_info["strike"]),
            option_type=sig["direction"],
            lots=filled_lots,
            multiplier=mult,
            entry_price=fill_px,
            entry_time=now.strftime("%H:%M:%S"),
            stop_loss_price=init_sl,
            initial_sl_price=init_sl,
            target_price=tgt_px,
            current_price=fill_px,
            max_loss_cap=max_loss_cap,
            status="OPEN",
            mode=mode,
            order_id=oid,
            created_at=now.isoformat(),
        )
        self.save_state()

        logger.info(
            "Silver Directional BUY Entered! %s (%s) x %d @ Rs %.2f | SL: Rs %.2f | Target: Rs %.2f | Cap: Rs %.2f | Signal: VWAP=%.1f",
            sym, sig["direction"], filled_lots, fill_px, init_sl, tgt_px, max_loss_cap, sig["vwap"],
        )
        if self.notifier:
            try:
                self.notifier.send_message(
                    f"⚡ <b>MCX SILVERM DIRECTIONAL BUY ({sig['direction']})</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Contract</b>: {sym} x {filled_lots} ({mult:.0f} kg)\n"
                    f"• <b>Spot / VWAP</b>: ₹{spot:,.1f} / ₹{sig['vwap']:,.1f}\n"
                    f"• <b>Entry</b>: ₹{fill_px:,.1f}\n"
                    f"• <b>SL / Max Cap</b>: ₹{init_sl:,.1f} (Cap: ₹{max_loss_cap:,.0f})\n"
                    f"• <b>Target (+{self.config.buyer_target_pct*100:.0f}%)</b>: ₹{tgt_px:,.1f}"
                )
            except Exception as e:
                logger.error("Failed to send Telegram Buyer entry alert: %s", e)
        return True

    def reconcile_with_broker(self, now: Optional[datetime] = None) -> None:
        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode != "LIVE" or self.kite is None or self.position is None or self.position.status != "OPEN":
            return
        now = now or datetime.now()
        try:
            raw_pos = self.kite.positions()
        except Exception as e:
            from kite.client import is_token_error, ensure_kite_session
            if is_token_error(e):
                try:
                    self._sync_kite(ensure_kite_session(self.kite, force_login=False))
                    raw_pos = self.kite.positions()
                except Exception:
                    return
            else:
                return
        net = raw_pos.get("net", []) if isinstance(raw_pos, dict) else []
        pos_row = next(
            (p for p in net if p.get("exchange") == self.config.exchange and p.get("tradingsymbol") == self.position.symbol),
            {},
        )
        if int(pos_row.get("quantity") or 0) == 0:
            sell_px = float(pos_row.get("sell_price") or 0.0)
            self.position.exit_price = sell_px if sell_px > 0 else float(self.position.current_price or self.position.entry_price)
            self.position.exit_filled_lots = self.position.lots
            self.position.exit_time = now.strftime("%H:%M:%S")
            self.position.exit_reason = "BROKER_RECONCILED"
            self.position.status = "BROKER_RECONCILED"
            self.position.closed_at = now.isoformat()
            self.save_state()
            logger.warning("Buyer Reconciliation: %s is flat on MCX. Marked CLOSED (@ %.2f).", self.position.symbol, self.position.exit_price)

    def _execute_live_sell_with_escalation(
        self,
        symbol: str,
        qty_needed: int,
        ltp: float,
        best_bid: Optional[float] = None,
        allow_market_escalation: bool = True,
    ) -> Dict[str, Any]:
        initial = self.order_manager.sell(
            symbol=symbol,
            quantity=qty_needed,
            price=ltp,
            order_type=self.config.order_type,
            buffer_pct=self.config.limit_buffer_pct,
            best_bid=best_bid,
            max_slippage_pct=self.config.max_limit_slippage_pct,
        )
        self._sync_kite(getattr(self.order_manager, "kite", None))
        oid = initial.get("order_id")
        if not oid:
            return {"filled_qty": 0, "avg_price": ltp, "last_order_id": None}

        st = self._wait_for_order(oid, timeout_seconds=10, poll_seconds=1)
        filled_qty = int(st.get("filled_quantity") or 0)
        avg_px = float(st.get("average_price") or 0.0) or ltp
        last_oid = oid
        if st.get("status") == "COMPLETE" and filled_qty >= qty_needed:
            return {"filled_qty": filled_qty, "avg_price": avg_px, "last_order_id": last_oid}

        if st.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED"):
            self.order_manager.cancel_order(oid)
            post_c = self._wait_for_order(oid, timeout_seconds=5, poll_seconds=1)
            filled_qty = int(post_c.get("filled_quantity") or filled_qty)
            if float(post_c.get("average_price") or 0.0) > 0:
                avg_px = float(post_c["average_price"])

        remaining = max(0, qty_needed - filled_qty)
        if remaining > 0 and allow_market_escalation and self.config.order_type.upper() != "MARKET":
            esc = self.order_manager.sell(symbol=symbol, quantity=remaining, price=ltp, order_type="MARKET")
            self._sync_kite(getattr(self.order_manager, "kite", None))
            esc_id = esc.get("order_id")
            if esc_id:
                last_oid = esc_id
                esc_st = self._wait_for_order(esc_id, timeout_seconds=10, poll_seconds=1)
                esc_filled = int(esc_st.get("filled_quantity") or 0)
                esc_px = float(esc_st.get("average_price") or 0.0) or ltp
                if esc_filled > 0:
                    total = filled_qty + esc_filled
                    avg_px = round(((filled_qty * avg_px) + (esc_filled * esc_px)) / total, 2)
                    filled_qty = total
        return {"filled_qty": filled_qty, "avg_price": avg_px, "last_order_id": last_oid}

    def _square_off(self, ltp: float, now: datetime, reason: str, best_bid: Optional[float] = None) -> bool:
        pos = self.position
        if not pos or pos.status != "OPEN":
            return True
        remaining = max(0, pos.lots - pos.exit_filled_lots)
        if remaining == 0:
            pos.status = reason
            pos.exit_reason = reason
            pos.exit_time = pos.exit_time or now.strftime("%H:%M:%S")
            pos.closed_at = now.isoformat()
            self.save_state()
            return True

        mode = getattr(self.order_manager, "mode", "PAPER")
        try:
            if mode == "LIVE":
                res = self._execute_live_sell_with_escalation(
                    pos.symbol, remaining, ltp, best_bid=best_bid, allow_market_escalation=(reason != "TARGET")
                )
                new_filled = int(res["filled_qty"] or 0)
                if new_filled > 0:
                    prev_q = pos.exit_filled_lots
                    prev_p = float(pos.exit_price or 0.0)
                    tot_q = prev_q + new_filled
                    pos.exit_price = round(((prev_q * prev_p) + (new_filled * float(res["avg_price"]))) / tot_q, 2)
                    pos.exit_filled_lots = tot_q
                    pos.exit_order_id = res["last_order_id"]
                if pos.exit_filled_lots < pos.lots:
                    if pos.exit_filled_lots > 0:
                        pos.exit_reason = pos.exit_reason or reason
                    self.save_state()
                    return False
            else:
                sell_order = self.order_manager.sell(
                    symbol=pos.symbol,
                    quantity=remaining,
                    price=ltp,
                    order_type=self.config.order_type,
                    buffer_pct=self.config.limit_buffer_pct,
                    best_bid=best_bid,
                    max_slippage_pct=self.config.max_limit_slippage_pct,
                )
                prev_q = pos.exit_filled_lots
                prev_p = float(pos.exit_price or 0.0)
                fill_px = float(sell_order.get("fill_price", ltp))
                tot_q = prev_q + remaining
                pos.exit_price = round(((prev_q * prev_p) + (remaining * fill_px)) / tot_q, 2) if tot_q > 0 else fill_px
                pos.exit_filled_lots = pos.lots
                pos.exit_order_id = sell_order.get("order_id")
        except Exception as e:
            logger.error("CRITICAL: Buyer square-off error for %s (%s): %s", pos.symbol, reason, e)
            return False

        pos.exit_time = now.strftime("%H:%M:%S")
        pos.exit_reason = reason
        pos.status = reason
        pos.closed_at = now.isoformat()
        self.save_state()

        logger.info(
            "Silver Directional BUY Closed (%s): %s | Entry: Rs %.2f -> Exit: Rs %.2f | Points: %+.2f | Est Net: Rs %+.2f",
            reason, pos.symbol, pos.entry_price, pos.exit_price, pos.points_captured, pos.estimated_net_pnl,
        )
        if self.notifier:
            try:
                icon = "🟢" if pos.estimated_net_pnl >= 0 else "🔴"
                self.notifier.send_message(
                    f"{icon} <b>MCX SILVERM DIRECTIONAL BUY CLOSED</b>\n"
                    f"• <b>Contract</b>: {pos.symbol} ({pos.option_type})\n"
                    f"• <b>Exit</b>: ₹{pos.exit_price:,.1f} (Entry: ₹{pos.entry_price:,.1f})\n"
                    f"• <b>Points</b>: {pos.points_captured:+,.1f} pts\n"
                    f"• <b>Est. Net P&amp;L</b>: ₹{pos.estimated_net_pnl:+,.2f}\n"
                    f"• <b>Reason</b>: {html.escape(reason)}"
                )
            except Exception as e:
                logger.error("Failed to send Telegram Buyer close alert: %s", e)
        return True

    def monitor_cycle(self, now: Optional[datetime] = None) -> bool:
        """Evaluates live LTP for open Directional Buyer position. Returns True if still OPEN."""
        if not self.position or self.position.status != "OPEN":
            return False

        now = now or datetime.now()
        curr_time = now.strftime("%H:%M")

        mode = getattr(self.order_manager, "mode", "PAPER")
        if mode == "LIVE" and self.kite is not None and (time.time() - self._last_reconcile_ts) >= 60.0:
            self.reconcile_with_broker(now=now)
            self._last_reconcile_ts = time.time()
            if not self.position or self.position.status != "OPEN":
                return False

        sym = self.position.symbol
        ltp = float(self.position.current_price or self.position.entry_price)
        best_bid: Optional[float] = None

        if self.kite is not None:
            q_key = f"{self.config.exchange}:{sym}"
            try:
                try:
                    quotes = self.kite.quote([q_key]) or {}
                except Exception as q_err:
                    from kite.client import is_token_error, ensure_kite_session
                    if is_token_error(q_err):
                        self._sync_kite(ensure_kite_session(self.kite, force_login=False))
                        quotes = self.kite.quote([q_key]) or {}
                    else:
                        raise
                q = quotes.get(q_key, {})
                if q.get("last_price"):
                    ltp = float(q["last_price"])
                    buy_depth = (q.get("depth") or {}).get("buy") or []
                    if buy_depth and buy_depth[0].get("price"):
                        best_bid = float(buy_depth[0]["price"])
            except Exception as e:
                logger.warning("Failed to refresh Buyer quote for %s: %s", sym, e)

        self.position.current_price = ltp

        if 0 < self.position.exit_filled_lots < self.position.lots:
            resume_reason = self.position.exit_reason or ("EOD" if curr_time >= self.config.exit_time else "SL")
            self._square_off(ltp, now, reason=resume_reason, best_bid=best_bid)
            return self.position.status == "OPEN"

        if curr_time >= self.config.exit_time:
            self._square_off(ltp, now, reason="EOD", best_bid=best_bid)
            return self.position.status == "OPEN"

        if ltp <= self.position.stop_loss_price:
            reason = "MAX_LOSS" if self.position.total_pnl <= -abs(self.position.max_loss_cap) * 0.95 and not self.position.breakeven_armed else "SL"
            self._square_off(ltp, now, reason=reason, best_bid=best_bid)
            return self.position.status == "OPEN"

        if self.position.target_price > 0 and ltp >= self.position.target_price:
            self._square_off(ltp, now, reason="TARGET", best_bid=best_bid)
            return self.position.status == "OPEN"

        if not self.position.breakeven_armed and self.config.buyer_trail_be_pct > 0:
            if ltp >= self.position.entry_price * (1.0 + self.config.buyer_trail_be_pct):
                self.position.stop_loss_price = max(self.position.stop_loss_price, self.position.entry_price)
                self.position.breakeven_armed = True
                logger.info("Directional Buyer BE trail armed for %s: SL -> Rs %.2f", sym, self.position.stop_loss_price)

        self.save_state()
        return True


class SilverBarbellDesk:
    """
    Unified Combo 2A Runner coordinating:
      1) 18:00-20:00 Directional Buyer (`SilverDirectionalBuyer`)
      2) 20:00-23:10 Primary Short Strangle (`SilverStrangleDesk`)
    """

    def __init__(
        self,
        buyer: Optional[SilverDirectionalBuyer],
        strangle: Optional[SilverStrangleDesk],
        config: Optional[SilverOptionsConfig] = None,
    ):
        self.config = config or SilverOptionsConfig()
        self.buyer = buyer
        self.strangle = strangle

    def step(self, now: Optional[datetime] = None, bars_5m: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Executes a single non-blocking evaluation cycle for both legs."""
        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        curr_time = now.strftime("%H:%M")

        buyer_open_syms: set = set()
        if self.buyer is not None:
            if self.buyer.position and self.buyer.position.trade_date != today_str:
                self.buyer.clear_state()
            if self.buyer.position and self.buyer.position.status == "OPEN":
                self.buyer.monitor_cycle(now)
            elif curr_time >= self.config.buyer_entry_start and curr_time < self.config.exit_time:
                if not self.buyer.position or self.buyer.position.trade_date != today_str:
                    strangle_syms = set()
                    if self.strangle and self.strangle.position and self.strangle.position.status == "OPEN":
                        if self.strangle.position.ce_leg:
                            strangle_syms.add(self.strangle.position.ce_leg.symbol)
                        if self.strangle.position.pe_leg:
                            strangle_syms.add(self.strangle.position.pe_leg.symbol)
                    self.buyer.evaluate_entry(now=now, bars_5m=bars_5m, ignore_symbols=strangle_syms)
            if self.buyer.position and self.buyer.position.status == "OPEN" and self.buyer.position.symbol:
                buyer_open_syms.add(self.buyer.position.symbol)

        if self.strangle is not None:
            if self.strangle.position and self.strangle.position.trade_date != today_str:
                self.strangle.clear_state()
            if self.strangle.position and self.strangle.position.status == "OPEN":
                self.strangle.monitor_cycle(now)
            elif self.config.entry_time <= curr_time < self.config.exit_time:
                if not self.strangle.position or self.strangle.position.trade_date != today_str:
                    self.strangle.enter_strangle(now, ignore_symbols=buyer_open_syms)

        buyer_active = bool(self.buyer and self.buyer.position and self.buyer.position.status == "OPEN")
        strangle_active = bool(self.strangle and self.strangle.position and self.strangle.position.status == "OPEN")
        return {"buyer_active": buyer_active, "strangle_active": strangle_active}

    def run_session(self, poll_interval: Optional[int] = None, execute_once: bool = False, now: Optional[datetime] = None):
        poll_secs = poll_interval or self.config.poll_seconds
        loop_now = now or datetime.now()
        while True:
            if not execute_once:
                loop_now = datetime.now()
            curr_time = loop_now.strftime("%H:%M")
            status = self.step(loop_now)
            if execute_once:
                break
            if curr_time > self.config.exit_time and not status["buyer_active"] and not status["strangle_active"]:
                break
            time.sleep(poll_secs)

    def run_scheduler(self, ignore_holidays: bool = False):
        mode = getattr(self.strangle.order_manager if self.strangle else self.buyer.order_manager, "mode", "LIVE")
        logger.info("=" * 80)
        logger.info("  MCX SILVERM COMBO 2A DESK STARTED [%s MODE | STRATEGY: %s]", mode, self.config.strategy_mode.upper())
        logger.info("  Lots: %d (%.0f kg) | Buyer: %s-%s (DTE<=%d OR ATR>=%.1f%%, Cap Rs %.0f, SL %.0f%%, Tgt +%.0f%%)",
                    self.config.lots, self.config.multiplier, self.config.buyer_entry_start, self.config.buyer_entry_cutoff,
                    self.config.buyer_max_dte_days, self.config.buyer_min_atr_pct, self.config.buyer_max_loss_cap,
                    self.config.buyer_sl_pct * 100, self.config.buyer_target_pct * 100)
        logger.info("  Strangle: %s-%s (ATR x%.2f, VWAP<%s: %s, PortSL %.0f%%, MaxDTE %d)",
                    self.config.entry_time, self.config.exit_time, self.config.atr_multiplier,
                    self.config.vwap_entry_cutoff, self.config.vwap_entry_enabled,
                    self.config.portfolio_sl_pct * 100, self.config.max_dte_days)
        logger.info("=" * 80)

        last_refresh_date: Optional[str] = None
        while True:
            try:
                now = datetime.now()
                today = now.date()
                today_str = now.strftime("%Y-%m-%d")

                if self.buyer and self.buyer.position and self.buyer.position.trade_date != today_str:
                    self.buyer.clear_state()
                if self.strangle and self.strangle.position and self.strangle.position.trade_date != today_str:
                    self.strangle.clear_state()

                if not ignore_holidays:
                    is_holiday, reason = is_mcx_trading_holiday(today, session="EVENING")
                    if is_holiday:
                        logger.info("MCX Evening Session is closed today (%s). Sleeping 30m...", reason)
                        time.sleep(1800)
                        continue

                curr_time = now.strftime("%H:%M")
                if mode == "LIVE" and curr_time >= "17:45" and last_refresh_date != today_str:
                    if self.strangle:
                        self.strangle._ensure_active_kite()
                        if self.buyer and self.strangle.kite is not None:
                            self.buyer._sync_kite(self.strangle.kite)
                    elif self.buyer:
                        self.buyer._ensure_active_kite(force_login=True)
                    last_refresh_date = today_str

                earliest_start = self.config.buyer_entry_start if self.buyer else self.config.entry_time
                if curr_time < earliest_start:
                    time.sleep(30)
                    continue

                if earliest_start <= curr_time <= self.config.exit_time:
                    self.step(now)
                    time.sleep(self.config.poll_seconds)
                    continue

                # Post-exit cutoff: ensure any still-open positions square off before clearing
                status = self.step(now)
                if status["buyer_active"] or status["strangle_active"]:
                    time.sleep(self.config.poll_seconds)
                    continue
                if self.buyer and (self.buyer.position or self.buyer.state_file.exists()):
                    self.buyer.clear_state()
                if self.strangle and (self.strangle.position or self.strangle.state_file.exists()):
                    self.strangle.clear_state()
                time.sleep(1800)
            except Exception as e:
                logger.exception("Unhandled error in Silver Combo 2A scheduler loop: %s. Retrying in 60s...", e)
                time.sleep(60)
