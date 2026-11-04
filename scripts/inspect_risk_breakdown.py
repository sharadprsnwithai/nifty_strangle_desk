import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_orb_risk_controlled import run_portfolio_simulation, calc_stats

df_tr, df_day, cb = run_portfolio_simulation(
    apply_wick_filter=True,
    apply_pdh_pdl_filter=True,
    apply_nifty_filter=True,
    sizing_mode="RISK_PARITY",
    fixed_risk_rs=2500,
    max_trades_per_day=5,
    daily_loss_limit_rs=10000
)

print("=" * 80)
print("--- B2 DEEP BREAKDOWN (Fixed Rs 2,500 Risk, Max 5 Trades/Day, Rs 10k Circuit Breaker) ---")
print("=" * 80)
print(f"Total Trades: {len(df_tr)}")
print("\nExit Reasons Breakdown:")
print(df_tr["final_exit_reason"].value_counts())

print("\nTrade P&L (R) Distribution:")
print(df_tr["total_pnl_r"].describe())

wins = df_tr[df_tr["total_pnl_rs"] > 0]
losses = df_tr[df_tr["total_pnl_rs"] < -50]
be = df_tr[(df_tr["total_pnl_rs"] >= -50) & (df_tr["total_pnl_rs"] <= 50)]

print(f"\nWinning Trades : {len(wins)} (Avg Win: Rs {wins['total_pnl_rs'].mean():,.0f})")
print(f"Losing Trades  : {len(losses)} (Avg Loss: Rs {losses['total_pnl_rs'].mean():,.0f})")
print(f"Breakeven (BE) : {len(be)} (Avg BE: Rs {be['total_pnl_rs'].mean():,.0f})")

total_profit = wins["total_pnl_rs"].sum()
total_loss = abs(losses["total_pnl_rs"].sum())
print(f"\nGross Profit: Rs {total_profit:,.0f} | Gross Loss: Rs {total_loss:,.0f} | Net: Rs {df_tr['total_pnl_rs'].sum():,.0f}")
print(f"Profit Factor: {total_profit / total_loss:.2f}")

winning_days = len(df_day[df_day["pnl_rs"] > 0])
losing_days = len(df_day[df_day["pnl_rs"] < 0])
flat_days = len(df_day[df_day["pnl_rs"] == 0])
total_days = len(df_day)
print(f"\nDays Summary (Total {total_days}):")
print(f"  Winning Days : {winning_days} ({winning_days / total_days * 100:.1f}%)")
print(f"  Losing Days  : {losing_days} ({losing_days / total_days * 100:.1f}%)")
print(f"  Flat / No-tr : {flat_days}")

print("\nTop 5 Best Days:")
print(df_day.sort_values("pnl_rs", ascending=False)[["date", "trades_count", "pnl_rs"]].head(5).to_string(index=False))

print("\nTop 5 Worst Days:")
print(df_day.sort_values("pnl_rs")[["date", "trades_count", "pnl_rs"]].head(5).to_string(index=False))
