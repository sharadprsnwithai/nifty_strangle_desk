import sys
from pathlib import Path
from datetime import datetime, date, time as dt_time
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backtest_orb import preload_indexed_data, run_orb_simulation
from strategy.orb_desk.config import TARGET_STOCKS

def run_2_lots_analysis():
    nifty_day_map, stock_data, trading_days = preload_indexed_data()

    print(f"Total available trading days: {len(trading_days)} ({trading_days[0]} to {trading_days[-1]})")

    # Define periods:
    # 1. Last 2 Calendar Months: From 2026-09-05 to 2026-11-05
    last_2_months_days = [d for d in trading_days if d >= date(2026, 9, 5)]
    # 2. Full Dataset (~3 Months): All trading days from Aug 10, 2026
    full_days = trading_days

    print(f"Last 2 Months trading days: {len(last_2_months_days)} ({last_2_months_days[0]} to {last_2_months_days[-1]})")

    # Run Backtest for Last 2 Months with 2 LOTS on Static 30 Stocks Universe
    res_2m = run_orb_simulation(
        nifty_day_map=nifty_day_map,
        stock_data=stock_data,
        trading_days=trading_days,  # Needs full history for T-3/T-2/T-1
        use_dynamic_selection=False,
        sizing_mode="LOTS",
        lots=2,
        max_trades_per_day=5,
        daily_stop_loss_limit=50000.0, # Scaled circuit breaker for 2 lots (e.g. Rs 50k or unconstrained)
        target_1_r=1.0,
        eod_squareoff_time=dt_time(15, 0)
    )

    df_all_trades = res_2m["df_trades"].copy()
    df_all_trades["date_obj"] = pd.to_datetime(df_all_trades["date"]).dt.date

    # Filter trades for Last 2 Months (>= 2026-09-05)
    df_2m_trades = df_all_trades[df_all_trades["date_obj"] >= date(2026, 9, 5)].copy()

    # Analyze both:
    for label, df_t, period_days in [
        ("LAST 2 MONTHS (Sep 05, 2026 - Nov 05, 2026)", df_2m_trades, last_2_months_days),
        ("FULL DATASET ~3 MONTHS (Aug 10, 2026 - Nov 05, 2026)", df_all_trades, full_days[3:])
    ]:
        print("\n" + "=" * 90)
        print(f"  PERFORMANCE ANALYSIS: {label}")
        print("  Strategy: Option 3 Canonical Desk (Static 30 Stocks Universe | 2 Lots Sizing)")
        print("=" * 90)

        total_trades = len(df_t)
        if total_trades == 0:
            print("No trades found.")
            continue

        wins = df_t[df_t["pnl"] > 0]
        losses = df_t[df_t["pnl"] < 0]
        be = df_t[df_t["pnl"] == 0]

        net_pnl = float(df_t["pnl"].sum())
        gross_profit = float(wins["pnl"].sum())
        gross_loss = abs(float(losses["pnl"].sum()))
        win_rate = (len(wins) / total_trades) * 100.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan

        # Group by day for daily curve and drawdown
        daily_pnl = df_t.groupby("date")["pnl"].sum()
        # Align with all trading days in period
        all_day_strs = [str(d) for d in period_days]
        daily_series = pd.Series([daily_pnl.get(d_str, 0.0) for d_str in all_day_strs], index=all_day_strs)

        cum_pnl = daily_series.cumsum()
        peak = cum_pnl.cummax()
        drawdown = peak - cum_pnl
        max_dd_rs = float(drawdown.max())

        # Monthly breakdown
        df_t["month"] = pd.to_datetime(df_t["date"]).dt.strftime("%B %Y")
        monthly_summary = df_t.groupby("month")["pnl"].agg(["count", "sum"])

        # -------------------------------------------------------------
        # MARGIN / CAPITAL / FUNDS REQUIRED CALCULATION
        # -------------------------------------------------------------
        # For 2 lots of NSE stock futures:
        # Contract Value per trade = 2 * lot_size * entry_price
        # Margin per trade = ~22% of Contract Value (SPAN + Exposure margin)
        # We enforce a maximum of 5 trades per day.
        # Let's compute:
        #  - Average margin per trade across the 30 stocks
        #  - Maximum single trade margin
        #  - Maximum concurrent margin required on any day (if up to 5 trades are open at peak)
        
        df_t["contract_value"] = df_t["entry_price"] * df_t["quantity"]
        # Standard NSE SPAN + Exposure margin for equity futures is ~20% - 25% (22% average)
        df_t["margin_required"] = df_t["contract_value"] * 0.22

        avg_margin_per_trade = float(df_t["margin_required"].mean())
        max_margin_single_trade = float(df_t["margin_required"].max())

        # Concurrent open trades per day
        trades_per_day = df_t.groupby("date")["margin_required"].agg(["count", "sum"])
        peak_daily_margin_exposure = float(trades_per_day["sum"].max())
        avg_daily_margin_exposure = float(trades_per_day["sum"].mean())

        # Capital / Funds Required:
        # A disciplined quant desk recommends:
        # Core Margin Capital (Peak concurrent margin) + Drawdown Buffer (2x Max Drawdown)
        # Case A: Strict Margin required to hold concurrent trades (Peak single-day margin)
        strict_margin_required = peak_daily_margin_exposure
        # Case B: Recommended Total Trading Capital (Peak margin + 2x Max DD safety cushion)
        recommended_capital = peak_daily_margin_exposure + (2.0 * max_dd_rs)

        # ROI Calculations
        # 1. ROI on Peak Margin:
        roi_peak_margin = (net_pnl / strict_margin_required) * 100.0 if strict_margin_required > 0 else 0
        # 2. ROI on Recommended Safe Capital:
        roi_recommended_capital = (net_pnl / recommended_capital) * 100.0 if recommended_capital > 0 else 0

        # Max Drawdown %:
        max_dd_pct_peak_margin = (max_dd_rs / strict_margin_required) * 100.0 if strict_margin_required > 0 else 0
        max_dd_pct_recommended = (max_dd_rs / recommended_capital) * 100.0 if recommended_capital > 0 else 0

        print(f"Total Trading Days:         {len(period_days)} days")
        print(f"Total Trades Executed:      {total_trades} trades (~{total_trades / len(period_days):.1f} trades/day)")
        print(f"Win Rate:                   {win_rate:.1f}% ({len(wins)} Wins, {len(losses)} Losses, {len(be)} BE)")
        print(f"Profit Factor:              {profit_factor:.2f}")
        print(f"Total Realized Net P&L:     Rs {net_pnl:>12,.2f}")
        print(f"Average P&L / Trade:        Rs {net_pnl / total_trades:>12,.2f}")
        print(f"Gross Profit:               Rs {gross_profit:>12,.2f}")
        print(f"Gross Loss:                 Rs -{gross_loss:>11,.2f}")
        print(f"Max Portfolio Drawdown:     Rs {max_dd_rs:>12,.2f}")
        print(f"Return / Max Drawdown:      {net_pnl / max_dd_rs:.2f}x")
        print("-" * 90)
        print("  FUNDS & CAPITAL REQUIREMENTS (2 LOTS SIZING):")
        print("-" * 90)
        print(f"Average Margin / Trade:     Rs {avg_margin_per_trade:>12,.2f}  (Contract Value ~Rs {df_t['contract_value'].mean():,.0f})")
        print(f"Max Margin / Single Trade:  Rs {max_margin_single_trade:>12,.2f}")
        print(f"Peak Margin in a Single Day:Rs {peak_daily_margin_exposure:>12,.2f}  (Max active concurrent exposure)")
        print(f"Recommended Safety Buffer:  Rs {2.0 * max_dd_rs:>12,.2f}  (2x Max Drawdown buffer)")
        print(f"RECOMMENDED TOTAL CAPITAL:  Rs {recommended_capital:>12,.2f}  (Peak Margin + 2x Max DD)")
        print("-" * 90)
        print("  RETURN ON INVESTMENT (ROI):")
        print("-" * 90)
        print(f"ROI on Peak Margin:         {roi_peak_margin:>11.1f}%  (Net P&L / Peak Margin)")
        print(f"ROI on Total Safe Capital:  {roi_recommended_capital:>11.1f}%  (Net P&L / Total Capital)")
        print(f"Max Drawdown on Safe Capital: {max_dd_pct_recommended:>9.1f}%  (Rs {max_dd_rs:,.2f} / Rs {recommended_capital:,.2f})")
        print("-" * 90)
        print("  MONTHLY P&L BREAKDOWN:")
        print("-" * 90)
        for m, row in monthly_summary.iterrows():
            print(f"  - {m:<18}: {row['count']:>3} trades | Net P&L: Rs {row['sum']:>12,.2f}")
        print("-" * 90)
        print("  EXIT REASON BREAKDOWN:")
        print("-" * 90)
        for r_name, r_grp in df_t.groupby("exit_reason"):
            print(f"  - {r_name:<20}: {len(r_grp):>3} trades | Net P&L: Rs {r_grp['pnl'].sum():>12,.2f} | Avg: Rs {r_grp['pnl'].mean():>8,.2f}")

if __name__ == "__main__":
    run_2_lots_analysis()
