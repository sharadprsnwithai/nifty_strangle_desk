#!/usr/bin/env python3
"""
Production Entrypoint for Milind Upasani's MTF Techno-Funda Breakout Scanner.
Screens for Multi-Timeframe Momentum (Monthly > 60, Weekly > 60, Daily crosses 65)
and High-Growth Operational Fundamentals (EBITDA Growth > 30%, D/E < 1.0, Margin >= 8%).
Runs daily at 15:15 IST before market close and dispatches actionable alerts to Telegram.
"""

import os
import sys
import argparse
from pathlib import Path
import logging
from datetime import datetime, date

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
from strategy.holidays import is_market_open, is_trading_holiday
from strategy.techno_funda.config import TechnoFundaConfig
from strategy.techno_funda.models import TechnoFundaCandidate
from strategy.techno_funda.scanner import TechnoFundaScanner
from strategy.techno_funda.desk import TechnoFundaDesk

logger = logging.getLogger("strategy.techno_funda.run")


def parse_args():
    parser = argparse.ArgumentParser(
        description="MTF Techno-Funda Breakout Strategy Scanner (Milind Upasani)"
    )
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument(
        "--now", "--once",
        dest="once",
        action="store_true",
        help="Execute single scan pass immediately and output results to console & Telegram",
    )
    run_group.add_argument(
        "--loop",
        action="store_true",
        default=True,
        help="Run continuously as daily daemon scheduler triggering at 15:15 IST (default: True)",
    )

    parser.add_argument(
        "--scan-time",
        type=str,
        default=os.getenv("TECHNO_FUNDA_SCAN_TIME", "15:15"),
        help="Daily execution trigger time in IST (default: 15:15)",
    )
    parser.add_argument(
        "--monthly-rsi",
        type=float,
        default=float(os.getenv("TECHNO_FUNDA_MONTHLY_RSI", "60.0")),
        help="Minimum Monthly RSI threshold (default: 60.0)",
    )
    parser.add_argument(
        "--weekly-rsi",
        type=float,
        default=float(os.getenv("TECHNO_FUNDA_WEEKLY_RSI", "60.0")),
        help="Minimum Weekly RSI threshold (default: 60.0)",
    )
    parser.add_argument(
        "--daily-rsi",
        type=float,
        default=float(os.getenv("TECHNO_FUNDA_DAILY_RSI", "65.0")),
        help="Daily RSI trigger threshold (default: 65.0)",
    )
    parser.add_argument(
        "--min-growth",
        type=float,
        default=float(os.getenv("TECHNO_FUNDA_MIN_GROWTH", "0.30")),
        help="Minimum TTM / Quarterly EBITDA growth ratio (default: 0.30 / 30%%)",
    )
    parser.add_argument(
        "--max-debt-equity",
        type=float,
        default=float(os.getenv("TECHNO_FUNDA_MAX_DE", "1.0")),
        help="Maximum Debt-to-Equity ratio (default: 1.0)",
    )
    parser.add_argument(
        "--tech-only",
        action="store_true",
        help="Bypass fundamental validation filter and output all technical MTF RSI breakouts",
    )
    parser.add_argument(
        "--ignore-holidays",
        action="store_true",
        help="Bypass weekend and market hour restrictions (for off-hours testing/diagnostics)",
    )
    parser.add_argument(
        "--stocks",
        type=str,
        default=None,
        help="Comma-separated subset of stock symbols to scan (e.g. TIPSMUSIC,MCX,TATASTEEL)",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=int(os.getenv("TECHNO_FUNDA_POLL", "30")),
        help="Scheduler loop poll interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=os.getenv("DATA_DIR", "data"),
        help="Directory for data caches (default: data)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=os.getenv("LOG_DIR", "logs"),
        help="Directory for log files (default: logs)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging verbosity level (default: INFO)",
    )

    return parser.parse_args()


