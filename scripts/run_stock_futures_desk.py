#!/usr/bin/env python3
"""
Production Entrypoint for Nifty 200 Momentum 30 Stock Futures Trading Desk.
Runs automated scanning and execution for 2 Lots on the updated 30 Momentum Stocks.
"""

import os
import sys
import argparse
from pathlib import Path
import logging

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ensure stdout and stderr handle UTF-8 characters gracefully across OS platforms
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
from strategy.logger import setup_logging
from notifications.telegram import TelegramNotifier
from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.engine import StockFuturesEngine
from strategy.stock_futures.order_manager import StockFuturesOrderManager
from strategy.stock_futures.desk import StockFuturesDesk

logger = logging.getLogger("strategy.stock_futures.run")


def parse_args():
    parser = argparse.ArgumentParser(description="Nifty 200 Momentum 30 Stock Futures Desk")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")
    parser.add_argument("--lots", type=int, default=int(os.getenv("STOCK_FUTURES_LOTS", "2")), help="Number of lots per trade (default: 2)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default=os.getenv("STOCK_FUTURES_ORDER_TYPE", "LIMIT"), help="Order execution type (LIMIT: marketable limit order, default: LIMIT)")
    parser.add_argument("--limit-buffer-pct", type=float, default=float(os.getenv("STOCK_FUTURES_LIMIT_BUFFER_PCT", "0.05")), help="Marketable limit price buffer percentage (default: 0.05 / 5%%)")
    parser.add_argument("--target-pct", type=float, default=float(os.getenv("STOCK_FUTURES_TARGET_PCT", "0.015")), help="Profit target percentage (default: 0.015 / 1.5%%)")
    parser.add_argument("--sl-pct", type=float, default=float(os.getenv("STOCK_FUTURES_SL_PCT", "0.010")), help="Hard stop loss percentage (default: 0.010 / 1.0%%)")
    parser.add_argument("--vol-threshold", type=float, default=float(os.getenv("STOCK_FUTURES_VOL_THRESHOLD", "1.5")), help="Volume breakout ratio vs 20-MA (default: 1.5)")
    parser.add_argument("--entry-cutoff", type=str, default=os.getenv("STOCK_FUTURES_ENTRY_CUTOFF", "13:00"), help="Entry cutoff time IST (default: 13:00)")
    parser.add_argument("--squareoff-time", type=str, default=os.getenv("STOCK_FUTURES_SQUAREOFF_TIME", "15:00"), help="Mandatory EOD squareoff time IST (default: 15:00)")
    parser.add_argument("--poll", type=int, default=int(os.getenv("STOCK_FUTURES_POLL", "15")), help="Poll interval in seconds (default: 15)")
    parser.add_argument("--loop", action="store_true", default=True, help="Run continuously as daemon loop during market hours (default: True)")
    parser.add_argument("--once", action="store_true", help="Run single scan & monitor iteration and exit")
    parser.add_argument("--ignore-holidays", action="store_true", help="Ignore weekends and official NSE holidays (useful for testing)")
    parser.add_argument("--state-file", type=str, default=os.getenv("STOCK_FUTURES_STATE_FILE"), help="Path to state JSON file")
    parser.add_argument("--data-dir", type=str, default=os.getenv("DATA_DIR", "data"), help="Directory for state data files (default: data)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")

    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(log_dir=args.log_dir, log_level=args.log_level, log_filename="stock_futures_desk.log")
    mode = "LIVE" if args.live else "PAPER"

    logger.info("=" * 80)
    logger.info("  NIFTY 200 MOMENTUM 30 STOCK FUTURES DESK [%s MODE]", mode)
    logger.info("  Lots Per Trade: %d | Target: %.1f%% | SL: %.1f%% | Vol Filter: %.1fx", args.lots, args.target_pct * 100, args.sl_pct * 100, args.vol_threshold)
    logger.info("=" * 80)

    # Load credentials & clients
    kite = None
    notifier = None
    try:
        app_config = load_config()
        if args.live:
            kite = get_kite_client(config=app_config)
        if app_config.telegram_bot_token and app_config.telegram_chat_id:
            notifier = TelegramNotifier(bot_token=app_config.telegram_bot_token, chat_id=app_config.telegram_chat_id)
    except Exception as e:
        if args.live:
            logger.error("Failed to initialize Kite client for LIVE mode: %s", e)
            if notifier:
                try:
                    import html
                    notifier.send_message(f"🚨 <b>Stock Futures Desk Startup Failed</b>: Could not initialize Kite client: {html.escape(str(e))}")
                except Exception:
                    pass
            sys.exit(1)
        else:
            logger.info("Paper mode running without live Kite credentials.")

    state_path = Path(args.state_file) if args.state_file else (Path(args.data_dir) / "stock_futures_trades_today.json")

    strategy_config = StockFuturesConfig(
        lots_per_trade=args.lots,
        order_type=args.order_type,
        limit_buffer_pct=args.limit_buffer_pct,
        target_pct=args.target_pct,
        sl_pct=args.sl_pct,
        vol_threshold=args.vol_threshold,
        entry_cutoff_time=args.entry_cutoff,
        squareoff_time=args.squareoff_time,
        state_file=state_path,
    )

    om = StockFuturesOrderManager(
        mode=mode,
        kite=kite,
        config=strategy_config,
        state_file=state_path,
    )

    engine = StockFuturesEngine(config=strategy_config)

    desk = StockFuturesDesk(
        order_manager=om,
        engine=engine,
        config=strategy_config,
        notifier=notifier,
        kite=kite,
    )

    if args.once:
        logger.info("Executing single monitoring & scan cycle (--once)...")
        desk.run_cycle()
        return 0

    logger.info("Launching daemon monitoring loop on 30 Momentum Stocks...")
    desk.run_loop(poll_seconds=args.poll, ignore_holidays=args.ignore_holidays)
    return 0


if __name__ == "__main__":
    main()
