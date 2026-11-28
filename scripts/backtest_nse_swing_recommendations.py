"""Backtest the two recommended NSE Swing Strategies on the 248-stock universe.

1. Upgraded 15-Day Consolidation + Supertrend(7, 2) Shift:
   - Requires primary uptrend: Close > EMA50 > EMA150.
   - Tight 15-day consolidation (range <= 15%) + breakout with volume >= 1.3x 20D avg.
   - Faster Supertrend(7, 2) shift (<= 3 days) and exit on ST(7, 2) bearish flip or 20 EMA close.

2. Mean-Reversion Pullback (RSI(2) < 10 in Primary Uptrend):
   - Primary uptrend: Close > EMA150 and EMA50 > EMA150.
   - Oversold pullback: RSI(2) < 10 (entered at day t close).
   - Exit on bounce: Close > 5-day high (or Close > EMA10), hard SL at 2 * ATR(14) (max 6%), 10-day time stop.

Both use identical portfolio rules (Rs 10L capital, 1% risk/trade, max 5 positions,
0.25% round-trip NSE delivery costs) and Nifty 50 regime filter.
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
OUT_TRADES = ROOT / "data" / "nse_swing_best_trades.csv"

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


def compute_supertrend(df: pd.DataFrame, period: int = 7, mult: float = 2.0) -> pd.DataFrame:
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
    supertrend[0] = final_lower[0]

    for i in range(1, n):
        final_upper[i] = (
            basic_upper[i]
            if (basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower[i]
            if (basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )
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


def compute_rsi(series: pd.Series, period: int = 2) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(100.0)


def prepare_stock_data(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    prepared: Dict[str, pd.DataFrame] = {}
    for sym, grp in df.groupby("symbol"):
        g = grp.sort_values("date").copy()
        g["date"] = pd.to_datetime(g["date"]).dt.tz_localize(None)
        g = g.drop_duplicates(subset=["date"]).set_index("date")
        if len(g) < 160:
            continue

        g["ema10"] = g["close"].ewm(span=10, adjust=False).mean()
        g["ema20"] = g["close"].ewm(span=20, adjust=False).mean()
        g["ema50"] = g["close"].ewm(span=50, adjust=False).mean()
        g["ema150"] = g["close"].ewm(span=150, adjust=False).mean()

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

        g = compute_supertrend(g, period=7, mult=2.0)
        g["st_flip_bull"] = (g["st_dir"] == 1) & (g["st_dir"].shift(1) == -1)
        g["st_recent_bull_3d"] = (g["st_dir"] == 1) & (
            g["st_flip_bull"]
            | g["st_flip_bull"].shift(1, fill_value=False)
            | g["st_flip_bull"].shift(2, fill_value=False)
        )

        g["consol_high_15"] = g["high"].rolling(15).max()
        g["consol_low_15"] = g["low"].rolling(15).min()
        g["consol_range_pct"] = (g["consol_high_15"] - g["consol_low_15"]) / g["consol_low_15"]
        g["high_5"] = g["high"].rolling(5).max()
        g["rsi2"] = compute_rsi(g["close"], period=2)

        g["avg_vol20"] = g["volume"].rolling(20).mean()
        g["turnover20"] = (g["close"] * g["volume"]).rolling(20).mean()
        g["liquid"] = (g["close"] > 100.0) & (g["turnover20"] >= 25_00_00_000.0)
        g["uptrend"] = (g["close"] > g["ema50"]) & (g["ema50"] > g["ema150"])

        prepared[sym] = g
    return prepared


def prepare_nifty_regime(path: Path) -> pd.Series:
    ndf = pd.read_parquet(path).sort_index()
    ndf.index = pd.to_datetime(ndf.index).tz_localize(None)
    ema50 = ndf["Close"].ewm(span=50, adjust=False).mean()
    return (ndf["Close"] > ema50) & (ema50 > ema50.shift(5))


def run_upgraded_supertrend(
    stock_data: Dict[str, pd.DataFrame],
    nifty_regime: pd.Series,
    exit_mode: str = "ST_OR_EMA20",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rec 1: 15D Consolidation + ST(7,2) shift + Primary Uptrend."""
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
            elif int(row["st_dir"]) == -1:
                exit_price = float(row["close"])
                exit_reason = "ST_7_2_BEAR"
            elif exit_mode == "ST_OR_EMA20" and row["close"] < row["ema20"]:
                exit_price = float(row["close"])
                exit_reason = "EMA20_EXIT"

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
                if not (bool(y_row["liquid"]) and bool(y_row["uptrend"])):
                    continue
                consol_high = float(y_row["consol_high_15"])
                consol_low = float(y_row["consol_low_15"])
                consol_rng = float(y_row["consol_range_pct"])
                avg_vol = float(y_row["avg_vol20"])
                if pd.isna(consol_high) or pd.isna(consol_rng) or pd.isna(avg_vol) or avg_vol <= 0:
                    continue
                if consol_rng > 0.15 or not bool(t_row["st_recent_bull_3d"]):
                    continue
                if t_row["close"] > consol_high and t_row["close"] > t_row["open"] and t_row["volume"] >= 1.3 * avg_vol:
                    candidates.append((float(t_row["volume"]) / avg_vol, sym, t_row, consol_low))

            candidates.sort(key=lambda x: x[0], reverse=True)
            for _, sym, t_row, consol_low in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                entry_px = float(t_row["close"])
                sl_px = max(consol_low, float(t_row["st_val"]), entry_px * 0.94)
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


