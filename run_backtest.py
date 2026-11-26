import sys
import os
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta, time
import pandas as pd
from tabulate import tabulate
from kite import get_kite_client
from strategy.backtest import run_backtest_simulation

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest.cli")


def load_historical_nifty_data(days: int = 180, force_fetch: bool = False) -> pd.DataFrame:
    """Fetches historical NIFTY 5m bars from local cache or Kite Connect, filtered to requested days."""
    env_cache = os.getenv("HISTORICAL_CACHE_PATH")
    cache_path = Path(__file__).resolve().parent / "data" / "nifty_5m_cache.parquet"
    sibling_cache = Path("D:/code/Sharads_monthly_option_selling_desk/data_cache/NSEI_5m_historical.parquet")

    df = pd.DataFrame()
    from_env = False
    if not force_fetch:
        if env_cache and Path(env_cache).exists():
            logger.info("Loading NIFTY 5m bars from HISTORICAL_CACHE_PATH: %s", env_cache)
            df = pd.read_parquet(Path(env_cache))
            from_env = True
        elif cache_path.exists():
            logger.info("Loading cached NIFTY 5-minute bars from %s", cache_path)
            df = pd.read_parquet(cache_path)
        elif sibling_cache.exists():
            logger.info("Loading NIFTY 5m bars from sibling desk cache: %s", sibling_cache)
            df = pd.read_parquet(sibling_cache)

    needs_fetch = force_fetch or df.empty
    if not from_env and not needs_fetch and days > 0 and not df.empty:
        ts_col = "timestamps" if "timestamps" in df.columns else ("date" if "date" in df.columns else df.columns[0])
        dt_series = pd.to_datetime(df[ts_col])
        span_days = (dt_series.max() - dt_series.min()).days
        if span_days < (days * 0.8):
            logger.info("Local cache covers %d days, but %d days were requested. Fetching full range from Kite...", span_days, days)
            needs_fetch = True

    if needs_fetch:
        logger.info("Fetching %d days of NIFTY 5m bars via Kite Connect API (in 60-day chunks)...", days)
        kite = get_kite_client()
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)

        chunks = []
        cur_start = start_dt
        while cur_start < end_dt:
            cur_end = min(cur_start + timedelta(days=60), end_dt)
            records = kite.historical_data(256265, cur_start, cur_end, "5minute")
            if records:
                chunks.extend(records)
            cur_start = cur_end

        df = pd.DataFrame(chunks)
        if not df.empty:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path)

    if not df.empty and days > 0:
        ts_col = "timestamps" if "timestamps" in df.columns else ("date" if "date" in df.columns else df.columns[0])
        cutoff = pd.to_datetime(df[ts_col]).max() - timedelta(days=days)
        df = df[pd.to_datetime(df[ts_col]) >= cutoff].copy()

    return df


