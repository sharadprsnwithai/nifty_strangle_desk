"""CLI entrypoint for running the MCX Silver Options Desk."""
import argparse
import logging
import sys
from pathlib import Path

# Ensure repository root is in sys.path when invoked directly
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.engine import SilverOptionsEngine
from kite.client import get_kite_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("silver_options.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCX Silver Options Desk")
    parser.add_argument("--paper", action="store_true", default=True, help="Run in paper trading mode")
    parser.add_argument("--live", action="store_true", help="Run in live trading mode")
    return parser


def main(args=None):
    parser = build_parser()
    parsed_args = parser.parse_args(args)

    is_paper = not parsed_args.live
    logger.info(f"Starting MCX Silver Options Desk (Mode: {'PAPER' if is_paper else 'LIVE'})...")

    cfg = SilverConfig()
    kite = get_kite_client()
    engine = SilverOptionsEngine(kite_client=kite, config=cfg, is_paper=is_paper)
    logger.info(f"Initialized Silver Options Engine for {cfg.symbol} (Lot size: {cfg.lot_size} kg). Ready.")
    return engine


if __name__ == "__main__":
    main()
