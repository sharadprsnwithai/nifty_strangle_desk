"""Backtest 15-Day Consolidation Breakout + Supertrend(10, 3) Shift on NSE Stocks.

Uses the same 248-stock NSE universe (data/swing_daily_2y.parquet) and
Nifty 50 daily data (data/nifty_daily_10y.parquet).

Zero look-ahead bias:
- 15-day consolidation (range <= 15% of low) & liquidity evaluated on day t-1.
- Entry at day t close when Close[t] > 15D_High[t-1] AND Supertrend(10,3)
  has shifted to Bullish (+1) on day t (or within last 3 bars).
- Exit on initial hard SL (consolidation low / max 7% cap, gap-aware) or when
  Supertrend(10,3) flips Bearish (-1) at daily close.
- Includes 0.25% round-trip NSE delivery costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STOCKS_PATH = ROOT / "data" / "swing_daily_2y.parquet"
NIFTY_PATH = ROOT / "data" / "nifty_daily_10y.parquet"
OUT_TRADES = ROOT / "data" / "nse_swing_st_trades.csv"

INITIAL_CAPITAL = 1_000_000.0
RISK_PCT = 0.01
MAX_POSITIONS = 5
MAX_ALLOC_PCT = 0.25
ROUND_TRIP_COST_PCT = 0.0025


@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    sl_price: float
    risk_per_share: float
    days_held: int = 0


def compute_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Compute standard TradingView/Kite Supertrend(period, mult)."""
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)

    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + mult * atr
    basic_lower = hl2 - mult * atr

    final_upper = np.zeros(n, dtype=float)
    final_lower = np.zeros(n, dtype=float)
    direction = np.ones(n, dtype=int)
    supertrend = np.zeros(n, dtype=float)

    final_upper[0] = basic_upper[0]
    final_lower[0] = basic_lower[0]
    direction[0] = 1
    supertrend[0] = final_lower[0]

    for i in range(1, n):
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        if direction[i - 1] == -1 and close[i] > final_upper[i]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close[i] < final_lower[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

        supertrend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    out = df.copy()
    out["st_dir"] = direction
    out["st_val"] = supertrend
    return out


def prepare_stock_data(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    prepared: Dict[str, pd.DataFrame] = {}
    for sym, grp in df.groupby("symbol"):
        g = grp.sort_values("date").copy()
        g["date"] = pd.to_datetime(g["date"]).dt.tz_localize(None)
        g = g.drop_duplicates(subset=["date"]).set_index("date")
        if len(g) < 160:
            continue

        g = compute_supertrend(g, period=10, mult=3.0)
        g["st_flip_bull"] = (g["st_dir"] == 1) & (g["st_dir"].shift(1) == -1)
        # Recent bullish shift within last 3 bars (including today)
        g["st_recent_bull_3d"] = (g["st_dir"] == 1) & (
            g["st_flip_bull"] | g["st_flip_bull"].shift(1, fill_value=False) | g["st_flip_bull"].shift(2, fill_value=False)
        )

        g["consol_high_15"] = g["high"].rolling(15).max()
        g["consol_low_15"] = g["low"].rolling(15).min()
        g["consol_range_pct"] = (g["consol_high_15"] - g["consol_low_15"]) / g["consol_low_15"]

        g["avg_vol20"] = g["volume"].rolling(20).mean()
        g["turnover20"] = (g["close"] * g["volume"]).rolling(20).mean()
        g["liquid"] = (g["close"] > 100.0) & (g["turnover20"] >= 25_00_00_000.0)

        prepared[sym] = g
    return prepared


def prepare_nifty_regime(path: Path) -> pd.Series:
    ndf = pd.read_parquet(path).sort_index()
    ndf.index = pd.to_datetime(ndf.index).tz_localize(None)
    ema50 = ndf["Close"].ewm(span=50, adjust=False).mean()
    return (ndf["Close"] > ema50) & (ema50 > ema50.shift(5))


def run_simulation(
    stock_data: Dict[str, pd.DataFrame],
    nifty_regime: pd.Series,
    use_regime_filter: bool = True,
    exact_flip_only: bool = False,
    max_consol_range: float = 0.15,
    require_vol_surge: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    all_dates = sorted({d for g in stock_data.values() for d in g.index})
    # Same start date as previous backtest (index 150) for apples-to-apples comparison
    start_date = all_dates[150]
    sim_dates = [d for d in all_dates if d >= start_date]

    cash = INITIAL_CAPITAL
    positions: Dict[str, Position] = {}
    trades: List[dict] = []
    equity_curve: List[dict] = []

    for i in range(1, len(sim_dates)):
        today = sim_dates[i]
        yesterday = sim_dates[i - 1]

        # 1. Manage open positions
        to_close: List[str] = []
        for sym, pos in list(positions.items()):
            g = stock_data[sym]
            if today not in g.index:
                continue
            row = g.loc[today]
            pos.days_held += 1

            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            if row["low"] <= pos.sl_price:
                exit_price = min(float(row["open"]), pos.sl_price)
                exit_reason = "STOP_LOSS"
            elif int(row["st_dir"]) == -1:
                exit_price = float(row["close"])
                exit_reason = "ST_BEAR_FLIP"

            if exit_price is not None:
                gross_pnl = (exit_price - pos.entry_price) * pos.qty
                cost = (pos.entry_price + exit_price) * pos.qty * (ROUND_TRIP_COST_PCT / 2.0)
                net_pnl = gross_pnl - cost
                cash += exit_price * pos.qty - (exit_price * pos.qty * ROUND_TRIP_COST_PCT / 2.0)
                total_risk = pos.risk_per_share * pos.qty
                r_mult = net_pnl / total_risk if total_risk > 0 else 0.0
                trades.append(
                    {
                        "symbol": sym,
                        "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
                        "exit_date": today.strftime("%Y-%m-%d"),
                        "days_held": pos.days_held,
                        "qty": pos.qty,
                        "entry_price": round(pos.entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "exit_reason": exit_reason,
                        "net_pnl": round(net_pnl, 2),
                        "r_multiple": round(r_mult, 2),
                    }
                )
                to_close.append(sym)

        for sym in to_close:
            del positions[sym]

        mtm = sum(
            (float(stock_data[s].loc[today, "close"]) if today in stock_data[s].index else p.entry_price)
            * p.qty
            for s, p in positions.items()
        )
        total_equity = cash + mtm

        # 2. Entry scan
        regime_ok = bool(nifty_regime.get(yesterday, True)) if use_regime_filter else True
        if regime_ok and len(positions) < MAX_POSITIONS:
            candidates = []
            for sym, g in stock_data.items():
                if sym in positions or today not in g.index or yesterday not in g.index:
                    continue
                y_row = g.loc[yesterday]
                t_row = g.loc[today]

                if not bool(y_row["liquid"]):
                    continue
                consol_high = float(y_row["consol_high_15"])
                consol_low = float(y_row["consol_low_15"])
                consol_rng = float(y_row["consol_range_pct"])
                avg_vol = float(y_row["avg_vol20"])
                if pd.isna(consol_high) or pd.isna(consol_rng) or pd.isna(avg_vol) or avg_vol <= 0:
                    continue
                if consol_rng > max_consol_range:
                    continue

                st_ok = bool(t_row["st_flip_bull"]) if exact_flip_only else bool(t_row["st_recent_bull_3d"])
                if not st_ok:
                    continue

                vol_ok = (t_row["volume"] >= 1.3 * avg_vol) if require_vol_surge else True
                if t_row["close"] > consol_high and t_row["close"] > t_row["open"] and vol_ok:
                    vol_ratio = float(t_row["volume"]) / avg_vol
                    candidates.append((vol_ratio, sym, t_row, consol_low))

            candidates.sort(key=lambda x: x[0], reverse=True)
            for _, sym, t_row, consol_low in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                entry_px = float(t_row["close"])
                st_sl = float(t_row["st_val"])
                sl_px = max(consol_low, st_sl, entry_px * 0.93)  # Cap max SL at 7%
                risk_per_share = entry_px - sl_px
                if risk_per_share <= 0.01 * entry_px:
                    continue

                qty_by_risk = int((total_equity * RISK_PCT) / risk_per_share)
                qty_by_cap = int((total_equity * MAX_ALLOC_PCT) / entry_px)
                qty_by_cash = int((cash * 0.98) / entry_px)
                qty = min(qty_by_risk, qty_by_cap, qty_by_cash)
                if qty <= 0:
                    continue

                entry_cost = entry_px * qty * (ROUND_TRIP_COST_PCT / 2.0)
                cash -= entry_px * qty + entry_cost
                positions[sym] = Position(
                    symbol=sym,
                    entry_date=today,
                    entry_price=entry_px,
                    qty=qty,
                    sl_price=sl_px,
                    risk_per_share=risk_per_share,
                )

        mtm_eod = sum(
            (float(stock_data[s].loc[today, "close"]) if today in stock_data[s].index else p.entry_price)
            * p.qty
            for s, p in positions.items()
        )
        equity_curve.append({"date": today, "equity": cash + mtm_eod})

    return pd.DataFrame(trades), pd.DataFrame(equity_curve).set_index("date")


def summarize(label: str, trades_df: pd.DataFrame, eq_df: pd.DataFrame) -> None:
    if trades_df.empty:
        print(f"\n=== {label}: 0 trades ===")
        return
    total_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100.0
    gross_win = wins["net_pnl"].sum()
    gross_loss = abs(losses["net_pnl"].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_r = trades_df["r_multiple"].mean()
    avg_days = trades_df["days_held"].mean()

    final_eq = float(eq_df["equity"].iloc[-1])
    total_ret = (final_eq / INITIAL_CAPITAL - 1.0) * 100.0
    days_span = (eq_df.index[-1] - eq_df.index[0]).days
    cagr = ((final_eq / INITIAL_CAPITAL) ** (365.0 / max(days_span, 1)) - 1.0) * 100.0
    roll_max = eq_df["equity"].cummax()
    max_dd = ((eq_df["equity"] - roll_max) / roll_max * 100.0).min()

    print(f"\n=== {label} ===")
    print(f"Period          : {eq_df.index[0].date()} -> {eq_df.index[-1].date()} ({days_span} days)")
    print(f"Total Trades    : {total_trades} | Win Rate: {win_rate:.1f}% | Avg Hold: {avg_days:.1f}d")
    print(f"Profit Factor   : {pf:.2f} | Avg R-Multiple: {avg_r:+.2f}R")
    print(f"Final Equity    : Rs {final_eq:,.0f} (Return: {total_ret:+.2f}%, CAGR: {cagr:+.2f}%)")
    print(f"Max Drawdown    : {max_dd:.2f}%")
    print("Exit Breakdown  :", trades_df["exit_reason"].value_counts().to_dict())


def main() -> None:
    raw_stocks = pd.read_parquet(STOCKS_PATH)
    stock_data = prepare_stock_data(raw_stocks)
    nifty_regime = prepare_nifty_regime(NIFTY_PATH)

    t1, eq1 = run_simulation(stock_data, nifty_regime, use_regime_filter=True, exact_flip_only=True)
    t2, eq2 = run_simulation(stock_data, nifty_regime, use_regime_filter=True, exact_flip_only=False)
    t3, eq3 = run_simulation(stock_data, nifty_regime, use_regime_filter=False, exact_flip_only=False)

    summarize("ST-1: Nifty50 Regime ON + Exact Same-Day ST Flip + 15D Breakout", t1, eq1)
    summarize("ST-2: Nifty50 Regime ON + ST Flip (<=3d) + 15D Breakout", t2, eq2)
    summarize("ST-3: No Regime Filter + ST Flip (<=3d) + 15D Breakout", t3, eq3)

    t2.to_csv(OUT_TRADES, index=False)
    print(f"\nSaved ST-2 trades to {OUT_TRADES}")


if __name__ == "__main__":
    main()