def print_candidates_table(candidates: list[TechnoFundaCandidate]) -> None:
    """Pretty prints Techno-Funda scan results to console."""
    print("\n" + "=" * 125)
    print("  🏆 MTF TECHNO-FUNDA BREAKOUT SCREENER RESULTS (MILIND UPASANI)")
    print(f"  Found {len(candidates)} Stocks Passing Multi-Timeframe Momentum & High-Growth Fundamentals")
    print("=" * 125)
    header = (
        f"{'Symbol':<12} {'LTP (Rs)':<10} {'D-RSI':<7} {'W-RSI':<7} {'M-RSI':<7} "
        f"{'TTM EBITDA':<12} {'Q-EBITDA':<10} {'D/E':<6} {'Margin%':<9} {'Pattern':<24} {'Stop Loss':<10}"
    )
    print(header)
    print("-" * 125)

    for c in candidates:
        ttm_str = f"+{c.ttm_ebitda_growth_pct:.1f}%" if c.ttm_ebitda_growth_pct is not None else "N/A"
        q_str = f"+{c.quarterly_ebitda_growth_pct:.1f}%" if c.quarterly_ebitda_growth_pct is not None else "N/A"
        de_str = f"{c.debt_to_equity:.2f}" if c.debt_to_equity is not None else "N/A"
        margin_str = f"{c.operating_margin_pct:.1f}%" if c.operating_margin_pct is not None else "N/A"

        row = (
            f"{c.symbol:<12} {c.ltp:<10.2f} {c.daily_rsi:<7.1f} {c.weekly_rsi:<7.1f} {c.monthly_rsi:<7.1f} "
            f"{ttm_str:<12} {q_str:<10} {de_str:<6} {margin_str:<9} {c.chart_pattern:<24} {c.swing_sl:<10.2f}"
        )
        print(row)
    print("=" * 125 + "\n")


def main():
    args = parse_args()
    setup_logging(log_dir=args.log_dir, log_level=args.log_level, log_filename="techno_funda_scanner.log")

    logger.info("=" * 80)
    logger.info("  MTF TECHNO-FUNDA BREAKOUT SCANNER (MILIND UPASANI)")
    logger.info("  Daily Scan: %s IST | Monthly RSI: >%.0f | Weekly RSI: >%.0f | Daily Trigger: >%.0f",
                args.scan_time, args.monthly_rsi, args.weekly_rsi, args.daily_rsi)
    logger.info("  Fundamentals: EBITDA Growth >%.0f%% | Max D/E <%.1f", args.min_growth * 100, args.max_debt_equity)
    logger.info("=" * 80)

    # 1. Market Hours and Trading Holiday Guard
    # If run in single scan mode (--now/--once) outside market hours without --ignore-holidays,
    # cleanly abort with zero Kite API hits.
    if args.once and not args.ignore_holidays:
        market_open, market_reason = is_market_open(ignore_holidays=False)
        if not market_open:
            logger.warning("[MARKET CLOSED] %s. Aborting single scan without hitting Kite API.", market_reason)
            logger.info("Pass '--ignore-holidays' if you wish to run off-market testing.")
            return 0

    # 2. Initialize Clients
    kite = None
    notifier = None
    try:
        app_config = load_config()
        kite = get_kite_client(config=app_config, check_market_hours=True, ignore_holidays=args.ignore_holidays)
        if app_config.telegram_bot_token and app_config.telegram_chat_id:
            notifier = TelegramNotifier(bot_token=app_config.telegram_bot_token, chat_id=app_config.telegram_chat_id)
            logger.info("Telegram notifier connected successfully.")
    except Exception as e:
        logger.error("Failed to initialize client credentials: %s", e)
        sys.exit(1)

    data_dir = Path(args.data_dir)
    config = TechnoFundaConfig(
        scan_time=args.scan_time,
        monthly_rsi_threshold=args.monthly_rsi,
        weekly_rsi_threshold=args.weekly_rsi,
        daily_rsi_trigger=args.daily_rsi,
        min_ttm_ebitda_growth=args.min_growth,
        min_quarterly_ebitda_growth=args.min_growth,
        max_debt_to_equity=args.max_debt_equity,
        data_dir=data_dir,
        daily_data_path=data_dir / "swing_daily_2y.parquet",
        fundamentals_cache_file=data_dir / "techno_funda_fundamentals.json",
        state_file=data_dir / "techno_funda_alerts.json",
    )

    scanner = TechnoFundaScanner(config=config, kite=kite)
    desk = TechnoFundaDesk(
        config=config,
        scanner=scanner,
        notifier=notifier,
        kite=kite,
        ignore_holidays=args.ignore_holidays,
    )

    target_stocks = [s.strip().upper() for s in args.stocks.split(",")] if args.stocks else None

    # Single pass execution (--now / --once)
    if args.once:
        logger.info("Executing immediate scan pass (--now)...")
        result = desk.run_scan_cycle(stocks=target_stocks, tech_only=args.tech_only)

        candidates = result.get("candidates", [])
        if candidates:
            print_candidates_table(candidates)
        else:
            print("\n[INFO] No stocks currently meeting both MTF RSI and Fundamental High-Growth criteria.\n")

        return 0

    # Daemon scheduler loop (--loop)
    logger.info("Launching MTF Techno-Funda Desk daemon scheduler (Trigger: %s IST daily)...", config.scan_time)
    desk.run_loop(poll_interval=args.poll)
    return 0


if __name__ == "__main__":
    main()
