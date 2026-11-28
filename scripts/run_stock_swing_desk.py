#!/usr/bin/env python3
"""Production CLI & Daemon for FP-ALL NSE Stock Swing Trading Desk (Risk 1.5%, CNC)."""

from __future__ import annotations

import argparse
import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

Path("logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "logs/stock_swing.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("strategy.stock_swing.run")

from config import load_config
from kite import get_kite_client
from kite.client import ensure_kite_session
from notifications.telegram import TelegramNotifier
from strategy.holidays import is_nse_holiday
from strategy.stock_swing import StockSwingConfig, StockSwingDesk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FP-ALL NSE Stock Swing Desk (1.5% Risk, CNC)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE CNC orders via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", default=True, help="Run PAPER simulation (default)")

    parser.add_argument("--capital", type=float, default=1_000_000.0, help="Total capital (default: 10,00,000)")
    parser.add_argument("--risk", type=float, default=0.015, help="Risk per trade fraction (default: 0.015 = 1.5%%)")
    parser.add_argument("--slots", type=int, default=5, help="Max concurrent positions (default: 5)")
    parser.add_argument("--scan-only", "--scan", action="store_true", help="Scan only without placing orders")
    parser.add_argument("--execute", action="store_true", help="Run daily scan and execute exits/entries now")
    parser.add_argument("--loop", action="store_true", help="Run daily daemon executing at 15:20 IST on trading days")
    parser.add_argument("--state-file", type=str, default="data/stock_swing_state.json", help="JSON state path")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram alerts")
    return parser.parse_args()


def build_notifier(disabled: bool) -> TelegramNotifier | None:
    if disabled:
        return None
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        return TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    logger.info("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; Telegram alerts disabled.")
    return None


def main() -> int:
    load_config()
    args = parse_args()
    mode = "LIVE" if args.live else "PAPER"

    kite_client = None
    if mode == "LIVE":
        logger.info("Connecting to Zerodha Kite Connect for LIVE CNC execution...")
        try:
            kite_client = get_kite_client()
            profile = kite_client.profile()
            logger.info("Connected to Kite as: %s (%s)", profile.get("user_name"), profile.get("user_id"))
        except Exception as e:
            logger.error("Kite authentication failed: %s", e)
            return 1

    notifier = build_notifier(args.no_telegram)
    config = StockSwingConfig(
        total_capital=args.capital,
        risk_pct=args.risk,
        max_positions=args.slots,
        mode=mode,
        state_file=args.state_file,
    )
    desk = StockSwingDesk(config=config, kite_client=kite_client, notifier=notifier)

    if args.loop:
        logger.info("Starting FP-ALL Stock Swing daemon (15:20 IST on trading days, Mode=%s)", mode)
        while True:
            try:
                now = datetime.datetime.now()
                today = now.date()
                today_str = today.strftime("%Y-%m-%d")
                time_str = now.strftime("%H:%M")
                already_ran = desk.state.get("last_run_date") == today_str
                is_trading_day = today.weekday() < 5 and not is_nse_holiday(today)
                if is_trading_day and not already_ran and "15:20" <= time_str <= "15:29":
                    logger.info("15:20 IST window reached; refreshing session & running FP-ALL rebalance...")
                    if mode == "LIVE":
                        refreshed = ensure_kite_session(desk.kite, force_login=False)
                        desk.kite = refreshed
                        desk.order_manager.kite = refreshed
                    desk.execute_daily_rebalance()
            except KeyboardInterrupt:
                logger.info("Daemon loop stopped by user.")
                break
            except Exception as e:
                logger.exception("Error in Stock Swing daemon loop: %s", e)
                if notifier:
                    notifier.send_message(f"⚠️ <b>[STOCK SWING - {mode}] Daemon Error:</b> {e}")
            time.sleep(30)
        return 0

    if args.execute and not args.scan_only:
        res = desk.execute_daily_rebalance()
        print(
            f"Done [{mode}] | Regime: {res['scan']['regime_ok']} | "
            f"Exits: {len(res['exits'])} | Entries: {len(res['entries'])}"
        )
        return 0

    scan = desk.scan_only()
    print(
        f"Scan [{mode}] | Nifty50: {scan['nifty_close']} (EMA50: {scan['nifty_ema50']}) | "
        f"Regime: {'ON' if scan['regime_ok'] else 'OFF'}"
    )
    print(f"Exits: {len(scan['exits'])} | Holds: {len(scan['holds'])} | Candidates: {len(scan['candidates'])}")
    for c in scan["candidates"]:
        print(
            f"  CANDIDATE {c['symbol']}: LTP={c['ltp']} | SL={c['sl_price']} | "
            f"Vol={c['vol_ratio']}x | Trigger={c['trigger_high']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
