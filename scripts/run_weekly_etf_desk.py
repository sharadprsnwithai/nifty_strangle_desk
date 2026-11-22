#!/usr/bin/env python3
"""
Production Entrypoint for Weekly Monthly-Fibonacci ETF Swing Trading Desk.
Runs automated scanning, portfolio state management, and CNC order execution on Zerodha Kite.
"""

import os
import sys
import argparse
import time
import datetime
from pathlib import Path
import logging

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
logger = logging.getLogger("strategy.etf_swing.run")

from config import load_config
from kite import get_kite_client
from notifications.telegram import TelegramNotifier
from strategy.etf_swing import ETFSwingConfig, ETFSwingDesk

def parse_args():
    parser = argparse.ArgumentParser(description="Weekly Monthly-Fibonacci ETF Swing Trading Desk")
    
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", default=True, help="Run simulated PAPER trading (default)")
    
    parser.add_argument("--capital", type=float, default=float(os.getenv("ETF_SWING_CAPITAL", "1000000.0")), 
                        help="Total capital allocated to ETF desk (default: Rs 10,00,000)")
    parser.add_argument("--slots", type=int, default=int(os.getenv("ETF_SWING_MAX_SLOTS", "3")), 
                        help="Maximum concurrent ETF positions (default: 3)")
    parser.add_argument("--scan-only", "--scan", action="store_true", 
                        help="Only scan the universe, print pivot levels and signals without placing orders")
    parser.add_argument("--rebalance", action="store_true", 
                        help="Execute rebalance immediately (exit non-conforming holdings, enter top candidates)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously as daemon loop scheduling Friday 15:20 scan and Monday 09:16 rebalance")
    parser.add_argument("--state-file", type=str, default="data/etf_swing_state.json", 
                        help="Path to JSON state file")
    parser.add_argument("--telegram", action="store_true", default=True, 
                        help="Send Telegram notifications if configured")
    
    return parser.parse_args()

