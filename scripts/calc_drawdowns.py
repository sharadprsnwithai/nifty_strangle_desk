import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from scripts.test_pdh_pdl_nifty_filter import simulate_orb_with_filters, all_stock_data


def get_drawdown_stats(trades_list):
    if not trades_list:
        return {}
    df = pd.DataFrame(trades_list)
    df["dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("dt").reset_index(drop=True)

    # Trade-level cumulative P&L
    df["cum_rs"] = df["total_pnl_rs"].cumsum()
    df["peak_rs"] = df["cum_rs"].cummax()
    df["dd_rs"] = df["cum_rs"] - df["peak_rs"]
    max_dd_trade_rs = df["dd_rs"].min()

    df["cum_r"] = df["total_pnl_r"].cumsum()
    df["peak_r"] = df["cum_r"].cummax()
    df["dd_r"] = df["cum_r"] - df["peak_r"]
    max_dd_trade_r = df["dd_r"].min()

    # Daily level cumulative P&L
    daily = df.groupby("date")[["total_pnl_rs", "total_pnl_r"]].sum().reset_index()
    daily["cum_rs"] = daily["total_pnl_rs"].cumsum()
    daily["peak_rs"] = daily["cum_rs"].cummax()
    daily["dd_rs"] = daily["cum_rs"] - daily["peak_rs"]
    max_dd_daily_rs = daily["dd_rs"].min()

    daily["cum_r"] = daily["total_pnl_r"].cumsum()
    daily["peak_r"] = daily["cum_r"].cummax()
    daily["dd_r"] = daily["cum_r"] - daily["peak_r"]
    max_dd_daily_r = daily["dd_r"].min()

    # Consecutive losses
    losses = (df["total_pnl_r"] < -0.05).astype(int)
    max_consec_loss = 0
    curr_loss = 0
    for l in losses:
        if l == 1:
            curr_loss += 1
            if curr_loss > max_consec_loss:
                max_consec_loss = curr_loss
        else:
            curr_loss = 0

    worst_day_rs = daily["total_pnl_rs"].min()
    best_day_rs = daily["total_pnl_rs"].max()
    losing_days = len(daily[daily["total_pnl_rs"] < 0])
    winning_days = len(daily[daily["total_pnl_rs"] > 0])
    daily_win_rate = winning_days / len(daily) * 100

    return {
        "trades": len(df),
        "total_pnl_rs": df["total_pnl_rs"].sum(),
        "total_pnl_r": df["total_pnl_r"].sum(),
        "max_dd_trade_rs": max_dd_trade_rs,
        "max_dd_daily_rs": max_dd_daily_rs,
        "max_dd_trade_r": max_dd_trade_r,
        "max_dd_daily_r": max_dd_daily_r,
        "max_consec_loss": max_consec_loss,
        "worst_day_rs": worst_day_rs,
        "best_day_rs": best_day_rs,
        "trading_days": len(daily),
        "winning_days": winning_days,
        "losing_days": losing_days,
        "daily_win_rate": daily_win_rate,
        "return_to_dd": abs(df["total_pnl_rs"].sum() / max_dd_daily_rs) if max_dd_daily_rs != 0 else 0
    }


configs = [
    ("Baseline (20% Wick Only)", True, False, "NONE"),
    ("Option A: Wick + NIFTY Day Open", True, False, "DAY_OPEN"),
    ("Option B: Wick + PDH/PDL + NIFTY Day Open", True, True, "DAY_OPEN")
]

print("=" * 85)
print("  MAX DRAWDOWN & RISK METRICS COMPARISON (83 TRADING DAYS, 30 STOCKS)")
print("=" * 85)

for name, wick, pdh, nifty in configs:
    trades = []
    for sym, (df, lot) in all_stock_data.items():
        t_list = simulate_orb_with_filters(df, lot_size=lot, apply_wick_filter=wick, apply_pdh_pdl_filter=pdh, nifty_filter_mode=nifty)
        for t in t_list:
            t["symbol"] = sym
            trades.append(t)

    stats = get_drawdown_stats(trades)
    print(f"\n>>> {name}:")
    print(f"    Trades Count       : {stats['trades']}")
    print(f"    Net Realized P&L   : Rs {stats['total_pnl_rs']:>11,.0f}  (+{stats['total_pnl_r']:>5.1f} R)")
    print(f"    Max Daily Drawdown : Rs {stats['max_dd_daily_rs']:>11,.0f}  ({stats['max_dd_daily_r']:>5.1f} R peak-to-trough)")
    print(f"    Max Trade Drawdown : Rs {stats['max_dd_trade_rs']:>11,.0f}  ({stats['max_dd_trade_r']:>5.1f} R trade-by-trade)")
    print(f"    Return / Max DD    : {stats['return_to_dd']:>11.2f}x (Higher is much safer/better)")
    print(f"    Daily Win Rate     : {stats['daily_win_rate']:>11.1f}% ({stats['winning_days']} Win Days / {stats['losing_days']} Loss Days)")
    print(f"    Worst Single Day   : Rs {stats['worst_day_rs']:>11,.0f}")
    print(f"    Best Single Day    : Rs {stats['best_day_rs']:>11,.0f}")
    print(f"    Max Consec Losses  : {stats['max_consec_loss']:>11} trades")
    print("-" * 85)
