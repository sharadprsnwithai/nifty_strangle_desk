import os
import sys
import argparse
import logging
from datetime import datetime, date
from config import load_config
from kite import get_kite_client
from strategy.order_manager import OrderManager
from strategy.logger import setup_logging
from notifications.telegram import TelegramNotifier
from strategy.positional_condor.condor_desk import PositionalDesk
from strategy.positional_condor.backtest import run_positional_monthly_backtest

# Ensure stdout and stderr handle UTF-8 characters gracefully
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

logger = logging.getLogger("condor.run")


def run_positional_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="Sharad's NIFTY Positional Evergreen Condor Desk")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots (default: 1)")
    parser.add_argument("--capital-per-lot", type=float, default=100000.0, help="Capital allocated per lot in Rs (default: 100000.0)")
    parser.add_argument("--monthly-stop-loss", type=float, default=0.03, help="Monthly stop loss percentage (default: 0.03 / 3%)")
    parser.add_argument("--monthly-target", type=float, default=0.04, help="Monthly target profit percentage (default: 0.04 / 4%)")
    parser.add_argument("--schedule-time", type=str, default="15:20", help="Daily execution time in IST (default: 15:20)")
    parser.add_argument("--dry-run", action="store_true", help="Perform single strike and regime lookup and print plan without submitting orders")
    parser.add_argument("--once", action="store_true", help="Run single daily evaluation pass and exit (default runs automated daemon)")
    parser.add_argument("--ignore-holidays", action="store_true", help="Ignore NSE holiday and weekend checks")
    parser.add_argument("--backtest", action="store_true", help="Run historical monthly backtest simulation")
    parser.add_argument("--backtest-days", type=int, default=365, help="Number of historical days for backtest (default: 365)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")

    parsed = parser.parse_args(args)
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level, log_filename="positional_condor.log")
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("Initializing NIFTY Positional Evergreen Condor Desk in [%s] mode...", mode)
    config = load_config()
    kite = get_kite_client(config=config)

    if parsed.backtest:
        logger.info("Running historical monthly backtest for past %d days...", parsed.backtest_days)
        from strategy.positional_condor.indicator import fetch_daily_nifty_rsi
        _, _, df_daily = fetch_daily_nifty_rsi(kite, days=parsed.backtest_days)
        summary = run_positional_monthly_backtest(
            df_daily,
            initial_capital=parsed.capital_per_lot,
            lots=parsed.lots,
            target_pct=parsed.monthly_target,
            stop_pct=parsed.monthly_stop_loss
        )

        print("\n" + "=" * 80)
        print("  [+] NIFTY POSITIONAL CONDOR MONTHLY BACKTEST SUMMARY")
        print("=" * 80)
        print(f"Total Months Tested : {summary['total_months']}")
        print(f"Profitable Months   : {summary['profitable_months']} / {summary['total_months']} ({summary['win_rate']}%)")
        print(f"Losing Months       : {summary['losing_months']}")
        print(f"Total Net P&L (Rs)  : Rs {summary['total_net_pnl']:,.2f}")
        print("-" * 80)
        print(f"{'Month':<10} | {'Regime':<10} | {'Status':<15} | {'Rolls':<6} | {'Net P&L (Rs)':<14} | {'Return %':<8}")
        print("-" * 80)
        for rec in summary["monthly_records"]:
            print(f"{rec['month']:<10} | {rec['regime']:<10} | {rec['status']:<15} | {rec['rolls']:<6} | Rs {rec['net_pnl']:>10,.2f} | {rec['return_pct']:>6.2f}%")
        print("=" * 80 + "\n")
        return 0

    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)
    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None)
    desk = PositionalDesk(
        order_manager=om,
        notifier=notifier,
        lots=parsed.lots,
        capital_per_lot=parsed.capital_per_lot,
        monthly_stop_pct=parsed.monthly_stop_loss,
        monthly_target_pct=parsed.monthly_target
    )

    try:
        if parsed.dry_run or parsed.once:
            res = desk.evaluate_daily_close(
                kite=kite,
                execute_once=True,
                dry_run=parsed.dry_run,
                ignore_holidays=parsed.ignore_holidays
            )
            print("\n" + "=" * 70)
            print("  [+] NIFTY POSITIONAL DESK DAILY EVALUATION RESULT")
            print("=" * 70)
            print(f"Date          : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Status        : {res.get('status')}")
            print(f"Regime        : {res.get('regime', 'UNKNOWN')}")
            print(f"Active Month  : {res.get('month', 'UNKNOWN')} (Expiry: {res.get('expiry', 'UNKNOWN')})")
            print(f"Realized P&L  : Rs {res.get('monthly_realized_pnl', 0.0):,.2f}")
            print("=" * 70 + "\n")
            return 0
        else:
            # Fully automated daily daemon mode: runs continuous scheduler waking up at schedule_time IST
            desk.run_scheduler(
                kite=kite,
                schedule_time=parsed.schedule_time,
                ignore_holidays=parsed.ignore_holidays
            )
            return 0
    except Exception as e:
        logger.exception("Positional Condor Desk run failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(run_positional_cli())
