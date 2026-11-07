import os
import sys
import time
import argparse
import logging
from datetime import datetime, time as dt_time
from pathlib import Path

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
from strategy.orb_desk import ORBConfig, ORBOrderManager, ORBDesk, TARGET_STOCKS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("run_orb")


def parse_args(args=None):
    parser = argparse.ArgumentParser(description="Sharad's 15-Minute ORB Intraday Desk (Indrajit Shantharaj Strategy)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--paper", action="store_true", default=True, help="Run simulated PAPER trading with live Kite data (default)")
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")

    parser.add_argument("--risk", type=float, default=2500.0, help="Fixed Rupee Risk per trade in FIXED_RISK mode (default: 2500.0)")
    parser.add_argument("--sizing-mode", choices=["FIXED_RISK", "LOTS"], default="FIXED_RISK", help="Position sizing mode (default: FIXED_RISK)")
    parser.add_argument("--lots", type=int, default=2, help="Number of futures lots to trade if sizing-mode is LOTS (default: 2)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT", help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.002, help="Marketable limit price buffer percentage (default: 0.002 / 0.2%%)")
    parser.add_argument("--max-trades", type=int, default=5, help="Maximum trades per day (default: 5)")
    parser.add_argument("--daily-loss", type=float, default=10000.0, help="Daily Portfolio Circuit Breaker Loss limit (default: 10000.0)")
    parser.add_argument("--poll", type=int, default=5, help="Monitoring polling interval in seconds (default: 5)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS", help="Order product type (default: MIS)")
    parser.add_argument("--exchange", choices=["NSE", "NFO"], default="NSE", help="Exchange segment (default: NSE)")
    parser.add_argument("--dry-run", action="store_true", help="Initialize desk, verify Kite and Telegram, and display setup status without looping")
    parser.add_argument("--once", action="store_true", help="Run single scan/monitor cycle and exit")

    return parser.parse_args(args)


def main(args=None):
    parsed = parse_args(args)
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("=" * 75)
    logger.info("  STARTING SHARAD'S 15-MINUTE ORB TRADING DESK [%s MODE]", mode)
    logger.info("  Strategy: Option 3 Canonical ORB Desk (Armed Candle Confirmation Break)")
    logger.info("  Risk Guardrails: Fixed Rs %.0f Risk | Max %d Trades/Day | Rs %.0f Daily SL", parsed.risk, parsed.max_trades, parsed.daily_loss)
    logger.info("  Universe: %d Stocks | Sizing: Risk Parity Equity MIS", len(TARGET_STOCKS))
    logger.info("  Execution: %s Orders (Buffer: %.2f%%) | Mode: %s", parsed.order_type, parsed.limit_buffer_pct * 100.0, mode)
    logger.info("=" * 75)

    config = load_config()
    kite = get_kite_client(config=config)
    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)

    orb_cfg = ORBConfig(
        fixed_risk_per_trade=parsed.risk,
        sizing_mode=parsed.sizing_mode,
        lots=parsed.lots,
        order_type=parsed.order_type,
        limit_buffer_pct=parsed.limit_buffer_pct,
        max_trades_per_day=parsed.max_trades,
        daily_stop_loss_limit=parsed.daily_loss,
        mode=mode,
        product=parsed.product,
        exchange=parsed.exchange,
        poll_interval_seconds=parsed.poll
    )

    om = ORBOrderManager(
        mode=mode,
        kite=kite if mode == "LIVE" else None,
        exchange=parsed.exchange,
        product=parsed.product,
        order_type=parsed.order_type,
        limit_buffer_pct=parsed.limit_buffer_pct
    )

    desk = ORBDesk(
        kite=kite,
        order_manager=om,
        notifier=notifier,
        config=orb_cfg
    )

    logger.info("Executing initial day setup...")
    desk.initialize_day()

    if parsed.dry_run:
        logger.info("[DRY RUN] Initialized successfully. Inspecting status:")
        logger.info("  NIFTY 50 Day Open : %s", desk.nifty_day_open)
        logger.info("  Target Universe   : %d stocks (Static 30 F&O)", len(desk.target_stocks))
        for item in desk.target_stocks:
            sym = item["symbol"]
            stat = desk.stock_stats.get(sym, {})
            logger.info(
                "    -> %-12s | PDH: %s | PDL: %s",
                sym, stat.get("pdh"), stat.get("pdl")
            )
        logger.info("  Active Positions  : %d", len(desk.active_positions))
        logger.info("  Closed Trades     : %d", len(desk.closed_trades))
        logger.info("  Circuit Breaker   : %s", "TRIPPED" if desk.circuit_tripped else "NORMAL")
        logger.info("[DRY RUN] Complete. Exiting without entering loop.")
        return 0

    if parsed.once:
        logger.info("[ONCE] Executing single scan and monitoring cycle...")
        desk.compute_initial_balance()
        desk.scan_and_enter_breakouts()
        desk.monitor_positions()
        logger.info("[ONCE] Completed.")
        return 0

    # Main Automated Trading Loop
    logger.info("Entering live automated market loop (Polling every %d seconds)...", parsed.poll)
    last_scan_slot = None

    try:
        while True:
            now = datetime.now()
            current_time = now.time()

            # Off-market hours sleeping (before 09:15 or after 15:30)
            if current_time < dt_time(9, 15) or current_time > dt_time(15, 30):
                logger.debug("Off-market hours (%s). Sleeping...", now.strftime("%H:%M:%S"))
                time.sleep(30)
                continue

            # Step 1: Compute Initial Balance at or after 10:15
            if not desk.ib_formed and current_time >= dt_time(10, 15):
                desk.compute_initial_balance()

            # Step 2: Scan for breakouts on 15-minute candle completion (10:15 to 13:15)
            # 15-min candles complete at :00, :15, :30, :45
            slot_key = (now.date(), now.hour, now.minute // 15)
            if desk.ib_formed and current_time <= dt_time(13, 15):
                if (now.minute in [0, 15, 30, 45]) and (last_scan_slot != slot_key):
                    last_scan_slot = slot_key
                    logger.info("15-minute candle closed at %s. Scanning for breakouts...", now.strftime("%H:%M:%S"))
                    desk.scan_and_enter_breakouts()

            # Step 3: Continuously monitor active positions & check circuit breaker
            desk.monitor_positions()

            # Step 4: After 15:15 EOD square-off, sleep longer
            if current_time >= dt_time(15, 20):
                logger.info("Post 15:20. Daily session completed. Desk resting...")
                time.sleep(60)
            else:
                time.sleep(parsed.poll)

    except KeyboardInterrupt:
        logger.info("Desk stopped by user (Ctrl+C). Saving state...")
        desk._save_state()
        return 0


if __name__ == "__main__":
    sys.exit(main())
