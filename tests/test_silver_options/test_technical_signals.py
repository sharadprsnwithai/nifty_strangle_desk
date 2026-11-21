from datetime import time
import numpy as np
import pandas as pd
import pytest

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType
from strategy.silver_options.technical_signals import (
    check_trend_reversal,
    compute_ema,
    compute_rsi,
    compute_williams_r,
    evaluate_intraday_signals,
    evaluate_technical_signal,
)


def test_compute_williams_r_accuracy_and_boundaries():
    # 10 bars: high: 95..104, low: 85..94, close: 104
    # Highest high = 104, lowest low = 85, diff = 19
    highs = [95 + i for i in range(10)]
    lows = [85 + i for i in range(10)]
    closes = [90 + i for i in range(10)]
    closes[-1] = 104  # Close at the highest high

    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    wr = compute_williams_r(df, period=10)

    # When close == highest_high: (104 - 104) / 19 * -100 = 0.0
    assert wr.iloc[-1] == 0.0
    assert wr.iloc[-1] > -20.0  # Overbought territory

    # When close == lowest_low: (104 - 85) / 19 * -100 = -100.0
    df.loc[9, "close"] = 85
    wr_oversold = compute_williams_r(df, period=10)
    assert wr_oversold.iloc[-1] == -100.0
    assert wr_oversold.iloc[-1] < -80.0  # Oversold territory


def test_compute_williams_r_zero_division_and_edge_cases():
    # Flat line: high == low == close for all bars
    flat_df = pd.DataFrame({
        "high": [100.0] * 20,
        "low": [100.0] * 20,
        "close": [100.0] * 20,
    })
    wr = compute_williams_r(flat_df, period=10)
    # Should fallback cleanly to -50.0
    assert (wr == -50.0).all()

    # Empty DataFrame
    empty_df = pd.DataFrame(columns=["high", "low", "close"])
    assert compute_williams_r(empty_df).empty

    # Missing required columns
    bad_df = pd.DataFrame({"price": [100.0, 101.0]})
    assert compute_williams_r(bad_df).empty


def test_compute_ema():
    series = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    ema = compute_ema(series, span=3)
    assert len(ema) == 5
    assert ema.iloc[-1] > ema.iloc[0]

    empty_series = pd.Series(dtype=float)
    assert compute_ema(empty_series, span=5).empty


