import argparse
import logging
import os
import sys
import time
from datetime import datetime

# Ensure stdout and stderr handle UTF-8 characters gracefully
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import load_config
from kite import get_kite_client
from notifications.telegram import TelegramNotifier
from strategy.logger import setup_logging
from strategy.nifty_orb import NiftyORBConfig, NiftyORBDesk, NiftyOrderManager

logger = logging.getLogger("run_nifty_orb")


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Sharad's NIFTY 50 Index 30-Min ORB Futures Trading Desk"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--paper", action="store_true", default=True, help="Run simulated PAPER trading (default)"
    )
    mode_group.add_argument(
        "--live", action="store_true", help="Execute real LIVE orders via Kite Connect"
    )

    parser.add_argument("--lots", type=int, default=2, help="Number of Nifty Futures lots (default: 2)")
    parser.add_argument("--sl", type=float, default=50.0, help="Initial Stop Loss in points (default: 50.0)")
    parser.add_argument("--target1", type=float, default=75.0, help="Tranche 1 Target in points (default: 75.0)")
    parser.add_argument("--poll", type=int, default=5, help="Polling interval in seconds (default: 5)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS", help="Product type (default: MIS)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT", help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pts", type=float, default=5.0, help="Marketable limit price buffer in points (default: 5.0)")
    parser.add_argument("--state-file", type=str, default=os.getenv("NIFTY_STATE_FILE", "data/nifty_orb_trades_today.json"), help="State file path")
    parser.add_argument("--dry-run", action="store_true", help="Inspect active contract and day state without trading")
    parser.add_argument("--once", action="store_true", help="Run single evaluation cycle and exit")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")

    parsed = parser.parse_args(args)
    if parsed.live:
        parsed.paper = False
    return parsed


def main(args=None) -> int:
    parsed = parse_args(args)
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level, log_filename="nifty_orb_desk.log")
    mode = "LIVE" if parsed.live else "PAPER"

    t1_lots = max(1, parsed.lots // 2)
    t2_lots = parsed.lots - t1_lots

    logger.info("=" * 75)
    logger.info("  STARTING NIFTY 50 INDEX 30-MIN ORB FUTURES DESK [%s MODE]", mode)
    logger.info("  Lots: %d | Initial SL: %.1f pts | Tranche 1 Target: %.1f pts", parsed.lots, parsed.sl, parsed.target1)
    logger.info("  Plan: Book Tranche 1 (%d lots) @ +%.1f pts & Move SL to Cost; Runner (%d lots) rides to 15:15 IST", t1_lots, parsed.target1, t2_lots)
    logger.info("=" * 75)

    config = load_config()
    kite = get_kite_client(config=config)
    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)

    nifty_cfg = NiftyORBConfig(
        lots=parsed.lots,
        initial_sl_pts=parsed.sl,
        target1_pts=parsed.target1,
        mode=mode,
        product=parsed.product,
        order_type=parsed.order_type,
        limit_buffer_pts=parsed.limit_buffer_pts,
        poll_interval_seconds=parsed.poll,
        state_file=parsed.state_file,
    )

    om = NiftyOrderManager(
        mode=mode,
        kite=kite if mode == "LIVE" else None,
        product=parsed.product,
        order_type=parsed.order_type,
        limit_buffer_pts=parsed.limit_buffer_pts,
    )

    desk = NiftyORBDesk(
        config=nifty_cfg,
        kite=kite,
        order_manager=om,
        notifier=notifier,
    )

    state = desk.initialize_day()
    logger.info("Active Contract: %s (Token: %s, Lot Size: %d)", state["symbol"], state["instrument_token"], state["lot_size"])

    if parsed.dry_run:
        logger.info("Dry-run complete. Exiting.")
        return 0

    if parsed.once:
        desk.run_monitoring_cycle(datetime.now().time(), datetime.now())
        return 0

    logger.info("Entering daemon monitoring loop (polling every %ds)...", parsed.poll)
    try:
        while True:
            now = datetime.now()
            desk.run_monitoring_cycle(now.time(), now)
            if desk.state and desk.state.get("status") == "COMPLETED":
                logger.info("Session complete. Desk finished for today.")
                break
            time.sleep(parsed.poll)
    except KeyboardInterrupt:
        logger.info("Shutdown requested. State preserved.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
