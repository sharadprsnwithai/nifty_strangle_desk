import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from kite import get_kite_client
from notifications.telegram import TelegramNotifier
from strategy.logger import setup_logging
from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.desk import SilverStrangleDesk
from strategy.silver_options.order_manager import SilverOrderManager

logger = logging.getLogger("strategy.silver.run")


def parse_args():
    parser = argparse.ArgumentParser(description="MCX SILVERM US Post-Open IV Crush Evening Strangle Desk")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders on MCX via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")

    parser.add_argument("--lots", type=int, default=int(os.getenv("SILVER_LOTS", "2")), help="Number of lots to trade (default: 2, or SILVER_LOTS env)")
    parser.add_argument("--sl-per-lot", type=float, default=float(os.getenv("SILVER_SL_PER_LOT", "3000.0")), help="Max portfolio loss cap per lot in Rs (default: 3000.0)")
    parser.add_argument("--sl-pct", type=float, default=float(os.getenv("SILVER_SL_PCT", "0.40")), help="Single-leg stop loss percentage (default: 0.40 / 40%%)")
    parser.add_argument("--strike-otm-pct", type=float, default=float(os.getenv("SILVER_STRIKE_OTM_PCT", "0.015")), help="OTM strike distance percentage from spot (default: 0.015 / 1.5%%)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default=os.getenv("SILVER_ORDER_TYPE", "LIMIT"), help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pct", type=float, default=float(os.getenv("SILVER_LIMIT_BUFFER_PCT", "0.05")), help="Fallback limit price buffer percentage when depth unavailable (default: 0.05)")
    parser.add_argument("--max-limit-slippage-pct", type=float, default=float(os.getenv("SILVER_MAX_LIMIT_SLIPPAGE_PCT", "0.01")), help="Tight bid/ask-anchored limit slippage percentage (default: 0.01 / 1%%)")
    parser.add_argument("--min-oi", type=int, default=int(os.getenv("SILVER_MIN_OI", "100")), help="Minimum option OI required to select a strike (default: 100)")
    parser.add_argument("--max-spread-pct", type=float, default=float(os.getenv("SILVER_MAX_SPREAD_PCT", "0.03")), help="Maximum allowed bid-ask spread pct (default: 0.03 / 3%%)")
    parser.add_argument("--entry-time", type=str, default=os.getenv("SILVER_ENTRY_TIME", "20:00"), help="Entry time in IST (default: 20:00)")
    parser.add_argument("--exit-time", type=str, default=os.getenv("SILVER_EXIT_TIME", "23:10"), help="Mandatory EOD exit time in IST (default: 23:10)")
    parser.add_argument("--poll", type=int, default=int(os.getenv("SILVER_POLL_SECONDS", "5")), help="Monitoring poll interval in seconds (default: 5)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default=os.getenv("SILVER_PRODUCT", "MIS"), help="Product type (default: MIS)")
    parser.add_argument("--target-decay-pct", type=float, default=float(os.getenv("SILVER_TARGET_DECAY_PCT", "0.70")), help="Target combined/surviving decay percentage (default: 0.70)")
    parser.add_argument("--trail-activation-pct", type=float, default=float(os.getenv("SILVER_TRAIL_ACTIVATION_PCT", "0.30")), help="Decay pct on one leg to arm cost trailing on the other (default: 0.30)")
    parser.add_argument("--no-trail-cost", dest="trail_to_cost", action="store_false", default=os.getenv("SILVER_TRAIL_COST", "true").lower() in ("true", "1", "yes"), help="Disable trailing surviving leg SL to cost")
    parser.add_argument("--no-single-leg-exit", dest="single_leg_exit", action="store_false", default=os.getenv("SILVER_SINGLE_LEG_EXIT", "true").lower() in ("true", "1", "yes"), help="Disable single-leg SL (co-exits both legs together)")
    parser.add_argument("--no-skip-0dte", dest="skip_0dte", action="store_false", default=os.getenv("SILVER_SKIP_0DTE", "true").lower() in ("true", "1", "yes"), help="Disable skipping 0-DTE option expiry day")

    parser.add_argument("--state-file", type=str, default=os.getenv("SILVER_STATE_FILE"), help="Custom path to state JSON file")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for log files")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity")

    parser.add_argument("--loop", action="store_true", help="Run automated 24/7 scheduler daemon (default for production)")
    parser.add_argument("--now", action="store_true", help="Execute immediate trading session right now")
    parser.add_argument("--dry-run", action="store_true", help="Perform strike lookup and preview plan without placing orders")
    parser.add_argument("--ignore-holidays", action="store_true", help="Bypass MCX holiday and weekend checks")

    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(log_dir=args.log_dir, log_level=args.log_level, log_filename="silver_desk.log")

    env_mode = os.getenv("SILVER_MODE", "LIVE").upper()
    if args.live:
        mode = "LIVE"
    elif args.paper:
        mode = "PAPER"
    else:
        mode = env_mode if env_mode in ("LIVE", "PAPER") else "LIVE"

    # Configure Strategy
    config = SilverOptionsConfig(
        lots=args.lots,
        sl_per_lot=args.sl_per_lot,
        sl_pct=args.sl_pct,
        strike_otm_pct=args.strike_otm_pct,
        order_type=args.order_type,
        limit_buffer_pct=args.limit_buffer_pct,
        max_limit_slippage_pct=args.max_limit_slippage_pct,
        min_option_oi=args.min_oi,
        max_bid_ask_spread_pct=args.max_spread_pct,
        entry_time=args.entry_time,
        exit_time=args.exit_time,
        poll_seconds=args.poll,
        product=args.product,
        target_decay_pct=args.target_decay_pct,
        trail_activation_decay_pct=args.trail_activation_pct,
        trail_to_cost=args.trail_to_cost,
        single_leg_exit=args.single_leg_exit,
        skip_0dte=args.skip_0dte,
    )
    if args.state_file:
        config.state_file = args.state_file
    elif mode == "PAPER" and not os.getenv("SILVER_STATE_FILE"):
        config.state_file = "data/silver_paper_trades_today.json"

    logger.info("=" * 80)
    logger.info("  MCX SILVERM EVENING INTRADAY STRANGLE DESK [%s MODE]", mode)
    logger.info("  Lots: %d (%.0f kg) | Total SL Cap: Rs %.2f (Rs %.2f/lot) | Single-Leg SL: %.1f%%",
                config.lots, config.multiplier, config.max_portfolio_loss, config.sl_per_lot, config.sl_pct * 100)
    logger.info("  Entry: %s IST | Exit: %s IST | Strike: %.1f%% OTM",
                config.entry_time, config.exit_time, config.strike_otm_pct * 100)
    logger.info("=" * 80)

    # Initialize Kite and Telegram
    app_config = load_config()
    kite = None
    notifier = None

    try:
        notifier = TelegramNotifier(bot_token=app_config.telegram_bot_token, chat_id=app_config.telegram_chat_id)
        if mode == "LIVE":
            kite = get_kite_client(config=None, check_market_hours=False, ignore_holidays=args.ignore_holidays)
            logger.info("Connected to Zerodha Kite API for MCX Live Execution.")
        else:
            try:
                kite = get_kite_client(config=None, check_market_hours=False, ignore_holidays=True)
            except Exception:
                logger.info("Paper mode running without live Kite credentials.")
    except Exception as e:
        logger.error("Initialization error: %s", e)
        if mode == "LIVE":
            sys.exit(1)

    order_manager = SilverOrderManager(mode=mode, kite=kite, config=config)
    desk = SilverStrangleDesk(
        order_manager=order_manager,
        config=config,
        state_file=Path(config.state_file),
        notifier=notifier,
        kite_client=kite,
    )

    if args.dry_run:
        from strategy.silver_options.chain import find_silver_strangle_strikes
        strikes = find_silver_strangle_strikes(
            kite=kite,
            underlying=config.underlying,
            strike_otm_pct=config.strike_otm_pct,
            exchange=config.exchange,
            min_oi=config.min_option_oi,
            max_spread_pct=config.max_bid_ask_spread_pct,
            scan_steps=config.strike_scan_steps,
            skip_0dte=config.skip_0dte,
        )
        print("\n--- DRY RUN STRIKE SELECTION PREVIEW ---")
        for k, v in strikes.items():
            print(f"  {k}: {v}")
        print(f"  Configured Lots: {config.lots} ({config.multiplier:.0f} kg)")
        print(f"  Max Risk Cap: Rs {config.max_portfolio_loss:,.2f} (Calculated for {config.lots} lots @ Rs {config.sl_per_lot:,.2f}/lot)")
        return 0

    if args.now:
        logger.info("Running immediate on-demand session...")
        desk.run_session(poll_interval=args.poll, execute_once=False)
        return 0

    # Default or --loop runs automated scheduler daemon
    desk.run_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
