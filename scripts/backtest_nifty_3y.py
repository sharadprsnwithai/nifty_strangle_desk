import sys, os
from pathlib import Path
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))
from strategy.backtest import run_backtest_simulation

print("=" * 115)
print("     NIFTY 10:00 AM INTRADAY STRANGLE: 3-YEAR BACKTEST (10 LOTS | RS 10,000 PORTFOLIO SL)")
print("=" * 115)

df = pd.read_parquet("data/nifty_5m_3y.parquet")
print(f"Loaded {len(df)} 5m bars from {df['date'].min()} to {df['date'].max()}")

# Run backtest with 10 lots (lot_size=65 -> 650 qty), strategy_max_loss=10,000, slippage=0.50
res = run_backtest_simulation(
    df,
    slippage=0.50,
    lots=10,
    lot_size=65,
    iv=0.13,
    r=0.065,
    strategy_max_loss=10000.0
)

summary = res["summary"]
trades = res["trades"]
monthly = res["monthly"]

tdf = pd.DataFrame(trades)
mdf = pd.DataFrame(monthly)

# Save trades and monthly report
tdf.to_csv("data/nifty_10lots_3y_trades.csv", index=False)
mdf.to_csv("data/nifty_10lots_3y_monthly_report.csv", index=False)

print("\n" + "=" * 115)
print("                                    MONTHLY PERFORMANCE BREAKDOWN")
print("=" * 115)
print(tabulate(mdf, headers="keys", tablefmt="pipe", showindex=False))

print("\n" + "=" * 115)
print("                                  3-YEAR CUMULATIVE METRICS SUMMARY")
print("=" * 115)
for k, v in summary.items():
    if isinstance(v, float):
        print(f"  {k:<30}: {v:>15,.2f}")
    else:
        print(f"  {k:<30}: {v:>15}")

# Additional analytics:
win_months = (mdf["net_pnl"] > 0).sum() if "net_pnl" in mdf.columns else 0
total_months = len(mdf)
print(f"  {'Profitable Months':<30}: {win_months} / {total_months} ({win_months/total_months*100:.1f}%)")

# Calculate yearly metrics
tdf["year"] = pd.to_datetime(tdf["date"]).dt.year
yearly = []
for yr, grp in tdf.groupby("year"):
    tot_t = len(grp)
    w = (grp["net_pnl"] > 0).sum()
    l = (grp["net_pnl"] < 0).sum()
    gross = grp["gross_pnl"].sum()
    net = grp["net_pnl"].sum()
    taxes = grp["taxes"].sum()
    slippage = grp["slippage"].sum()
    cum = grp["net_pnl"].cumsum()
    dd = (cum.cummax() - cum).max()
    yearly.append({
        "Year": yr,
        "Trading Days": tot_t,
        "Wins": w,
        "Losses": l,
        "Win Rate (%)": round((w / tot_t) * 100, 1),
        "Gross PnL (Rs)": round(gross, 2),
        "Taxes & Charges": round(taxes, 2),
        "Slippage (Rs)": round(slippage, 2),
        "Net Realized PnL (Rs)": round(net, 2),
        "Max Drawdown (Rs)": round(dd, 2)
    })
ydf = pd.DataFrame(yearly)
print("\n" + "=" * 115)
print("                                     YEAR-WISE PERFORMANCE")
print("=" * 115)
print(tabulate(ydf, headers="keys", tablefmt="pipe", showindex=False))