def run_backtest_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="NIFTY 10:00 AM Short Strangle Realistic Backtester")
    parser.add_argument("--days", type=int, default=180, help="Historical days to evaluate")
    parser.add_argument("--slippage", type=float, default=0.50, help="Slippage in Rs per share (default: 0.50)")
    parser.add_argument("--lots", type=int, default=10, help="Number of lots (default: 10)")
    parser.add_argument("--lot-size", type=int, default=65, help="Lot size (default: 65)")
    parser.add_argument("--strategy-max-loss", "--max-loss", type=float, default=0.0, dest="strategy_max_loss", help="Strategy total loss exit in Rs across all legs (<= 0 disables, default: 0.0 / No SL)")
    parser.add_argument("--no-max-loss", dest="strategy_max_loss", action="store_const", const=0.0, help="Disable strategy-level portfolio max loss stop loss")
    parser.add_argument("--use-leg-sl", dest="use_leg_sl", action="store_true", default=True, help="Enable single-leg exit on stop loss (default: True)")
    parser.add_argument("--no-leg-sl", dest="use_leg_sl", action="store_false", help="Disable single-leg exit (use simultaneous dual-leg exit)")
    parser.add_argument("--leg-sl-pct", type=float, default=0.70, help="Single-leg stop loss percentage as decimal (e.g. 0.50 for 50%%, 0.70 for 70%%, default: 0.70)")
    parser.add_argument("--trail-cost", dest="trail_opposite_to_cost", action="store_true", default=False, help="Trail opposite leg to cost/breakeven after stop-loss (default: False)")
    parser.add_argument("--no-trail-cost", dest="trail_opposite_to_cost", action="store_false", help="Disable trailing opposite leg to cost")
    parser.add_argument("--no-sl", action="store_true", help="Disable all stop losses (disables leg SL, trail-to-cost, and portfolio max loss)")
    parser.add_argument("--roll-untested", dest="roll_untested", action="store_true", default=True, help="Roll untested leg to ATM on 80%% decay target when combined P&L <= 0 (default: True)")
    parser.add_argument("--no-roll-untested", dest="roll_untested", action="store_false", help="Disable rolling untested leg on decay target")
    parser.add_argument("--entry-time", type=str, default="10:00", help="Intraday entry time HH:MM (default: 10:00)")
    parser.add_argument("--exit-cutoff", type=str, default="15:15", help="Intraday square-off cutoff time HH:MM (default: 15:15)")
    parser.add_argument("--expiry-weekday", type=int, default=1, help="Expiry weekday (0=Mon, 1=Tue, ..., 3=Thu. Default: 1 for Tuesday)")
    parser.add_argument("--iv", type=float, default=0.13, help="Option IV (default: 0.13)")
    parser.add_argument("--strike-mode", choices=["ATR", "PRICE"], default="ATR", help="Strike selection mode (ATR: 1.25x ATR distance, PRICE: Rs 30 premium, default: ATR)")
    parser.add_argument("--atr-multiplier", type=float, default=1.25, help="ATR multiplier for strike distance (default: 1.25)")
    parser.add_argument("--target-price", type=float, default=30.0, help="Target premium in Rs for PRICE strike mode (default: 30.0)")
    parser.add_argument("--morning-filter", dest="morning_filter", action="store_true", default=True, help="Enable 09:15-10:00 AM range trend filter (default: True)")
    parser.add_argument("--no-morning-filter", dest="morning_filter", action="store_false", help="Disable morning trend filter")
    parser.add_argument("--morning-filter-pct", type=float, default=0.60, help="Morning range percentage threshold to skip session (default: 0.60)")
    parser.add_argument("--output", type=str, default="data/backtest_trade_log.csv", help="CSV trade log path")
    parser.add_argument("--force-fetch", action="store_true", help="Force fresh fetch from Kite Connect API, bypassing local cache")

    parsed = parser.parse_args(args)
    if parsed.no_sl:
        parsed.use_leg_sl = False
        parsed.trail_opposite_to_cost = False
        parsed.strategy_max_loss = 0.0
    entry_h, entry_m = map(int, parsed.entry_time.split(":"))
    entry_time = time(entry_h, entry_m)
    cutoff_h, cutoff_m = map(int, parsed.exit_cutoff.split(":"))
    exit_cutoff = time(cutoff_h, cutoff_m)

    try:
        bars_df = load_historical_nifty_data(days=parsed.days, force_fetch=parsed.force_fetch)
        logger.info("Running simulation across %d bars...", len(bars_df))

        results = run_backtest_simulation(
            bars_df=bars_df,
            slippage=parsed.slippage,
            lots=parsed.lots,
            lot_size=parsed.lot_size,
            iv=parsed.iv,
            expiry_weekday=parsed.expiry_weekday,
            strategy_max_loss=parsed.strategy_max_loss,
            use_leg_sl=parsed.use_leg_sl,
            leg_sl_pct=parsed.leg_sl_pct,
            trail_opposite_to_cost=parsed.trail_opposite_to_cost,
            roll_untested=parsed.roll_untested,
            entry_time=entry_time,
            exit_cutoff_time=exit_cutoff,
            morning_filter=parsed.morning_filter,
            morning_filter_threshold_pct=parsed.morning_filter_pct,
            use_atr_strikes=(parsed.strike_mode.upper() == "ATR"),
            atr_multiplier=parsed.atr_multiplier,
            target_price=parsed.target_price,
        )

        summary = results["summary"]
        print("\n" + "=" * 60)
        print("   [+] NIFTY 10:00 AM SHORT STRANGLE BACKTEST RESULTS")
        print("=" * 60)
        
        sum_rows = [
            ["Total Days Traded", summary["total_days_traded"]],
            ["Win / Loss Days", f"{summary['win_days']} Wins / {summary['loss_days']} Losses"],
            ["Win Rate", f"{summary['win_rate_pct']:.1f}%"],
            ["Gross P&L", f"Rs. {summary['gross_pnl']:+,.2f}"],
            ["Total Taxes & Brokerage", f"Rs. {summary['total_taxes_and_brokerage']:,.2f}"],
            ["Total Execution Slippage", f"Rs. {summary['total_slippage']:,.2f}"],
            ["Net Realized P&L", f"Rs. {summary['net_pnl']:+,.2f}"],
            ["Profit Factor", summary["profit_factor"]],
            ["Max Drawdown", f"Rs. {summary['max_drawdown']:+,.2f}"],
            ["Avg Daily Net", f"Rs. {summary['avg_daily_net']:+,.2f}"]
        ]
        print(tabulate(sum_rows, tablefmt="grid"))

        if results["monthly"]:
            print("\n--- Monthly Performance Breakdown ---")
            m_rows = [[m["month"], f"Rs. {m['net_pnl']:+,.2f}"] for m in results["monthly"]]
            print(tabulate(m_rows, headers=["Month", "Net P&L (Rs.)"], tablefmt="simple"))

        if results["trades"]:
            out_path = Path(parsed.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(results["trades"]).to_csv(out_path, index=False)
            print(f"\n[+] Saved full trade log ({len(results['trades'])} days) to: {out_path}\n")

        return 0
    except Exception as e:
        logger.error("Backtest failed: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(run_backtest_cli())
