import json
import logging
import time
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Dict, Any, Optional, List
from kiteconnect import KiteConnect

from strategy.order_manager import OrderManager
from strategy.holidays import is_trading_holiday
from notifications.telegram import TelegramNotifier
from strategy.positional_condor.indicator import fetch_daily_nifty_rsi
from strategy.positional_condor.strike_solver import (
    get_monthly_expiry,
    get_directional_ratio_strikes,
    solve_1_to_1_iron_condor,
    parse_expiry
)

logger = logging.getLogger("condor.desk")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "positional_condor_state.json"


class PositionalDesk:
    """
    Manages the Positional Evergreen NIFTY Condor & 1:2:1 Ratio strategy.
    Evaluates market regime at 15:20 IST daily, manages break-even rolls,
    and enforces monthly circuit breakers (+4% target lock / -3% stop loss).
    """

    def __init__(
        self,
        order_manager: OrderManager,
        state_file: Optional[Path] = None,
        notifier: Optional[TelegramNotifier] = None,
        lots: int = 1,
        capital_per_lot: float = 100000.0,
        monthly_target_pct: float = 0.04,
        monthly_stop_pct: float = 0.03
    ):
        self.order_manager = order_manager
        self.state_file = state_file or DEFAULT_STATE_FILE
        self.notifier = notifier
        self.lots = lots
        self.capital_per_lot = capital_per_lot
        self.monthly_target_pct = monthly_target_pct
        self.monthly_stop_pct = monthly_stop_pct
        self.total_capital = lots * capital_per_lot
        self.state: Optional[Dict[str, Any]] = None

    def save_state(self, state: Dict[str, Any]) -> None:
        """Atomically saves desk state to disk."""
        self.state = state
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.state_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        temp_file.replace(self.state_file)
        logger.info("Saved positional state to %s (Status: %s, Realized: Rs %.2f)",
                    self.state_file, state.get("status"), state.get("monthly_realized_pnl", 0.0))

    def load_state(self) -> Optional[Dict[str, Any]]:
        """Loads state from disk if present and valid."""
        if not self.state_file.exists():
            self.state = None
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            return self.state
        except Exception as e:
            logger.error("Failed to load positional state file: %s", e)
            self.state = None
            return None

    def clear_state(self) -> None:
        """Clears state from memory and removes file."""
        self.state = None
        if self.state_file.exists():
            try:
                self.state_file.unlink()
            except Exception as e:
                logger.warning("Could not delete state file: %s", e)

    def check_circuit_breakers(self, unrealized_pnl: float = 0.0) -> Optional[str]:
        """
        Enforces monthly circuit breakers:
        - Target Lock: Net month P&L >= +4.0%
        - Stop Out: Net month P&L <= -4.0%
        """
        if not self.state:
            return None

        status = self.state.get("status", "ACTIVE")
        if status in ("TARGET_LOCKED", "STOPPED_OUT"):
            return status

        realized = float(self.state.get("monthly_realized_pnl", 0.0))
        total_pnl = realized + unrealized_pnl
        net_pct = total_pnl / self.total_capital

        target_gain = self.total_capital * self.monthly_target_pct
        max_stop_loss = -self.total_capital * self.monthly_stop_pct

        if total_pnl >= target_gain:
            logger.info("TARGET ACHIEVED: Net month P&L Rs %.2f (+%.2f%%) >= Target (+%.2f%%). Locking capital.",
                        total_pnl, net_pct * 100, self.monthly_target_pct * 100)
            self.state["status"] = "TARGET_LOCKED"
            self.state["monthly_realized_pnl"] = total_pnl
            self.save_state(self.state)
            if self.notifier:
                self.notifier.send_message(
                    f"🎯 <b>NIFTY Positional Target Achieved</b>\n"
                    f"Month P&L: <b>+Rs {total_pnl:,.2f} (+{net_pct*100:.2f}%)</b>\n"
                    f"All positions locked. Capital preserved until next monthly expiry."
                )
            return "TARGET_LOCKED"

        if total_pnl <= max_stop_loss:
            logger.warning("MAX LOSS HIT: Net month P&L Rs %.2f (%.2f%%) <= Stop (%.2f%%). Halting trading.",
                           total_pnl, net_pct * 100, -self.monthly_stop_pct * 100)
            self.state["status"] = "STOPPED_OUT"
            self.state["monthly_realized_pnl"] = total_pnl
            self.save_state(self.state)
            if self.notifier:
                self.notifier.send_message(
                    f"🛑 <b>NIFTY Positional Monthly Max Loss Stop Hit</b>\n"
                    f"Month P&L: <b>-Rs {abs(total_pnl):,.2f} ({net_pct*100:.2f}%)</b>\n"
                    f"All positions closed to preserve capital until next monthly expiry."
                )
            return "STOPPED_OUT"

        return None

    def close_current_trade(self, kite: KiteConnect, reason: str = "EXIT") -> float:
        """
        Squares off all legs of the current active trade and calculates realized P&L.
        """
        if not self.state or not self.state.get("current_trade"):
            return 0.0

        trade = self.state["current_trade"]
        legs = trade.get("legs", [])
        if not legs:
            self.state["current_trade"] = None
            self.save_state(self.state)
            return 0.0

        symbols = [leg["symbol"] for leg in legs]
        quotes_map: Dict[str, float] = {}
        try:
            raw_quotes = kite.ltp([f"NFO:{s}" if ":" not in s else s for s in symbols])
            for s in symbols:
                k = f"NFO:{s}" if ":" not in s else s
                quotes_map[s] = float(raw_quotes.get(k, {}).get("last_price", 0.0))
        except Exception as e:
            logger.warning("Could not fetch exit quotes: %s", e)

        trade_pnl = 0.0
        mode = getattr(self.order_manager, "mode", "PAPER")

        for leg in legs:
            sym = leg["symbol"]
            qty = leg["qty"]
            action = leg["action"]
            entry_price = float(leg.get("entry_price", 0.0))
            exit_price = quotes_map.get(sym, entry_price)

            if action == "BUY":
                # Sell to exit long leg
                self.order_manager.sell(sym, qty, exit_price, order_type="LIMIT", buffer_pct=0.05)
                leg_pnl = (exit_price - entry_price) * qty
            else:
                # Buy to cover short leg
                self.order_manager.buy_to_cover(sym, qty, exit_price, reason=reason, order_type="LIMIT", buffer_pct=0.05)
                leg_pnl = (entry_price - exit_price) * qty

            trade_pnl += leg_pnl
            logger.info("[%s] Exited %s %s x %d @ Rs %.2f (P&L: Rs %.2f, Reason: %s)",
                        mode, action, sym, qty, exit_price, leg_pnl, reason)

        self.state["monthly_realized_pnl"] = float(self.state.get("monthly_realized_pnl", 0.0)) + trade_pnl
        self.state["current_trade"] = None
        self.save_state(self.state)
        logger.info("Closed %s trade. Realized Trade P&L: Rs %.2f. Cumulative Month P&L: Rs %.2f",
                    trade.get("type"), trade_pnl, self.state["monthly_realized_pnl"])

        if self.notifier:
            pnl_icon = "🟢" if trade_pnl >= 0 else "🔴"
            self.notifier.send_message(
                f"📤 <b>NIFTY Positional Trade Exited [{mode}]</b>\n"
                f"Strategy: <b>{trade.get('type')}</b> ({trade.get('regime', 'UNKNOWN')})\n"
                f"Reason: <code>{reason}</code>\n"
                f"Trade Realized P&L: <b>{pnl_icon} Rs {trade_pnl:,.2f}</b>\n"
                f"Month Realized P&L: <b>Rs {self.state['monthly_realized_pnl']:,.2f}</b>"
            )

        return trade_pnl

    def deploy_iron_condor(self, condor_setup: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deploys 4 legs of a 1:1 Iron Condor:
        - Buy PE Wing (far OTM)
        - Sell PE Body (inner OTM)
        - Sell CE Body (inner OTM)
        - Buy CE Wing (far OTM)
        """
        qty = condor_setup["total_quantity"]
        mode = getattr(self.order_manager, "mode", "PAPER")

        pe_buy = condor_setup["pe_buy"]
        pe_sell = condor_setup["pe_sell"]
        ce_sell = condor_setup["ce_sell"]
        ce_buy = condor_setup["ce_buy"]

        # 1. Buy outer wings first (avoids margin penalty in live trading)
        self.order_manager.buy_to_cover(pe_buy["tradingsymbol"], qty, pe_buy["ltp"], reason="CONDOR_WING")
        self.order_manager.buy_to_cover(ce_buy["tradingsymbol"], qty, ce_buy["ltp"], reason="CONDOR_WING")

        # 2. Sell inner legs
        self.order_manager.sell(pe_sell["tradingsymbol"], qty, pe_sell["ltp"])
        self.order_manager.sell(ce_sell["tradingsymbol"], qty, ce_sell["ltp"])

        trade_info = {
            "type": "IRON_CONDOR",
            "regime": "SIDEWAYS",
            "expiry": str(condor_setup["expiry"]),
            "entry_date": datetime.now().strftime("%Y-%m-%d"),
            "net_credit": condor_setup["net_credit"],
            "max_loss": condor_setup["max_loss"],
            "lower_breakeven": condor_setup["lower_breakeven"],
            "upper_breakeven": condor_setup["upper_breakeven"],
            "legs": [
                {"symbol": pe_buy["tradingsymbol"], "strike": pe_buy["strike"], "type": "PE", "action": "BUY", "qty": qty, "entry_price": pe_buy["ltp"]},
                {"symbol": pe_sell["tradingsymbol"], "strike": pe_sell["strike"], "type": "PE", "action": "SELL", "qty": qty, "entry_price": pe_sell["ltp"]},
                {"symbol": ce_sell["tradingsymbol"], "strike": ce_sell["strike"], "type": "CE", "action": "SELL", "qty": qty, "entry_price": ce_sell["ltp"]},
                {"symbol": ce_buy["tradingsymbol"], "strike": ce_buy["strike"], "type": "CE", "action": "BUY", "qty": qty, "entry_price": ce_buy["ltp"]},
            ]
        }

        if self.notifier:
            self.notifier.send_message(
                f"🦅 <b>NIFTY 1:1 Iron Condor Deployed [{mode}]</b>\n"
                f"Expiry: <code>{condor_setup['expiry']}</code> | Lots: {self.lots}\n"
                f"• PE Spread: Buy {pe_buy['strike']} @ Rs {pe_buy['ltp']:.1f} | Sell {pe_sell['strike']} @ Rs {pe_sell['ltp']:.1f}\n"
                f"• CE Spread: Sell {ce_sell['strike']} @ Rs {ce_sell['ltp']:.1f} | Buy {ce_buy['strike']} @ Rs {ce_buy['ltp']:.1f}\n"
                f"• Net Credit: <b>Rs {condor_setup['net_credit']:.2f}</b> | Max Loss: Rs {condor_setup['max_loss']:.2f}\n"
                f"• BE Range: <b>{condor_setup['lower_breakeven']:.0f} – {condor_setup['upper_breakeven']:.0f}</b>"
            )

        return trade_info

    def deploy_directional_ratio(self, ratio_setup: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deploys 1:2:1 Directional Ratio structure based on user spec:
        - Buy 1 lot @ ATM +- 50
        - Sell 2 lots @ ATM +- 250
        - Buy 1 lot @ ATM +- 300
        """
        mode = getattr(self.order_manager, "mode", "PAPER")
        leg1 = ratio_setup["buy_leg_1"]
        body = ratio_setup["sell_body"]
        leg2 = ratio_setup["buy_leg_2"]

        # Buy wings first to avoid margin spikes
        self.order_manager.buy_to_cover(leg1["symbol"], leg1["quantity"], 0.0, reason="RATIO_WING_1")
        self.order_manager.buy_to_cover(leg2["symbol"], leg2["quantity"], 0.0, reason="RATIO_WING_2")
        self.order_manager.sell(body["symbol"], body["quantity"], 0.0)

        trade_info = {
            "type": "DIRECTIONAL_RATIO",
            "regime": ratio_setup["regime"],
            "expiry": str(ratio_setup["expiry"]),
            "entry_date": datetime.now().strftime("%Y-%m-%d"),
            "legs": [
                {"symbol": leg1["symbol"], "strike": leg1["strike"], "type": ratio_setup["instrument_type"], "action": "BUY", "qty": leg1["quantity"], "entry_price": 0.0},
                {"symbol": body["symbol"], "strike": body["strike"], "type": ratio_setup["instrument_type"], "action": "SELL", "qty": body["quantity"], "entry_price": 0.0},
                {"symbol": leg2["symbol"], "strike": leg2["strike"], "type": ratio_setup["instrument_type"], "action": "BUY", "qty": leg2["quantity"], "entry_price": 0.0},
            ]
        }

        if self.notifier:
            icon = "📉" if ratio_setup["regime"] == "BEARISH" else "📈"
            self.notifier.send_message(
                f"{icon} <b>NIFTY 1:2:1 {ratio_setup['regime']} Ratio Deployed [{mode}]</b>\n"
                f"Expiry: <code>{ratio_setup['expiry']}</code> | ATM: {ratio_setup['atm_strike']}\n"
                f"• Buy 1x: {leg1['strike']} {ratio_setup['instrument_type']} ({leg1['quantity']} qty)\n"
                f"• Sell 2x: {body['strike']} {ratio_setup['instrument_type']} ({body['quantity']} qty)\n"
                f"• Buy 1x: {leg2['strike']} {ratio_setup['instrument_type']} ({leg2['quantity']} qty)"
            )

        return trade_info

    def evaluate_daily_close(
        self,
        kite: KiteConnect,
        execute_once: bool = False,
        force_date: Optional[date] = None,
        dry_run: bool = False,
        ignore_holidays: bool = False
    ) -> Dict[str, Any]:
        """
        Executes daily 15:20 IST evaluation routine:
        1. Checks trading holidays
        2. Loads/initializes monthly state
        3. Enforces monthly circuit breakers (+4% target / -4% stop)
        4. Handles monthly contract rollover on expiry
        5. Computes Daily RSI(14) to determine regime (SIDEWAYS, BEARISH, BULLISH)
        6. Manages trade deployment, regime transitions, and Iron Condor break-even rolls
        """
        today_date = force_date or date.today()
        month_str = today_date.strftime("%Y-%m")

        if not ignore_holidays:
            is_holiday, reason = is_trading_holiday(check_date=today_date)
            if is_holiday:
                logger.info("Exchange is closed today (%s). Skipping daily evaluation.", reason)
                return {"status": "HOLIDAY", "reason": reason}

        state = self.load_state()
        if state is None or state.get("month") != month_str:
            instruments = kite.instruments(exchange="NFO")
            monthly_exp = get_monthly_expiry(instruments, current_date=today_date)
            state = {
                "month": month_str,
                "expiry": str(monthly_exp),
                "status": "ACTIVE",
                "regime": "UNKNOWN",
                "monthly_realized_pnl": 0.0,
                "current_trade": None
            }
            self.save_state(state)

        # Check circuit breakers from prior evaluations
        if state.get("status") in ("TARGET_LOCKED", "STOPPED_OUT"):
            logger.info("Month %s is %s. Preserving capital until next monthly expiry.", month_str, state.get("status"))
            return state

        expiry_date = parse_expiry(state["expiry"])

        # Check if monthly contract reached expiry
        if today_date >= expiry_date:
            logger.info("Expiry date reached (%s >= %s). Closing open trade and rolling to next month.", today_date, expiry_date)
            if state.get("current_trade"):
                self.close_current_trade(kite, reason="MONTHLY_EXPIRY_ROLL")
            instruments = kite.instruments(exchange="NFO")
            next_exp = get_monthly_expiry(instruments, current_date=today_date + timedelta(days=1))
            state["expiry"] = str(next_exp)
            state["status"] = "ACTIVE"
            self.save_state(state)
            expiry_date = next_exp

        # Compute unrealized MTM of active position
        trade = state.get("current_trade")
        unrealized_pnl = 0.0
        if trade and trade.get("legs"):
            symbols = [leg["symbol"] for leg in trade["legs"]]
            try:
                raw_quotes = kite.ltp([f"NFO:{s}" if ":" not in s else s for s in symbols])
                for leg in trade["legs"]:
                    k = f"NFO:{leg['symbol']}" if ":" not in leg['symbol'] else leg['symbol']
                    ltp = float(raw_quotes.get(k, {}).get("last_price", leg.get("entry_price", 0.0)))
                    if leg["action"] == "BUY":
                        unrealized_pnl += (ltp - float(leg.get("entry_price", 0.0))) * leg["qty"]
                    else:
                        unrealized_pnl += (float(leg.get("entry_price", 0.0)) - ltp) * leg["qty"]
            except Exception as e:
                logger.warning("Could not fetch MTM quotes for active trade: %s", e)

        # Circuit breaker evaluation with real-time MTM
        cb_status = self.check_circuit_breakers(unrealized_pnl=unrealized_pnl)
        if cb_status in ("TARGET_LOCKED", "STOPPED_OUT"):
            if trade:
                self.close_current_trade(kite, reason=cb_status)
            return self.state or {}

        # Fetch Daily RSI(14) and spot price
        latest_rsi, regime, _ = fetch_daily_nifty_rsi(kite)
        spot_quote = kite.ltp(["NSE:NIFTY 50"])
        spot_price = float(spot_quote.get("NSE:NIFTY 50", {}).get("last_price", 0.0))
        if spot_price <= 0:
            spot_price = float(spot_quote.get("NIFTY 50", {}).get("last_price", 0.0))

        logger.info("Daily Evaluation at 15:20 IST | Spot: %.2f | RSI: %.2f | Regime: %s", spot_price, latest_rsi, regime)
        instruments = kite.instruments(exchange="NFO")

        if not trade:
            # Deploy initial position
            if regime == "SIDEWAYS":
                pe_syms = [inst["tradingsymbol"] for inst in instruments if inst.get("name") == "NIFTY" and parse_expiry(inst.get("expiry")) == expiry_date and inst.get("instrument_type") == "PE"]
                ce_syms = [inst["tradingsymbol"] for inst in instruments if inst.get("name") == "NIFTY" and parse_expiry(inst.get("expiry")) == expiry_date and inst.get("instrument_type") == "CE"]
                all_syms = [f"NFO:{s}" for s in (pe_syms + ce_syms)]
                chain_quotes: Dict[str, float] = {}
                for i in range(0, len(all_syms), 100):
                    q = kite.ltp(all_syms[i:i+100])
                    for k, v in q.items():
                        chain_quotes[k] = float(v.get("last_price", 0.0))

                condor_setup = solve_1_to_1_iron_condor(spot_price, expiry_date, instruments, chain_quotes, lots=self.lots)
                if dry_run:
                    logger.info("[DRY-RUN] Planned 1:1 Iron Condor: %s", condor_setup)
                    return {
                        "status": "DRY_RUN",
                        "regime": "SIDEWAYS",
                        "month": month_str,
                        "expiry": str(expiry_date),
                        "setup": condor_setup,
                        "monthly_realized_pnl": state.get("monthly_realized_pnl", 0.0)
                    }
                new_trade = self.deploy_iron_condor(condor_setup)
                state["current_trade"] = new_trade
                state["regime"] = "SIDEWAYS"
                self.save_state(state)
                return state
            else:
                ratio_setup = get_directional_ratio_strikes(spot_price, regime, expiry_date, instruments, lots=self.lots)
                if dry_run:
                    logger.info("[DRY-RUN] Planned 1:2:1 %s Ratio: %s", regime, ratio_setup)
                    return {
                        "status": "DRY_RUN",
                        "regime": regime,
                        "month": month_str,
                        "expiry": str(expiry_date),
                        "setup": ratio_setup,
                        "monthly_realized_pnl": state.get("monthly_realized_pnl", 0.0)
                    }
                new_trade = self.deploy_directional_ratio(ratio_setup)
                state["current_trade"] = new_trade
                state["regime"] = regime
                self.save_state(state)
                return state

        # Trade is active: check for regime transition
        current_regime = trade.get("regime")
        if current_regime != regime:
            logger.info("Regime transition detected (%s -> %s). Closing existing trade and re-evaluating.", current_regime, regime)
            if not dry_run:
                self.close_current_trade(kite, reason=f"REGIME_TRANSITION_TO_{regime}")
                return self.evaluate_daily_close(kite, execute_once=execute_once, force_date=today_date, dry_run=dry_run, ignore_holidays=ignore_holidays)
            return {"status": "DRY_RUN", "action": f"TRANSITION_{current_regime}_TO_{regime}"}

        # Check break-even breach for Iron Condors
        if current_regime == "SIDEWAYS" and trade.get("type") == "IRON_CONDOR":
            lower_be = float(trade.get("lower_breakeven", 0.0))
            upper_be = float(trade.get("upper_breakeven", float("inf")))
            if spot_price < lower_be or spot_price > upper_be:
                logger.warning("Iron Condor Break-Even Breached! Spot: %.2f outside [%.1f, %.1f]. Rolling into fresh condor.",
                               spot_price, lower_be, upper_be)
                if not dry_run:
                    self.close_current_trade(kite, reason="CONDOR_BE_BREACH_ROLL")
                    return self.evaluate_daily_close(kite, execute_once=execute_once, force_date=today_date, dry_run=dry_run, ignore_holidays=ignore_holidays)
                return {"status": "DRY_RUN", "action": "ROLL_CONDOR_BE_BREACH"}

        logger.info("Holding active %s trade. Regime: %s. Unrealized P&L: Rs %.2f", trade.get("type"), regime, unrealized_pnl)
        return state

    def run_scheduler(
        self,
        kite: KiteConnect,
        schedule_time: str = "15:20",
        poll_interval: int = 15,
        max_iterations: Optional[int] = None,
        ignore_holidays: bool = False
    ) -> None:
        """
        Runs automated daily evaluation daemon:
        Wakes up every weekday at schedule_time (default: 15:20 IST),
        verifies holiday calendar, executes daily evaluation, and persists state.
        """
        logger.info(
            "Starting Positional Condor Automated Daemon (Schedule: %s IST, Capital: Rs %s, Stop: %.1f%%, Target: %.1f%%)...",
            schedule_time, f"{self.total_capital:,.0f}", self.monthly_stop_pct * 100, self.monthly_target_pct * 100
        )
        if self.notifier:
            mode = getattr(self.order_manager, "mode", "PAPER")
            self.notifier.send_message(
                f"🤖 <b>NIFTY Positional Desk Automated Daemon Started [{mode}]</b>\n"
                f"Schedule: <b>Daily at {schedule_time} IST</b>\n"
                f"Lots: {self.lots} | Capital: Rs {self.total_capital:,.0f}\n"
                f"Risk Bounds: Stop -{self.monthly_stop_pct*100:.1f}% | Target +{self.monthly_target_pct*100:.1f}%"
            )

        target_h, target_m = map(int, schedule_time.split(":"))
        last_evaluated_date: Optional[date] = None
        last_heartbeat_hour: int = -1
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
                logger.info("Scheduler heartbeat [%s] | Active Month: %s | Status: %s | Realized P&L: Rs %.2f",
                            now.strftime("%Y-%m-%d %H:%M:%S"),
                            state.get("month", "NONE"),
                            state.get("status", "IDLE"),
                            float(state.get("monthly_realized_pnl", 0.0)))

            # Check if execution window reached (e.g. 15:20 - 15:28 IST)
            if (
                now.weekday() < 5
                and current_t >= dt_time(target_h, target_m)
                and current_t <= dt_time(target_h, target_m + 8)
                and last_evaluated_date != today_d
            ):
                logger.info("Triggering scheduled 15:20 IST evaluation for %s...", today_d)
                try:
                    self.evaluate_daily_close(kite=kite, ignore_holidays=ignore_holidays)
                    last_evaluated_date = today_d
                    logger.info("Evaluation complete for %s. Sleeping until next session.", today_d)
                except Exception as e:
                    logger.exception("Error during scheduled evaluation on %s: %s", today_d, e)
                    if self.notifier:
                        self.notifier.send_message(f"⚠️ <b>Positional Desk Error</b>: Scheduled evaluation failed on {today_d}: {e}")

            time.sleep(poll_interval)
