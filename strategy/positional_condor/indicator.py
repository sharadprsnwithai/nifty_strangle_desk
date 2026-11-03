import logging
from datetime import datetime, timedelta
from typing import Tuple, Optional
import pandas as pd
from kiteconnect import KiteConnect

logger = logging.getLogger("condor.indicator")

NIFTY_INDEX_TOKEN = 256265  # NSE:NIFTY 50 instrument token


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Computes standard Wilder's Relative Strength Index (RSI).
    """
    if len(series) < period:
        return pd.Series([float("nan")] * len(series), index=series.index)

    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    # Wilder's smoothing uses alpha = 1 / period
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # Avoid division by zero
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def classify_regime(rsi_value: float) -> str:
    """
    Classifies market regime based on Uday Chandra's rules:
    - 40.0 <= RSI <= 60.0 -> 'SIDEWAYS' (1:1 Iron Condor)
    - RSI < 40.0          -> 'BEARISH'  (1:2:1 Put Ratio)
    - RSI > 60.0          -> 'BULLISH'  (1:2:1 Call Ratio)
    """
    if 40.0 <= rsi_value <= 60.0:
        return "SIDEWAYS"
    elif rsi_value < 40.0:
        return "BEARISH"
    else:
        return "BULLISH"


def fetch_daily_nifty_rsi(
    kite: KiteConnect,
    days: int = 100,
    instrument_token: Optional[int] = None
) -> Tuple[float, str, pd.DataFrame]:
    """
    Fetches daily bars for NIFTY 50 from Kite Connect and returns the latest RSI and regime.
    """
    token = instrument_token or NIFTY_INDEX_TOKEN
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days + 40)

    logger.info("Fetching %d days of daily NIFTY candles for RSI calculation...", days)
    try:
        candles = kite.historical_data(
            instrument_token=token,
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
            interval="day"
        )
    except Exception as e:
        logger.error("Failed to fetch historical data from Kite for token %s: %s", token, e)
        raise RuntimeError(f"Kite historical_data API failed for token {token}: {e}") from e

    if not candles:
        raise ValueError(f"No daily historical data returned from Kite for token {token}")

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df["rsi"] = compute_rsi(df["close"], period=14)
    valid_rsi = df["rsi"].dropna()
    if valid_rsi.empty:
        raise ValueError(f"Insufficient historical data ({len(df)} bars) to compute RSI(14). Need at least 15 bars.")

    latest_rsi = float(valid_rsi.iloc[-1])
    regime = classify_regime(latest_rsi)

    logger.info(
        "NIFTY Daily Close: Rs %.2f | Daily RSI(14): %.2f | Regime: %s",
        float(df["close"].iloc[-1]), latest_rsi, regime
    )
    return latest_rsi, regime, df
