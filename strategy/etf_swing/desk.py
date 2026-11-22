"""
Desk Coordinator for Weekly Monthly-Fibonacci ETF Swing Trading.
Manages state persistence, weekly scans, order execution, and Telegram notifications.
"""

from typing import Dict, List, Optional
import os
import json
import logging
import datetime
from strategy.etf_swing.config import ETFSwingConfig
from strategy.etf_swing.scanner import ETFScanner
from strategy.etf_swing.order_manager import ETFOrderManager
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("etf_swing.desk")

class ETFSwingDesk:
    def __init__(self, config: ETFSwingConfig, kite_client=None, notifier: Optional[TelegramNotifier] = None):
        self.config = config
        self.kite = kite_client
        self.notifier = notifier
        self.scanner = ETFScanner(config, kite_client)
        self.orders = ETFOrderManager(config, kite_client)
        self.state = self._load_state()

    def _load_state(self) -> Dict:
        """
        Loads positions and trading history from state file.
        """
        if os.path.exists(self.config.state_file):
            try:
                with open(self.config.state_file, "r") as f:
                    state = json.load(f)
                    logger.info(f"Loaded existing ETF state with {len(state.get('positions', {}))} active positions.")
                    return state
            except Exception as e:
                logger.error(f"Failed to read state file {self.config.state_file}: {e}")
                
        # Default fresh state
        initial_state = {
            "initial_capital": self.config.total_capital,
            "cash": self.config.total_capital,
            "positions": {}, # symbol -> {quantity, entry_price, entry_date, cost}
            "trade_history": [],
            "last_rebalance": None
        }
        self._save_state(initial_state)
        return initial_state

    def _save_state(self, state: Optional[Dict] = None) -> None:
        """
        Saves current positions and history to disk.
        """
        if state is not None:
            self.state = state
        os.makedirs(os.path.dirname(self.config.state_file), exist_ok=True)
        with open(self.config.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def scan_status(self) -> Dict:
        """
        Runs a comprehensive scan and returns current status and recommendations.
        """
        current_holdings = list(self.state.get("positions", {}).keys())
        scan_results = self.scanner.scan_universe(current_holdings)
        return scan_results

    def execute_rebalance(self) -> Dict:
        """
        Executes weekly portfolio rebalancing:
        1. Exits any holding that closed below S1.
        2. Calculates free slots (up to max_slots).
        3. Enters top momentum candidates that closed above Monthly Pivot.
        4. Updates state and sends Telegram notifications.
        """
        logger.info(f"Starting Weekly ETF Rebalance in {self.config.mode} mode...")
        scan_results = self.scan_status()
        
        exits_to_run = scan_results["exits"]
        candidates = scan_results["candidates"]
        
        exited_trades = []
        new_entries = []
        
        # 1. Process EXITS
        for symbol in exits_to_run:
            pos = self.state["positions"].get(symbol)
            if not pos:
                continue
            
            qty = pos["quantity"]
            # Get latest price from details table
            df_details = scan_results["details"]
            row = df_details[df_details["Symbol"] == symbol]
            current_price = row["LTP"].iloc[0] if len(row) > 0 else pos["entry_price"]
            
            res = self.orders.execute_sell(symbol, qty, current_price)
            if res.get("status") == "COMPLETE":
                sold_qty = res.get("quantity", qty)
                sold_price = res.get("price", current_price)
                proceeds = sold_qty * sold_price
                
                # Pro-rate cost basis if partially sold
                cost_basis = pos["cost"] * (sold_qty / pos["quantity"]) if pos["quantity"] > 0 else pos["cost"]
                pnl = proceeds - cost_basis
                pnl_pct = (sold_price / pos["entry_price"] - 1.0) * 100.0 if pos["entry_price"] > 0 else 0.0
                
                self.state["cash"] += proceeds
                trade_record = {
                    "symbol": symbol,
                    "entry_date": pos["entry_date"],
                    "entry_price": pos["entry_price"],
                    "exit_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "exit_price": sold_price,
                    "quantity": sold_qty,
                    "pnl": round(pnl, 2),
                    "return_pct": round(pnl_pct, 2)
                }
                self.state["trade_history"].append(trade_record)
                exited_trades.append(trade_record)
                
                if sold_qty >= pos["quantity"]:
                    del self.state["positions"][symbol]
                else:
                    self.state["positions"][symbol]["quantity"] -= sold_qty
                    self.state["positions"][symbol]["cost"] -= cost_basis
                    logger.warning(
                        f"Partial exit for {symbol}: sold {sold_qty}/{pos['quantity']}. Remaining {self.state['positions'][symbol]['quantity']} units retained in state."
                    )
                
        # 2. Process ENTRIES
        current_active = len(self.state["positions"])
        free_slots = self.config.max_slots - current_active
        
        if free_slots > 0 and len(candidates) > 0:
            target_allocation = self.config.capital_per_slot
            
            # Select top candidates up to free_slots
            to_buy = candidates[:free_slots]
            for cand in to_buy:
                sym = cand["symbol"]
                ltp = cand["ltp"]
                
                # Check available cash
                alloc = min(self.state["cash"], target_allocation)
                if alloc < 5000:
                    logger.warning(f"Insufficient cash (Rs {self.state['cash']:.0f}) to buy {sym}")
                    continue
                    
                res = self.orders.execute_buy(sym, alloc, ltp)
                if res.get("status") == "COMPLETE":
                    cost = res["cost"]
                    qty = res["quantity"]
                    fill_price = res.get("price", ltp)
                    if qty <= 0:
                        continue
                    self.state["cash"] -= cost
                    self.state["positions"][sym] = {
                        "symbol": sym,
                        "quantity": qty,
                        "entry_price": fill_price,
                        "cost": cost,
                        "entry_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                        "pivot": cand["pivot"],
                        "s1": cand["s1"]
                    }
                    new_entries.append({
                        "symbol": sym,
                        "quantity": qty,
                        "price": fill_price,
                        "cost": cost
                    })

        self.state["last_rebalance"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()

        # Send Telegram Summary
        self._notify_telegram(exited_trades, new_entries, scan_results)

        return {
            "exits": exited_trades,
            "entries": new_entries,
            "active_positions": self.state["positions"],
            "cash": self.state["cash"]
        }

    def _notify_telegram(self, exits: List[Dict], entries: List[Dict], scan_results: Dict):
        if not self.notifier:
            return

        msg_lines = [
            f"📊 <b>Weekly ETF Swing Desk Rebalance ({self.config.mode})</b>",
            f"📅 Date: {datetime.datetime.now().strftime('%d-%b-%Y %H:%M')}",
            f"💼 Active Slots: {len(self.state['positions'])}/{self.config.max_slots} | Free Cash: Rs {self.state['cash']:,.0f}\n"
        ]

        if exits:
            msg_lines.append("🔴 <b>EXITS EXECUTED:</b>")
            for ex in exits:
                emoji = "🟢" if ex["pnl"] >= 0 else "🔴"
                msg_lines.append(f"{emoji} {ex['symbol']}: Sold {ex['quantity']} @ Rs {ex['exit_price']} | Return: {ex['return_pct']}% (Rs {ex['pnl']:+,.0f})")
            msg_lines.append("")

        if entries:
            msg_lines.append("🟢 <b>NEW ENTRIES EXECUTED:</b>")
            for en in entries:
                msg_lines.append(f"✅ {en['symbol']}: Bought {en['quantity']} @ Rs {en['price']} (Alloc: Rs {en['cost']:,.0f})")
            msg_lines.append("")

        if not exits and not entries:
            msg_lines.append("⏸️ <b>NO REBALANCE NEEDED:</b> All current holdings valid above S1.\n")

        msg_lines.append("📌 <b>CURRENT PORTFOLIO:</b>")
        for sym, pos in self.state["positions"].items():
            msg_lines.append(f"• {sym}: {pos['quantity']} shares @ Rs {pos['entry_price']:.2f}")

        try:
            self.notifier.send_message("\n".join(msg_lines))
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
