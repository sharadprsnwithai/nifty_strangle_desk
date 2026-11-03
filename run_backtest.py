import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
from tabulate import tabulate
from kite import get_kite_client
from strategy.backtest import run_backtest_simulation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest.cli")


def load_historical_nifty_data(days: int = 180) -> pd.DataFrame:
    """Fetches historical NIFTY 5m bars from local cache or Kite Connect."""
    cache_path = Path(__file__).resolve().parent / "data" / "nifty_5m_cache.parquet"
    if cache_path.exists():
        logger.info("Loading cached NIFTY 5-minute bars from %s", cache_path)
        return pd.read_parquet(cache_path)

    sibling_cache = Path("D:/code/Sharads_monthly_option_selling_desk/data_cache/NSEI_5m_historical.parquet")
    if sibling_cache.exists():
        logger.info("Loading NIFTY 5m bars from sibling desk cache: %s", sibling_cache)
        return pd.read_parquet(sibling_cache)

    logger.info("Fetching %d days of NIFTY 5m bars via Kite Connect API...", days)
    kite = get_kite_client()
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    
    # NIFTY 50 token on NSE
    records = kite.historical_data(256265, start_dt, end_dt, "5minute")
    df = pd.DataFrame(records)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df


def run_backtest_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="NIFTY 10:00 AM Short Strangle Realistic Backtester")
    parser.add_argument("--days", type=int, default=180, help="Historical days to evaluate")
    parser.add_argument("--slippage", type=float, default=0.50, help="Slippage in Rs per share (default: 0.50)")
    parser.add_argument("--lots", type=int, default=10, help="Number of lots (default: 10)")
    parser.add_argument("--lot-size", type=int, default=75, help="Lot size (default: 75)")
    parser.add_argument("--iv", type=float, default=0.13, help="Option IV (default: 0.13)")
    parser.add_argument("--output", type=str, default="data/backtest_trade_log.csv", help="CSV trade log path")
    
    parsed = parser.parse_args(args)

    try:
        bars_df = load_historical_nifty_data(days=parsed.days)
        logger.info("Running simulation across %d bars...", len(bars_df))

        results = run_backtest_simulation(
            bars_df=bars_df,
            slippage=parsed.slippage,
            lots=parsed.lots,
            lot_size=parsed.lot_size,
            iv=parsed.iv
        )

        summary = results["summary"]
        print("\n" + "=" * 60)
        print("   📊 NIFTY 10:00 AM SHORT STRANGLE BACKTEST RESULTS")
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
            print("\n📅 Monthly Performance Breakdown:")
            m_rows = [[m["month"], f"Rs. {m['net_pnl']:+,.2f}"] for m in results["monthly"]]
            print(tabulate(m_rows, headers=["Month", "Net P&L (Rs.)"], tablefmt="simple"))

        if results["trades"]:
            out_path = Path(parsed.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(results["trades"]).to_csv(out_path, index=False)
            print(f"\n💾 Saved full trade log ({len(results['trades'])} days) to: {out_path}\n")

        return 0
    except Exception as e:
        logger.error("Backtest failed: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(run_backtest_cli())
