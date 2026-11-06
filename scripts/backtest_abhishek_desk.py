import sys
from pathlib import Path
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.abhishek_desk.config import AbhishekConfig
from strategy.abhishek_desk.engine import AbhishekStrategyEngine


def run_abhishek_backtest(days: int = 30, start_date_str: str = "2026-10-06", end_date_str: str = "2026-11-06"):
    start_d = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()

    print("=" * 95)
    print("  ABHISHEK'S 15-MINUTE PDH/PDL PULLBACK-TRAP STRATEGY BACKTEST")
    print(f"  Evaluation Window: {start_d} to {end_d} (~Last 1 Month)")
    print("=" * 95)

    # 1. Load Daily Bars to construct PDH / PDL map
    df_day = pd.read_parquet("data/nifty_daily_cache.parquet")
    df_day["dt"] = pd.to_datetime(df_day["date"])
    df_day["d"] = df_day["dt"].dt.date
    df_day = df_day.sort_values("d").reset_index(drop=True)

    pdh_pdl_map = {}
    for i in range(1, len(df_day)):
        curr_date = df_day.loc[i, "d"]
        prev_row = df_day.loc[i - 1]
        pdh_pdl_map[curr_date] = {
            "pdh": float(prev_row["high"]),
            "pdl": float(prev_row["low"]),
            "pd_open": float(prev_row["open"]),
            "pd_close": float(prev_row["close"])
        }

    # 2. Load 15m and 5m bars
    df_15m = pd.read_parquet("data/nifty_15m_cache.parquet")
    df_15m["dt"] = pd.to_datetime(df_15m["date"])
    df_15m["d"] = df_15m["dt"].dt.date

    df_5m = pd.read_parquet("data/nifty_5m_cache.parquet")
    df_5m["dt"] = pd.to_datetime(df_5m["date"])
    df_5m["d"] = df_5m["dt"].dt.date

    # Filter trading days in window
    unique_days = sorted([d for d in df_15m["d"].unique() if start_d <= d <= end_d])
    print(f"Total Trading Days in Period: {len(unique_days)} ({unique_days[0]} to {unique_days[-1]})\n")

    cfg = AbhishekConfig(lots=2, lot_size=65)
    engine = AbhishekStrategyEngine(config=cfg)

    results_by_mode = {}

    for mode in ["FUTURES", "OPTIONS"]:
        all_trades = []
        for t_date in unique_days:
            if t_date not in pdh_pdl_map:
                continue

            day_pdh = pdh_pdl_map[t_date]["pdh"]
            day_pdl = pdh_pdl_map[t_date]["pdl"]

            day_15m = df_15m[df_15m["d"] == t_date].copy()
            day_5m = df_5m[df_5m["d"] == t_date].copy()

            trades = engine.simulate_day(
                trade_date=t_date,
                pdh=day_pdh,
                pdl=day_pdl,
                day_15m_bars=day_15m,
                day_5m_bars=day_5m,
                mode=mode,
                trail_runner_to_be=True
            )
            all_trades.extend(trades)

        df_trades = pd.DataFrame(all_trades)
        results_by_mode[mode] = df_trades

    # -------------------------------------------------------------
    # 3. Print Performance Summary
    # -------------------------------------------------------------
    summary_rows = []

    for mode, df_t in results_by_mode.items():
        total_trades = len(df_t)
        if total_trades == 0:
            summary_rows.append([mode, 0, 0, 0, "0.0%", "0.00", "Rs 0.00", "Rs 0.00", "0.0%"])
            continue

        wins = df_t[df_t["net_pnl"] > 0]
        losses = df_t[df_t["net_pnl"] < 0]
        win_rate = (len(wins) / total_trades) * 100.0

        gross_pnl = float(df_t["gross_pnl"].sum())
        net_pnl = float(df_t["net_pnl"].sum())
        gross_profit = float(wins["net_pnl"].sum())
        gross_loss = abs(float(losses["net_pnl"].sum()))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan

        # Drawdown calculation
        daily_pnl = df_t.groupby("date")["net_pnl"].sum()
        all_day_strs = [str(d) for d in unique_days]
        daily_series = pd.Series([daily_pnl.get(d_str, 0.0) for d_str in all_day_strs], index=all_day_strs)
        cum_pnl = daily_series.cumsum()
        peak = cum_pnl.cummax()
        dd_series = peak - cum_pnl
        max_dd = float(dd_series.max())

        # Capital required
        if mode == "FUTURES":
            # 20% margin on 2 lots futures
            avg_cap = float(df_t["capital_required"].mean())
            max_cap = float(df_t["capital_required"].max())
            safe_capital = max_cap + (2 * max_dd)
        else:
            # Option buying total premium paid
            avg_cap = float(df_t["capital_required"].mean())
            max_cap = float(df_t["capital_required"].max())
            # For option buying, recommended account capital is at least ~Rs 1,00,000 to absorb drawdowns safely
            safe_capital = max(100000.0, (2 * max_cap) + (2 * max_dd))

        roi_pct = (net_pnl / safe_capital) * 100.0

        summary_rows.append([
            f"NIFTY {mode} (2 Lots)",
            total_trades,
            f"{len(wins)}W / {len(losses)}L",
            f"{win_rate:.1f}%",
            f"{profit_factor:.2f}" if not np.isnan(profit_factor) else "N/A",
            f"Rs {net_pnl:+,.2f}",
            f"Rs {max_dd:,.2f}",
            f"Rs {safe_capital:,.2f}",
            f"{roi_pct:+.2f}%"
        ])

    headers = [
        "Execution Mode",
        "Total Trades",
        "Wins / Losses",
        "Win Rate",
        "Profit Factor",
        "Net Realized P&L",
        "Max Drawdown",
        "Safe Capital",
        "1-Month ROI (%)"
    ]
    print(tabulate(summary_rows, headers=headers, tablefmt="grid"))

    # Print Detailed Trade Log for both
    for mode, df_t in results_by_mode.items():
        print("\n" + "=" * 95)
        print(f"  TRADE LOG BREAKDOWN: NIFTY {mode} (LAST 1 MONTH)")
        print("=" * 95)
        if df_t.empty:
            print("No trades triggered.")
            continue

        display_cols = [
            "date", "direction", "entry_time", "entry_price", "initial_sl",
            "target_1_price", "exit_time", "exit_price", "exit_reason", "t1_hit", "net_pnl"
        ]
        if mode == "OPTIONS":
            display_cols.extend(["option_strike", "entry_opt_price", "exit_opt_price"])
        
        print(tabulate(df_t[display_cols], headers="keys", tablefmt="simple", showindex=False))

    # Save to CSV
    out_dir = Path("data")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not results_by_mode["FUTURES"].empty:
        results_by_mode["FUTURES"].to_csv(out_dir / "abhishek_futures_trades.csv", index=False)
    if not results_by_mode["OPTIONS"].empty:
        results_by_mode["OPTIONS"].to_csv(out_dir / "abhishek_options_trades.csv", index=False)
    print(f"\n[+] Trade logs saved to: data/abhishek_futures_trades.csv and data/abhishek_options_trades.csv\n")

    return results_by_mode


if __name__ == "__main__":
    run_abhishek_backtest()
