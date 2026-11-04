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
    parser.add_argument("--lots", type=int, default=10, help="Number of lots (default: 10)")
    parser.add_argument("--poll", type=int, default=5, help="Monitoring poll interval in seconds (default: 5)")
    parser.add_argument("--strategy-max-loss", "--max-loss", type=float, default=10000.0, dest="strategy_max_loss", help="Strategy total loss exit in Rs across all legs (default: 10000.0)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT", help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.05, help="Marketable limit price buffer percentage (default: 0.05 / 5%%)")
    parser.add_argument("--ignore-holidays", action="store_true", help="Ignore NSE holiday and weekend checks")
    parser.add_argument("--order-timeout", type=int, default=300, help="Order fill timeout in seconds before cancellation (default: 300)")
    parser.add_argument("--entry-time", type=str, default="10:00", help="Daily execution entry time in IST (default: 10:00)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS", help="Order product type (MIS for intraday, NRML for normal carry/margin, default: MIS)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")
    parser.add_argument("--state-file", type=str, default=os.getenv("STRANGLE_STATE_FILE"), help="Path to state JSON file")
    parser.add_argument("--data-dir", type=str, default=os.getenv("DATA_DIR"), help="Directory for state data files (default: data)")
    parser.add_argument("--dry-run", action="store_true", help="Perform single strike lookup and print plan without waiting")
    parser.add_argument("--once", action="store_true", help="Run single monitoring iteration and exit (default runs automated daemon)")
    parser.add_argument("--morning-login", action="store_true", help="Execute 08:00 AM TOTP morning login to renew credentials, notify Telegram, and exit")

    parsed = parser.parse_args(args)
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level)
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("Initializing 10:00 AM NIFTY Short Strangle Desk in [%s] mode...", mode)
    config = load_config()
    kite = get_kite_client(config=config)

    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)
    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None, product=parsed.product)

    state_file_path = None
    if parsed.state_file:
        state_file_path = Path(parsed.state_file)
    elif parsed.data_dir:
        state_file_path = Path(parsed.data_dir) / "trades_today.json"

    desk = StrangleDesk(order_manager=om, state_file=state_file_path, notifier=notifier)

    if parsed.morning_login:
        logger.info("Executing on-demand morning TOTP login renewal...")
        desk.perform_daily_morning_login(kite=kite)
        return 0

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
            "order_timeout": parsed.order_timeout
        }

        if parsed.dry_run or parsed.once:
            desk.run_session(
                dry_run=parsed.dry_run,
                execute_once=parsed.once,
                **session_kwargs
            )
            return 0
        else:
            desk.run_scheduler(
                entry_time_str=parsed.entry_time,
                **session_kwargs
            )
            return 0
    except Exception as e:
        logger.error("Strategy session failed: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(run_strategy_cli())
