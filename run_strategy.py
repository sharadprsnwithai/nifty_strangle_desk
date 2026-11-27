import os
import sys
import argparse
from pathlib import Path
import logging
from config import load_config
from kite import get_kite_client
from strategy.option_chain import find_strangle_strikes
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk
from strategy.logger import setup_logging
from notifications.telegram import TelegramNotifier

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("strategy.run")


def run_strategy_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="Sharad's 10:00 AM NIFTY Short Strangle Desk")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")
    parser.add_argument("--lots", type=int, default=int(os.getenv("STRANGLE_LOTS", "10")), help="Number of lots (default: 10, or STRANGLE_LOTS env var)")
    parser.add_argument("--poll", type=int, default=5, help="Monitoring poll interval in seconds (default: 5)")
    parser.add_argument("--strategy-max-loss", "--max-loss", type=float, default=0.0, dest="strategy_max_loss", help="Strategy total loss exit in Rs across all legs (<= 0 disables, default: 0.0 / No SL)")
    parser.add_argument("--no-max-loss", dest="strategy_max_loss", action="store_const", const=0.0, help="Disable strategy-level portfolio max loss stop loss")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT", help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.05, help="Marketable limit price buffer percentage (default: 0.05 / 5%%)")
    parser.add_argument("--ignore-holidays", action="store_true", help="Ignore NSE holiday and weekend checks")
    parser.add_argument("--order-timeout", type=int, default=300, help="Order fill timeout in seconds before cancellation (default: 300)")
    parser.add_argument("--entry-time", type=str, default="10:00", help="Daily execution entry time in IST (default: 10:00)")
    parser.add_argument("--exit-time", type=str, default=os.getenv("STRANGLE_EXIT_TIME", "15:15"), help="Mandatory intraday square-off cutoff in IST (default: 15:15)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS", help="Order product type (MIS for intraday, NRML for normal carry/margin, default: MIS)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")
    parser.add_argument("--state-file", type=str, default=os.getenv("STRANGLE_STATE_FILE"), help="Path to state JSON file")
    parser.add_argument("--data-dir", type=str, default=os.getenv("DATA_DIR"), help="Directory for state data files (default: data)")
    parser.add_argument("--dry-run", action="store_true", help="Perform single strike lookup and print plan without waiting")
    parser.add_argument("--once", action="store_true", help="Run single monitoring iteration and exit (default runs automated daemon)")
    parser.add_argument("--morning-login", action="store_true", help="Execute 08:00 AM TOTP morning login to renew credentials, notify Telegram, and exit")
    morning_filter_default = os.getenv("STRANGLE_MORNING_FILTER", "true").lower() in ("true", "1", "yes")
    parser.add_argument("--morning-filter", dest="morning_filter", action="store_true", default=morning_filter_default, help="Enable 09:15-10:00 AM range trend filter (default: True)")
    parser.add_argument("--no-morning-filter", dest="morning_filter", action="store_false", help="Disable morning trend filter")
    parser.add_argument("--morning-filter-pct", type=float, default=float(os.getenv("STRANGLE_MORNING_FILTER_PCT", "0.60")), help="Morning range percentage threshold to skip session (default: 0.60)")
    parser.add_argument("--max-adr14", type=float, default=float(os.getenv("STRANGLE_MAX_ADR14", "0.0")), help="Max prior-day 14-day NIFTY ADR in pts to allow entry (0 = disabled, e.g. 220)")
    parser.add_argument("--strike-mode", choices=["ATR", "PRICE"], default=os.getenv("STRANGLE_STRIKE_MODE", "ATR"), help="Strike selection mode (ATR: 1.25x ATR distance, PRICE: Rs 30 premium, default: ATR)")
    parser.add_argument("--atr-multiplier", type=float, default=float(os.getenv("STRANGLE_ATR_MULTIPLIER", "1.25")), help="ATR multiplier for strike distance (default: 1.25)")
    parser.add_argument("--target-price", type=float, default=float(os.getenv("STRANGLE_TARGET_PRICE", "30.0")), help="Target premium in Rs for PRICE mode or fallback (default: 30.0)")
    single_leg_default = os.getenv("STRANGLE_SINGLE_LEG_EXIT", "true").lower() in ("true", "1", "yes")
    parser.add_argument("--single-leg-exit", "--use-leg-sl", dest="use_leg_sl", action="store_true", default=single_leg_default, help="Enable single-leg exit on leg SL (winning leg stays open to decay, default: True)")
    parser.add_argument("--no-single-leg-exit", dest="use_leg_sl", action="store_false", help="Disable single-leg exit")
    parser.add_argument("--leg-sl-pct", type=float, default=float(os.getenv("STRANGLE_LEG_SL_PCT", "0.70")), help="Single-leg stop loss percentage as decimal (e.g. 0.70 for 70%%, default: 0.70)")
    trail_cost_default = os.getenv("STRANGLE_TRAIL_COST", "false").lower() in ("true", "1", "yes")
    parser.add_argument("--trail-cost", dest="trail_to_cost", action="store_true", default=trail_cost_default, help="Trail winning leg stop-loss to cost/breakeven after losing leg stops out (default: False)")
    parser.add_argument("--no-trail-cost", dest="trail_to_cost", action="store_false", help="Disable trailing winning leg to cost")
    parser.add_argument("--no-sl", action="store_true", help="Disable all stop losses (disables leg SL, trail-to-cost, and portfolio max loss)")
    roll_untested_default = os.getenv("STRANGLE_ROLL_UNTESTED", "true").lower() in ("true", "1", "yes")
    parser.add_argument("--roll-untested", dest="roll_untested", action="store_true", default=roll_untested_default, help="Roll untested leg to ATM on 80%% decay target when combined P&L <= 0 (default: True)")
    parser.add_argument("--no-roll-untested", dest="roll_untested", action="store_false", help="Disable rolling untested leg on decay target")

    parsed = parser.parse_args(args)
    if parsed.no_sl:
        parsed.use_leg_sl = False
        parsed.trail_to_cost = False
        parsed.strategy_max_loss = 0.0
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level)
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("Initializing 10:00 AM NIFTY Short Strangle Desk in [%s] mode...", mode)
    config = load_config()
    kite = get_kite_client(
        config=None,
        force_login=parsed.morning_login,
        validate=True if parsed.morning_login else None,
        check_market_hours=True,
        ignore_holidays=parsed.ignore_holidays
    )

    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)
    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None, product=parsed.product)

    state_file_path = None
    if parsed.state_file:
        state_file_path = Path(parsed.state_file)
    elif parsed.data_dir:
        state_file_path = Path(parsed.data_dir) / "trades_today.json"

    desk = StrangleDesk(order_manager=om, state_file=state_file_path, notifier=notifier)
    logger.info(
        "Strangle config | mode=%s lots=%d max_loss=Rs %.0f use_leg_sl=%s trail_to_cost=%s "
        "strike_mode=%s atr_mult=%.2f morning_filter=%s (%.2f%%) exit_time=%s state_file=%s",
        mode,
        parsed.lots,
        parsed.strategy_max_loss,
        parsed.use_leg_sl,
        parsed.trail_to_cost,
        parsed.strike_mode,
        parsed.atr_multiplier,
        parsed.morning_filter,
        parsed.morning_filter_pct,
        parsed.exit_time,
        desk.state_file,
    )

    if parsed.morning_login:
        logger.info("Executing on-demand morning TOTP login renewal...")
        desk.perform_daily_morning_login(kite=kite)
        return 0

    if kite is not None:
        try:
            prof = kite.profile()
            if isinstance(prof, dict):
                logger.info(
                    "Kite connection verified: %s (%s)",
                    prof.get("user_name", "Trader"),
                    prof.get("user_id", "unknown"),
                )
        except Exception as e:
            logger.warning("Kite profile check failed at startup: %s", e)

        try:
            ltp_data = kite.ltp(["NSE:NIFTY 50"])
            nifty_ltp = ltp_data.get("NSE:NIFTY 50", {}).get("last_price") if isinstance(ltp_data, dict) else None
            if nifty_ltp is not None:
                logger.info("NIFTY 50 live data check OK: LTP = Rs %.2f", float(nifty_ltp))
            elif isinstance(ltp_data, dict):
                logger.warning("NIFTY 50 live data check returned empty payload: %s", ltp_data)
        except Exception as e:
            logger.warning("NIFTY 50 live data check failed at startup: %s", e)

    try:
        session_kwargs = {
            "underlying": "NIFTY",
            "lots": parsed.lots,
            "poll_seconds": parsed.poll,
            "kite": kite,
            "strategy_max_loss": parsed.strategy_max_loss,
            "order_type": parsed.order_type,
            "limit_buffer_pct": parsed.limit_buffer_pct,
            "ignore_holidays": parsed.ignore_holidays,
            "order_timeout": parsed.order_timeout,
            "morning_filter": parsed.morning_filter,
            "morning_filter_pct": parsed.morning_filter_pct,
            "strike_mode": parsed.strike_mode,
            "atr_multiplier": parsed.atr_multiplier,
            "target_price": parsed.target_price,
            "use_leg_sl": parsed.use_leg_sl,
            "trail_to_cost": parsed.trail_to_cost
        }

        extra_time_kwargs = {"exit_time_str": parsed.exit_time} if parsed.exit_time != "15:15" else {}
        if not parsed.roll_untested:
            extra_time_kwargs["roll_untested"] = False
        if parsed.leg_sl_pct != 0.70:
            extra_time_kwargs["leg_sl_pct"] = parsed.leg_sl_pct
        if parsed.max_adr14 > 0:
            extra_time_kwargs["max_adr14"] = parsed.max_adr14
        if mode == "LIVE" and (parsed.dry_run or parsed.once):
            extra_time_kwargs["check_market_hours"] = True

        if parsed.dry_run or parsed.once:
            desk.run_session(
                dry_run=parsed.dry_run,
                execute_once=parsed.once,
                **extra_time_kwargs,
                **session_kwargs
            )
            return 0
        else:
            desk.run_scheduler(
                entry_time_str=parsed.entry_time,
                **extra_time_kwargs,
                **session_kwargs
            )
            return 0
    except Exception as e:
        logger.error("Strategy session failed: %s", e, exc_info=True)
        if 'notifier' in locals() and notifier:
            try:
                import html
                notifier.send_message(
                    f"🚨 <b>STRANGLE DESK FATAL ERROR</b>: {html.escape(str(e))}\nCheck container logs for full traceback."
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(run_strategy_cli())
