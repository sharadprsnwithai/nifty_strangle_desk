"""Intraday Technical Signal Engine for MCX Silver Options Desk.

Implements:
- 70-period Williams %R calculation with boundary-safe flat-line handling.
- Triple Exponential Moving Average (5, 15, 50 EMA) trend filtering.
- Intraday breakout/breakdown momentum signal evaluation.
- Active short option position trend reversal detection (5 EMA / 50 EMA crossover with candle confirmation).
"""

from datetime import time
from typing import Optional

import numpy as np
import pandas as pd

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI using standard Wilder's exponential moving average.

    Handles edge cases:
    - If avg_loss == 0 and avg_gain > 0: RSI = 100.0
    - If avg_loss == 0 and avg_gain == 0: RSI = 50.0
    - If avg_gain == 0 and avg_loss > 0: RSI = 0.0
    """
    if series.empty:
        return pd.Series(dtype=float)

    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).copy()
    loss = (-delta.where(delta < 0, 0.0)).copy()

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    standard_rsi = 100.0 - (100.0 / (1.0 + rs))

    conditions = [
        (avg_loss == 0) & (avg_gain > 0),
        (avg_loss == 0) & (avg_gain == 0),
    ]
    choices = [100.0, 50.0]
    rsi = pd.Series(np.select(conditions, choices, default=standard_rsi), index=series.index)
    return rsi.fillna(50.0)


def compute_williams_r(df: pd.DataFrame, period: int = 70) -> pd.Series:
    """Calculate Williams %R over the specified lookback period.

    Formula:
        ((highest_high - close) / (highest_high - lowest_low)) * -100.0

    Values range from 0.0 (maximum overbought) to -100.0 (maximum oversold).
    Handles flat lines (highest_high == lowest_low) or NaN by falling back to -50.0.
    """
    if df.empty or not {"high", "low", "close"}.issubset(df.columns):
        return pd.Series(dtype=float)

    highest_high = df["high"].rolling(window=period, min_periods=1).max()
    lowest_low = df["low"].rolling(window=period, min_periods=1).min()
    denom = highest_high - lowest_low

    wr = ((highest_high - df["close"]) / denom.replace(0, np.nan)) * -100.0
    return wr.fillna(-50.0)


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    """Compute standard exponential moving average (EMA)."""
    if series.empty:
        return pd.Series(dtype=float)
    return series.ewm(span=span, adjust=False).mean()


def evaluate_technical_signal(
    df_5m: pd.DataFrame,
    config: Optional[SilverConfig] = None,
    current_time: Optional[time] = None,
) -> SignalType:
    """Evaluate 5-minute technical momentum signal.

    Conditions:
    - Minimum bars: len(df_5m) >= config.williams_r_period (default 70).
    - Session window: config.session_start <= current_time <= config.session_end (if provided).
    - Bullish Momentum:
        Williams %R(70) > -20.0 AND 5 EMA > 15 EMA > 50 EMA AND current High > previous High.
    - Bearish Momentum:
        Williams %R(70) < -80.0 AND 5 EMA < 15 EMA < 50 EMA AND current Low < previous Low.
    - Otherwise: NO_SIGNAL.
    """
    if config is None:
        config = SilverConfig()

    if df_5m is None or len(df_5m) < config.williams_r_period:
        return SignalType.NO_SIGNAL

    if current_time is not None:
        if not (config.session_start <= current_time <= config.session_end):
            return SignalType.NO_SIGNAL

    if not {"high", "low", "close"}.issubset(df_5m.columns):
        return SignalType.NO_SIGNAL

    wr_series = compute_williams_r(df_5m, period=config.williams_r_period)
    ema_fast = compute_ema(df_5m["close"], span=config.ema_fast)
    ema_mid = compute_ema(df_5m["close"], span=config.ema_mid)
    ema_slow = compute_ema(df_5m["close"], span=config.ema_slow)

    curr_wr = float(wr_series.iloc[-1])
    curr_ema5 = float(ema_fast.iloc[-1])
    curr_ema15 = float(ema_mid.iloc[-1])
    curr_ema50 = float(ema_slow.iloc[-1])

    curr_high = float(df_5m["high"].iloc[-1])
    prev_high = float(df_5m["high"].iloc[-2])
    curr_low = float(df_5m["low"].iloc[-1])
    prev_low = float(df_5m["low"].iloc[-2])

    # Bullish Momentum Check
    if curr_wr > -20.0 and (curr_ema5 > curr_ema15 > curr_ema50) and (curr_high > prev_high):
        return SignalType.BULLISH_MOMENTUM

    # Bearish Momentum Check
    if curr_wr < -80.0 and (curr_ema5 < curr_ema15 < curr_ema50) and (curr_low < prev_low):
        return SignalType.BEARISH_MOMENTUM

    return SignalType.NO_SIGNAL


# Alias for backward/naming compatibility
evaluate_intraday_signals = evaluate_technical_signal


def check_trend_reversal(
    df_5m: pd.DataFrame,
    opt_type: str,
    config: Optional[SilverConfig] = None,
) -> bool:
    """Check whether a short option position has experienced an adverse trend reversal.

    Reversal Conditions:
    - For Short Put (PE): 5 EMA crosses below 50 EMA AND current Low < previous Low.
    - For Short Call (CE): 5 EMA crosses above 50 EMA AND current High > previous High.

    Requires at least 2 bars.
    Returns True if trend reversal occurred, False otherwise.
    """
    if df_5m is None or len(df_5m) < 2:
        return False

    if not {"high", "low", "close"}.issubset(df_5m.columns):
        return False

    if config is None:
        config = SilverConfig()

    ema5 = compute_ema(df_5m["close"], span=config.ema_fast)
    ema50 = compute_ema(df_5m["close"], span=config.ema_slow)

    curr_ema5 = float(ema5.iloc[-1])
    prev_ema5 = float(ema5.iloc[-2])
    curr_ema50 = float(ema50.iloc[-1])
    prev_ema50 = float(ema50.iloc[-2])

    curr_low = float(df_5m["low"].iloc[-1])
    prev_low = float(df_5m["low"].iloc[-2])
    curr_high = float(df_5m["high"].iloc[-1])
    prev_high = float(df_5m["high"].iloc[-2])

    opt_str = opt_type.upper().strip()

    # Short Put is threatened when market reverses bearishly (5 EMA crosses below 50 EMA + breakdowns)
    if opt_str in ("PE", "PUT", "SHORT_PUT"):
        bearish_cross = (curr_ema5 < curr_ema50) and (prev_ema5 >= prev_ema50)
        breakdown = curr_low < prev_low
        return bool(bearish_cross and breakdown)

    # Short Call is threatened when market reverses bullishly (5 EMA crosses above 50 EMA + breakouts)
    if opt_str in ("CE", "CALL", "SHORT_CALL"):
        bullish_cross = (curr_ema5 > curr_ema50) and (prev_ema5 <= prev_ema50)
        breakout = curr_high > prev_high
        return bool(bullish_cross and breakout)

    return False
