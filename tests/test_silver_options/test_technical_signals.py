import pandas as pd
import numpy as np
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType
from strategy.silver_options.technical_signals import (
    compute_rsi,
    detect_rsi_divergence,
    verify_hourly_confirmation,
    evaluate_technical_signal,
)

def test_compute_rsi_accuracy():
    prices = pd.Series([100, 102, 104, 103, 105, 107, 106, 108, 110, 112, 111, 113, 115, 114, 116, 118])
    rsi = compute_rsi(prices, period=14)
    assert not np.isnan(rsi.iloc[-1])
    assert 0 <= rsi.iloc[-1] <= 100

def test_compute_rsi_zero_loss_boundary():
    # 25 consecutive increasing prices (zero losses) -> RSI must be 100.0
    prices = pd.Series([100.0 + i * 2.0 for i in range(25)])
    rsi = compute_rsi(prices, period=14)
    assert rsi.iloc[-1] == 100.0

    # Flat prices (zero gains and zero losses) -> RSI defaults to 50.0
    flat_prices = pd.Series([100.0] * 20)
    flat_rsi = compute_rsi(flat_prices, period=14)
    assert flat_rsi.iloc[-1] == 50.0

def test_detect_bullish_divergence():
    # Price makes lower low (150,000 -> 148,000) while RSI makes higher low (19 -> 25)
    cfg = SilverConfig(daily_rsi_oversold=25.0)
    data = {
        "close": [165000, 162000, 158000, 154000, 150000, 152000, 151000, 148000],
        "low": [164000, 161000, 157000, 153000, 149000, 151000, 150000, 147500],
    }
    df = pd.DataFrame(data)
    # Inject mocked RSI series directly
    df["rsi"] = [35.0, 30.0, 24.0, 21.0, 19.0, 23.0, 22.0, 25.0]

    signal = detect_rsi_divergence(df, cfg)
    assert signal == SignalType.BULLISH_REVERSAL

def test_detect_bullish_divergence_15_bars_lookback():
    # Divergence low occurred 15 bars prior to the current candle
    cfg = SilverConfig(daily_rsi_oversold=25.0)
    
    # 18 bars total. Bar index 2 is 15 bars before current bar (index 17).
    # Bar index 2 has low close of 150,000 and oversold RSI of 19.0.
    closes = [
        160000, 155000, 150000, 152000, 153000, 154000, 155000, 156000, 155000,
        154000, 153000, 152000, 151500, 151000, 150500, 150000, 149500, 148000
    ]
    rsis = [
        35.0, 28.0, 19.0, 23.0, 26.0, 28.0, 31.0, 33.0, 30.0,
        29.0, 27.0, 25.0, 24.0, 23.0, 22.0, 21.0, 22.0, 24.0
    ]
    df = pd.DataFrame({"close": closes, "rsi": rsis})
    assert len(df) == 18

    # Prior min close is at index 2 (150,000, RSI 19.0 <= 25.0).
    # Current bar at index 17 has close 148,000 (< 150,000) and RSI 24.0 (> 19.0).
    signal = detect_rsi_divergence(df, cfg)
    assert signal == SignalType.BULLISH_REVERSAL

def test_detect_bearish_divergence():
    # Price makes higher high (160,000 -> 165,000) while RSI makes lower high (75 -> 71)
    cfg = SilverConfig(daily_rsi_overbought=70.0)
    data = {
        "close": [150000, 155000, 158000, 160000, 158000, 157000, 159000, 165000],
        "high": [151000, 156000, 159000, 161000, 159000, 158000, 160000, 166000],
    }
    df = pd.DataFrame(data)
    df["rsi"] = [55.0, 60.0, 68.0, 75.0, 65.0, 62.0, 64.0, 71.0]

    signal = detect_rsi_divergence(df, cfg)
    assert signal == SignalType.BEARISH_BREAKDOWN

def test_detect_no_divergence():
    cfg = SilverConfig()
    # Insufficient bars (< 5)
    short_df = pd.DataFrame({"close": [100, 101, 102]})
    assert detect_rsi_divergence(short_df, cfg) == SignalType.NO_SIGNAL

    # Normal trend without divergence
    normal_df = pd.DataFrame({
        "close": [100, 101, 102, 103, 104, 105, 106, 107],
        "rsi": [50.0, 52.0, 54.0, 55.0, 56.0, 57.0, 58.0, 59.0],
    })
    assert detect_rsi_divergence(normal_df, cfg) == SignalType.NO_SIGNAL

def test_hourly_confirmation_bullish():
    cfg = SilverConfig(hourly_ema_period=20)
    # Hourly close crosses above 20 EMA
    hourly_df = pd.DataFrame({
        "close": [148000, 148500, 149200, 150500],
        "ema_20": [149000, 149000, 149100, 149200],
    })
    confirmed = verify_hourly_confirmation(hourly_df, SignalType.BULLISH_REVERSAL, cfg)
    assert confirmed is True

def test_hourly_confirmation_bearish():
    cfg = SilverConfig(hourly_ema_period=20)
    hourly_df = pd.DataFrame({
        "close": [150000, 149500, 148500, 147000],
        "ema_20": [149000, 149000, 148800, 148500],
    })
    confirmed = verify_hourly_confirmation(hourly_df, SignalType.BEARISH_BREAKDOWN, cfg)
    assert confirmed is True

def test_hourly_confirmation_short_or_no_signal():
    cfg = SilverConfig()
    # Single candle hourly
    hourly_short = pd.DataFrame({"close": [150000]})
    assert verify_hourly_confirmation(hourly_short, SignalType.BULLISH_REVERSAL, cfg) is False

    # Valid candle hourly with NO_SIGNAL daily signal
    hourly_df = pd.DataFrame({
        "close": [148000, 149500],
        "ema_20": [148500, 148600],
    })
    assert verify_hourly_confirmation(hourly_df, SignalType.NO_SIGNAL, cfg) is False

def test_evaluate_technical_signal_end_to_end():
    cfg = SilverConfig(daily_rsi_oversold=25.0, hourly_ema_period=20)
    daily_df = pd.DataFrame({
        "close": [165000, 162000, 158000, 154000, 150000, 152000, 151000, 148000],
        "rsi": [35.0, 30.0, 24.0, 21.0, 19.0, 23.0, 22.0, 25.0],
    })
    
    # Confirmed hourly
    hourly_confirmed = pd.DataFrame({
        "close": [148000, 148500, 149200, 150500],
        "ema_20": [149000, 149000, 149100, 149200],
    })
    sig = evaluate_technical_signal(daily_df, hourly_confirmed, cfg)
    assert sig == SignalType.BULLISH_REVERSAL

    # Unconfirmed hourly (still below EMA)
    hourly_unconfirmed = pd.DataFrame({
        "close": [148000, 147500, 147200, 147000],
        "ema_20": [149000, 149000, 149100, 149200],
    })
    sig_unconf = evaluate_technical_signal(daily_df, hourly_unconfirmed, cfg)
    assert sig_unconf == SignalType.NO_SIGNAL
