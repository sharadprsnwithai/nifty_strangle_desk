import sys
import json
import argparse
import logging
from typing import List, Optional
from config import load_config
from kite import get_kite_client, fetch_positions, summarize_positions, format_positions_table

# Ensure stdout and stderr handle UTF-8 characters (e.g. ₹ rupee symbol) gracefully across OS platforms
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("desk.main")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sharad's Intraday Option Selling Desk — Kite Connect TOTP & Positions Viewer"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw structured JSON to stdout instead of formatted table"
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Force fresh headless TOTP login, discarding any cached access token"
    )
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="Display only currently open/active positions (filters out qty == 0)"
    )
    view_group = parser.add_mutually_exclusive_group()
    view_group.add_argument(
        "--day",
        action="store_true",
        help="Display only today's intraday positions"
    )
    view_group.add_argument(
        "--net",
        action="store_true",
        help="Display cumulative net positions (default)"
    )
    return parser

def run_cli(args: Optional[List[str]] = None) -> int:
    parser = build_parser()
    parsed_args = parser.parse_args(args)

    prev_level = logging.getLogger().level
    if parsed_args.json:
        # Silence standard info logs in JSON mode so stdout remains clean JSON
        logging.getLogger().setLevel(logging.ERROR)

    try:
        try:
            config = load_config()
        except Exception as e:
            logger.error("Configuration error: %s", e)
            return 1

        try:
            kite = get_kite_client(config=config, force_login=parsed_args.force_login)
            raw_positions = fetch_positions(kite)
            summary = summarize_positions(raw_positions)

            if parsed_args.json:
                print(json.dumps(summary, indent=2))
            else:
                view = "day" if parsed_args.day else "net"
                table_str = format_positions_table(summary, view=view, open_only=parsed_args.open_only)
                print(table_str)

            return 0
        except Exception as e:
            logger.error("Error fetching Kite positions: %s", e, exc_info=True)
            return 1
    finally:
        if parsed_args.json:
            logging.getLogger().setLevel(prev_level)

if __name__ == "__main__":
    sys.exit(run_cli())
