#!/usr/bin/env python3
"""
Production Entrypoint for Saurabh's Max OI Unwinding Option Buying Scanner & Desk.
Screens NSE F&O universe for stocks testing Max OI strikes in the 2nd half of monthly expiry,
detects >20% institutional unwinding, and issues alerts or executes buys with 50% SL and 2-day exit.
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
from strategy.max_oi.config import MaxOIConfig
from strategy.max_oi.models import MaxOICandidate, OptionBuySignal
from strategy.max_oi.scanner import MaxOIScanner
from strategy.max_oi.desk import MaxOIDesk

logger = logging.getLogger("strategy.max_oi.run")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Saurabh's Max OI Unwinding Option Buying Strategy Scanner & Alert Desk"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--paper",
        action="store_true",
        default=True,
        help="Run simulated PAPER trading execution (default: True)",
    )
    mode_group.add_argument(
        "--alert-only",
        action="store_true",
        help="Run in alert-only mode without executing paper orders",
    )

    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument(
        "--now", "--once",
        dest="once",
        action="store_true",
        help="Execute single scan pass immediately and output candidate table / alerts",
    )
    run_group.add_argument(
        "--loop",
        action="store_true",
        default=True,
        help="Run continuously as daemon loop during market hours (default: True)",
    )

    parser.add_argument(
        "--lots",
        type=int,
        default=int(os.getenv("MAX_OI_LOTS", "1")),
        help="Number of option lots per trade (default: 1)",
    )
    parser.add_argument(
        "--max-trades",
        type=int,
        default=int(os.getenv("MAX_OI_MAX_TRADES", "3")),
        help="Maximum concurrent open option positions (default: 3)",
    )
    parser.add_argument(
        "--proximity",
        type=float,
        default=float(os.getenv("MAX_OI_PROXIMITY_PCT", "0.02")),
        help="Max OI proximity filter percentage (default: 0.02 / 2.0%%)",
    )
    parser.add_argument(
        "--min-unwinding",
        type=float,
        default=float(os.getenv("MAX_OI_UNWINDING_PCT", "0.20")),
        help="Minimum OI unwinding percentage to trigger breakout signal (default: 0.20 / 20.0%%)",
    )
    parser.add_argument(
        "--sl-pct",
        type=float,
        default=float(os.getenv("MAX_OI_SL_PCT", "0.50")),
        help="Hard stop loss percentage on option premium (default: 0.50 / 50.0%%)",
    )
    parser.add_argument(
        "--max-holding-days",
        type=int,
        default=int(os.getenv("MAX_OI_HOLDING_DAYS", "2")),
        help="Maximum holding period in trading days before time exit (default: 2)",
    )
    parser.add_argument(
        "--ignore-date-filter",
        action="store_true",
        help="Bypass post-15th monthly expiry requirement (for testing or early scans)",
    )
    parser.add_argument(
        "--ignore-holidays",
        action="store_true",
        help="Bypass weekend and market hour restrictions (for testing/diagnostics)",
    )
    parser.add_argument(
        "--stocks",
        type=str,
        default=None,
        help="Comma-separated subset of stock symbols to scan (e.g. RELIANCE,TCS,INFY)",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=int(os.getenv("MAX_OI_POLL", "60")),
        help="Scheduler loop poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=os.getenv("DATA_DIR", "data"),
        help="Directory for OI snapshots and position state (default: data)",
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


def print_candidates_table(candidates: list[MaxOICandidate], target_expiry: str) -> None:
    """Pretty prints scan results to console."""
    print("\n" + "=" * 110)
    print(f"  MAX OI PROXIMITY SCREENER RESULTS | Target Monthly Expiry: {target_expiry}")
    print(f"  Found {len(candidates)} F&O stocks within 2.0% of their Max OI Strike")
    print("=" * 110)
    header = (
        f"{'Symbol':<12} {'Spot (Rs)':<10} {'Max CE':<9} {'CE OI':<10} {'CE Chg%':<10} "
        f"{'Max PE':<9} {'PE OI':<10} {'PE Chg%':<10} {'Near Side':<10} {'Dist%':<8}"
    )
    print(header)
    print("-" * 110)

    for c in candidates:
        ce_chg_str = f"{c.ce_oi_change_pct:+.1f}%" if c.ce_oi_change_pct is not None else "N/A"
        pe_chg_str = f"{c.pe_oi_change_pct:+.1f}%" if c.pe_oi_change_pct is not None else "N/A"
        near_side = "CALL (CE)" if c.is_near_ce else "PUT (PE)"
        dist_str = f"{c.ce_dist_pct:+.1f}%" if c.is_near_ce else f"{c.pe_dist_pct:+.1f}%"

        row = (
            f"{c.symbol:<12} {c.spot:<10.2f} {c.max_ce_strike:<9.0f} {c.max_ce_oi:<10,d} {ce_chg_str:<10} "
            f"{c.max_pe_strike:<9.0f} {c.max_pe_oi:<10,d} {pe_chg_str:<10} {near_side:<10} {dist_str:<8}"
        )
        print(row)
    print("=" * 110 + "\n")


def print_signals_table(signals: list[OptionBuySignal]) -> None:
    """Pretty prints actionable breakout signals."""
    if not signals:
        return
    print("\n" + "#" * 100)
    print(f"  ACTIONABLE BREAKOUT SIGNALS (>20% OI UNWINDING DETECTED): {len(signals)}")
    print("#" * 100)
    for s in signals:
        direction = "BULLISH CALL BUY 🚀" if s.side == "CE" else "BEARISH PUT BUY 📉"
        print(f"  [{direction}] Stock: {s.symbol} | Spot: Rs {s.spot_price:.2f}")
        print(f"  Breached Strike: {s.breached_strike:.0f} {s.side} | Panic Unwinding: {s.oi_unwinding_pct:.1f}%")
        print(f"  Contract: {s.atm_tradingsymbol} | Entry Premium: Rs {s.atm_ltp:.2f} (Lot: {s.lot_size})")
        print(f"  Stop Loss (50%): Rs {s.sl_price:.2f} | Target (2x): Rs {s.target_price:.2f} | Cutoff: {s.max_exit_date}")
        print("-" * 100)
    print("#" * 100 + "\n")


def main():
    args = parse_args()
    setup_logging(log_dir=args.log_dir, log_level=args.log_level, log_filename="max_oi_scanner.log")
    mode = "ALERT_ONLY" if args.alert_only else "PAPER"

    logger.info("=" * 80)
    logger.info("  SAURABH'S PUT-CALL PARITY MAX OI OPTION BUYING DESK [%s MODE]", mode)
    logger.info("  Lots: %d | Max Positions: %d | Proximity: %.1f%% | Min Unwinding: %.0f%% | SL: %.0f%% | Holding: %d Days",
                args.lots, args.max_trades, args.proximity * 100, args.min_unwinding * 100, args.sl_pct * 100, args.max_holding_days)
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

    # 2. Initialize Kite Connect and Telegram Notifier
    kite = None
    notifier = None
    try:
        app_config = load_config()
        # check_market_hours ensures profile() is NOT called when market is closed
        kite = get_kite_client(config=app_config, check_market_hours=True, ignore_holidays=args.ignore_holidays)
        if app_config.telegram_bot_token and app_config.telegram_chat_id:
            notifier = TelegramNotifier(bot_token=app_config.telegram_bot_token, chat_id=app_config.telegram_chat_id)
            logger.info("Telegram notifier connected successfully.")
    except Exception as e:
        logger.error("Failed to initialize Kite client: %s", e)
        sys.exit(1)

    data_dir = Path(args.data_dir)
    config = MaxOIConfig(
        mode=mode,
        lots_per_trade=args.lots,
        max_concurrent_trades=args.max_trades,
        proximity_pct=args.proximity,
        unwinding_pct=args.min_unwinding,
        sl_pct=args.sl_pct,
        max_holding_days=args.max_holding_days,
        post_15_only=not args.ignore_date_filter,
        snapshots_dir=data_dir / "oi_snapshots",
        state_file=data_dir / "max_oi_trades.json",
    )

    scanner = MaxOIScanner(kite=kite, config=config)
    desk = MaxOIDesk(
        config=config,
        scanner=scanner,
        notifier=notifier,
        kite=kite,
        ignore_holidays=args.ignore_holidays,
        mode=mode,
    )

    target_stocks = [s.strip().upper() for s in args.stocks.split(",")] if args.stocks else None

    # Single pass execution (--now / --once)
    if args.once:
        logger.info("Executing immediate scan pass (--now) [%s Mode]...", mode)
        # If custom stocks provided, scanner can take them
        if target_stocks:
            result = scanner.scan(
                stocks=target_stocks,
                ignore_date_filter=args.ignore_date_filter,
                ignore_holidays=args.ignore_holidays,
            )
            # Execute paper buy if signals found
            if result.get("signals") and mode == "PAPER":
                for sig in result["signals"]:
                    desk.execute_paper_buy(sig)
        else:
            result = desk.run_scan_cycle(ignore_date_filter=args.ignore_date_filter)

        candidates = result.get("candidates", [])
        signals = result.get("signals", [])
        expiry = result.get("expiry", "N/A")

        if candidates:
            print_candidates_table(candidates, expiry)
        else:
            print("\n[INFO] No stocks currently within 2.0% range of Max OI strike.\n")

        if signals:
            print_signals_table(signals)

        return 0

    # Daemon scheduler loop (--loop)
    logger.info("Launching Max OI Option Buying Desk Scheduler daemon...")
    desk.run_loop(poll_interval=args.poll)
    return 0


if __name__ == "__main__":
    main()
