"""Scanner for FP-ALL NSE Stock Swing Strategy (15D Base + Institutional Footprints)."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

from strategy.stock_swing.config import StockSwingConfig

logger = logging.getLogger("stock_swing.scanner")


def compute_indicators(g: pd.DataFrame, nifty_ret60: pd.Series) -> pd.DataFrame:
    """Compute technical + institutional footprint indicators for one stock."""
    g = g.sort_index().copy()
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

    # Footprint 1: Pocket Pivot inside 15D base
    is_up = g["close"] > prev_close
    is_down = g["close"] < prev_close
    down_vol = g["volume"].where(is_down, 0.0)
    max_down_vol_10 = down_vol.rolling(10).max().shift(1)
    pocket_pivot_day = is_up & (g["volume"] > max_down_vol_10)
    g["fp_pocket_pivot_15d"] = pocket_pivot_day.rolling(15).max().fillna(0).astype(bool)

    # Footprint 2: 20D Up-Volume / Down-Volume >= 1.25
    up_vol_20 = g["volume"].where(is_up, 0.0).rolling(20).sum()
    down_vol_20 = g["volume"].where(is_down, 0.0).rolling(20).sum()
    g["fp_accum_ratio"] = (up_vol_20 / down_vol_20.replace(0.0, np.nan)).fillna(0.0) >= 1.25

    # Footprint 3: 60D Relative Strength > Nifty 50
    stock_ret60 = g["close"].pct_change(60)
    aligned_nifty_ret60 = nifty_ret60.reindex(g.index).ffill()
    g["fp_rs_leader"] = stock_ret60 > aligned_nifty_ret60

    uptrend = (g["close"] > g["ema50"]) & (g["ema50"] > g["ema150"])
    near_high = g["close"] >= 0.80 * g["high_200"]
    vol_dry = g["volume"].rolling(3).mean() < g["avg_vol20"]
    liquid = (g["close"] > 100.0) & (g["turnover20"] >= 25_00_00_000.0)

    g["setup_fp_all"] = (
        uptrend
        & near_high
        & vol_dry
        & liquid
        & (g["consol_range_15"] <= 0.15)
        & g["fp_pocket_pivot_15d"]
        & g["fp_accum_ratio"]
        & g["fp_rs_leader"]
    )
    return g


class StockSwingScanner:
    """Scans the NSE universe for FP-ALL entries and exit signals on open holdings."""

    def __init__(self, config: StockSwingConfig, symbols: Optional[List[str]] = None):
        self.config = config
        self.symbols = symbols if symbols is not None else config.default_universe_symbols(config.universe_file)

    @staticmethod
    def fetch_nifty_data(period: str = "1y") -> pd.DataFrame:
        df = yf.download("^NSEI", period=period, auto_adjust=True, progress=False)
        if df is None or df.empty:
            raise RuntimeError("Failed to fetch Nifty 50 (^NSEI) data from Yahoo Finance.")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    def fetch_universe_data(self, symbols: List[str], period: str = "1y") -> Dict[str, pd.DataFrame]:
        """Batch-download daily OHLCV data for NSE symbols (.NS)."""
        if not symbols:
            return {}
        yf_tickers = [f"{s}.NS" for s in symbols]
        raw = yf.download(
            yf_tickers,
            period=period,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
        out: Dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return out

        for sym in symbols:
            tk = f"{sym}.NS"
            try:
                if len(symbols) == 1 and not isinstance(raw.columns, pd.MultiIndex):
                    sub = raw.copy()
                else:
                    if tk not in raw.columns.get_level_values(0):
                        continue
                    sub = raw[tk].copy()
                sub = sub.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
                sub.index = pd.to_datetime(sub.index).tz_localize(None)
                if len(sub) >= 160:
                    out[sym] = sub
            except Exception as e:
                logger.debug("Skipping %s during batch parse: %s", sym, e)
        return out

    def scan(
        self,
        open_positions: Dict[str, dict],
        nifty_df: Optional[pd.DataFrame] = None,
        stock_data: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Dict:
        """Run FP-ALL exit check on open positions and entry scan on universe."""
        ndf = nifty_df if nifty_df is not None else self.fetch_nifty_data()
        ndf = ndf.sort_index()
        nifty_ema50 = ndf["Close"].ewm(span=50, adjust=False).mean()
        nifty_regime_series = (ndf["Close"] > nifty_ema50) & (nifty_ema50 > nifty_ema50.shift(5))
        nifty_ret60 = ndf["Close"].pct_change(60)
        regime_ok = bool(nifty_regime_series.iloc[-2]) if len(nifty_regime_series) >= 2 else False

        target_syms = sorted(set(self.symbols) | set(open_positions.keys()))
        raw_map = stock_data if stock_data is not None else self.fetch_universe_data(target_syms)

        exits: List[dict] = []
        holds: List[dict] = []
        candidates: List[dict] = []

        for sym, raw_g in raw_map.items():
            if len(raw_g) < 160:
                continue
            g = compute_indicators(raw_g, nifty_ret60)
            if len(g) < 2:
                continue
            t_row = g.iloc[-1]
            y_row = g.iloc[-2]
            ltp = float(t_row["close"])

            if sym in open_positions:
                pos = open_positions[sym]
                sl_px = float(pos["sl_price"])
                entry_px = float(pos["entry_price"])
                risk_ps = float(pos["risk_per_share"])
                days_held = int(pos.get("days_held", 0)) + 1

                reason: Optional[str] = None
                if float(t_row["low"]) <= sl_px or ltp <= sl_px:
                    reason = "STOP_LOSS"
                elif ltp < float(t_row["ema20"]):
                    reason = "EMA20_TRAIL"
                elif days_held >= 10 and risk_ps > 0 and ((ltp - entry_px) / risk_ps) < 0.5:
                    reason = "TIME_STOP"

                if reason:
                    exits.append(
                        {
                            "symbol": sym,
                            "ltp": ltp,
                            "exit_reason": reason,
                            "days_held": days_held,
                            "ema20": round(float(t_row["ema20"]), 2),
                        }
                    )
                else:
                    holds.append(
                        {
                            "symbol": sym,
                            "ltp": ltp,
                            "sl_price": sl_px,
                            "ema20": round(float(t_row["ema20"]), 2),
                            "days_held": days_held,
                        }
                    )
                continue

            if not regime_ok:
                continue
            if not bool(y_row["setup_fp_all"]):
                continue

            trigger_high = float(y_row["consol_high_15"])
            avg_vol = float(y_row["avg_vol20"])
            if pd.isna(trigger_high) or pd.isna(avg_vol) or avg_vol <= 0:
                continue

            if ltp > trigger_high and ltp > float(t_row["open"]) and float(t_row["volume"]) >= 1.5 * avg_vol:
                atr = float(y_row["atr14"])
                swing_low = float(y_row["swing_low_5"])
                sl_px = round(max(swing_low, ltp - 1.5 * atr, ltp * 0.94), 2)
                risk_ps = ltp - sl_px
                if risk_ps <= 0.01 * ltp:
                    continue
                candidates.append(
                    {
                        "symbol": sym,
                        "ltp": round(ltp, 2),
                        "sl_price": sl_px,
                        "risk_per_share": round(risk_ps, 2),
                        "vol_ratio": round(float(t_row["volume"]) / avg_vol, 2),
                        "trigger_high": round(trigger_high, 2),
                    }
                )

        candidates.sort(key=lambda x: x["vol_ratio"], reverse=True)
        return {
            "regime_ok": regime_ok,
            "nifty_close": round(float(ndf["Close"].iloc[-1]), 2),
            "nifty_ema50": round(float(nifty_ema50.iloc[-1]), 2),
            "exits": exits,
            "holds": holds,
            "candidates": candidates,
        }
