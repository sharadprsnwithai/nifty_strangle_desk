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
from strategy.silver_options.directional_buyer import SilverDirectionalBuyer, SilverBarbellDesk
from strategy.silver_options.order_manager import SilverOrderManager

logger = logging.getLogger("strategy.silver.run")


def parse_args():
    parser = argparse.ArgumentParser(description="MCX SILVERM Combo 2A Barbell Desk (Directional Buyer + Evening Strangle)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders on MCX via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")

    parser.add_argument("--strategy-mode", choices=["BARBELL", "STRANGLE", "BUYER"], default=os.getenv("SILVER_STRATEGY_MODE", "BARBELL"), help="Strategy execution mode: BARBELL (Combo 2A), STRANGLE, or BUYER (default: BARBELL)")
    parser.add_argument("--lots", type=int, default=int(os.getenv("SILVER_LOTS", "2")), help="Number of lots to trade (default: 2, or SILVER_LOTS env)")
    parser.add_argument("--sl-per-lot", type=float, default=float(os.getenv("SILVER_SL_PER_LOT", "3000.0")), help="Max portfolio loss cap per lot in Rs (default: 3000.0)")
    parser.add_argument("--sl-pct", type=float, default=float(os.getenv("SILVER_SL_PCT", "0.40")), help="Single-leg stop loss percentage (default: 0.40 / 40%%)")
    parser.add_argument("--strike-otm-pct", type=float, default=float(os.getenv("SILVER_STRIKE_OTM_PCT", "0.015")), help="OTM strike distance percentage from spot when strike-mode=PERCENT (default: 0.015 / 1.5%%)")
    parser.add_argument("--strike-mode", choices=["PERCENT", "ATR"], default=os.getenv("SILVER_STRIKE_MODE", "ATR"), help="Strike selection mode: ATR or PERCENT (default: ATR)")
    parser.add_argument("--atr-period", type=int, default=int(os.getenv("SILVER_ATR_PERIOD", "14")), help="Daily ATR lookback period (default: 14)")
    parser.add_argument("--atr-multiplier", type=float, default=float(os.getenv("SILVER_ATR_MULTIPLIER", "0.75")), help="ATR multiplier for strike distance (default: 0.75)")
    parser.add_argument("--vwap-entry", dest="vwap_entry", action="store_true", default=os.getenv("SILVER_VWAP_ENTRY", "true").lower() in ("true", "1", "yes"), help="Wait for 20:00-anchored combined premium < VWAP before entry (default: enabled)")
    parser.add_argument("--no-vwap-entry", dest="vwap_entry", action="store_false", help="Disable VWAP entry filter and enter immediately at entry_time")
    parser.add_argument("--vwap-cutoff", type=str, default=os.getenv("SILVER_VWAP_CUTOFF", "21:45"), help="Latest IST time to accept VWAP entry (default: 21:45)")
    parser.add_argument("--portfolio-sl-pct", type=float, default=float(os.getenv("SILVER_PORTFOLIO_SL_PCT", "0.25")), help="Portfolio SL as fraction of combined credit (default: 0.25; 0 = use --sl-per-lot)")
    parser.add_argument("--max-dte-days", type=int, default=int(os.getenv("SILVER_MAX_DTE_DAYS", "10")), help="Maximum calendar DTE allowed for entry (default: 10; 0 = unlimited)")
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

    parser.add_argument("--state-file", type=str, default=os.getenv("SILVER_STATE_FILE"), help="Custom path to Strangle state JSON file")
    parser.add_argument("--buyer-entry-start", type=str, default=os.getenv("SILVER_BUYER_ENTRY_START", "18:00"), help="Directional Buyer entry window start IST (default: 18:00)")
    parser.add_argument("--buyer-entry-cutoff", type=str, default=os.getenv("SILVER_BUYER_ENTRY_CUTOFF", "20:00"), help="Directional Buyer entry window cutoff IST (default: 20:00)")
    parser.add_argument("--buyer-max-dte-days", type=int, default=int(os.getenv("SILVER_BUYER_MAX_DTE_DAYS", "10")), help="Buyer DTE gate (default: 10)")
    parser.add_argument("--buyer-min-atr-pct", type=float, default=float(os.getenv("SILVER_BUYER_MIN_ATR_PCT", "3.0")), help="Buyer high-vol ATR%% gate when DTE > max (default: 3.0)")
    parser.add_argument("--buyer-vol-mult", type=float, default=float(os.getenv("SILVER_BUYER_VOL_MULT", "1.3")), help="Buyer 5m volume surge multiplier over 12-bar MA (default: 1.3)")
    parser.add_argument("--buyer-sl-pct", type=float, default=float(os.getenv("SILVER_BUYER_SL_PCT", "0.30")), help="Buyer option stop-loss fraction (default: 0.30)")
    parser.add_argument("--buyer-sl-per-lot", type=float, default=float(os.getenv("SILVER_BUYER_SL_PER_LOT", "3000.0")), help="Buyer hard max loss cap per lot in Rs (default: 3000.0)")
    parser.add_argument("--buyer-target-pct", type=float, default=float(os.getenv("SILVER_BUYER_TARGET_PCT", "0.60")), help="Buyer profit target fraction (default: 0.60)")
    parser.add_argument("--buyer-trail-be-pct", type=float, default=float(os.getenv("SILVER_BUYER_TRAIL_BE_PCT", "0.25")), help="Buyer breakeven trail arm gain fraction (default: 0.25)")
    parser.add_argument("--buyer-state-file", type=str, default=os.getenv("SILVER_BUYER_STATE_FILE"), help="Custom path to Buyer state JSON file")
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

    if args.lots <= 0:
        logger.error("Invalid --lots value (%d): must be >= 1.", args.lots)
        return 1
    if args.atr_period <= 0:
        logger.error("Invalid --atr-period value (%d): must be >= 1.", args.atr_period)
        return 1

    # Configure Strategy
    config = SilverOptionsConfig(
        strategy_mode=args.strategy_mode,
        lots=args.lots,
        sl_per_lot=args.sl_per_lot,
        sl_pct=args.sl_pct,
        strike_otm_pct=args.strike_otm_pct,
        strike_mode=args.strike_mode,
        atr_period=args.atr_period,
        atr_multiplier=args.atr_multiplier,
        vwap_entry_enabled=args.vwap_entry,
        vwap_entry_cutoff=args.vwap_cutoff,
        portfolio_sl_pct=args.portfolio_sl_pct,
        max_dte_days=args.max_dte_days,
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
        buyer_entry_start=args.buyer_entry_start,
        buyer_entry_cutoff=args.buyer_entry_cutoff,
        buyer_max_dte_days=args.buyer_max_dte_days,
        buyer_min_atr_pct=args.buyer_min_atr_pct,
        buyer_vol_mult=args.buyer_vol_mult,
        buyer_sl_pct=args.buyer_sl_pct,
        buyer_sl_per_lot=args.buyer_sl_per_lot,
        buyer_target_pct=args.buyer_target_pct,
        buyer_trail_be_pct=args.buyer_trail_be_pct,
    )
    if args.state_file:
        config.state_file = args.state_file
    elif mode == "PAPER" and not os.getenv("SILVER_STATE_FILE"):
        config.state_file = "data/silver_paper_trades_today.json"

    if args.buyer_state_file:
        config.buyer_state_file = args.buyer_state_file
    elif mode == "PAPER" and not os.getenv("SILVER_BUYER_STATE_FILE"):
        config.buyer_state_file = "data/silver_buyer_paper_trades_today.json"

    logger.info("=" * 80)
    logger.info("  MCX SILVERM EVENING DESK [%s MODE | STRATEGY: %s]", mode, config.strategy_mode.upper())
    logger.info("  Lots: %d (%.0f kg) | Buyer: %s-%s (DTE<=%d OR ATR>=%.1f%%, Cap Rs %.0f, SL %.0f%%, Tgt +%.0f%%)",
                config.lots, config.multiplier, config.buyer_entry_start, config.buyer_entry_cutoff,
                config.buyer_max_dte_days, config.buyer_min_atr_pct, config.buyer_max_loss_cap,
                config.buyer_sl_pct * 100, config.buyer_target_pct * 100)
    logger.info("  Strangle: %s-%s (VWAP<%s: %s) | Strike: %s (ATR x%.2f / %.1f%% OTM) | PortSL: %.0f%% | Max DTE: %d",
                config.entry_time, config.exit_time, config.vwap_entry_cutoff, config.vwap_entry_enabled,
                config.strike_mode, config.atr_multiplier, config.strike_otm_pct * 100,
                config.portfolio_sl_pct * 100, config.max_dte_days)
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
    strat_mode = config.strategy_mode.upper()
    strangle_desk = SilverStrangleDesk(
        order_manager=order_manager,
        config=config,
        state_file=Path(config.state_file),
        notifier=notifier,
        kite_client=kite,
    ) if strat_mode in ("BARBELL", "STRANGLE") else None
    buyer_desk = SilverDirectionalBuyer(
        order_manager=order_manager,
        config=config,
        state_file=Path(config.buyer_state_file),
        notifier=notifier,
        kite_client=kite,
    ) if strat_mode in ("BARBELL", "BUYER") else None
    runner = SilverBarbellDesk(buyer=buyer_desk, strangle=strangle_desk, config=config)

    if args.dry_run:
        from strategy.silver_options.chain import find_silver_strangle_strikes, resolve_silver_context
        ctx = resolve_silver_context(
            kite=kite, underlying=config.underlying, exchange=config.exchange,
            allow_fallback=(mode != "LIVE"), skip_0dte=config.skip_0dte, atr_period=config.atr_period,
        )
        dte_days = int(ctx.get("dte_days") or 0)
        atr_pct = ctx.get("atr_pct")
        buyer_gate = (config.buyer_max_dte_days <= 0 or dte_days <= config.buyer_max_dte_days) or (
            atr_pct is not None and float(atr_pct) >= config.buyer_min_atr_pct
        )
        strangle_gate = config.max_dte_days <= 0 or dte_days <= config.max_dte_days

        print(f"\n--- DRY RUN COMBO 2A PREVIEW [{mode} | {strat_mode}] ---")
        print(f"  Futures: {ctx.get('fut_symbol')} | Spot: Rs {ctx.get('spot_price'):,.2f} | Expiry: {ctx.get('active_expiry')} (DTE={dte_days})")
        print(f"  Daily ATR({config.atr_period}): {ctx.get('daily_atr')} ({atr_pct}%)")
        print(f"  Buyer Gate (DTE<={config.buyer_max_dte_days} OR ATR%>={config.buyer_min_atr_pct}%): {'PASS' if buyer_gate else 'SKIP'}")
        print(f"  Strangle Gate (DTE<={config.max_dte_days}): {'PASS' if strangle_gate else 'SKIP'}")

        if strangle_desk is not None:
            strikes = find_silver_strangle_strikes(
                kite=kite,
                underlying=config.underlying,
                strike_otm_pct=config.strike_otm_pct,
                exchange=config.exchange,
                allow_fallback=(mode != "LIVE"),
                min_oi=config.min_option_oi,
                max_spread_pct=config.max_bid_ask_spread_pct,
                scan_steps=config.strike_scan_steps,
                skip_0dte=config.skip_0dte,
                strike_mode=config.strike_mode,
                atr_multiplier=config.atr_multiplier,
                atr_period=config.atr_period,
                atr_min_otm_pct=config.atr_min_otm_pct,
                context=ctx,
            )
            comb_prem = float(strikes["ce_ltp"] or 0.0) + float(strikes["pe_ltp"] or 0.0)
            eff_cap = config.effective_max_portfolio_loss(comb_prem)
            print("  Strangle Strikes:")
            for k, v in strikes.items():
                print(f"    {k}: {v}")
            print(f"    Max Risk Cap: Rs {eff_cap:,.2f} (port_sl_pct={config.portfolio_sl_pct*100:.0f}%, fallback Rs {config.max_portfolio_loss:,.2f})")
        return 0

    if args.now:
        logger.info("Running immediate on-demand session (%s)...", strat_mode)
        runner.run_session(poll_interval=args.poll, execute_once=False)
        return 0

    # Default or --loop runs automated scheduler daemon
    runner.run_scheduler(ignore_holidays=args.ignore_holidays)
    return 0


if __name__ == "__main__":
    sys.exit(main())
