import sys
from pathlib import Path
from datetime import date, time as dt_time
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backtest_orb import preload_indexed_data, run_orb_simulation
from strategy.backtest import run_backtest_simulation

def main():
    start_date = date(2026, 9, 5)
    end_date = date(2026, 11, 5)

    print("=" * 90)
    print("  HEAD-TO-HEAD STRATEGY COMPARISON: 2 LOTS EACH (LAST 2 MONTHS)")
    print(f"  Period: {start_date} to {end_date}")
    print("=" * 90)

    # -------------------------------------------------------------
    # 1. ORB DESK (OPTION 3 CANONICAL, STATIC 30 STOCKS, 2 LOTS)
    # -------------------------------------------------------------
    nifty_day_map, stock_data, trading_days = preload_indexed_data()
    period_days = [d for d in trading_days if d >= start_date and d <= end_date]

    res_orb = run_orb_simulation(
        nifty_day_map=nifty_day_map,
        stock_data=stock_data,
        trading_days=trading_days,
        use_dynamic_selection=False,
        sizing_mode="LOTS",
        lots=2,
        max_trades_per_day=5,
        daily_stop_loss_limit=50000.0,
        target_1_r=1.0,
        eod_squareoff_time=dt_time(15, 0)
    )

    df_orb_trades = res_orb["df_trades"].copy()
    df_orb_trades["date_obj"] = pd.to_datetime(df_orb_trades["date"]).dt.date
    df_orb_2m = df_orb_trades[(df_orb_trades["date_obj"] >= start_date) & (df_orb_trades["date_obj"] <= end_date)].copy()

    total_orb_trades = len(df_orb_2m)
    orb_wins = df_orb_2m[df_orb_2m["pnl"] > 0]
    orb_losses = df_orb_2m[df_orb_2m["pnl"] < 0]
    orb_net_pnl = float(df_orb_2m["pnl"].sum())
    orb_gross_profit = float(orb_wins["pnl"].sum())
    orb_gross_loss = abs(float(orb_losses["pnl"].sum()))
    orb_win_rate = (len(orb_wins) / total_orb_trades) * 100.0 if total_orb_trades > 0 else 0.0
    orb_pf = (orb_gross_profit / orb_gross_loss) if orb_gross_loss > 0 else np.nan

    orb_daily_pnl = df_orb_2m.groupby("date")["pnl"].sum()
    all_day_strs = [str(d) for d in period_days]
    orb_daily_series = pd.Series([orb_daily_pnl.get(d_str, 0.0) for d_str in all_day_strs], index=all_day_strs)
    orb_cum = orb_daily_series.cumsum()
    orb_peak = orb_cum.cummax()
    orb_dd_series = orb_peak - orb_cum
    orb_max_dd = float(orb_dd_series.max())

    df_orb_2m["contract_val"] = df_orb_2m["entry_price"] * df_orb_2m["quantity"]
    df_orb_2m["margin"] = df_orb_2m["contract_val"] * 0.22
    # Group by date to find peak daily concurrent margin
    daily_orb_margin = df_orb_2m.groupby("date")["margin"].sum()
    orb_peak_margin = float(daily_orb_margin.max())
    orb_avg_margin_per_trade = float(df_orb_2m["margin"].mean())
    orb_safe_capital = orb_peak_margin + (2 * orb_max_dd)
    orb_roi_peak_margin = (orb_net_pnl / orb_peak_margin) * 100.0
    orb_roi_safe_capital = (orb_net_pnl / orb_safe_capital) * 100.0
    orb_dd_pct = (orb_max_dd / orb_safe_capital) * 100.0

    # -------------------------------------------------------------
    # 2. NIFTY 50 INTRADAY OPTION SELLING (SHORT STRANGLE, 2 LOTS)
    # -------------------------------------------------------------
    nifty_df = pd.read_parquet("data/nifty_5m_cache.parquet")
    nifty_df["dt"] = pd.to_datetime(nifty_df["date"])
    nifty_df["trade_date"] = nifty_df["dt"].dt.date
    nifty_period_df = nifty_df[(nifty_df["trade_date"] >= start_date) & (nifty_df["trade_date"] <= end_date)].copy()

    # For 2 lots of NIFTY Short Strangle (lot size = 65, total qty = 130 CE + 130 PE)
    # Standard strategy_max_loss for 10 lots is 10k, so for 2 lots it is 2000 Rs (scaled) or 10000 Rs unconstrained
    # Let's run with standard scaled max loss (2,000 Rs) and also check 10,000 Rs:
    res_nifty_2k = run_backtest_simulation(
        bars_df=nifty_period_df,
        slippage=0.50,
        lots=2,
        lot_size=65,
        iv=0.13,
        strategy_max_loss=2000.0
    )
    s_nifty_2k = res_nifty_2k["summary"]

    res_nifty_10k = run_backtest_simulation(
        bars_df=nifty_period_df,
        slippage=0.50,
        lots=2,
        lot_size=65,
        iv=0.13,
        strategy_max_loss=10000.0
    )
    s_nifty_10k = res_nifty_10k["summary"]

    # Calculate capital and margin for NIFTY 2 lots
    # Zerodha margin for 1 lot NIFTY Short Strangle:
    # 1 CE short + 1 PE short with OTM strikes: ~Rs 1,30,000 per lot
    # For 2 lots: Rs 2,60,000 margin required.
    # Safe capital with buffer: Rs 3,00,000 (margin + buffer)
    nifty_margin = 260000.0
    nifty_safe_capital = 300000.0

    # Let's print detailed results
    print("\n" + "-" * 90)
    print("1. 15-MINUTE ORB DESK (OPTION 3 CANONICAL, STATIC 30 STOCKS, 2 LOTS)")
    print("-" * 90)
    print(f"Trading Days:          {len(period_days)} days")
    print(f"Total Trades:          {total_orb_trades} trades")
    print(f"Win Rate:              {orb_win_rate:.1f}% ({len(orb_wins)} Wins / {len(orb_losses)} Losses)")
    print(f"Profit Factor:         {orb_pf:.2f}")
    print(f"Net Realized P&L:      Rs {orb_net_pnl:+,.2f}")
    print(f"Max Portfolio DD:      Rs {orb_max_dd:,.2f} ({orb_dd_pct:.2f}% of capital)")
    print(f"Peak Concurrent Margin:Rs {orb_peak_margin:,.2f}")
    print(f"Total Safe Capital:    Rs {orb_safe_capital:,.2f}")
    print(f"2-Month ROI (Peak):    {orb_roi_peak_margin:.2f}%")
    print(f"2-Month ROI (Capital): {orb_roi_safe_capital:.2f}%")
    print(f"Monthly Avg ROI:       {orb_roi_safe_capital / 2.0:.2f}% / month")

    print("\n" + "-" * 90)
    print("2. NIFTY 50 SHORT STRANGLE (2 LOTS, SCALED MAX LOSS Rs 2,000)")
    print("-" * 90)
    nifty_pnl_2k = s_nifty_2k["net_pnl"]
    nifty_dd_2k = abs(s_nifty_2k["max_drawdown"])
    nifty_roi_margin_2k = (nifty_pnl_2k / nifty_margin) * 100.0
    nifty_roi_cap_2k = (nifty_pnl_2k / nifty_safe_capital) * 100.0
    nifty_dd_pct_2k = (nifty_dd_2k / nifty_safe_capital) * 100.0
    print(f"Trading Days Traded:   {s_nifty_2k['total_days_traded']} days")
    print(f"Win / Loss Days:       {s_nifty_2k['win_days']} Wins / {s_nifty_2k['loss_days']} Losses")
    print(f"Win Rate:              {s_nifty_2k['win_rate_pct']:.1f}%")
    print(f"Profit Factor:         {s_nifty_2k['profit_factor']}")
    print(f"Gross P&L:             Rs {s_nifty_2k['gross_pnl']:+,.2f}")
    print(f"Taxes & Slippage:      Rs {s_nifty_2k['total_taxes_and_brokerage'] + s_nifty_2k['total_slippage']:,.2f}")
    print(f"Net Realized P&L:      Rs {nifty_pnl_2k:+,.2f}")
    print(f"Max Drawdown:          Rs {nifty_dd_2k:,.2f} ({nifty_dd_pct_2k:.2f}% of capital)")
    print(f"Margin Required:       Rs {nifty_margin:,.2f}")
    print(f"Safe Capital:          Rs {nifty_safe_capital:,.2f}")
    print(f"2-Month ROI (Margin):  {nifty_roi_margin_2k:.2f}%")
    print(f"2-Month ROI (Capital): {nifty_roi_cap_2k:.2f}%")
    print(f"Monthly Avg ROI:       {nifty_roi_cap_2k / 2.0:.2f}% / month")

    print("\n" + "-" * 90)
    print("3. NIFTY 50 SHORT STRANGLE (2 LOTS, LEG STOP-LOSS 50% / MAX LOSS Rs 10,000)")
    print("-" * 90)
    nifty_pnl_10k = s_nifty_10k["net_pnl"]
    nifty_dd_10k = abs(s_nifty_10k["max_drawdown"])
    nifty_roi_margin_10k = (nifty_pnl_10k / nifty_margin) * 100.0
    nifty_roi_cap_10k = (nifty_pnl_10k / nifty_safe_capital) * 100.0
    nifty_dd_pct_10k = (nifty_dd_10k / nifty_safe_capital) * 100.0
    print(f"Trading Days Traded:   {s_nifty_10k['total_days_traded']} days")
    print(f"Win / Loss Days:       {s_nifty_10k['win_days']} Wins / {s_nifty_10k['loss_days']} Losses")
    print(f"Win Rate:              {s_nifty_10k['win_rate_pct']:.1f}%")
    print(f"Profit Factor:         {s_nifty_10k['profit_factor']}")
    print(f"Gross P&L:             Rs {s_nifty_10k['gross_pnl']:+,.2f}")
    print(f"Taxes & Slippage:      Rs {s_nifty_10k['total_taxes_and_brokerage'] + s_nifty_10k['total_slippage']:,.2f}")
    print(f"Net Realized P&L:      Rs {nifty_pnl_10k:+,.2f}")
    print(f"Max Drawdown:          Rs {nifty_dd_10k:,.2f} ({nifty_dd_pct_10k:.2f}% of capital)")
    print(f"Margin Required:       Rs {nifty_margin:,.2f}")
    print(f"Safe Capital:          Rs {nifty_safe_capital:,.2f}")
    print(f"2-Month ROI (Margin):  {nifty_roi_margin_10k:.2f}%")
    print(f"2-Month ROI (Capital): {nifty_roi_cap_10k:.2f}%")
    print(f"Monthly Avg ROI:       {nifty_roi_cap_10k / 2.0:.2f}% / month")

if __name__ == "__main__":
    main()