def run_rsi2_mean_reversion(
    stock_data: Dict[str, pd.DataFrame],
    nifty_regime: pd.Series,
    use_regime_filter: bool = True,
    rsi_thresh: float = 10.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rec 2: RSI(2) < 10 pullback inside primary uptrend, exit on close > 5D high or EMA10."""
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
            if today not in g.index or yesterday not in g.index:
                continue
            row = g.loc[today]
            y_row = g.loc[yesterday]
            pos.days_held += 1
            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            if row["low"] <= pos.sl_price:
                exit_price = min(float(row["open"]), pos.sl_price)
                exit_reason = "STOP_LOSS"
            elif row["close"] > y_row["high_5"]:
                exit_price = float(row["close"])
                exit_reason = "HIGH_5D_BOUNCE"
            elif row["close"] > row["ema10"] and row["close"] > pos.entry_price:
                exit_price = float(row["close"])
                exit_reason = "EMA10_BOUNCE"
            elif pos.days_held >= 10:
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

        regime_ok = bool(nifty_regime.get(yesterday, True)) if use_regime_filter else True
        if regime_ok and len(positions) < MAX_POSITIONS:
            candidates = []
            for sym, g in stock_data.items():
                if sym in positions or today not in g.index:
                    continue
                t_row = g.loc[today]
                if not (bool(t_row["liquid"]) and bool(t_row["uptrend"])):
                    continue
                if float(t_row["rsi2"]) < rsi_thresh:
                    # Prefer deepest oversold pullback among uptrending stocks
                    candidates.append((float(t_row["rsi2"]), sym, t_row))

            candidates.sort(key=lambda x: x[0])
            for _, sym, t_row in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                entry_px = float(t_row["close"])
                atr = float(t_row["atr14"])
                if pd.isna(atr) or atr <= 0:
                    continue
                sl_px = max(entry_px - 2.0 * atr, entry_px * 0.94)
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
    return total_ret


def main() -> None:
    raw_stocks = pd.read_parquet(STOCKS_PATH)
    stock_data = prepare_stock_data(raw_stocks)
    nifty_regime = prepare_nifty_regime(NIFTY_PATH)

    t_st_ema, eq_st_ema = run_upgraded_supertrend(stock_data, nifty_regime, exit_mode="ST_OR_EMA20")
    t_st_only, eq_st_only = run_upgraded_supertrend(stock_data, nifty_regime, exit_mode="ST_ONLY")
    t_rsi_reg, eq_rsi_reg = run_rsi2_mean_reversion(stock_data, nifty_regime, use_regime_filter=True)
    t_rsi_all, eq_rsi_all = run_rsi2_mean_reversion(stock_data, nifty_regime, use_regime_filter=False)

    results = [
        ("Rec 1A: 15D + ST(7,2) + Uptrend + ST/EMA20 Exit", t_st_ema, eq_st_ema),
        ("Rec 1B: 15D + ST(7,2) + Uptrend + ST(7,2) Exit", t_st_only, eq_st_only),
        ("Rec 2A: RSI(2)<10 Mean-Rev + Uptrend + Regime ON", t_rsi_reg, eq_rsi_reg),
        ("Rec 2B: RSI(2)<10 Mean-Rev + Uptrend + Regime OFF", t_rsi_all, eq_rsi_all),
    ]

    best_ret = -999.0
    best_trades = t_rsi_reg
    for label, tdf, edf in results:
        ret = summarize(label, tdf, edf)
        if ret > best_ret:
            best_ret = ret
            best_trades = tdf

    best_trades.to_csv(OUT_TRADES, index=False)
    print(f"\nSaved best strategy trades to {OUT_TRADES}")


if __name__ == "__main__":
    main()
