import os
import json
import time
import logging
from datetime import datetime, date, time as dt_time, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd
from kiteconnect import KiteConnect

from strategy.orb_desk.config import ORBConfig, TARGET_STOCKS, NIFTY_TOKEN, NIFTY_SYMBOL
from strategy.orb_desk.order_manager import ORBOrderManager, _round_to_nse_tick
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("orb.desk")


class ORBDesk:
    """
    15-Minute Opening Range Breakout (ORB) Automated Desk.
    Implements Indrajit Shantharaj's strategy with 20% wick filter,
    PDH/PDL filter, NIFTY direction filter, and 3 risk guardrails:
      1. Fixed-Risk Risk-Parity Sizing (Rs 2,500/trade)
      2. Max Trades / Day Cap (5 trades)
      3. Portfolio Stop-Loss Circuit Breaker (Rs 10,000 limit)
    """

    def __init__(
        self,
        kite: KiteConnect,
        order_manager: ORBOrderManager,
        notifier: Optional[TelegramNotifier] = None,
        config: Optional[ORBConfig] = None
    ):
        self.kite = kite
        self.om = order_manager
        self.notifier = notifier or TelegramNotifier()
        self.config = config or ORBConfig()

        self.state_file = Path(self.config.state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # In-memory daily state
        self.today: date = date.today()
        self.nifty_day_open: Optional[float] = None
        self.stock_stats: Dict[str, Dict[str, Any]] = {}  # {sym: {pdh, pdl, ib_high, ib_low, token}}
        self.active_positions: List[Dict[str, Any]] = []
        self.closed_trades: List[Dict[str, Any]] = []
        self.traded_stocks_today: set = set()
        self.circuit_tripped: bool = False
        self.ib_formed: bool = False

        self._load_state()

    # -------------------------------------------------------------------------
    # State Persistence
    # -------------------------------------------------------------------------
    def _save_state(self):
        state_data = {
            "date": str(self.today),
            "nifty_day_open": self.nifty_day_open,
            "ib_formed": self.ib_formed,
            "circuit_tripped": self.circuit_tripped,
            "traded_stocks_today": list(self.traded_stocks_today),
            "active_positions": self.active_positions,
            "closed_trades": self.closed_trades,
            "stock_stats": self.stock_stats
        }
        try:
            with open(self.state_file, "w") as f:
                json.dump(state_data, f, indent=2, default=str)
        except Exception as e:
            logger.error("Failed to save ORB state: %s", e)

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            if data.get("date") == str(self.today):
                self.nifty_day_open = data.get("nifty_day_open")
                self.ib_formed = data.get("ib_formed", False)
                self.circuit_tripped = data.get("circuit_tripped", False)
                self.traded_stocks_today = set(data.get("traded_stocks_today", []))
                self.active_positions = data.get("active_positions", [])
                self.closed_trades = data.get("closed_trades", [])
                self.stock_stats = data.get("stock_stats", {})
                logger.info(
                    "Restored daily state for %s: %d active, %d closed trades.",
                    self.today,
                    len(self.active_positions),
                    len(self.closed_trades)
                )
        except Exception as e:
            logger.warning("Could not read previous ORB state file: %s", e)

    # -------------------------------------------------------------------------
    # Morning Initialization & Previous Day Stats
    # -------------------------------------------------------------------------
    def initialize_day(self, force: bool = False):
        """
        Fetches Previous Day High (PDH) and Previous Day Low (PDL) for all 30 stocks,
        and retrieves NIFTY 50 Day Open.
        """
        logger.info("Initializing ORB Desk for %s across %d stocks...", self.today, len(TARGET_STOCKS))

        # Check NIFTY 50 Day Open
        try:
            quote = self.kite.quote([NIFTY_SYMBOL])
            if NIFTY_SYMBOL in quote:
                self.nifty_day_open = float(quote[NIFTY_SYMBOL]["ohlc"]["open"])
                logger.info("NIFTY 50 Day Open: %.2f", self.nifty_day_open)
        except Exception as e:
            logger.warning("Could not fetch NIFTY Day Open: %s", e)

        # Fetch Previous Day High and Low for all 30 stocks
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=10)

        for item in TARGET_STOCKS:
            sym = item["symbol"]
            token = item["token"]
            if sym in self.stock_stats and not force:
                continue

            try:
                # Fetch daily candles
                hist = self.kite.historical_data(
                    instrument_token=token,
                    from_date=start_dt.date(),
                    to_date=end_dt.date(),
                    interval="day"
                )
                if len(hist) >= 2:
                    prev_candle = hist[-2]  # Candle immediately preceding today
                    pdh = float(prev_candle["high"])
                    pdl = float(prev_candle["low"])
                elif len(hist) == 1:
                    pdh = float(hist[0]["high"])
                    pdl = float(hist[0]["low"])
                else:
                    pdh = None
                    pdl = None

                self.stock_stats[sym] = {
                    "token": token,
                    "lot": item["lot"],
                    "pdh": pdh,
                    "pdl": pdl,
                    "ib_high": None,
                    "ib_low": None
                }
                time.sleep(0.35)  # Respect Kite 3 req/sec rate limit
            except Exception as e:
                logger.warning("Failed to fetch daily history for %s: %s", sym, e)
                self.stock_stats[sym] = {
                    "token": token,
                    "lot": item["lot"],
                    "pdh": None,
                    "pdl": None,
                    "ib_high": None,
                    "ib_low": None
                }

        self._save_state()
        self._notify_desk_started()

    # -------------------------------------------------------------------------
    # Initial Balance Formation (09:15 - 10:15)
    # -------------------------------------------------------------------------
    def compute_initial_balance(self):
        """
        Fetches 15-minute candles from 09:15 to 10:15 for all 30 stocks
        and records Initial Balance (IB High, IB Low).
        """
        logger.info("Computing 15-minute Initial Balance (09:15 - 10:15) for all 30 stocks...")
        now = datetime.now()
        today_start = datetime.combine(self.today, dt_time(9, 15))

        for item in TARGET_STOCKS:
            sym = item["symbol"]
            token = item["token"]
            try:
                candles = self.kite.historical_data(
                    instrument_token=token,
                    from_date=today_start,
                    to_date=now,
                    interval="15minute"
                )
                if len(candles) >= 4:
                    ib_candles = candles[:4]
                    ib_h = max(float(c["high"]) for c in ib_candles)
                    ib_l = min(float(c["low"]) for c in ib_candles)
                    if sym in self.stock_stats:
                        self.stock_stats[sym]["ib_high"] = ib_h
                        self.stock_stats[sym]["ib_low"] = ib_l
                elif len(candles) > 0:
                    # In case of partial bars
                    ib_h = max(float(c["high"]) for c in candles)
                    ib_l = min(float(c["low"]) for c in candles)
                    if sym in self.stock_stats:
                        self.stock_stats[sym]["ib_high"] = ib_h
                        self.stock_stats[sym]["ib_low"] = ib_l
                time.sleep(0.35)  # Respect Kite 3 req/sec rate limit
            except Exception as e:
                logger.warning("Could not fetch IB candles for %s: %s", sym, e)

        self.ib_formed = True
        self._save_state()
        self._notify_ib_formed()

    # -------------------------------------------------------------------------
    # Breakout Scanning & Trigger Execution (10:15 - 13:15)
    # -------------------------------------------------------------------------
    def scan_and_enter_breakouts(self):
        """
        Scans all 30 stocks at candle completion for valid breakout triggers.
        Applies:
          - 20% rejection wick filter
          - PDH / PDL clearance filter
          - NIFTY direction alignment filter
          - Risk-parity sizing (Fixed Rs 2,500 risk)
          - Max 5 trades per day cap
        """
        if self.circuit_tripped:
            logger.info("Circuit breaker active. Scanning skipped.")
            return

        total_trades_today = len(self.closed_trades) + len(self.active_positions)
        available_slots = self.config.max_trades_per_day - total_trades_today
        if available_slots <= 0:
            logger.info("Daily trade limit reached (%d/%d). No new entries.", total_trades_today, self.config.max_trades_per_day)
            return

        # Check NIFTY Direction
        nifty_bias = None
        try:
            n_ltp_res = self.kite.ltp([NIFTY_SYMBOL])
            if NIFTY_SYMBOL in n_ltp_res and self.nifty_day_open is not None:
                n_ltp = float(n_ltp_res[NIFTY_SYMBOL]["last_price"])
                nifty_bias = "BULLISH" if n_ltp >= self.nifty_day_open else "BEARISH"
                logger.debug("NIFTY 50 LTP: %.2f vs Open %.2f -> Bias: %s", n_ltp, self.nifty_day_open, nifty_bias)
        except Exception as e:
            logger.warning("Could not check NIFTY bias: %s", e)

        now = datetime.now()
        today_start = datetime.combine(self.today, dt_time(9, 15))
        candidates = []

        # Collect current 15-min candles for candidates
        for item in TARGET_STOCKS:
            sym = item["symbol"]
            token = item["token"]
            if sym in self.traded_stocks_today:
                continue
            if any(p["symbol"] == sym for p in self.active_positions):
                continue

            stk_stat = self.stock_stats.get(sym, {})
            ib_h = stk_stat.get("ib_high")
            ib_l = stk_stat.get("ib_low")
            pdh = stk_stat.get("pdh")
            pdl = stk_stat.get("pdl")

            if ib_h is None or ib_l is None:
                continue

            try:
                candles = self.kite.historical_data(
                    instrument_token=token,
                    from_date=today_start,
                    to_date=now,
                    interval="15minute"
                )
                if len(candles) < 5:  # Needs at least 1 candle past IB
                    continue

                latest_candle = candles[-1]
                o = float(latest_candle["open"])
                h = float(latest_candle["high"])
                l = float(latest_candle["low"])
                c = float(latest_candle["close"])
                c_range = h - l

                if c_range <= 0 or (c_range / c) > self.config.max_sl_pct:
                    continue

                direction = None
                wick_ratio = 0.0

                if c > ib_h:
                    upper_wick = h - c
                    wick_ratio = upper_wick / c_range
                    if wick_ratio <= self.config.wick_max_pct:
                        direction = "LONG"
                elif c < ib_l:
                    lower_wick = c - l
                    wick_ratio = lower_wick / c_range
                    if wick_ratio <= self.config.wick_max_pct:
                        direction = "SHORT"

                if not direction:
                    continue

                # PDH / PDL Filter
                if self.config.use_pdh_pdl_filter and pdh is not None and pdl is not None:
                    if direction == "LONG" and c < pdh:
                        continue  # Blocked by PDH resistance
                    elif direction == "SHORT" and c > pdl:
                        continue  # Blocked by PDL support

                # NIFTY Direction Filter
                if self.config.use_nifty_filter and nifty_bias is not None:
                    if direction == "LONG" and nifty_bias != "BULLISH":
                        continue
                    elif direction == "SHORT" and nifty_bias != "BEARISH":
                        continue

                # Trade Setup
                entry_trigger = _round_to_nse_tick(h + 0.05 if direction == "LONG" else l - 0.05)
                initial_sl = _round_to_nse_tick(l - 0.05 if direction == "LONG" else h + 0.05)
                risk_per_share = abs(entry_trigger - initial_sl)
                if risk_per_share <= 0:
                    continue

                # Risk-Parity Sizing: Fixed Rs 2,500
                qty = max(1, int(self.config.fixed_risk_per_trade / risk_per_share))

                candidates.append({
                    "symbol": sym,
                    "direction": direction,
                    "entry_trigger": entry_trigger,
                    "initial_sl": initial_sl,
                    "risk_per_share": risk_per_share,
                    "qty": qty,
                    "wick_ratio": wick_ratio,
                    "token": token
                })

            except Exception as e:
                logger.warning("Error evaluating candle for %s: %s", sym, e)
            finally:
                time.sleep(0.35)  # Respect Kite 3 req/sec rate limit

        if not candidates:
            return

        # Sort by cleanest rejection wick ratio (strongest momentum conviction)
        candidates.sort(key=lambda x: x["wick_ratio"])
        to_execute = candidates[:available_slots]

        for cand in to_execute:
            self._execute_entry(cand)

    def _execute_entry(self, cand: Dict[str, Any]):
        sym = cand["symbol"]
        direction = cand["direction"]
        qty = cand["qty"]
        entry_price = cand["entry_trigger"]
        initial_sl = cand["initial_sl"]
        risk = cand["risk_per_share"]

        target_1r = _round_to_nse_tick(entry_price + risk if direction == "LONG" else entry_price - risk)
        target_2_5r = _round_to_nse_tick(entry_price + 2.5 * risk if direction == "LONG" else entry_price - 2.5 * risk)

        # Place Entry Order
        order_res = self.om.place_entry(
            symbol=sym,
            direction=direction,
            quantity=qty,
            entry_price=entry_price,
            initial_sl=initial_sl,
            target_2_5r=target_2_5r
        )

        pos_record = {
            "symbol": sym,
            "direction": direction,
            "quantity": qty,
            "half_quantity": max(1, qty // 2),
            "entry_price": entry_price,
            "initial_sl": initial_sl,
            "current_sl": initial_sl,
            "target_1r": target_1r,
            "target_2_5r": target_2_5r,
            "risk_per_share": risk,
            "half_booked": False,
            "be_moved": False,
            "entry_time": datetime.now().strftime("%H:%M:%S"),
            "order_id": order_res.get("order_id"),
            "wick_pct": cand["wick_ratio"] * 100
        }

        self.active_positions.append(pos_record)
        self.traded_stocks_today.add(sym)
        self._save_state()
        self._notify_trade_entry(pos_record)

    # -------------------------------------------------------------------------
    # Continuous Position Monitoring & Risk Management
    # -------------------------------------------------------------------------
    def monitor_positions(self, current_time: Optional[dt_time] = None):
        """
        Monitors active positions against live Kite quotes:
          - 1:1 trail to Breakeven
          - 1:2.5 book 50%
          - Stop-Loss exit
          - Portfolio Circuit Breaker (-Rs 10,000)
          - EOD 15:15 Square-Off
        """
        if not self.active_positions:
            return

        now = datetime.now()
        check_time = current_time if current_time is not None else now.time()
        is_eod = (check_time >= dt_time(15, 15))

        # Fetch live LTP for all active symbols
        symbols_to_fetch = [f"NSE:{p['symbol']}" for p in self.active_positions]
        try:
            quotes = self.kite.ltp(symbols_to_fetch)
        except Exception as e:
            logger.error("Failed to fetch live quotes for active positions: %s", e)
            return

        remaining_positions = []
        day_realized_pnl = sum(t.get("realized_pnl_rs", 0.0) for t in self.closed_trades)
        total_unrealized_mtm = 0.0

        for pos in self.active_positions:
            sym = pos["symbol"]
            kite_key = f"NSE:{sym}"
            if kite_key not in quotes:
                remaining_positions.append(pos)
                continue

            ltp = float(quotes[kite_key]["last_price"])
            direction = pos["direction"]
            entry_p = pos["entry_price"]
            cur_sl = pos["current_sl"]
            t_1r = pos["target_1r"]
            t_2_5r = pos["target_2_5r"]
            qty = pos["quantity"]
            half_qty = pos["half_quantity"]
            rem_qty = half_qty if pos["half_booked"] else qty

            # Calculate open MTM for this position
            cur_mtm = (ltp - entry_p) * rem_qty if direction == "LONG" else (entry_p - ltp) * rem_qty
            total_unrealized_mtm += cur_mtm

            pos_closed = False
            exit_reason = None
            exit_price = ltp

            # 1. Stop-Loss Check
            if (direction == "LONG" and ltp <= cur_sl) or (direction == "SHORT" and ltp >= cur_sl):
                pos_closed = True
                exit_price = cur_sl
                if pos["half_booked"]:
                    exit_reason = "TARGET_2.5_THEN_BE"
                elif pos["be_moved"]:
                    exit_reason = "BREAKEVEN"
                else:
                    exit_reason = "STOP_LOSS"

            # 2. Target 1:2.5 Book 50%
            elif not pos["half_booked"] and ((direction == "LONG" and ltp >= t_2_5r) or (direction == "SHORT" and ltp <= t_2_5r)):
                pos["half_booked"] = True
                half_booked_pnl = (t_2_5r - entry_p) * half_qty if direction == "LONG" else (entry_p - t_2_5r) * half_qty
                pos["half_booked_pnl"] = half_booked_pnl
                logger.info("50%% Booked at 1:2.5 on %s (+Rs %.2f)", sym, half_booked_pnl)
                # Place half-position exit
                self.om.place_exit(
                    symbol=sym,
                    direction=direction,
                    quantity=half_qty,
                    exit_price=t_2_5r,
                    reason="TARGET_2.5_BOOK_50%"
                )
                self._notify_target_booked(pos, t_2_5r, half_booked_pnl)

            # 3. 1:1 Trail to Breakeven
            if not pos["be_moved"] and not pos_closed:
                if (direction == "LONG" and ltp >= t_1r) or (direction == "SHORT" and ltp <= t_1r):
                    pos["be_moved"] = True
                    pos["current_sl"] = entry_p
                    logger.info("1:1 Reached for %s. Trailing SL moved to BE (Rs %.2f)", sym, entry_p)
                    self._notify_trail_be(pos)

            # 4. Mandatory EOD 15:15 Square-Off
            if is_eod and not pos_closed:
                pos_closed = True
                exit_reason = "TARGET_2.5_THEN_EOD" if pos["half_booked"] else "EOD_SQUAREOFF"
                exit_price = ltp

            if pos_closed:
                # Place exit order
                self.om.place_exit(
                    symbol=sym,
                    direction=direction,
                    quantity=rem_qty,
                    exit_price=exit_price,
                    reason=exit_reason
                )

                # Calculate final trade P&L
                rem_pnl = (exit_price - entry_p) * rem_qty if direction == "LONG" else (entry_p - exit_price) * rem_qty
                total_trade_pnl = rem_pnl + pos.get("half_booked_pnl", 0.0)
                r_multiple = total_trade_pnl / self.config.fixed_risk_per_trade

                trade_summary = {
                    "symbol": sym,
                    "direction": direction,
                    "quantity": qty,
                    "entry_price": entry_p,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "realized_pnl_rs": round(total_trade_pnl, 2),
                    "r_multiple": round(r_multiple, 2),
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now().strftime("%H:%M:%S")
                }
                self.closed_trades.append(trade_summary)
                self._notify_trade_exit(trade_summary)
            else:
                remaining_positions.append(pos)

        self.active_positions = remaining_positions

        # ---------------------------------------------------------------------
        # 5. Daily Portfolio Circuit Breaker Check
        # -------------------------------------------------------------------------
        total_day_mtm = day_realized_pnl + total_unrealized_mtm
        if not self.circuit_tripped and total_day_mtm <= -self.config.daily_stop_loss_limit:
            self.circuit_tripped = True
            logger.critical("PORTFOLIO CIRCUIT BREAKER TRIPPED! Current MTM: Rs %.2f <= -Rs %.2f", total_day_mtm, self.config.daily_stop_loss_limit)
            self._emergency_circuit_breaker_squareoff(quotes)

        self._save_state()

        if is_eod and not remaining_positions:
            self._notify_daily_summary()

    def _emergency_circuit_breaker_squareoff(self, quotes: Dict[str, Any]):
        """Closes all active positions immediately at market on circuit breaker trip."""
        for pos in self.active_positions:
            sym = pos["symbol"]
            direction = pos["direction"]
            rem_qty = pos["half_quantity"] if pos["half_booked"] else pos["quantity"]
            kite_key = f"NSE:{sym}"
            exit_price = float(quotes.get(kite_key, {}).get("last_price", pos["current_sl"]))

            self.om.place_exit(
                symbol=sym,
                direction=direction,
                quantity=rem_qty,
                exit_price=exit_price,
                reason="CIRCUIT_BREAKER_HALT"
            )

            pnl = (exit_price - pos["entry_price"]) * rem_qty if direction == "LONG" else (pos["entry_price"] - exit_price) * rem_qty
            total_pnl = pnl + pos.get("half_booked_pnl", 0.0)

            self.closed_trades.append({
                "symbol": sym,
                "direction": direction,
                "quantity": pos["quantity"],
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "exit_reason": "CIRCUIT_BREAKER_HALT",
                "realized_pnl_rs": round(total_pnl, 2),
                "r_multiple": round(total_pnl / self.config.fixed_risk_per_trade, 2),
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now().strftime("%H:%M:%S")
            })

        self.active_positions = []
        self._save_state()
        self._notify_circuit_breaker()

    # -------------------------------------------------------------------------
    # Rich Telegram Notifications
    # -------------------------------------------------------------------------
    def _notify_desk_started(self):
        msg = (
            f"🌅 <b>ORB Trading Desk Initialized</b>\n\n"
            f"📅 Date: <b>{self.today}</b>\n"
            f"⚙️ Mode: <b>{self.config.mode} TRADING</b>\n"
            f"🎯 Universe: <b>{len(TARGET_STOCKS)} High-Beta Stocks</b>\n"
            f"🛡️ Risk / Trade: <b>₹{self.config.fixed_risk_per_trade:,.0f}</b>\n"
            f"🛑 Daily Circuit Breaker: <b>₹{self.config.daily_stop_loss_limit:,.0f}</b>\n"
            f"📊 NIFTY Day Open: <b>{self.nifty_day_open or 'Pending'}</b>\n\n"
            f"<i>Collecting 09:15 - 10:15 Initial Balance candles...</i>"
        )
        self.notifier.send_message(msg)

    def _notify_ib_formed(self):
        msg = (
            f"📊 <b>Initial Balance (IB) Established!</b>\n\n"
            f"Initial 1-hour range (09:15 - 10:15) calculated across 30 stocks.\n"
            f"Scanning for breakouts with <b>20% Wick Filter</b> + <b>PDH/PDL</b> + <b>NIFTY Bias</b>.\n"
            f"Max Available Trades Today: <b>{self.config.max_trades_per_day}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_trade_entry(self, pos: Dict[str, Any]):
        direction_emoji = "🟢" if pos["direction"] == "LONG" else "🔴"
        msg = (
            f"{direction_emoji} <b>ORB BREAKOUT ENTERED ({self.config.mode})</b>\n\n"
            f"Stock: <b>{pos['symbol']}</b> ({pos['direction']})\n"
            f"Quantity: <b>{pos['quantity']} shares</b>\n"
            f"Entry Trigger: <b>₹{pos['entry_price']:.2f}</b>\n"
            f"Initial Stop Loss: <b>₹{pos['initial_sl']:.2f}</b> (Risk: ₹{pos['risk_per_share'] * pos['quantity']:,.0f})\n"
            f"🎯 1:1 BE Target: <b>₹{pos['target_1r']:.2f}</b>\n"
            f"💰 1:2.5 Target: <b>₹{pos['target_2_5r']:.2f}</b>\n"
            f"Conviction Wick: <b>{pos['wick_pct']:.1f}%</b>\n"
            f"Time: <b>{pos['entry_time']}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_trail_be(self, pos: Dict[str, Any]):
        msg = (
            f"🎯 <b>1:1 Target Reached — Trailed to Breakeven!</b>\n\n"
            f"Stock: <b>{pos['symbol']}</b> ({pos['direction']})\n"
            f"Stop-Loss moved to: <b>₹{pos['entry_price']:.2f}</b> (Entry Price)\n"
            f"🛡️ <i>Downside risk completely eliminated. Free trade running!</i>"
        )
        self.notifier.send_message(msg)

    def _notify_target_booked(self, pos: Dict[str, Any], exit_p: float, booked_pnl: float):
        msg = (
            f"💰 <b>1:2.5 Target Hit — 50% Profit Booked!</b>\n\n"
            f"Stock: <b>{pos['symbol']}</b>\n"
            f"Booked 50% Qty ({pos['half_quantity']} shares) @ <b>₹{exit_p:.2f}</b>\n"
            f"Realized Gain: <b>+₹{booked_pnl:,.2f} (+1.25 R)</b>\n"
            f"Remaining 50% trailing with SL at Breakeven until 15:15 EOD."
        )
        self.notifier.send_message(msg)

    def _notify_trade_exit(self, t: Dict[str, Any]):
        pnl = t["realized_pnl_rs"]
        pnl_emoji = "🟢" if pnl > 0 else ("⚪" if pnl == 0 else "🔴")
        msg = (
            f"{pnl_emoji} <b>ORB Trade Closed: {t['symbol']}</b>\n\n"
            f"Direction: <b>{t['direction']}</b> x {t['quantity']}\n"
            f"Exit Reason: <b>{t['exit_reason']}</b>\n"
            f"Entry: ₹{t['entry_price']:.2f} ➔ Exit: ₹{t['exit_price']:.2f}\n"
            f"Realized P&L: <b>₹{pnl:+,.2f} ({t['r_multiple']:+,.2f} R)</b>\n"
            f"Time: <b>{t['exit_time']}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_circuit_breaker(self):
        msg = (
            f"🚨 <b>PORTFOLIO CIRCUIT BREAKER TRIPPED!</b>\n\n"
            f"Cumulative daily portfolio loss hit threshold (<b>-₹{self.config.daily_stop_loss_limit:,.0f}</b>).\n"
            f"All active positions have been immediately squared off at market.\n"
            f"Trading desk is <b>HALTED</b> for the rest of today to protect capital."
        )
        self.notifier.send_message(msg)

    def _notify_daily_summary(self):
        total_pnl = sum(t["realized_pnl_rs"] for t in self.closed_trades)
        wins = [t for t in self.closed_trades if t["realized_pnl_rs"] > 0]
        losses = [t for t in self.closed_trades if t["realized_pnl_rs"] < 0]
        be = [t for t in self.closed_trades if t["realized_pnl_rs"] == 0]

        summary_emoji = "🎉" if total_pnl > 0 else ("⚪" if total_pnl == 0 else "🛑")
        msg = (
            f"{summary_emoji} <b>ORB Daily Session Summary ({self.config.mode})</b>\n\n"
            f"📅 Date: <b>{self.today}</b>\n"
            f"Total Trades: <b>{len(self.closed_trades)}</b>\n"
            f"Wins: <b>{len(wins)}</b> | Losses: <b>{len(losses)}</b> | BE: <b>{len(be)}</b>\n"
            f"Net P&L: <b>₹{total_pnl:+,.2f}</b>\n"
        )
        self.notifier.send_message(msg)
