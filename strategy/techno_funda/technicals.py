import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, Tuple
from strategy.techno_funda.config import TechnoFundaConfig


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates Wilder's Relative Strength Index (RSI) with adaptive min periods."""
    if len(series) < 2:
        return pd.Series(index=series.index, data=50.0)

    p = min(period, max(3, len(series) - 1))
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's exponential smoothing
    avg_gain = gain.ewm(alpha=1.0 / p, adjust=False, min_periods=p).mean()
    avg_loss = loss.ewm(alpha=1.0 / p, adjust=False, min_periods=p).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(100.0 if (len(avg_loss) and avg_loss.iloc[-1] == 0) else 50.0)


def evaluate_mtf_rsi(
    daily_df: pd.DataFrame,
    config: Optional[TechnoFundaConfig] = None,
) -> Optional[Dict[str, Any]]:
    """
    Evaluates Multi-Timeframe RSI (Monthly > 60, Weekly > 60, Daily crosses 65)
    and base chart breakout patterns on daily OHLCV bars.
    """
    cfg = config or TechnoFundaConfig()
    if daily_df is None or len(daily_df) < 30:
        return None

    df = daily_df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        else:
            return None

    df = df.sort_index()

    # 1. Daily RSI
    df["rsi_daily"] = calculate_rsi(df["close"], 14)
    if len(df["rsi_daily"].dropna()) < 2:
        return None

    curr_daily_rsi = float(df["rsi_daily"].iloc[-1])
    prev_daily_rsi = float(df["rsi_daily"].iloc[-2])

    # 2. Resample to Weekly ('W-FRI') & Monthly ('ME')
    weekly_close = df["close"].resample("W-FRI").last().dropna()
    monthly_close = df["close"].resample("ME").last().dropna()

    weekly_rsi_series = calculate_rsi(weekly_close, 14)
    monthly_rsi_series = calculate_rsi(monthly_close, 14)

    curr_weekly_rsi = float(weekly_rsi_series.iloc[-1]) if len(weekly_rsi_series.dropna()) else 50.0
    curr_monthly_rsi = float(monthly_rsi_series.iloc[-1]) if len(monthly_rsi_series.dropna()) else 50.0

    # Fallback for shorter monthly samples: if monthly bars < 6, use longer weekly proxy
    if len(monthly_close) < 6 and len(weekly_close) >= 10:
        curr_monthly_rsi = curr_weekly_rsi

    # Check Primary (Monthly > 60) and Secondary (Weekly > 60) Trend Alignment
    if curr_monthly_rsi < cfg.monthly_rsi_threshold:
        return None
    if curr_weekly_rsi < cfg.weekly_rsi_threshold:
        return None

    # Check Daily Trigger: Crossing 65 or in trigger zone (65 - 78)
    crossed_65 = (curr_daily_rsi >= cfg.daily_rsi_trigger and prev_daily_rsi < cfg.daily_rsi_trigger)
    in_trigger_zone = (cfg.daily_rsi_trigger <= curr_daily_rsi <= cfg.daily_rsi_max)

    if not (crossed_65 or in_trigger_zone):
        return None

    # 3. Volume and Pattern Analysis
    close = float(df["close"].iloc[-1])
    vol = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0
    vol_ma20 = float(df["volume"].rolling(20).mean().iloc[-1]) if "volume" in df.columns and len(df) >= 20 else vol
    vol_ratio = round(vol / vol_ma20, 2) if vol_ma20 > 0 else 1.0

    high_20 = float(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else close
    low_15 = float(df["low"].iloc[-15:].min()) if len(df) >= 15 else close * 0.95

    # Determine Pattern
    pattern = "MTF_MOMENTUM_BREAKOUT"
    if close >= high_20 and vol_ratio >= cfg.volume_surge_ratio:
        pattern = "BOX_BREAKOUT_WITH_VOLUME"
    elif prev_daily_rsi >= 65.0 and 60.0 <= curr_daily_rsi <= 66.0:
        pattern = "BREAKOUT_RETEST"
    else:
        # Check Volatility Contraction Pattern (VCP)
        range_5d = df["high"].iloc[-5:].max() - df["low"].iloc[-5:].min()
        range_20d = df["high"].iloc[-20:].max() - df["low"].iloc[-20:].min() if len(df) >= 20 else range_5d
        if range_20d > 0 and (range_5d / range_20d) <= 0.60:
            pattern = "VCP_CONTRACTION_BREAKOUT"

    # Reference Risk Levels (Swing Stop & 1.5x / 2.5x Targets)
    swing_sl = round(low_15 * 0.995, 2)
    risk = max(close - swing_sl, close * 0.03)  # Minimum 3% risk distance
    target_1 = round(close + (1.5 * risk), 2)
    target_2 = round(close + (2.5 * risk), 2)

    return {
        "ltp": close,
        "daily_rsi": round(curr_daily_rsi, 2),
        "prev_daily_rsi": round(prev_daily_rsi, 2),
        "weekly_rsi": round(curr_weekly_rsi, 2),
        "monthly_rsi": round(curr_monthly_rsi, 2),
        "rsi_cross_confirmed": crossed_65,
        "volume_ratio": vol_ratio,
        "chart_pattern": pattern,
        "swing_sl": swing_sl,
        "target_1": target_1,
        "target_2": target_2,
    }
