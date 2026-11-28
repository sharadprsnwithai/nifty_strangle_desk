"""Backtest Institutional Footprint Filters on top of VCP Variant B-15D.

Uses the 248-stock NSE universe (data/swing_daily_2y.parquet) and Nifty 50
(data/nifty_daily_10y.parquet). Note: OHLCV parquet does not contain NSE
delivery % or FII/DII filings, so this backtests the price-volume-RS
institutional footprints:
  1. Pocket Pivot inside 15D base: up-day volume > max down-day volume in prior 10 days.
  2. Accumulation Volume Ratio: 20D Up-Volume / Down-Volume >= 1.25.
  3. Relative Strength (RS) vs Nifty 50: 60D stock return > 60D Nifty 50 return.

Zero look-ahead bias; 0.25% round-trip NSE delivery costs; Rs 10L capital;
1% risk/trade; max 5 positions; 20 EMA close trail + 10D time stop (<0.5R).
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
OUT_TRADES = ROOT / "data" / "nse_swing_footprint_trades.csv"

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


def prepare_nifty(path: Path) -> tuple[pd.Series, pd.Series]:
    ndf = pd.read_parquet(path).sort_index()
    ndf.index = pd.to_datetime(ndf.index).tz_localize(None)
    ema50 = ndf["Close"].ewm(span=50, adjust=False).mean()
    regime = (ndf["Close"] > ema50) & (ema50 > ema50.shift(5))
    ret60 = ndf["Close"].pct_change(60)
    return regime, ret60


def prepare_stock_data(df: pd.DataFrame, nifty_ret60: pd.Series) -> Dict[str, pd.DataFrame]:
    prepared: Dict[str, pd.DataFrame] = {}
    for sym, grp in df.groupby("symbol"):
        g = grp.sort_values("date").copy()
        g["date"] = pd.to_datetime(g["date"]).dt.tz_localize(None)
        g = g.drop_duplicates(subset=["date"]).set_index("date")
        if len(g) < 160:
            continue

        g["ema20"] = g["close"].ewm(span=20, adjust=False).mean()
        g["ema50"] = g["close"].ewm(span=50, adjust=False).mean()
        g["ema150"] = g["close"].ewm(span=150, adjust=False).mean()
        g["avg_vol20"] = g["volume"].rolling(20).mean()
        g["turnover20"] = (g["close"] * g["volume"]).rolling(20).mean()

        prev_close = g["close"].shift(1)
        tr = pd.concat(
            [
                g["high"] - g["low"],
                (g["high"] - prev_close).abs(),
                (g["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        g["atr14"] = tr.rolling(14).mean()
        g["high_200"] = g["high"].rolling(200, min_periods=120).max()
        g["swing_low_5"] = g["low"].rolling(5).min()
        g["consol_high_15"] = g["high"].rolling(15).max()
        consol_low_15 = g["low"].rolling(15).min()
        g["consol_range_15"] = (g["consol_high_15"] - consol_low_15) / consol_low_15

        # Institutional Footprint 1: Pocket Pivot (up day volume > max down-day volume in prior 10 days)
        is_up = g["close"] > prev_close
        is_down = g["close"] < prev_close
        down_vol = g["volume"].where(is_down, 0.0)
        max_down_vol_10 = down_vol.rolling(10).max().shift(1)
        pocket_pivot_day = is_up & (g["volume"] > max_down_vol_10)
        g["fp_pocket_pivot_15d"] = pocket_pivot_day.rolling(15).max().fillna(0).astype(bool)

        # Institutional Footprint 2: 20D Up-Volume / Down-Volume >= 1.25
        up_vol_20 = g["volume"].where(is_up, 0.0).rolling(20).sum()
        down_vol_20 = g["volume"].where(is_down, 0.0).rolling(20).sum()
        g["fp_accum_ratio"] = (up_vol_20 / down_vol_20.replace(0.0, np.nan)).fillna(0.0) >= 1.25

        # Institutional Footprint 3: 60D Relative Strength > Nifty 50
        stock_ret60 = g["close"].pct_change(60)
        aligned_nifty_ret60 = nifty_ret60.reindex(g.index).ffill()
        g["fp_rs_leader"] = stock_ret60 > aligned_nifty_ret60

        uptrend = (g["close"] > g["ema50"]) & (g["ema50"] > g["ema150"])
        near_high = g["close"] >= 0.80 * g["high_200"]
        vol_dry = g["volume"].rolling(3).mean() < g["avg_vol20"]
        liquid = (g["close"] > 100.0) & (g["turnover20"] >= 25_00_00_000.0)

        g["setup_base_15d"] = uptrend & near_high & vol_dry & liquid & (g["consol_range_15"] <= 0.15)
        prepared[sym] = g
    return prepared


def run_simulation(
    stock_data: Dict[str, pd.DataFrame],
    nifty_regime: pd.Series,
    require_pp: bool = False,
    require_accum: bool = False,
    require_rs: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    all_dates = sorted({d for g in stock_data.values() for d in g.index})
    sim_dates = [d for d in all_dates if d >= all_dates[150]]

    cash = INITIAL_CAPITAL
    positions: Dict[str, Position] = {}
    trades: List[dict] = []
    equity_curve: List[dict] = []

    for i in range(1, len(sim_dates)):
        today = sim_dates[i]
        yesterday = sim_dates[i - 1]

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
            elif row["close"] < row["ema20"]:
                exit_price = float(row["close"])
                exit_reason = "EMA20_TRAIL"
            elif pos.days_held >= 10:
                curr_r = (float(row["close"]) - pos.entry_price) / pos.risk_per_share
                if curr_r < 0.5:
                    exit_price = float(row["close"])
                    exit_reason = "TIME_STOP"

            if exit_price is not None:
                gross_pnl = (exit_price - pos.entry_price) * pos.qty
                cost = (pos.entry_price + exit_price) * pos.qty * (ROUND_TRIP_COST_PCT / 2.0)
                net_pnl = gross_pnl - cost
                cash += exit_price * pos.qty - (exit_price * pos.qty * ROUND_TRIP_COST_PCT / 2.0)
                total_risk = pos.risk_per_share * pos.qty
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
                        "r_multiple": round(net_pnl / total_risk, 2) if total_risk > 0 else 0.0,
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

        if bool(nifty_regime.get(yesterday, True)) and len(positions) < MAX_POSITIONS:
            candidates = []
            for sym, g in stock_data.items():
                if sym in positions or today not in g.index or yesterday not in g.index:
                    continue
                y_row = g.loc[yesterday]
                t_row = g.loc[today]
                if not bool(y_row["setup_base_15d"]):
                    continue
                if require_pp and not bool(y_row["fp_pocket_pivot_15d"]):
                    continue
                if require_accum and not bool(y_row["fp_accum_ratio"]):
                    continue
                if require_rs and not bool(y_row["fp_rs_leader"]):
                    continue

                trigger_high = float(y_row["consol_high_15"])
                avg_vol = float(y_row["avg_vol20"])
                if pd.isna(trigger_high) or pd.isna(avg_vol) or avg_vol <= 0:
                    continue

                if (
                    t_row["close"] > trigger_high
                    and t_row["close"] > t_row["open"]
                    and t_row["volume"] >= 1.5 * avg_vol
                ):
                    candidates.append((float(t_row["volume"]) / avg_vol, sym, t_row, y_row))

            candidates.sort(key=lambda x: x[0], reverse=True)
            for _, sym, t_row, y_row in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                entry_px = float(t_row["close"])
                atr = float(y_row["atr14"])
                swing_low = float(y_row["swing_low_5"])
                sl_px = max(swing_low, entry_px - 1.5 * atr, entry_px * 0.94)
                risk_per_share = entry_px - sl_px
                if risk_per_share <= 0.01 * entry_px:
                    continue

                qty = min(
                    int((total_equity * RISK_PCT) / risk_per_share),
                    int((total_equity * MAX_ALLOC_PCT) / entry_px),
                    int((cash * 0.98) / entry_px),
                )
                if qty <= 0:
                    continue
                cash -= entry_px * qty + entry_px * qty * (ROUND_TRIP_COST_PCT / 2.0)
                positions[sym] = Position(sym, today, entry_px, qty, sl_px, risk_per_share)

        mtm_eod = sum(
            (float(stock_data[s].loc[today, "close"]) if today in stock_data[s].index else p.entry_price)
            * p.qty
            for s, p in positions.items()
        )
        equity_curve.append({"date": today, "equity": cash + mtm_eod})

    return pd.DataFrame(trades), pd.DataFrame(equity_curve).set_index("date")


def summarize(label: str, trades_df: pd.DataFrame, eq_df: pd.DataFrame) -> float:
    if trades_df.empty:
        print(f"\n=== {label}: 0 trades ===")
        return 0.0
    total_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100.0
    pf = wins["net_pnl"].sum() / abs(losses["net_pnl"].sum()) if not losses.empty else float("inf")
    avg_r = trades_df["r_multiple"].mean()
    avg_days = trades_df["days_held"].mean()

    final_eq = float(eq_df["equity"].iloc[-1])
    total_ret = (final_eq / INITIAL_CAPITAL - 1.0) * 100.0
    days_span = (eq_df.index[-1] - eq_df.index[0]).days
    cagr = ((final_eq / INITIAL_CAPITAL) ** (365.0 / max(days_span, 1)) - 1.0) * 100.0
    roll_max = eq_df["equity"].cummax()
    max_dd = ((eq_df["equity"] - roll_max) / roll_max * 100.0).min()

    print(f"\n=== {label} ===")
    print(f"Total Trades    : {total_trades} | Win Rate: {win_rate:.1f}% | Avg Hold: {avg_days:.1f}d")
    print(f"Profit Factor   : {pf:.2f} | Avg R-Multiple: {avg_r:+.2f}R")
    print(f"Final Equity    : Rs {final_eq:,.0f} (Return: {total_ret:+.2f}%, CAGR: {cagr:+.2f}%)")
    print(f"Max Drawdown    : {max_dd:.2f}%")
    print("Exit Breakdown  :", trades_df["exit_reason"].value_counts().to_dict())
    return total_ret


def main() -> None:
    nifty_regime, nifty_ret60 = prepare_nifty(NIFTY_PATH)
    raw_stocks = pd.read_parquet(STOCKS_PATH)
    stock_data = prepare_stock_data(raw_stocks, nifty_ret60)

    runs = [
        ("Baseline: VCP B-15D (No Footprint Filter)", False, False, False),
        ("FP-1: + Pocket Pivot in 15D Base", True, False, False),
        ("FP-2: + 20D Up/Down Vol >= 1.25", False, True, False),
        ("FP-3: + 60D RS > Nifty 50", False, False, True),
        ("FP-ALL: Pocket Pivot + Accum Vol + RS Leader", True, True, True),
    ]

    best_ret = -999.0
    best_trades = pd.DataFrame()
    for label, pp, acc, rs in runs:
        tdf, edf = run_simulation(stock_data, nifty_regime, require_pp=pp, require_accum=acc, require_rs=rs)
        ret = summarize(label, tdf, edf)
        if ret > best_ret:
            best_ret = ret
            best_trades = tdf

    best_trades.to_csv(OUT_TRADES, index=False)
    print(f"\nSaved best footprint trades to {OUT_TRADES}")


if __name__ == "__main__":
    main()
