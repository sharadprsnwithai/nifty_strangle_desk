import sys
import time
import argparse
import logging
from datetime import datetime
from config import load_config
from kite import get_kite_client
from strategy.option_chain import find_strangle_strikes
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("strategy.run")


def run_strategy_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="Sharad's 10:00 AM NIFTY Short Strangle Desk")
    parser.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    parser.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")
    parser.add_argument("--lots", type=int, default=10, help="Number of lots (default: 10)")
    parser.add_argument("--poll", type=int, default=5, help="Monitoring poll interval in seconds (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Perform single strike lookup and print plan without waiting")

    parsed = parser.parse_args(args)
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("Initializing 10:00 AM NIFTY Short Strangle Desk in [%s] mode...", mode)
    config = load_config()
    kite = get_kite_client(config=config)

    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None)
    desk = StrangleDesk(order_manager=om)

    logger.info("Scanning option chain for strikes with LTP >= Rs 30 (0 DTE -> Next Week)...")
    strikes = find_strangle_strikes(kite, underlying="NIFTY", target_price=30.0, lots=parsed.lots)

    print("\n" + "=" * 65)
    print(f"  🎯 10:00 AM NIFTY STRANGLE PLAN ({mode} MODE)")
    print("=" * 65)
    print(f"Target Expiry : {strikes['expiry']}")
    print(f"Order Sizing  : {strikes['total_lots']} lots ({strikes['total_quantity']} qty)")
    print(f"CALL Leg      : {strikes['ce']['symbol']} @ Rs {strikes['ce']['ltp']:.2f}")
    print(f"                SL: Rs {strikes['ce']['sl_price']:.2f} (+50%) | Target: Rs {strikes['ce']['target_price']:.2f} (80% decay)")
    print(f"PUT Leg       : {strikes['pe']['symbol']} @ Rs {strikes['pe']['ltp']:.2f}")
    print(f"                SL: Rs {strikes['pe']['sl_price']:.2f} (+50%) | Target: Rs {strikes['pe']['target_price']:.2f} (80% decay)")
    print("=" * 65 + "\n")

    if parsed.dry_run:
        logger.info("Dry-run complete.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(run_strategy_cli())
