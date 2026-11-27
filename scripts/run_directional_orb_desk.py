"""CLI runner for 30m ORB Directional NIFTY Option Selling Desk."""
import argparse
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import load_config
from kite import get_kite_client
from notifications.telegram import TelegramNotifier
from strategy.directional_orb_desk import DirectionalOrbDesk
from strategy.logger import setup_logging
from strategy.order_manager import OrderManager

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("strategy.directional_orb.cli")


def _parse_hhmm(val: str) -> time:
    h, m = map(int, val.split(":"))
    return time(h, m)


def main(args=None) -> int:
    parser = argparse.ArgumentParser(description="NIFTY 30m ORB Directional Option Selling Desk (100 OTM Naked, 50% SL)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    mode_group.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")
    parser.add_argument("--lots", type=int, default=5, help="Number of lots (default: 5)")
    parser.add_argument("--otm-offset", type=int, default=100, help="OTM distance in points (default: 100)")
    parser.add_argument("--leg-sl-pct", type=float, default=0.50, help="Option stop-loss fraction (default: 0.50)")
    parser.add_argument("--trail-decay-pct", type=float, default=0.30, help="Decay fraction to arm cost-trail (default: 0.30)")
    parser.add_argument("--target-decay-pct", type=float, default=0.75, help="Target decay fraction (default: 0.75)")
    parser.add_argument("--max-loss", type=float, default=15000.0, help="Portfolio max loss cap in Rs (0 = disabled, default: 15000.0)")
    parser.add_argument("--orb-end", type=str, default="09:45", help="ORB end time IST HH:MM (default: 09:45)")
    parser.add_argument("--entry-cutoff", type=str, default="13:30", help="Latest entry time IST HH:MM (default: 13:30)")
    parser.add_argument("--exit-time", type=str, default="15:15", help="Mandatory EOD exit IST HH:MM (default: 15:15)")
    parser.add_argument("--min-adx", type=float, default=20.0, help="Minimum 5m ADX(14) required (0 = disabled, default: 20.0)")
    parser.add_argument("--max-orb-width-pct", type=float, default=0.65, help="Max ORB range %% of open (0 = disabled, default: 0.65)")
    parser.add_argument("--rsi-filter", dest="rsi_filter", action="store_true", default=True, help="Require 5m vs 15m RSI alignment on entry (default: True)")
    parser.add_argument("--no-rsi-filter", dest="rsi_filter", action="store_false")
    parser.add_argument("--rsi-exit", dest="rsi_exit", action="store_true", default=False, help="Exit when 5m RSI crosses against 15m RSI (default: False)")
    parser.add_argument("--no-rsi-exit", dest="rsi_exit", action="store_false")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.05)
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS")
    parser.add_argument("--poll", type=int, default=120, help="Poll interval seconds (default: 120 / 2 min)")
    parser.add_argument("--ignore-holidays", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run a single step and exit")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"))
    parser.add_argument("--state-file", type=str, default=None)

    parsed = parser.parse_args(args)
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level)

    mode = "LIVE" if parsed.live else "PAPER"
    config = load_config()
    kite = get_kite_client(
        config=None,
        check_market_hours=True,
        ignore_holidays=parsed.ignore_holidays,
    )
    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)
    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None, product=parsed.product)
    desk = DirectionalOrbDesk(
        order_manager=om,
        state_file=Path(parsed.state_file) if parsed.state_file else None,
        notifier=notifier,
    )

    step_kwargs = dict(
        lots=parsed.lots,
        otm_offset=parsed.otm_offset,
        leg_sl_pct=parsed.leg_sl_pct,
        trail_decay_pct=parsed.trail_decay_pct,
        target_decay_pct=parsed.target_decay_pct,
        max_loss_rs=parsed.max_loss,
        orb_end_time=_parse_hhmm(parsed.orb_end),
        entry_cutoff_time=_parse_hhmm(parsed.entry_cutoff),
        exit_cutoff_time=_parse_hhmm(parsed.exit_time),
        min_adx=parsed.min_adx,
        max_orb_width_pct=parsed.max_orb_width_pct,
        use_rsi_filter=parsed.rsi_filter,
        use_rsi_exit=parsed.rsi_exit,
        order_type=parsed.order_type,
        limit_buffer_pct=parsed.limit_buffer_pct,
    )

    logger.info(
        "Initialized Directional ORB Desk [%s] | lots=%d otm=%d sl=%.0f%% orb_end=%s exit=%s state=%s",
        mode, parsed.lots, parsed.otm_offset, parsed.leg_sl_pct * 100,
        parsed.orb_end, parsed.exit_time, desk.state_file,
    )

    if parsed.once:
        instruments = kite.instruments("NFO")
        desk.step(kite=kite, instruments=instruments, now_dt=datetime.now(), **step_kwargs)
        return 0

    desk.run_loop(kite=kite, poll_seconds=parsed.poll, ignore_holidays=parsed.ignore_holidays, **step_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
