import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
from scripts.test_pdh_pdl_nifty_filter import simulate_orb_with_filters, all_stock_data, TARGET_STOCKS

lot_map = {item["symbol"]: item["lot"] for item in TARGET_STOCKS}

# Run Option B
trades_b = []
for sym, (df, lot) in all_stock_data.items():
    t_list = simulate_orb_with_filters(
        df, lot_size=lot, apply_wick_filter=True, apply_pdh_pdl_filter=True, nifty_filter_mode="DAY_OPEN"
    )
    for t in t_list:
        t["symbol"] = sym
        trades_b.append(t)

df_b = pd.DataFrame(trades_b)
daily_b = df_b.groupby("date")["total_pnl_rs"].sum().reset_index()
worst_b = daily_b.sort_values("total_pnl_rs").iloc[0]
worst_date_b = worst_b["date"]
worst_loss_b = worst_b["total_pnl_rs"]

print("=" * 85)
print(f"OPTION B WORST DAY: {worst_date_b}")
print(f"Total Portfolio Day Loss: Rs {worst_loss_b:,.2f}")
print("=" * 85)

day_b_trades = df_b[df_b["date"] == worst_date_b]
print(f"Number of distinct stocks traded on {worst_date_b}: {len(day_b_trades)} stocks (1 lot each)")
print("-" * 85)
print(f"{'Stock':<14} | {'Direction':<8} | {'Lot Size':<10} | {'Exit Reason':<18} | {'R PnL':<7} | {'P&L (Rs)':<12}")
print("-" * 85)
for _, r in day_b_trades.iterrows():
    lot = lot_map.get(r["symbol"], 1)
    print(f"{r['symbol']:<14} | {r['direction']:<8} | {lot:<10} | {r['final_exit_reason']:<18} | {r['total_pnl_r']:>5.1f}R | Rs {r['total_pnl_rs']:>9,.0f}")
print("=" * 85)

losses_all = df_b[df_b["total_pnl_rs"] < 0]["total_pnl_rs"]
print(f"\nAverage Loss per individual stock trade: Rs {losses_all.mean():,.0f}")
print(f"Median Loss per individual stock trade : Rs {losses_all.median():,.0f}")
print(f"Max single trade loss                  : Rs {losses_all.min():,.0f}")

# Check Option A worst day as well
trades_a = []
for sym, (df, lot) in all_stock_data.items():
    t_list = simulate_orb_with_filters(
        df, lot_size=lot, apply_wick_filter=True, apply_pdh_pdl_filter=False, nifty_filter_mode="DAY_OPEN"
    )
    for t in t_list:
        t["symbol"] = sym
        trades_a.append(t)

df_a = pd.DataFrame(trades_a)
daily_a = df_a.groupby("date")["total_pnl_rs"].sum().reset_index()
worst_a = daily_a.sort_values("total_pnl_rs").iloc[0]
print("\n" + "=" * 85)
print(f"OPTION A WORST DAY: {worst_a['date']}")
print(f"Total Day Loss across portfolio: Rs {worst_a['total_pnl_rs']:,.2f}")
day_a_trades = df_a[df_a["date"] == worst_a["date"]]
print(f"Number of distinct stocks traded on {worst_a['date']}: {len(day_a_trades)} stocks (1 lot each)")
print("=" * 85)
