#!/usr/bin/env python3
"""
Automated Scheduler for Nifty 200 Momentum 30 Universe Rehydration.
Runs on a scheduled cron cadence (default: 08:30 AM IST every trading day) to check for:
1. Rebalanced constituent lists in drop files (data/momentum_30_constituents.txt or .csv).
2. Live F&O lot sizes from Kite Connect instrument master.
3. Automatically writes data/momentum_30_universe.json to trigger hot-reloading in the live desk.
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from kite import get_kite_client
from strategy.logger import setup_logging
from notifications.telegram import TelegramNotifier
from strategy.stock_futures.rehydrator import UniverseRehydrator

logger = logging.getLogger("scheduler.rehydrate")


def parse_args():
    parser = argparse.ArgumentParser(description="Automated Rehydration Scheduler")
    parser.add_argument("--now", action="store_true", help="Run rehydration check immediately and exit")
    parser.add_argument("--loop", action="store_true", default=True, help="Run continuously on daily schedule (default: True)")
    parser.add_argument("--schedule-time", type=str, default="08:30", help="Daily execution time IST (default: 08:30)")
    parser.add_argument("--universe-file", type=str, default="data/momentum_30_universe.json", help="Target universe JSON path")
    parser.add_argument("--log-dir", type=str, default="logs", help="Host logs directory")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging verbosity")
    return parser.parse_args()


def run_rehydration(universe_file: Path, kite=None, notifier=None):
    logger.info("Executing scheduled universe rehydration check...")
    rehydrator = UniverseRehydrator(kite=kite, universe_file=universe_file)
    try:
        result = rehydrator.auto_rehydrate(save=True)
        symbols = result["symbols"]
        changes = result["changes"]

        if changes["is_changed"]:
            logger.info("Universe rehydrated with CHANGES: Added %s | Removed %s", changes["added"], changes["removed"])
            if notifier:
                msg = (
                    "🔄 *MOMENTUM 30 UNIVERSE REHYDRATED (SCHEDULER)*\n"
                    f"Active Equities: {len(symbols)}\n"
                    f"Added: {', '.join(changes['added']) if changes['added'] else 'None'}\n"
                    f"Removed: {', '.join(changes['removed']) if changes['removed'] else 'None'}\n"
                    f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                notifier.send_message(msg)
        else:
            logger.info("Universe verified: all %d constituents and lot sizes are current.", len(symbols))
        return result
    except Exception as e:
        logger.exception("Failed to execute scheduled rehydration: %s", e)
        return None


def main():
    args = parse_args()
    setup_logging(log_dir=args.log_dir, log_level=args.log_level, log_filename="stock_futures_desk.log")

    logger.info("=" * 80)
    logger.info("  NIFTY 200 MOMENTUM 30 AUTOMATED REHYDRATION SCHEDULER")
    logger.info("  Scheduled Time: %s IST | Target: %s", args.schedule_time, args.universe_file)
    logger.info("=" * 80)

    # Initialize Kite Connect & Telegram
    kite = None
    notifier = None
    try:
        cfg = load_config()
        kite = get_kite_client(config=cfg)
        if cfg.telegram_bot_token and cfg.telegram_chat_id:
            notifier = TelegramNotifier(bot_token=cfg.telegram_bot_token, chat_id=cfg.telegram_chat_id)
    except Exception as e:
        logger.info("Running scheduler without live Kite credentials (%s). Default lot sizes will be used.", e)

    universe_path = Path(args.universe_file)

    if args.now:
        logger.info("Running on-demand rehydration (--now)...")
        run_rehydration(universe_file=universe_path, kite=kite, notifier=notifier)
        return 0

    last_run_date = None
    logger.info("Scheduler daemon started. Waiting for %s IST on weekdays...", args.schedule_time)

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            now_time = now.strftime("%H:%M")
            weekday = now.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday

            # Check if weekday (Mon-Fri) and matches scheduled time
            if weekday < 5 and now_time == args.schedule_time and last_run_date != today_str:
                last_run_date = today_str
                run_rehydration(universe_file=universe_path, kite=kite, notifier=notifier)

            time.sleep(20)
        except KeyboardInterrupt:
            logger.info("Scheduler daemon stopped by user.")
            break
        except Exception as e:
            logger.exception("Unexpected error in scheduler loop: %s", e)
            time.sleep(30)

    return 0


if __name__ == "__main__":
    main()
