"""Backtest Trend-Pullback + Breakout Swing Strategy on NSE Stocks.

Uses cached daily OHLCV data in data/swing_daily_2y.parquet (248 NSE stocks)
and Nifty 50 daily data in data/nifty_daily_10y.parquet for the regime filter.

Zero look-ahead bias:
- Setup filters are evaluated on day t-1 close.
- Breakout confirmation (Close > trigger_high, Close > Open, Volume >= 1.5x 20D avg)
  is entered at day t close (simulating 15:15-15:25 IST execution).
- Exits (gap-aware SL, 2R partial, 20 EMA close trail, time stop) start on day t+1.
- Includes 0.25% round-trip NSE delivery transaction costs (STT + charges + slippage).
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
OUT_TRADES = ROOT / "data" / "nse_swing_vcp_trades.csv"

INITIAL_CAPITAL = 1_000_000.0
RISK_PCT = 0.01
MAX_POSITIONS = 5
MAX_ALLOC_PCT = 0.25
ROUND_TRIP_COST_PCT = 0.0025  # 0.25% (0.20% STT + ~0.05% exchange/stamp/slippage)


@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    initial_qty: int
    sl_price: float
    initial_risk_per_share: float
    partial_taken: bool = False
    days_held: int = 0
    realized_partial_pnl: float = 0.0


def prepare_stock_indicators(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Compute technical indicators per symbol."""
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
        g["consol_high_3"] = g["high"].rolling(3).max()
        g["consol_high_15"] = g["high"].rolling(15).max()
        consol_low_15 = g["low"].rolling(15).min()
        g["consol_range_15"] = (g["consol_high_15"] - consol_low_15) / consol_low_15

        # Setup on t-1: uptrend + near highs + pullback/tightness near 20 EMA + quiet volume
        uptrend = (g["close"] > g["ema50"]) & (g["ema50"] > g["ema150"])
        near_high = g["close"] >= 0.80 * g["high_200"]
        near_ema20 = (g["low"] <= g["ema20"] * 1.03) & (g["close"] >= g["ema20"] * 0.96)
        vol_dry = g["volume"].rolling(3).mean() < g["avg_vol20"]
        liquid = (g["close"] > 100.0) & (g["turnover20"] >= 25_00_00_000.0)  # >= 25 Cr

        g["setup_ok"] = uptrend & near_high & near_ema20 & vol_dry & liquid
        g["setup_base_15d"] = uptrend & near_high & vol_dry & liquid & (g["consol_range_15"] <= 0.15)
        prepared[sym] = g
    return prepared


def prepare_nifty_regime(path: Path) -> pd.Series:
    """Return boolean Series indexed by date: True when Nifty 50 > 50 EMA and 50 EMA rising."""
    ndf = pd.read_parquet(path).sort_index()
    ndf.index = pd.to_datetime(ndf.index).tz_localize(None)
    ema50 = ndf["Close"].ewm(span=50, adjust=False).mean()
    regime = (ndf["Close"] > ema50) & (ema50 > ema50.shift(5))
    return regime


