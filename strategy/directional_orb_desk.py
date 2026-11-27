"""30m ORB Directional NIFTY Option Selling Desk (100 OTM Naked, 50% SL)."""
import json
import logging
import os
import time as py_time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from kite.client import ensure_kite_session, is_token_error
from notifications.telegram import TelegramNotifier
from strategy.holidays import is_market_open
from strategy.option_chain import NIFTY_INDEX_TOKEN, resolve_target_expiry
from strategy.order_manager import OrderManager

logger = logging.getLogger("strategy.directional_orb")

NIFTY_INDEX_TOKEN = 256265
DEFAULT_LOT_SIZE = 65


def compute_rsi14(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute Wilder 14-period RSI on a price series."""
    s = series.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def compute_mtf_rsi(df_5m: pd.DataFrame, now_dt: Optional[datetime] = None) -> pd.DataFrame:
    """
    Compute completed-candle 5m RSI(14) and 15m RSI(14) on 5m bars using prior days' history.
    Drops any still-forming 5m/15m bar when now_dt is provided.
    """
    df = df_5m.copy()
    ts_col = "date" if "date" in df.columns else ("timestamps" if "timestamps" in df.columns else df.columns[0])
    df["dt"] = pd.to_datetime(df[ts_col])
    if df["dt"].dt.tz is not None:
        df["dt"] = df["dt"].dt.tz_localize(None)

    if now_dt is not None:
        cur_5m_start = now_dt.replace(minute=(now_dt.minute // 5) * 5, second=0, microsecond=0, tzinfo=None)
        df = df[df["dt"] < cur_5m_start]
    if df.empty:
        return df

    df = df.sort_values("dt").reset_index(drop=True)
    df["rsi5"] = compute_rsi14(df["close"], 14)

    # Resample to 15m completed bars (NSE 15m candles align to :15/:30/:45/:00 -> offset='15min' or origin='start_day')
    df_idx = df.set_index("dt")
    df_15m = df_idx.resample("15min").agg({"close": "last"}).dropna().reset_index()
    if now_dt is not None:
        cur_15m_start = now_dt.replace(minute=(now_dt.minute // 15) * 15, second=0, microsecond=0, tzinfo=None)
        df_15m = df_15m[df_15m["dt"] < cur_15m_start]

    if df_15m.empty:
        df["rsi15"] = 50.0
        return df

    df_15m["rsi15"] = compute_rsi14(df_15m["close"], 14)
    # A 15m bar starting at T closes at T + 15min; merge_asof on bar close time avoids lookahead
    df_15m["close_dt"] = df_15m["dt"] + pd.Timedelta(minutes=15)
    df["bar_close_dt"] = df["dt"] + pd.Timedelta(minutes=5)
    df = pd.merge_asof(
        df.sort_values("bar_close_dt"),
        df_15m[["close_dt", "rsi15"]].sort_values("close_dt"),
        left_on="bar_close_dt",
        right_on="close_dt",
        direction="backward",
    )
    df["rsi15"] = df["rsi15"].ffill().fillna(50.0)
    return df


def compute_adx14(df: pd.DataFrame) -> pd.Series:
    """Compute 14-period Wilder ADX on OHLC bars."""
    high, low, close_s = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    prev_close = close_s.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / atr14
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / atr14
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / 14, adjust=False).mean().fillna(0.0)


class DirectionalOrbDesk:
    """
    Intraday NIFTY 50 Directional ORB Option Selling Desk.
    Default strategy: 30m ORB (09:15-09:45) + TWAP + EMA(9/21),
    100 OTM Naked Short Option, 50% SL with 30% decay cost-trailing,
    75% target decay, and 15:15 IST EOD square-off.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        state_file: Optional[Path] = None,
        notifier: Optional[TelegramNotifier] = None,
        lot_size: int = DEFAULT_LOT_SIZE,
    ):
        self.order_manager = order_manager
        self.mode = order_manager.mode
        self.notifier = notifier
        self.lot_size = lot_size
        self._last_checked_bar_start: Optional[datetime] = None
        self._last_reconcile_ts: float = 0.0
        self._last_state_save_ts: float = 0.0

        data_dir = Path("data")
        default_name = (
            "directional_orb_paper_state.json"
            if self.mode == "PAPER"
            else "directional_orb_state.json"
        )
        self.state_file = Path(state_file) if state_file else (data_dir / default_name)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = self._load_or_init_state()

    def _empty_state(self, today_str: str) -> Dict[str, Any]:
        return {
            "date": today_str,
            "mode": self.mode,
            "status": "IDLE",  # IDLE -> OPEN -> CLOSED
            "direction": None,
            "opt_type": None,
            "symbol": None,
            "strike": None,
            "expiry": None,
            "quantity": 0,
            "entry_price": 0.0,
            "current_ltp": 0.0,
            "trailed_to_cost": False,
            "entry_time": None,
            "exit_time": None,
            "exit_price": 0.0,
            "exit_reason": None,
            "realized_pnl": 0.0,
        }

    def _load_or_init_state(self) -> Dict[str, Any]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("date") == today_str and data.get("mode", self.mode) == self.mode:
                    return data
            except Exception as e:
                logger.warning("Failed to load state file %s: %s. Starting fresh.", self.state_file, e)
        return self._empty_state(today_str)

    def save_state(self) -> None:
        tmp_path = self.state_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp_path, self.state_file)
        self._last_state_save_ts = py_time.time()

    def _notify(self, msg: str) -> None:
        if self.notifier:
            try:
                self.notifier.send_message(msg)
            except Exception as e:
                logger.warning("Telegram notification failed: %s", e)

    @staticmethod
    def evaluate_signal(
        bars_df: pd.DataFrame,
        target_date: Optional[date] = None,
        orb_end_time: time = time(9, 45),
        entry_cutoff_time: time = time(13, 30),
        min_adx: float = 20.0,
        max_orb_width_pct: float = 0.65,
        use_rsi_filter: bool = True,
        now_dt: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluate completed 5m NIFTY bars for an ORB + TWAP + EMA(9/21) + ADX(14) + MTF RSI breakout signal.
        Drops any currently forming 5m/15m bar when now_dt is provided.
        """
        if bars_df is None or bars_df.empty:
            return None

        df = compute_mtf_rsi(bars_df, now_dt=now_dt)
        if df.empty:
            return None

        df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["adx14"] = compute_adx14(df)

        t_date = target_date or df["dt"].dt.date.max()
        day_df = df[df["dt"].dt.date == t_date].copy()
        if day_df.empty:
            return None

        day_df["tp"] = (day_df["high"] + day_df["low"] + day_df["close"]) / 3.0
        day_df["twap"] = day_df["tp"].expanding().mean()

        orb_bars = day_df[day_df["dt"].dt.time < orb_end_time]
        if orb_bars.empty:
            return None
        orb_high = float(orb_bars["high"].max())
        orb_low = float(orb_bars["low"].min())
        orb_open = float(orb_bars.iloc[0]["open"])
        if max_orb_width_pct > 0 and orb_open > 0:
            orb_width_pct = ((orb_high - orb_low) / orb_open) * 100.0
            if orb_width_pct > max_orb_width_pct:
                return None

        post_orb = day_df[
            (day_df["dt"].dt.time >= orb_end_time)
            & (day_df["dt"].dt.time <= entry_cutoff_time)
        ]
        if post_orb.empty:
            return None

        latest = post_orb.iloc[-1]
        close = float(latest["close"])
        twap = float(latest["twap"])
        ema9 = float(latest["ema9"])
        ema21 = float(latest["ema21"])
        adx14 = float(latest["adx14"])
        rsi5 = float(latest["rsi5"])
        rsi15 = float(latest["rsi15"])

        if min_adx > 0 and adx14 < min_adx:
            return None

        if close > orb_high and close > twap and ema9 > ema21:
            if use_rsi_filter and not (rsi5 > rsi15 and rsi5 > 50.0):
                return None
            return {
                "direction": "BULL",
                "opt_type": "PE",
                "spot": close,
                "orb_high": orb_high,
                "orb_low": orb_low,
                "twap": twap,
                "adx14": round(adx14, 1),
                "rsi5": round(rsi5, 1),
                "rsi15": round(rsi15, 1),
                "bar_time": latest["dt"].strftime("%H:%M"),
            }
        if close < orb_low and close < twap and ema9 < ema21:
            if use_rsi_filter and not (rsi5 < rsi15 and rsi5 < 50.0):
                return None
            return {
                "direction": "BEAR",
                "opt_type": "CE",
                "spot": close,
                "orb_high": orb_high,
                "orb_low": orb_low,
                "twap": twap,
                "adx14": round(adx14, 1),
                "rsi5": round(rsi5, 1),
                "rsi15": round(rsi15, 1),
                "bar_time": latest["dt"].strftime("%H:%M"),
            }
        return None

    @staticmethod
    def should_exit_on_rsi(
        bars_df: pd.DataFrame,
        direction: str,
        now_dt: Optional[datetime] = None,
    ) -> bool:
        """Exit BULL when completed 5m RSI < 15m RSI; exit BEAR when completed 5m RSI > 15m RSI."""
        if bars_df is None or bars_df.empty:
            return False
        df = compute_mtf_rsi(bars_df, now_dt=now_dt)
        if df.empty:
            return False
        latest = df.iloc[-1]
        rsi5 = float(latest["rsi5"])
        rsi15 = float(latest["rsi15"])
        if direction == "BULL":
            return rsi5 < rsi15
        if direction == "BEAR":
            return rsi5 > rsi15
        return False

    @staticmethod
    def resolve_option_contract(
        instruments: List[Dict[str, Any]],
        spot: float,
        opt_type: str,
        otm_offset: int = 100,
        today_date: Optional[date] = None,
    ) -> Tuple[str, float, date, int]:
        """Resolve exact NFO NIFTY option tradingsymbol, strike, expiry, and lot_size."""
        target_expiry = resolve_target_expiry(instruments, today_date=today_date, underlying="NIFTY")
        atm = round(spot / 50.0) * 50.0
        target_strike = atm - otm_offset if opt_type.upper() == "PE" else atm + otm_offset

        for inst in instruments:
            if (
                inst.get("name") == "NIFTY"
                and inst.get("instrument_type") == opt_type.upper()
                and float(inst.get("strike", 0)) == float(target_strike)
                and str(inst.get("expiry"))[:10] == str(target_expiry)
            ):
                symbol = f"NFO:{inst['tradingsymbol']}"
                lot_size = int(inst.get("lot_size") or DEFAULT_LOT_SIZE)
                return symbol, float(target_strike), target_expiry, lot_size

        raise ValueError(
            f"Could not find NIFTY {int(target_strike)} {opt_type} for expiry {target_expiry}"
        )

    def _fetch_recent_5m_bars(self, kite: Any, now_dt: datetime) -> pd.DataFrame:
        start_dt = now_dt - timedelta(days=5)
        records = kite.historical_data(NIFTY_INDEX_TOKEN, start_dt, now_dt, "5minute")
        return pd.DataFrame(records)

    def _fetch_ltp(self, kite: Any, symbol: str) -> float:
        data = kite.ltp([symbol])
        return float(data[symbol]["last_price"])

    def _verify_order_fill(
        self,
        order_id: str,
        fallback_price: float,
        expected_qty: int,
        timeout_sec: int = 15,
    ) -> Tuple[bool, int, float, str]:
        """Poll OrderManager.get_order_status until filled, rejected, or timeout-cancelled."""
        if self.mode == "PAPER" or not order_id:
            st = self.order_manager.get_order_status(order_id) if order_id else {}
            fq = int(st.get("filled_quantity") or expected_qty)
            ap = float(st.get("average_price") or fallback_price)
            return True, fq, ap, "COMPLETE"

        deadline = py_time.time() + max(1, timeout_sec)
        while py_time.time() < deadline:
            st = self.order_manager.get_order_status(order_id)
            status = st.get("status", "UNKNOWN")
            fq = int(st.get("filled_quantity") or 0)
            ap = float(st.get("average_price") or fallback_price)
            if status == "COMPLETE":
                return (fq > 0), fq, ap, "COMPLETE"
            if status in ("REJECTED", "CANCELLED", "ERROR"):
                return (fq > 0), fq, ap, (st.get("status_message") or status)
            py_time.sleep(1)

        try:
            self.order_manager.cancel_order(order_id)
        except Exception as e:
            logger.warning("Cancel order %s on timeout failed: %s", order_id, e)
        st = self.order_manager.get_order_status(order_id)
        fq = int(st.get("filled_quantity") or 0)
        ap = float(st.get("average_price") or fallback_price)
        return (fq > 0), fq, ap, ("TIMEOUT_PARTIAL" if fq > 0 else "TIMEOUT_CANCELLED")

    def reconcile_positions(self, kite: Any, now_dt: Optional[datetime] = None) -> bool:
        """
        In LIVE mode, verify OPEN position still exists in kite.positions().
        If manually closed in Zerodha, mark CLOSED (BROKER_RECONCILED) and persist JSON.
        Returns True if still open (or PAPER mode), False if closed externally.
        """
        if self.mode != "LIVE" or self.state.get("status") != "OPEN":
            return True
        now_ts = py_time.time()
        if (now_ts - self._last_reconcile_ts) < 60.0:
            return True
        self._last_reconcile_ts = now_ts

        symbol = self.state.get("symbol") or ""
        tsym = symbol.split(":")[-1]
        try:
            net_positions = kite.positions().get("net", [])
            broker_qty = sum(
                int(p.get("quantity") or 0)
                for p in net_positions
                if p.get("tradingsymbol") == tsym
            )
            if broker_qty == 0:
                now_dt = now_dt or datetime.now()
                self.state.update({
                    "status": "CLOSED",
                    "exit_time": now_dt.strftime("%H:%M:%S"),
                    "exit_reason": "BROKER_RECONCILED",
                })
                self.save_state()
                self._notify(f"<b>[ORB-LIVE] BROKER RECONCILED</b>\n<code>{symbol}</code> has 0 qty in Kite. Marked CLOSED.")
                return False
        except Exception as e:
            if is_token_error(e):
                raise
            logger.warning("Broker position reconciliation skipped: %s", e)
        return True

    def enter_position(
        self,
        kite: Any,
        instruments: List[Dict[str, Any]],
        signal: Dict[str, Any],
        lots: int = 10,
        otm_offset: int = 100,
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
    ) -> bool:
        """Sell the directional 100 OTM option leg and persist state."""
        symbol, strike, expiry, inst_lot_size = self.resolve_option_contract(
            instruments,
            spot=signal["spot"],
            opt_type=signal["opt_type"],
            otm_offset=otm_offset,
            today_date=datetime.now().date(),
        )
        qty = lots * inst_lot_size
        ltp = self._fetch_ltp(kite, symbol)
        if ltp <= 0:
            logger.warning("Skipping entry: %s LTP is %.2f", symbol, ltp)
            return False

        order_res = self.order_manager.sell(
            symbol=symbol,
            quantity=qty,
            price=ltp,
            order_type=order_type,
            buffer_pct=limit_buffer_pct,
        )
        ok, filled_qty, fill_px, fill_msg = self._verify_order_fill(
            order_id=str(order_res.get("order_id") or ""),
            fallback_price=ltp,
            expected_qty=qty,
            timeout_sec=15,
        )
        if not ok or filled_qty <= 0:
            logger.warning("[%s] Entry order %s not filled (%s). Staying IDLE.", self.mode, symbol, fill_msg)
            self._notify(f"<b>[ORB-{self.mode}] ENTRY FAILED</b>\n<code>{symbol}</code>: {fill_msg}")
            return False

        self.state.update({
            "status": "OPEN",
            "direction": signal["direction"],
            "opt_type": signal["opt_type"],
            "symbol": symbol,
            "strike": strike,
            "expiry": str(expiry),
            "quantity": filled_qty,
            "entry_price": fill_px,
            "current_ltp": fill_px,
            "trailed_to_cost": False,
            "entry_time": datetime.now().strftime("%H:%M:%S"),
        })
        self.save_state()

        self._notify(
            f"<b>[ORB-{self.mode}] ENTRY {signal['direction']}</b>\n"
            f"Sold <code>{symbol}</code> x {filled_qty} @ ₹{fill_px:.2f}\n"
            f"Spot: {signal['spot']:.1f} | ORB: [{signal['orb_low']:.1f}, {signal['orb_high']:.1f}]"
        )
        return True

    def exit_position(
        self,
        kite: Any,
        reason: str,
        order_type: str = "MARKET",
        limit_buffer_pct: float = 0.05,
    ) -> float:
        """Square off the open option leg and mark CLOSED."""
        symbol = self.state["symbol"]
        qty = int(self.state["quantity"])
        entry_px = float(self.state["entry_price"])
        cur_ltp = self._fetch_ltp(kite, symbol)

        buy_res = self.order_manager.buy_to_cover(
            symbol=symbol,
            quantity=qty,
            price=cur_ltp,
            reason=reason,
            order_type=order_type,
            buffer_pct=limit_buffer_pct,
        )
        _, filled_qty, exit_px, _ = self._verify_order_fill(
            order_id=str(buy_res.get("order_id") or ""),
            fallback_price=cur_ltp,
            expected_qty=qty,
            timeout_sec=15,
        )
        rem_qty = max(0, qty - filled_qty)
        if rem_qty > 0:
            esc_res = self.order_manager.buy_to_cover(
                symbol=symbol,
                quantity=rem_qty,
                price=cur_ltp,
                reason=f"{reason}_MARKET_ESC",
                order_type="MARKET",
            )
            _, esc_fq, esc_px, _ = self._verify_order_fill(
                order_id=str(esc_res.get("order_id") or ""),
                fallback_price=cur_ltp,
                expected_qty=rem_qty,
                timeout_sec=10,
            )
            if esc_fq > 0:
                total_f = filled_qty + esc_fq
                exit_px = round((filled_qty * exit_px + esc_fq * esc_px) / total_f, 2)
                filled_qty = total_f
                rem_qty = max(0, qty - filled_qty)

        pnl = round((entry_px - exit_px) * filled_qty, 2) if filled_qty > 0 else 0.0
        self.state["realized_pnl"] = round(float(self.state.get("realized_pnl", 0.0)) + pnl, 2)
        if rem_qty > 0:
            self.state["quantity"] = rem_qty
            self.save_state()
            return pnl

        self.state.update({
            "status": "CLOSED",
            "quantity": 0,
            "current_ltp": exit_px,
            "exit_price": exit_px,
            "exit_time": datetime.now().strftime("%H:%M:%S"),
            "exit_reason": reason,
        })
        self.save_state()

        self._notify(
            f"<b>[ORB-{self.mode}] EXIT ({reason})</b>\n"
            f"Bought <code>{symbol}</code> x {filled_qty} @ ₹{exit_px:.2f} (Entry: ₹{entry_px:.2f})\n"
            f"Realized P&amp;L: <b>₹{pnl:+,.2f}</b>"
        )
        return pnl

    def step(
        self,
        kite: Any,
        instruments: List[Dict[str, Any]],
        lots: int = 10,
        otm_offset: int = 100,
        leg_sl_pct: float = 0.50,
        trail_decay_pct: float = 0.30,
        target_decay_pct: float = 0.75,
        max_loss_rs: float = 0.0,
        orb_end_time: time = time(9, 45),
        entry_cutoff_time: time = time(13, 30),
        exit_cutoff_time: time = time(15, 15),
        min_adx: float = 20.0,
        max_orb_width_pct: float = 0.65,
        use_rsi_filter: bool = True,
        use_rsi_exit: bool = False,
        order_type: str = "LIMIT",
        limit_buffer_pct: float = 0.05,
        now_dt: Optional[datetime] = None,
    ) -> str:
        """Run one monitoring/execution cycle. Returns current state status."""
        now_dt = now_dt or datetime.now()
        now_t = now_dt.time()
        status = self.state.get("status", "IDLE")

        if status == "CLOSED":
            return "CLOSED"

        if status == "IDLE":
            if now_t < orb_end_time or now_t > entry_cutoff_time or now_t >= exit_cutoff_time:
                return "IDLE"
            cur_bar_start = now_dt.replace(minute=(now_dt.minute // 5) * 5, second=0, microsecond=0, tzinfo=None)
            if self._last_checked_bar_start == cur_bar_start:
                return "IDLE"
            self._last_checked_bar_start = cur_bar_start
            bars_df = self._fetch_recent_5m_bars(kite, now_dt)
            signal = self.evaluate_signal(
                bars_df,
                target_date=now_dt.date(),
                orb_end_time=orb_end_time,
                entry_cutoff_time=entry_cutoff_time,
                min_adx=min_adx,
                max_orb_width_pct=max_orb_width_pct,
                use_rsi_filter=use_rsi_filter,
                now_dt=now_dt,
            )
            if signal:
                self.enter_position(
                    kite=kite,
                    instruments=instruments,
                    signal=signal,
                    lots=lots,
                    otm_offset=otm_offset,
                    order_type=order_type,
                    limit_buffer_pct=limit_buffer_pct,
                )
            return self.state["status"]

        if not self.reconcile_positions(kite, now_dt=now_dt):
            return "CLOSED"

        # Manage OPEN position
        symbol = self.state["symbol"]
        entry_px = float(self.state["entry_price"])
        qty = int(self.state["quantity"])
        cur_ltp = self._fetch_ltp(kite, symbol)
        self.state["current_ltp"] = cur_ltp

        if not self.state.get("trailed_to_cost") and cur_ltp <= entry_px * (1.0 - trail_decay_pct):
            self.state["trailed_to_cost"] = True
            self.save_state()
            logger.info("[%s] %s decayed >= %.0f%%; SL trailed to cost ₹%.2f", self.mode, symbol, trail_decay_pct * 100, entry_px)

        sl_px = entry_px if self.state.get("trailed_to_cost") else entry_px * (1.0 + leg_sl_pct)
        target_px = entry_px * (1.0 - target_decay_pct)
        unrealized_pnl = (entry_px - cur_ltp) * qty

        if now_t >= exit_cutoff_time:
            self.exit_position(kite, "EOD_CUTOFF", order_type="MARKET", limit_buffer_pct=limit_buffer_pct)
        elif max_loss_rs > 0 and unrealized_pnl <= -abs(max_loss_rs):
            self.exit_position(kite, "MAX_LOSS_CAP", order_type="MARKET", limit_buffer_pct=limit_buffer_pct)
        elif cur_ltp >= sl_px:
            reason = "COST_TRAIL_SL" if self.state.get("trailed_to_cost") else "LEG_SL"
            self.exit_position(kite, reason, order_type="MARKET", limit_buffer_pct=limit_buffer_pct)
        elif cur_ltp <= target_px:
            self.exit_position(kite, "TARGET_DECAY", order_type=order_type, limit_buffer_pct=limit_buffer_pct)
        else:
            if use_rsi_exit:
                cur_bar_start = now_dt.replace(minute=(now_dt.minute // 5) * 5, second=0, microsecond=0, tzinfo=None)
                if self._last_checked_bar_start != cur_bar_start:
                    self._last_checked_bar_start = cur_bar_start
                    bars_df = self._fetch_recent_5m_bars(kite, now_dt)
                    if self.should_exit_on_rsi(bars_df, self.state.get("direction", ""), now_dt=now_dt):
                        self.exit_position(kite, "RSI_REVERSAL", order_type=order_type, limit_buffer_pct=limit_buffer_pct)
                        return self.state["status"]
            if (py_time.time() - self._last_state_save_ts) >= 30.0:
                self.save_state()

        return self.state["status"]

    def run_loop(
        self,
        kite: Any,
        poll_seconds: int = 5,
        ignore_holidays: bool = False,
        **step_kwargs: Any,
    ) -> None:
        """Continuous intraday daemon loop with market-hours and holiday guards."""
        instruments = None
        while True:
            now_dt = datetime.now()
            if not is_market_open(now_dt, ignore_holidays=ignore_holidays):
                py_time.sleep(60)
                continue

            today_str = now_dt.strftime("%Y-%m-%d")
            if self.state.get("date") != today_str:
                self.state = self._empty_state(today_str)
                self._last_checked_bar_start = None
                self._last_reconcile_ts = 0.0
                self.save_state()
                instruments = None

            if instruments is None:
                try:
                    instruments = kite.instruments("NFO")
                except Exception as e:
                    if is_token_error(e):
                        kite = ensure_kite_session(kite, force_login=False)
                        self.order_manager.kite = kite if self.mode == "LIVE" else None
                        instruments = kite.instruments("NFO")
                    else:
                        logger.error("Failed to fetch NFO instruments: %s", e)
                        py_time.sleep(poll_seconds)
                        continue

            try:
                status = self.step(kite=kite, instruments=instruments, now_dt=now_dt, **step_kwargs)
                if status == "CLOSED":
                    py_time.sleep(60)
                    continue
            except Exception as e:
                if is_token_error(e):
                    kite = ensure_kite_session(kite, force_login=False)
                    self.order_manager.kite = kite if self.mode == "LIVE" else None
                else:
                    logger.exception("Error in DirectionalOrbDesk step: %s", e)

            py_time.sleep(poll_seconds)
