import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

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
from strategy.silver_orb import SilverORBConfig, SilverORBDesk, SilverOrderManager

logger = logging.getLogger("run_silver_orb")


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Sharad's MCX Silver Mini 18:00 US Session Breakout Desk"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--live", action="store_true", help="Execute real LIVE orders via Kite Connect"
    )
    mode_group.add_argument(
        "--paper", action="store_true", help="Run simulated PAPER trading (default)"
    )

    parser.add_argument("--lots", type=int, default=2, help="Number of Silver Mini lots (default: 2)")
    parser.add_argument("--sl", type=float, default=500.0, help="Initial Stop Loss in points (default: 500.0)")
    parser.add_argument("--target1", type=float, default=800.0, help="Lot 1 Target in points (default: 800.0)")
    parser.add_argument("--poll", type=int, default=5, help="Polling interval in seconds (default: 5)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS", help="Product type (default: MIS)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT", help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pts", type=float, default=15.0, help="Marketable limit price buffer in points (default: 15.0)")
    parser.add_argument("--state-file", type=str, default=os.getenv("SILVER_STATE_FILE", "data/silver_trades_today.json"), help="State file path")
    parser.add_argument("--dry-run", action="store_true", help="Inspect active contract and day state without trading")
    parser.add_argument("--once", action="store_true", help="Run single evaluation cycle and exit")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")

    return parser.parse_args(args)


def main(args=None) -> int:
    parsed = parse_args(args)
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level, log_filename="silver_orb_desk.log")
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("=" * 75)
    logger.info("  STARTING MCX SILVER MINI 18:00 US SESSION DESK [%s MODE]", mode)
    logger.info("  Lots: %d (10 kg total) | Initial SL: %.0f pts | Lot 1 Target: %.0f pts", parsed.lots, parsed.sl, parsed.target1)
    logger.info("  Plan: Book Lot 1 @ +%.0f pts & Move SL to Cost; Runner Lot 2 rides to 22:45 IST", parsed.target1)
    logger.info("=" * 75)

    config = load_config()
    kite = get_kite_client(config=config)
    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)

    silver_cfg = SilverORBConfig(
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

    om = SilverOrderManager(
        mode=mode,
        kite=kite if mode == "LIVE" else None,
        product=parsed.product,
        order_type=parsed.order_type,
        limit_buffer_pts=parsed.limit_buffer_pts,
    )

    desk = SilverORBDesk(
        config=silver_cfg,
        kite=kite,
        order_manager=om,
        notifier=notifier,
    )

    if parsed.dry_run:
        logger.info("[DRY RUN] Inspecting active contract and setup...")
        state = desk.initialize_day()
        logger.info("  Contract Symbol   : %s", state["contract"]["tradingsymbol"])
        logger.info("  Instrument Token  : %s", state["contract"]["instrument_token"])
        logger.info("  Expiry Date       : %s", state["contract"]["expiry"])
        logger.info("  Desk Status       : %s", state.get("status"))
        logger.info("  Realized Day P&L  : Rs %.2f", state.get("realized_pnl_rs", 0.0))
        logger.info("[DRY RUN] Inspection complete. Exiting without entering loop.")
        return 0

    if parsed.once:
        logger.info("[ONCE] Executing single evaluation cycle...")
        res = desk.run_session_cycle()
        logger.info("[ONCE] Cycle complete. Status: %s", res.get("status"))
        return 0

    try:
        desk.run_daemon()
        return 0
    except KeyboardInterrupt:
        logger.info("Silver ORB Desk stopped by user (Ctrl+C). Saving state...")
        desk.save_state(desk.load_state())
        return 0
    except Exception as fatal_err:
        logger.exception("Fatal unhandled error in Silver ORB Desk: %s", fatal_err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