def run_simulation(
    stock_data: Dict[str, pd.DataFrame],
    nifty_regime: pd.Series,
    use_regime_filter: bool = True,
    partial_at_2r: bool = True,
    consol_mode: str = "3D",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run daily portfolio backtest."""
    all_dates = sorted({d for g in stock_data.values() for d in g.index})
    # Start after 150-bar warm-up so EMA150 & 200D high are well-formed
    start_date = all_dates[150]
    sim_dates = [d for d in all_dates if d >= start_date]

    cash = INITIAL_CAPITAL
    positions: Dict[str, Position] = {}
    trades: List[dict] = []
    equity_curve: List[dict] = []

    for i in range(1, len(sim_dates)):
        today = sim_dates[i]
        yesterday = sim_dates[i - 1]

        # 1. Manage existing positions
        to_close: List[str] = []
        for sym, pos in list(positions.items()):
            g = stock_data[sym]
            if today not in g.index:
                continue
            row = g.loc[today]
            pos.days_held += 1

            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            # Gap-aware stop loss check
            if row["low"] <= pos.sl_price:
                exit_price = min(float(row["open"]), pos.sl_price)
                exit_reason = "STOP_LOSS" if not pos.partial_taken else "TRAIL_BE_SL"

            # Optional 2R partial profit booking (intraday hit)
            elif partial_at_2r and not pos.partial_taken:
                target_2r = pos.entry_price + 2.0 * pos.initial_risk_per_share
                if row["high"] >= target_2r and pos.qty >= 2:
                    sell_qty = pos.qty // 2
                    fill_px = max(float(row["open"]), target_2r)
                    gross_pnl = (fill_px - pos.entry_price) * sell_qty
                    cost = (pos.entry_price + fill_px) * sell_qty * (ROUND_TRIP_COST_PCT / 2.0)
                    net_partial = gross_pnl - cost
                    cash += fill_px * sell_qty - (fill_px * sell_qty * ROUND_TRIP_COST_PCT / 2.0)
                    pos.realized_partial_pnl += net_partial
                    pos.qty -= sell_qty
                    pos.partial_taken = True
                    pos.sl_price = pos.entry_price  # Move SL to breakeven

            # End-of-day trailing exit: daily close below 20 EMA
            if exit_price is None and row["close"] < row["ema20"]:
                exit_price = float(row["close"])
                exit_reason = "EMA20_TRAIL"

            # Time stop: 10 days held and still < +0.5R
            if exit_price is None and pos.days_held >= 10:
                curr_r = (float(row["close"]) - pos.entry_price) / pos.initial_risk_per_share
                if curr_r < 0.5:
                    exit_price = float(row["close"])
                    exit_reason = "TIME_STOP"

            if exit_price is not None:
                gross_rem = (exit_price - pos.entry_price) * pos.qty
                exit_cost = exit_price * pos.qty * (ROUND_TRIP_COST_PCT / 2.0)
                entry_cost_rem = pos.entry_price * pos.qty * (ROUND_TRIP_COST_PCT / 2.0)
                total_net_pnl = pos.realized_partial_pnl + gross_rem - exit_cost - entry_cost_rem
                cash += exit_price * pos.qty - exit_cost

                total_risk = pos.initial_risk_per_share * pos.initial_qty
                r_mult = total_net_pnl / total_risk if total_risk > 0 else 0.0

                trades.append(
                    {
                        "symbol": sym,
                        "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
                        "exit_date": today.strftime("%Y-%m-%d"),
                        "days_held": pos.days_held,
                        "qty": pos.initial_qty,
                        "entry_price": round(pos.entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "exit_reason": exit_reason,
                        "net_pnl": round(total_net_pnl, 2),
                        "r_multiple": round(r_mult, 2),
                    }
                )
                to_close.append(sym)

        for sym in to_close:
            del positions[sym]

        # Compute mark-to-market equity
        mtm_value = 0.0
        for sym, pos in positions.items():
            g = stock_data[sym]
            px = float(g.loc[today, "close"]) if today in g.index else pos.entry_price
            mtm_value += px * pos.qty
        total_equity = cash + mtm_value

        # 2. Scan for new entries (only if slots available & regime allows)
        regime_ok = True
        if use_regime_filter:
            regime_ok = bool(nifty_regime.get(yesterday, True))

        if regime_ok and len(positions) < MAX_POSITIONS:
            candidates = []
            for sym, g in stock_data.items():
                if sym in positions or today not in g.index or yesterday not in g.index:
                    continue
                y_row = g.loc[yesterday]
                t_row = g.loc[today]
                if consol_mode == "15D_STRICT":
                    if not (bool(y_row["setup_ok"]) and float(y_row["consol_range_15"]) <= 0.15):
                        continue
                    trigger_high = float(y_row["consol_high_15"])
                elif consol_mode == "15D_BASE":
                    if not bool(y_row["setup_base_15d"]):
                        continue
                    trigger_high = float(y_row["consol_high_15"])
                else:
                    if not bool(y_row["setup_ok"]):
                        continue
                    trigger_high = float(y_row["consol_high_3"])

                avg_vol = float(y_row["avg_vol20"])
                if pd.isna(trigger_high) or pd.isna(avg_vol) or avg_vol <= 0:
                    continue

                # Breakout confirmation at today's close
                if (
                    t_row["close"] > trigger_high
                    and t_row["close"] > t_row["open"]
                    and t_row["volume"] >= 1.5 * avg_vol
                ):
                    vol_ratio = float(t_row["volume"]) / avg_vol
                    candidates.append((vol_ratio, sym, t_row, y_row))

            # Rank by strongest relative volume surge
            candidates.sort(key=lambda x: x[0], reverse=True)

            for _, sym, t_row, y_row in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                entry_px = float(t_row["close"])
                atr = float(y_row["atr14"])
                swing_low = float(y_row["swing_low_5"])
                raw_sl = max(swing_low, entry_px - 1.5 * atr)
                sl_px = max(raw_sl, entry_px * 0.94)  # Cap max SL at 6%
                risk_per_share = entry_px - sl_px
                if risk_per_share <= 0.01 * entry_px:
                    continue

                risk_budget = total_equity * RISK_PCT
                qty_by_risk = int(risk_budget / risk_per_share)
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
                    initial_qty=qty,
                    sl_price=sl_px,
                    initial_risk_per_share=risk_per_share,
                )

        # Record end-of-day equity
        mtm_eod = sum(
            (float(stock_data[s].loc[today, "close"]) if today in stock_data[s].index else p.entry_price)
            * p.qty
            for s, p in positions.items()
        )
        equity_curve.append({"date": today, "equity": cash + mtm_eod})

    trades_df = pd.DataFrame(trades)
    eq_df = pd.DataFrame(equity_curve).set_index("date")
    return trades_df, eq_df


def summarize(label: str, trades_df: pd.DataFrame, eq_df: pd.DataFrame) -> None:
    """Print clean backtest metrics."""
    if trades_df.empty:
        print(f"\n=== {label}: 0 trades ===")
        return
    total_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100.0
    gross_win = wins["net_pnl"].sum()
    gross_loss = abs(losses["net_pnl"].sum())
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
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
    print(f"Profit Factor   : {profit_factor:.2f} | Avg R-Multiple: {avg_r:+.2f}R")
    print(f"Final Equity    : Rs {final_eq:,.0f} (Return: {total_ret:+.2f}%, CAGR: {cagr:+.2f}%)")
    print(f"Max Drawdown    : {max_dd:.2f}%")
    print("Exit Breakdown  :", trades_df["exit_reason"].value_counts().to_dict())


def main() -> None:
    print("Loading NSE stock & Nifty 50 data...")
    raw_stocks = pd.read_parquet(STOCKS_PATH)
    stock_data = prepare_stock_indicators(raw_stocks)
    nifty_regime = prepare_nifty_regime(NIFTY_PATH)
    print(f"Prepared {len(stock_data)} liquid NSE symbols.")

    t_reg_partial, eq_reg_partial = run_simulation(
        stock_data, nifty_regime, use_regime_filter=True, partial_at_2r=True
    )
    t_reg_trail, eq_reg_trail = run_simulation(
        stock_data, nifty_regime, use_regime_filter=True, partial_at_2r=False, consol_mode="3D"
    )
    t_b15_strict, eq_b15_strict = run_simulation(
        stock_data, nifty_regime, use_regime_filter=True, partial_at_2r=False, consol_mode="15D_STRICT"
    )
    t_b15_base, eq_b15_base = run_simulation(
        stock_data, nifty_regime, use_regime_filter=True, partial_at_2r=False, consol_mode="15D_BASE"
    )
    t_noreg_partial, eq_noreg_partial = run_simulation(
        stock_data, nifty_regime, use_regime_filter=False, partial_at_2r=True
    )

    summarize("Variant A: Nifty50 Regime ON + 2R Partial + 20EMA Trail", t_reg_partial, eq_reg_partial)
    summarize("Variant B: Nifty50 Regime ON + 3D Breakout + Full 20EMA Trail", t_reg_trail, eq_reg_trail)
    summarize("Variant B-15D (Strict): + 15D Consol (<=15%) + 20EMA Touch", t_b15_strict, eq_b15_strict)
    summarize("Variant B-15D (Base): + 15D Consol (<=15%, No 20EMA Touch)", t_b15_base, eq_b15_base)
    summarize("Variant C: No Regime Filter + 2R Partial + 20EMA Trail", t_noreg_partial, eq_noreg_partial)

    # Nifty 50 benchmark over same simulation window
    ndf = pd.read_parquet(NIFTY_PATH).sort_index()
    ndf.index = pd.to_datetime(ndf.index).tz_localize(None)
    sub_n = ndf.loc[eq_reg_partial.index[0] : eq_reg_partial.index[-1], "Close"]
    if len(sub_n) >= 2:
        n_ret = (sub_n.iloc[-1] / sub_n.iloc[0] - 1.0) * 100.0
        n_dd = ((sub_n - sub_n.cummax()) / sub_n.cummax() * 100.0).min()
        print(f"\n=== Benchmark: Nifty 50 Buy & Hold ===")
        print(f"Return: {n_ret:+.2f}% | Max Drawdown: {n_dd:.2f}%")

    t_reg_partial.to_csv(OUT_TRADES, index=False)
    print(f"\nSaved Variant A trades to {OUT_TRADES}")


if __name__ == "__main__":
    main()
