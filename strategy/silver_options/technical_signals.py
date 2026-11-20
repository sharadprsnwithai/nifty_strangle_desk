"""Multi-Timeframe Technical Signal Engine for MCX Silver Options Desk.

Implements:
- 14-period Wilder's / Exponential RSI calculation with boundary-safe zero-loss handling.
- Daily RSI Bullish/Bearish Divergence detection (e.g. daily RSI < 25 or > 70) with 5-20 bar lookback.
- Hourly 20 EMA confirmation for trend alignment / timing.
- End-to-end multi-timeframe signal evaluation.
"""

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


def detect_rsi_divergence(daily_df: pd.DataFrame, config: SilverConfig) -> SignalType:
    """Detect Bullish or Bearish RSI Divergence on Daily candles over trailing 5-20 bars.

    Bullish Reversal Divergence:
    - Prior lowest price in window had RSI <= daily_rsi_oversold.
    - Current price forms a lower low (close < prior min close).
    - Current RSI forms a higher low (RSI > prior min RSI).

    Bearish Breakdown Divergence:
    - Prior highest price in window had RSI >= daily_rsi_overbought.
    - Current price forms a higher high (close > prior max close).
    - Current RSI forms a lower high (RSI < prior max RSI).
    """
    if len(daily_df) < 5:
        return SignalType.NO_SIGNAL

    if "rsi" not in daily_df.columns:
        daily_df = daily_df.copy()
        daily_df["rsi"] = compute_rsi(daily_df["close"], period=config.daily_rsi_period)

    recent = daily_df.tail(20)
    current_close = float(recent["close"].iloc[-1])
    current_rsi = float(recent["rsi"].iloc[-1])

    prior_window = recent.iloc[:-1]

    # Check for Bullish Divergence in oversold territory
    prior_min_pos = int(np.argmin(prior_window["close"].values))
    prior_min_close = float(prior_window["close"].iloc[prior_min_pos])
    prior_min_rsi = float(prior_window["rsi"].iloc[prior_min_pos])

    if prior_min_rsi <= config.daily_rsi_oversold:
        if current_close < prior_min_close and current_rsi > prior_min_rsi:
            return SignalType.BULLISH_REVERSAL

    # Check for Bearish Divergence in overbought territory
    prior_max_pos = int(np.argmax(prior_window["close"].values))
    prior_max_close = float(prior_window["close"].iloc[prior_max_pos])
    prior_max_rsi = float(prior_window["rsi"].iloc[prior_max_pos])

    if prior_max_rsi >= config.daily_rsi_overbought:
        if current_close > prior_max_close and current_rsi < prior_max_rsi:
            return SignalType.BEARISH_BREAKDOWN

    return SignalType.NO_SIGNAL


def verify_hourly_confirmation(
    hourly_df: pd.DataFrame, daily_signal: SignalType, config: SilverConfig
) -> bool:
    """Verify whether hourly timeframe confirms the daily divergence signal.

    For BULLISH_REVERSAL:
    - Hourly close must be above 20 EMA, and either crossing above or expanding upward.

    For BEARISH_BREAKDOWN:
    - Hourly close must be below 20 EMA, and either crossing below or expanding downward.

    Returns False for NO_SIGNAL or invalid data.
    """
    if len(hourly_df) < 2 or daily_signal == SignalType.NO_SIGNAL:
        return False

    df = hourly_df.copy()
    if "ema_20" not in df.columns:
        df["ema_20"] = df["close"].ewm(span=config.hourly_ema_period, adjust=False).mean()

    curr_close = float(df["close"].iloc[-1])
    curr_ema = float(df["ema_20"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    prev_ema = float(df["ema_20"].iloc[-2])

    if daily_signal == SignalType.BULLISH_REVERSAL:
        return bool(curr_close > curr_ema and (prev_close <= prev_ema or curr_close > prev_close))

    if daily_signal == SignalType.BEARISH_BREAKDOWN:
        return bool(curr_close < curr_ema and (prev_close >= prev_ema or curr_close < prev_close))

    return False


def evaluate_technical_signal(
    daily_df: pd.DataFrame, hourly_df: pd.DataFrame, config: SilverConfig
) -> SignalType:
    """Evaluate multi-timeframe technical signals across daily and hourly data."""
    daily_signal = detect_rsi_divergence(daily_df, config)
    if daily_signal in (SignalType.BULLISH_REVERSAL, SignalType.BEARISH_BREAKDOWN):
        if verify_hourly_confirmation(hourly_df, daily_signal, config):
            return daily_signal
        return SignalType.NO_SIGNAL
    return SignalType.NO_SIGNAL