def _create_uptrend_df(n_bars: int = 80) -> pd.DataFrame:
    """Helper to generate a sustained uptrend with 5 EMA > 15 EMA > 50 EMA and wr > -20."""
    closes = [1000.0 + i * 15.0 for i in range(n_bars)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _create_downtrend_df(n_bars: int = 80) -> pd.DataFrame:
    """Helper to generate a sustained downtrend with 5 EMA < 15 EMA < 50 EMA and wr < -80."""
    closes = [2500.0 - i * 15.0 for i in range(n_bars)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def test_evaluate_technical_signal_bullish_momentum():
    cfg = SilverConfig(williams_r_period=70, ema_fast=5, ema_mid=15, ema_slow=50)
    df = _create_uptrend_df(80)

    # Within session time (e.g. 10:30)
    sig = evaluate_technical_signal(df, cfg, current_time=time(10, 30))
    assert sig == SignalType.BULLISH_MOMENTUM

    # Test alias
    sig_alias = evaluate_intraday_signals(df, cfg, current_time=time(10, 30))
    assert sig_alias == SignalType.BULLISH_MOMENTUM


def test_evaluate_technical_signal_bearish_momentum():
    cfg = SilverConfig(williams_r_period=70, ema_fast=5, ema_mid=15, ema_slow=50)
    df = _create_downtrend_df(80)

    # Within session time (e.g. 14:15)
    sig = evaluate_technical_signal(df, cfg, current_time=time(14, 15))
    assert sig == SignalType.BEARISH_MOMENTUM


def test_candle_breakout_and_breakdown_requirement():
    cfg = SilverConfig(williams_r_period=70)

    # Bullish setup but current high <= previous high (inside candle / pullback)
    df_bull = _create_uptrend_df(80)
    df_bull.loc[79, "high"] = df_bull.loc[78, "high"] - 1.0  # Fails curr_high > prev_high
    sig_bull = evaluate_technical_signal(df_bull, cfg, current_time=time(11, 0))
    assert sig_bull == SignalType.NO_SIGNAL

    # Bearish setup but current low >= previous low
    df_bear = _create_downtrend_df(80)
    df_bear.loc[79, "low"] = df_bear.loc[78, "low"] + 1.0  # Fails curr_low < prev_low
    sig_bear = evaluate_technical_signal(df_bear, cfg, current_time=time(11, 0))
    assert sig_bear == SignalType.NO_SIGNAL


def test_triple_ema_alignment_requirement():
    cfg = SilverConfig(williams_r_period=70)
    df = _create_uptrend_df(80)

    # Force 5 EMA to be below 15 EMA by dropping recent closes
    df.loc[70:79, "close"] = df.loc[60, "close"] - 100.0
    # Keep curr_high > prev_high to isolate the EMA condition
    df.loc[79, "high"] = df.loc[78, "high"] + 10.0

    sig = evaluate_technical_signal(df, cfg, current_time=time(11, 0))
    # EMAs are not aligned 5 > 15 > 50
    assert sig == SignalType.NO_SIGNAL

    # Also test bearish setup with misaligned EMAs (5 EMA > 15 EMA)
    df_bear = _create_downtrend_df(80)
    df_bear.loc[70:79, "close"] = df_bear.loc[60, "close"] + 100.0
    df_bear.loc[79, "low"] = df_bear.loc[78, "low"] - 10.0

    sig_bear = evaluate_technical_signal(df_bear, cfg, current_time=time(11, 0))
    assert sig_bear == SignalType.NO_SIGNAL


def test_session_time_filter():
    cfg = SilverConfig(
        williams_r_period=70,
        session_start=time(9, 15),
        session_end=time(18, 0),
    )
    df = _create_uptrend_df(80)

    # Before session start
    assert evaluate_technical_signal(df, cfg, current_time=time(9, 14)) == SignalType.NO_SIGNAL

    # At session start boundary
    assert evaluate_technical_signal(df, cfg, current_time=time(9, 15)) == SignalType.BULLISH_MOMENTUM

    # Mid session
    assert evaluate_technical_signal(df, cfg, current_time=time(12, 0)) == SignalType.BULLISH_MOMENTUM

    # At session end boundary
    assert evaluate_technical_signal(df, cfg, current_time=time(18, 0)) == SignalType.BULLISH_MOMENTUM

    # After session end (evening window)
    assert evaluate_technical_signal(df, cfg, current_time=time(18, 1)) == SignalType.NO_SIGNAL
    assert evaluate_technical_signal(df, cfg, current_time=time(21, 30)) == SignalType.NO_SIGNAL

    # If current_time is None, session filter is not enforced
    assert evaluate_technical_signal(df, cfg, current_time=None) == SignalType.BULLISH_MOMENTUM


def test_insufficient_data_handling():
    cfg = SilverConfig(williams_r_period=70)

    # Only 69 bars (< 70)
    df_short = _create_uptrend_df(69)
    assert evaluate_technical_signal(df_short, cfg, current_time=time(10, 0)) == SignalType.NO_SIGNAL

    # Empty DataFrame
    assert evaluate_technical_signal(pd.DataFrame(), cfg) == SignalType.NO_SIGNAL

    # None DataFrame
    assert evaluate_technical_signal(None, cfg) == SignalType.NO_SIGNAL


def test_check_trend_reversal_short_put():
    cfg = SilverConfig(ema_fast=5, ema_slow=50)

    # To create a fresh bearish cross (5 EMA crossing below 50 EMA):
    # Bars 0..60 at 1000, bars 61..70 plunge sharply so 5 EMA crosses below 50 EMA at the last bar
    closes = [1000.0] * 60 + [1000.0 - i * 20.0 for i in range(1, 15)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    # Let's inspect where 5 EMA crosses 50 EMA
    ema5 = compute_ema(df["close"], span=5)
    ema50 = compute_ema(df["close"], span=50)

    # Find the exact cross index
    cross_idx = None
    for i in range(1, len(df)):
        if ema5.iloc[i] < ema50.iloc[i] and ema5.iloc[i - 1] >= ema50.iloc[i - 1]:
            cross_idx = i
            break

    assert cross_idx is not None, "A crossover should occur in this synthetic dataset"

    # Slice up to cross_idx + 1 so the last bar is the crossover bar
    df_cross = df.iloc[: cross_idx + 1].copy().reset_index(drop=True)
    # Ensure current low < previous low
    df_cross.loc[len(df_cross) - 1, "low"] = df_cross.loc[len(df_cross) - 2, "low"] - 5.0

    # Short Put should trigger trend reversal
    assert check_trend_reversal(df_cross, "PE", cfg) is True
    assert check_trend_reversal(df_cross, "SHORT_PUT", cfg) is True

    # If current low does not breakdown (curr_low >= prev_low), reversal should be False
    df_no_breakdown = df_cross.copy()
    df_no_breakdown.loc[len(df_no_breakdown) - 1, "low"] = df_no_breakdown.loc[len(df_no_breakdown) - 2, "low"] + 2.0
    assert check_trend_reversal(df_no_breakdown, "PE", cfg) is False


def test_check_trend_reversal_short_call():
    cfg = SilverConfig(ema_fast=5, ema_slow=50)

    # To create a fresh bullish cross (5 EMA crossing above 50 EMA):
    # Bars 0..60 at 1000, bars 61..75 rally sharply so 5 EMA crosses above 50 EMA
    closes = [1000.0] * 60 + [1000.0 + i * 20.0 for i in range(1, 16)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    ema5 = compute_ema(df["close"], span=5)
    ema50 = compute_ema(df["close"], span=50)

    cross_idx = None
    for i in range(1, len(df)):
        if ema5.iloc[i] > ema50.iloc[i] and ema5.iloc[i - 1] <= ema50.iloc[i - 1]:
            cross_idx = i
            break

    assert cross_idx is not None, "A crossover should occur in this synthetic dataset"

    df_cross = df.iloc[: cross_idx + 1].copy().reset_index(drop=True)
    # Ensure current high > previous high
    df_cross.loc[len(df_cross) - 1, "high"] = df_cross.loc[len(df_cross) - 2, "high"] + 5.0

    # Short Call should trigger trend reversal
    assert check_trend_reversal(df_cross, "CE", cfg) is True
    assert check_trend_reversal(df_cross, "SHORT_CALL", cfg) is True

    # If current high does not breakout (curr_high <= prev_high), reversal should be False
    df_no_breakout = df_cross.copy()
    df_no_breakout.loc[len(df_no_breakout) - 1, "high"] = df_no_breakout.loc[len(df_no_breakout) - 2, "high"] - 2.0
    assert check_trend_reversal(df_no_breakout, "CE", cfg) is False


def test_check_trend_reversal_edge_cases():
    cfg = SilverConfig()

    # Single candle (< 2 bars)
    df_single = pd.DataFrame({"high": [100.0], "low": [90.0], "close": [95.0]})
    assert check_trend_reversal(df_single, "PE", cfg) is False

    # Empty DataFrame
    assert check_trend_reversal(pd.DataFrame(), "PE", cfg) is False

    # Unknown option type
    df_two = pd.DataFrame({"high": [100.0, 101.0], "low": [90.0, 91.0], "close": [95.0, 96.0]})
    assert check_trend_reversal(df_two, "UNKNOWN", cfg) is False


def test_compute_rsi_utility():
    prices = pd.Series([100.0 + i * 2.0 for i in range(25)])
    rsi = compute_rsi(prices, period=14)
    assert rsi.iloc[-1] == 100.0

    flat_prices = pd.Series([100.0] * 20)
    flat_rsi = compute_rsi(flat_prices, period=14)
    assert flat_rsi.iloc[-1] == 50.0
