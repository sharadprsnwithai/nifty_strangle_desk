"""FP-ALL Stock Swing Trading Desk with state persistence, ledger logging, and Telegram alerts."""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from strategy.ledger import append_trade_ledger
from strategy.stock_swing.config import StockSwingConfig
from strategy.stock_swing.order_manager import StockOrderManager
from strategy.stock_swing.scanner import StockSwingScanner

logger = logging.getLogger("stock_swing.desk")


class StockSwingDesk:
    """Manages FP-ALL stock swing positions, 1.5% risk sizing, CNC execution, and Telegram alerts."""

    def __init__(
        self,
        config: StockSwingConfig,
        kite_client: Any = None,
        notifier: Any = None,
        scanner: Optional[StockSwingScanner] = None,
        order_manager: Optional[StockOrderManager] = None,
    ):
        self.config = config
        self.kite = kite_client
        self.notifier = notifier
        self.scanner = scanner or StockSwingScanner(config)
        self.order_manager = order_manager or StockOrderManager(config, kite_client=kite_client)
        self.state_file = Path(config.state_file)
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Failed to load %s: %s", self.state_file, e)
        return {
            "cash": self.config.total_capital,
            "positions": {},
            "closed_trades": [],
            "last_run_date": None,
        }

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.with_suffix(".json.tmp")
        tmp_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp_file, self.state_file)

    def _total_equity(self, ltp_map: Dict[str, float]) -> float:
        mtm = sum(
            ltp_map.get(sym, float(pos["entry_price"])) * int(pos["quantity"])
            for sym, pos in self.state["positions"].items()
        )
        return float(self.state["cash"]) + mtm

    def compute_position_qty(self, total_equity: float, cash: float, entry_price: float, sl_price: float) -> int:
        """Size position at config.risk_pct (1.5%), capped at config.max_alloc_pct (25%) and cash."""
        risk_per_share = entry_price - sl_price
        if risk_per_share <= 0 or entry_price <= 0:
            return 0
        qty_by_risk = int((total_equity * self.config.risk_pct) / risk_per_share)
        qty_by_cap = int((total_equity * self.config.max_alloc_pct) / entry_price)
        qty_by_cash = int((cash * 0.98) / entry_price)
        return max(0, min(qty_by_risk, qty_by_cap, qty_by_cash))

    def scan_only(self, nifty_df=None, stock_data=None) -> Dict[str, Any]:
        """Run scan without placing orders."""
        return self.scanner.scan(self.state["positions"], nifty_df=nifty_df, stock_data=stock_data)

    def execute_daily_rebalance(self, nifty_df=None, stock_data=None) -> Dict[str, Any]:
        """Scan universe, execute exits & 1.5%-risk CNC entries, log ledger, and send Telegram alert."""
        scan_res = self.scanner.scan(self.state["positions"], nifty_df=nifty_df, stock_data=stock_data)
        now_dt = datetime.datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        now_time = now_dt.strftime("%H:%M:%S")

        executed_exits: List[dict] = []
        executed_entries: List[dict] = []
        order_errors: List[str] = []

        # 1. Process exits first
        for ex in scan_res["exits"]:
            sym = ex["symbol"]
            pos = self.state["positions"].get(sym)
            if not pos:
                continue
            qty = int(pos["quantity"])
            res = self.order_manager.execute_sell(sym, qty, float(ex["ltp"]))
            if res.get("status") != "COMPLETE":
                err = f"SELL {sym} x {qty} failed: {res.get('reason', 'unknown')}"
                logger.error(err)
                order_errors.append(err)
                continue
            if res.get("status") == "COMPLETE":
                exit_px = float(res["price"])
                entry_px = float(pos["entry_price"])
                filled_qty = int(res["quantity"])
                pnl = round((exit_px - entry_px) * filled_qty, 2)
                total_risk = float(pos["risk_per_share"]) * filled_qty
                r_mult = round(pnl / total_risk, 2) if total_risk > 0 else 0.0

                self.state["cash"] = float(self.state["cash"]) + exit_px * filled_qty
                del self.state["positions"][sym]
                trade_rec = {
                    "symbol": sym,
                    "entry_date": pos["entry_date"],
                    "exit_date": today_str,
                    "days_held": ex["days_held"],
                    "quantity": filled_qty,
                    "entry_price": entry_px,
                    "exit_price": exit_px,
                    "exit_reason": ex["exit_reason"],
                    "net_pnl": pnl,
                    "r_multiple": r_mult,
                }
                self.state["closed_trades"].append(trade_rec)
                executed_exits.append(trade_rec)

                append_trade_ledger(
                    {
                        "date": today_str,
                        "desk": "DESK5_STOCK_SWING",
                        "mode": self.config.mode,
                        "direction": "LONG",
                        "instrument": sym,
                        "lots": 1,
                        "quantity": filled_qty,
                        "entry_time": pos.get("entry_time", ""),
                        "exit_time": now_time,
                        "entry_price": entry_px,
                        "exit_price": exit_px,
                        "why_taken": "FP_ALL_15D_BASE",
                        "exit_reason": ex["exit_reason"],
                        "realized_pnl": pnl,
                    },
                    ledger_path=self.state_file.parent / "trade_ledger.csv",
                )

        # Update days_held for continuing positions
        for h in scan_res["holds"]:
            sym = h["symbol"]
            if sym in self.state["positions"]:
                self.state["positions"][sym]["days_held"] = h["days_held"]

        # 2. Process new entries
        ltp_map = {h["symbol"]: float(h["ltp"]) for h in scan_res["holds"]}
        for c in scan_res["candidates"]:
            ltp_map[c["symbol"]] = float(c["ltp"])

        for cand in scan_res["candidates"]:
            if len(self.state["positions"]) >= self.config.max_positions:
                break
            sym = cand["symbol"]
            if sym in self.state["positions"]:
                continue

            total_eq = self._total_equity(ltp_map)
            cash = float(self.state["cash"])
            qty = self.compute_position_qty(total_eq, cash, float(cand["ltp"]), float(cand["sl_price"]))
            if qty <= 0:
                continue

            res = self.order_manager.execute_buy(sym, qty, float(cand["ltp"]))
            if res.get("status") != "COMPLETE":
                err = f"BUY {sym} x {qty} failed: {res.get('reason', 'unknown')}"
                logger.error(err)
                order_errors.append(err)
                continue
            if res.get("status") == "COMPLETE":
                fill_px = float(res["price"])
                filled_qty = int(res["quantity"])
                self.state["cash"] = float(self.state["cash"]) - fill_px * filled_qty
                pos_rec = {
                    "symbol": sym,
                    "entry_date": today_str,
                    "entry_time": now_time,
                    "entry_price": fill_px,
                    "quantity": filled_qty,
                    "sl_price": float(cand["sl_price"]),
                    "risk_per_share": float(cand["risk_per_share"]),
                    "days_held": 0,
                    "order_id": res.get("order_id", ""),
                }
                self.state["positions"][sym] = pos_rec
                executed_entries.append({**pos_rec, "vol_ratio": cand["vol_ratio"]})

        self.state["last_run_date"] = today_str
        self._save_state()
        self._notify_telegram(executed_exits, executed_entries, scan_res, order_errors=order_errors)
        return {
            "exits": executed_exits,
            "entries": executed_entries,
            "errors": order_errors,
            "scan": scan_res,
        }

    def _notify_telegram(
        self,
        exits: List[dict],
        entries: List[dict],
        scan_res: Dict[str, Any],
        order_errors: Optional[List[str]] = None,
    ) -> None:
        if not self.notifier:
            return
        regime_tag = "ON" if scan_res["regime_ok"] else "OFF (Cash)"
        lines = [
            f"<b>[STOCK SWING - {self.config.mode}] FP-ALL Daily Scan (Risk 1.5%)</b>",
            f"Nifty50: {scan_res['nifty_close']} (EMA50: {scan_res['nifty_ema50']}) | Regime: <b>{regime_tag}</b>",
        ]
        if exits:
            lines.append("\n<b>Exits Executed:</b>")
            for ex in exits:
                lines.append(
                    f"• SELL <code>{ex['symbol']}</code> x {ex['quantity']} @ ₹{ex['exit_price']:.2f} "
                    f"({ex['exit_reason']}) | P&amp;L: <b>₹{ex['net_pnl']:+,.0f} ({ex['r_multiple']:+.2f}R)</b>"
                )
        if entries:
            lines.append("\n<b>New CNC Entries:</b>")
            for en in entries:
                lines.append(
                    f"• BUY <code>{en['symbol']}</code> x {en['quantity']} @ ₹{en['entry_price']:.2f} "
                    f"| SL: ₹{en['sl_price']:.2f} | Vol: {en['vol_ratio']}x"
                )
        if not exits and not entries and not order_errors:
            lines.append(f"\nNo new trades today. Candidates found: {len(scan_res['candidates'])}")
        if order_errors:
            lines.append("\n<b>⚠️ Order Errors:</b>")
            for err in order_errors:
                lines.append(f"• {err}")

        lines.append(
            f"\nOpen Positions: {len(self.state['positions'])}/{self.config.max_positions} | "
            f"Cash: ₹{float(self.state['cash']):,.0f}"
        )
        self.notifier.send_message("\n".join(lines))