def main():
    args = parse_args()
    mode = "LIVE" if args.live else "PAPER"
    
    print("=" * 65)
    print(f"WEEKLY ETF SWING TRADING DESK - MODE: {mode}")
    print(f"Total Capital: Rs {args.capital:,.0f} | Max Slots: {args.slots} (Rs {args.capital/args.slots:,.0f} per ETF)")
    print("=" * 65)
    
    # 1. Initialize Kite Client if in LIVE mode
    kite_client = None
    if mode == "LIVE":
        logger.info("Connecting to Zerodha Kite Connect for LIVE trading...")
        try:
            kite_client = get_kite_client()
            profile = kite_client.profile()
            logger.info(f"Connected to Kite as: {profile.get('user_name')} ({profile.get('user_id')})")
        except Exception as e:
            logger.error(f"Failed to authenticate with Kite Connect: {e}")
            logger.warning("Aborting live execution. Run with --paper or check credentials.")
            return 1
            
    # 2. Setup Telegram Notifier
    notifier = None
    if args.telegram:
        try:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            if bot_token and chat_id:
                notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
        except Exception as e:
            logger.warning(f"Could not initialize Telegram notifier: {e}")
            
    # 3. Initialize Desk
    config = ETFSwingConfig(
        total_capital=args.capital,
        max_slots=args.slots,
        mode=mode,
        state_file=args.state_file
    )
    desk = ETFSwingDesk(config=config, kite_client=kite_client, notifier=notifier)
    
    # 4. Handle Daemon Loop
    if args.loop:
        logger.info("=" * 65)
        logger.info("STARTING WEEKLY ETF SWING DESK DAEMON LOOP...")
        logger.info(f"Target Schedule: Friday 15:20 IST (Weekly Scan) | Monday 09:16 IST (Rebalance Execution)")
        logger.info(f"Capital: Rs {args.capital:,.0f} | Slots: {args.slots} | Mode: {mode}")
        logger.info("=" * 65)
        
        last_friday_scan_date = None
        last_monday_exec_date = None
        
        # Initial status check at container startup
        try:
            logger.info("Running initial startup scan...")
            res = desk.scan_status()
            logger.info(f"Initial Scan: {len(res['candidates'])} candidates, {len(desk.state.get('positions', {}))} active holdings.")
        except Exception as e:
            logger.warning(f"Initial scan failed: {e}")
            
        while True:
            try:
                now = datetime.datetime.now()
                today_str = now.strftime("%Y-%m-%d")
                weekday = now.weekday() # 0 = Monday, 4 = Friday
                time_str = now.strftime("%H:%M")
                
                # Friday 15:20-15:25 Scan Preview
                if weekday == 4 and "15:20" <= time_str <= "15:25" and last_friday_scan_date != today_str:
                    logger.info("Triggering Friday pre-market-close weekly scan...")
                    desk.scan_status()
                    last_friday_scan_date = today_str
                    
                # Monday 09:16-09:20 Rebalance Execution
                if weekday == 0 and "09:16" <= time_str <= "09:20" and last_monday_exec_date != today_str:
                    logger.info(f"Triggering Monday market-open weekly rebalance in {mode} mode...")
                    desk.execute_rebalance()
                    last_monday_exec_date = today_str
                    
                time.sleep(30)
            except KeyboardInterrupt:
                logger.info("Daemon loop stopped by user.")
                break
            except Exception as e:
                logger.error(f"Error in daemon loop: {e}")
                time.sleep(30)
        return 0

    # 5. Handle Scan Only
    if args.scan_only or not args.rebalance:
        print("\n[STEP 1] Scanning Universe for Monthly Fibonacci Pivots and Signals...\n")
        results = desk.scan_status()
        details_df = results["details"]
        print(details_df.to_string(index=False))
        
        print("\n" + "-" * 65)
        print("CURRENT PORTFOLIO STATE:")
        active_positions = desk.state.get("positions", {})
        if active_positions:
            for sym, pos in active_positions.items():
                print(f"  • {sym}: {pos['quantity']} shares @ Rs {pos['entry_price']:.2f} (Entry: {pos['entry_date']})")
        else:
            print("  (No active ETF positions currently held - 100% Cash)")
        print(f"Available Cash: Rs {desk.state.get('cash', args.capital):,.2f}")
        print("-" * 65)
        
        exits = results["exits"]
        candidates = results["candidates"]
        
        print("\nRECOMMENDED ACTIONS FOR REBALANCE:")
        if exits:
            print(f"  🔴 EXITS: {', '.join(exits)} closed below Monthly S1.")
        else:
            print("  ✅ EXITS: None (all current holdings are holding above Monthly S1).")
            
        free_slots = args.slots - len(active_positions) + len(exits)
        print(f"  💼 Open Slots Available: {free_slots}")
        if free_slots > 0 and candidates:
            print(f"  🟢 TOP BUY CANDIDATES (Ranked by 12W Momentum):")
            for c in candidates[:free_slots]:
                print(f"     • {c['symbol']} ({c['name']}): Price Rs {c['ltp']:.2f} | 12W Mom: {c['mom12']:+.1f}% | Dist from P: {c['dist_p']:+.1f}%")
        elif free_slots <= 0:
            print("  ⏸️ ENTRIES: None (Portfolio slots are fully occupied).")
            
        if not args.rebalance:
            print("\nTo execute this rebalance, run with flag: --rebalance")
            return 0
            
    # 5. Handle Rebalance Execution
    if args.rebalance:
        print("\n[STEP 2] Executing Portfolio Rebalance...")
        summary = desk.execute_rebalance()
        print("\nRebalance Completed Successfully!")
        print(f"Active Positions: {list(summary['active_positions'].keys())}")
        print(f"Remaining Cash: Rs {summary['cash']:,.2f}")
        
    return 0

if __name__ == "__main__":
    sys.exit(main())
