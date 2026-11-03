import sys
import argparse
import logging
from config import load_config
from kite import get_kite_client
from strategy.option_chain import find_strangle_strikes
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk

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
    parser.add_argument("--max-loss", type=float, default=10000.0, help="Per-leg maximum loss exit in Rs (default: 10000.0)")
    parser.add_argument("--dry-run", action="store_true", help="Perform single strike lookup and print plan without waiting")
    parser.add_argument("--once", action="store_true", help="Run single monitoring iteration and exit")

    parsed = parser.parse_args(args)
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("Initializing 10:00 AM NIFTY Short Strangle Desk in [%s] mode...", mode)
    config = load_config()
    kite = get_kite_client(config=config)

    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None)
    desk = StrangleDesk(order_manager=om)

    try:
        desk.run_session(
            underlying="NIFTY",
            lots=parsed.lots,
            poll_seconds=parsed.poll,
            dry_run=parsed.dry_run,
            execute_once=parsed.once,
            kite=kite,
            max_loss_per_leg=parsed.max_loss
        )
        return 0
    except Exception as e:
        logger.error("Strategy session failed: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(run_strategy_cli())
