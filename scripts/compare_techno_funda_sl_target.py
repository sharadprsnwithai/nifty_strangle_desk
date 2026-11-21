import sys
from pathlib import Path
import pandas as pd
import numpy as np

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.techno_funda.technicals import calculate_rsi


def simulate_grid(sl_pct, target_pct, raw_df, symbols):
    allocation = 100000.0
    max_holding = 45
    trades = []

    for sym in symbols:
        sub = raw_df[raw_df["symbol"] == sym].sort_values("date").reset_index(drop=True)
        if len(sub) < 60:
            continue

        sub["daily_rsi"] = calculate_rsi(sub["close"], 14)
        sub["prev_daily_rsi"] = sub["daily_rsi"].shift(1)

        w_df = sub[["date", "close"]].set_index("date").resample("W-FRI").last().dropna().reset_index()
        w_df["weekly_rsi"] = calculate_rsi(w_df["close"], 14)

        m_df = sub[["date", "close"]].set_index("date").resample("ME").last().dropna().reset_index()
        m_df["monthly_rsi"] = calculate_rsi(m_df["close"], 14)

        sub = pd.merge_asof(sub, w_df[["date", "weekly_rsi"]], on="date", direction="backward")
        sub = pd.merge_asof(sub, m_df[["date", "monthly_rsi"]], on="date", direction="backward")

        crossed_daily = (sub["daily_rsi"] >= 65.0) & (sub["prev_daily_rsi"] < 65.0)
        in_zone = (sub["daily_rsi"] >= 65.0) & (sub["daily_rsi"] <= 75.0)
        trend_aligned = (sub["monthly_rsi"] >= 58.0) & (sub["weekly_rsi"] >= 58.0)
        signals = trend_aligned & (crossed_daily | in_zone)

        i = 0
        n = len(sub)
        while i < n - 1:
            if signals.iloc[i]:
                entry_idx = i + 1
                entry_date = sub["date"].iloc[entry_idx]
                entry_price = float(sub["open"].iloc[entry_idx])
                if entry_price <= 0:
                    i += 1
                    continue

                shares = int(allocation / entry_price)
                if shares <= 0:
                    i += 1
                    continue

                actual_cost = shares * entry_price
                sl_price = round(entry_price * (1.0 - sl_pct), 2)
                target_price = round(entry_price * (1.0 + target_pct), 2)

                exit_price = None
                exit_date = None
                exit_reason = None
                hold_days = 0

                for k in range(entry_idx, min(entry_idx + max_holding, n)):
                    hold_days += 1
                    bar_open = float(sub["open"].iloc[k])
                    bar_high = float(sub["high"].iloc[k])
                    bar_low = float(sub["low"].iloc[k])
                    curr_date = sub["date"].iloc[k]

                    if bar_open >= target_price:
                        exit_price = bar_open
                        exit_reason = "TARGET_GAP"
                        break
                    elif bar_open <= sl_price:
                        exit_price = bar_open
                        exit_reason = "SL_GAP"
                        break

                    hit_sl = bar_low <= sl_price
                    hit_target = bar_high >= target_price

                    if hit_sl and hit_target:
                        exit_price = sl_price
                        exit_reason = "SL_HIT"
                        break
                    elif hit_target:
                        exit_price = target_price
                        exit_reason = "TARGET_HIT"
                        break
                    elif hit_sl:
                        exit_price = sl_price
                        exit_reason = "SL_HIT"
                        break

                if exit_price is None:
                    last_idx = min(entry_idx + max_holding - 1, n - 1)
                    exit_price = float(sub["close"].iloc[last_idx])
                    exit_reason = "TIME_EXIT"

                gross_pnl = (exit_price - entry_price) * shares
                turnover = actual_cost + (shares * exit_price)
                charges = (turnover * 0.0025) + 15.93
                net_pnl = gross_pnl - charges

                trades.append({
                    "symbol": sym,
                    "net_pnl": net_pnl,
                    "gross_pnl": gross_pnl,
                    "exit_reason": exit_reason,
                    "return_pct": ((exit_price - entry_price) / entry_price) * 100.0,
                })
                i = k
            i += 1

    tdf = pd.DataFrame(trades)
    if tdf.empty:
        return {}

    wins = tdf[tdf["net_pnl"] > 0]
    losses = tdf[tdf["net_pnl"] <= 0]
    gross_win = wins["gross_pnl"].sum()
    gross_loss = abs(losses["gross_pnl"].sum())
    pf = (gross_win / gross_loss) if gross_loss > 0 else np.nan

    tdf["cum"] = tdf["net_pnl"].cumsum()
    max_dd = (tdf["cum"] - tdf["cum"].cummax()).min()

    return {
        "SL": f"{sl_pct*100:.1f}%",
        "Target": f"{target_pct*100:.1f}%",
        "R:R": f"1:{target_pct/sl_pct:.1f}",
        "Trades": len(tdf),
        "Win Rate": f"{(len(wins)/len(tdf))*100:.1f}%",
        "Net PnL (Rs)": tdf["net_pnl"].sum(),
        "Profit Factor": round(pf, 2),
        "Max DD (Rs)": max_dd,
    }


def main():
    raw_df = pd.read_parquet("data/swing_daily_2y.parquet")
    raw_df["date"] = pd.to_datetime(raw_df["date"])
    symbols = raw_df["symbol"].unique()

    grid = [
        (0.05, 0.10),   # User request: 5% SL, 10% Target
        (0.05, 0.075),  # 5% SL, 7.5% Target (1:1.5)
        (0.06, 0.12),   # 6% SL, 12% Target
        (0.07, 0.14),   # 7% SL, 14% Target
        (0.08, 0.16),   # 8% SL, 16% Target
        (0.08, 0.20),   # 8% SL, 20% Target (1:2.5)
        (0.10, 0.20),   # 10% SL, 20% Target (Minervini Standard)
        (0.10, 0.25),   # 10% SL, 25% Target (1:2.5)
    ]

    results = []
    print("Running parameter grid comparison across 248 equities...")
    for sl, tg in grid:
        res = simulate_grid(sl, tg, raw_df, symbols)
        if res:
            results.append(res)

    res_df = pd.DataFrame(results)
    print("\n" + "=" * 90)
    print("  COMPARATIVE RISK-REWARD ANALYSIS (MTF CASH SWING BUYING)")
    print("=" * 90)
    print(res_df.to_string(index=False))
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
