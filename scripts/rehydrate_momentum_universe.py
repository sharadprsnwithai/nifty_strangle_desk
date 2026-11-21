#!/usr/bin/env python3
"""
Rehydrate Nifty 200 Momentum 30 Stock Futures Trading Universe.
Automatically resolves company names, filters non-equity assets, queries Kite for live F&O lot sizes,
detects additions/removals, and atomically persists the updated universe for zero-downtime hot reloading.
"""

import os
import sys
import argparse
from pathlib import Path

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from kite import get_kite_client
from notifications.telegram import TelegramNotifier
from strategy.stock_futures.rehydrator import UniverseRehydrator

DEFAULT_CONSTITUENTS_TEXT = """Laurus Labs Limited
Multi Commodity Exchange of India Limited
Shriram Finance Limited
Hindalco Industries Limited
Tata Steel Limited
Cummins India Limited
NTPC Limited
GE Vernova T&D India Limited
Vedanta Limited
Adani Power Limited
BSE Limited
The Federal Bank  Limited
Bharat Heavy Electricals Limited
Samvardhana Motherson International Limited
Bharat Forge Limited
CG Power and Industrial Solutions Limited
Hitachi Energy India Limited
Adani Energy Solutions Limited
Torrent Pharmaceuticals Limited
National Aluminium Company Limited
Polycab India Limited
Vodafone Idea Limited
Solar Industries India Limited
ABB India Limited
Adani Green Energy Limited
Glenmark Pharmaceuticals Limited
KEI Industries Limited
Steel Authority of India Limited
Aditya Birla Capital Limited
L&T Finance Limited
Triparty Repo"""


def parse_args():
    parser = argparse.ArgumentParser(description="Rehydrate Nifty 200 Momentum 30 Stock Universe")
    parser.add_argument("--file", "-f", type=str, help="Path to text or CSV file containing updated constituents")
    parser.add_argument("--text", "-t", type=str, help="Raw newline-separated or comma-separated constituent list string")
    parser.add_argument("--auto", action="store_true", help="Auto-detect from data/momentum_30_constituents.txt or .csv")
    parser.add_argument("--output", "-o", type=str, default="data/momentum_30_universe.json", help="Target universe JSON path")
    parser.add_argument("--no-kite", action="store_true", help="Skip live Kite Connect lot size lookup and use default lot sizes")
    return parser.parse_args()


def main():
    args = parse_args()

    # Determine input source
    source_content = None
    if args.file and os.path.isfile(args.file):
        print(f"Loading constituents from file: {args.file}")
        source_content = Path(args.file)
    elif args.text:
        source_content = args.text
    elif args.auto:
        for candidate in ["data/momentum_30_constituents.txt", "data/momentum_30_constituents.csv"]:
            if os.path.isfile(candidate):
                print(f"Auto-detected constituent drop file: {candidate}")
                source_content = Path(candidate)
                break

    if source_content is None:
        print("Using current baseline 30-stock constituent list...")
        source_content = DEFAULT_CONSTITUENTS_TEXT

    # Try connecting to Kite for live lot sizes
    kite = None
    notifier = None
    if not args.no_kite:
        try:
            cfg = load_config()
            kite = get_kite_client(config=cfg)
            if cfg.telegram_bot_token and cfg.telegram_chat_id:
                notifier = TelegramNotifier(bot_token=cfg.telegram_bot_token, chat_id=cfg.telegram_chat_id)
        except Exception as e:
            print(f"Kite Connect login skipped ({e}). Using default lot sizes.")

    output_path = Path(args.output)
    rehydrator = UniverseRehydrator(kite=kite, universe_file=output_path)

    print("=" * 85)
    print("      REHYDRATING NIFTY 200 MOMENTUM 30 TRADING UNIVERSE")
    print("=" * 85)

    result = rehydrator.rehydrate(source=source_content, save=True)

    symbols = result["symbols"]
    lot_sizes = result["lot_sizes"]
    names = result["names"]
    changes = result["changes"]

    print(f"\nSuccessfully parsed & verified: {len(symbols)} active F&O equities.")
    print(f"Universe File Saved: {output_path}")

    print("\n" + "-" * 85)
    print("                    ACTIVE CONSTITUENTS & F&O LOT SIZES")
    print("-" * 85)
    for idx, sym in enumerate(symbols, 1):
        lot = lot_sizes.get(sym, 1)
        name = names.get(sym, sym)
        print(f" {idx:>2}. {sym:<12} | Lot: {lot:>6} (2L: {lot*2:>6}) | {name}")

    print("\n" + "=" * 85)
    print("                    SEMI-ANNUAL REBALANCE CHANGES DETECTED")
    print("=" * 85)
    added = changes["added"]
    removed = changes["removed"]

    if changes["is_changed"]:
        print(f"🟢 Added Constituents   ({len(added)}): {', '.join(added) if added else 'None'}")
        print(f"🔴 Removed Constituents ({len(removed)}): {', '.join(removed) if removed else 'None'}")
    else:
        print("ℹ️  No changes detected against existing cached universe (Universe is identical).")

    # Send Telegram Notification
    if notifier and changes["is_changed"]:
        msg = (
            f"🔄 *MOMENTUM 30 UNIVERSE REHYDRATED*\n"
            f"Active Equities: {len(symbols)}\n"
            f"Added: {', '.join(added) if added else 'None'}\n"
            f"Removed: {', '.join(removed) if removed else 'None'}\n"
            f"File: `{output_path}`"
        )
        notifier.send_message(msg)
        print("Telegram rehydration notification dispatched.")

    print("\nDone! Running Docker / desk daemon will dynamically hot-reload this universe.")
    return 0


if __name__ == "__main__":
    main()
