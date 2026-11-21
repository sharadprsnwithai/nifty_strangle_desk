import pytest
import pandas as pd
from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.models import SignalType, StockPosition
from strategy.stock_futures.engine import StockFuturesEngine


def make_dummy_bars(breakout_side=None, vol_mult=2.0, break_consolidation=False):
    """Creates a sample 15m dataframe for testing signals (4 consolidation candles: 09:30, 09:45, 10:00, 10:15)."""
    rows = [
        {"time": "09:15", "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 1000},
        {"time": "09:30", "open": 102.0, "high": 104.0, "low": 96.0, "close": 106.0 if break_consolidation else 103.0, "volume": 500},
        {"time": "09:45", "open": 103.0, "high": 104.5, "low": 97.0, "close": 101.0, "volume": 400},
        {"time": "10:00", "open": 101.0, "high": 103.0, "low": 98.0, "close": 100.0, "volume": 450},
        {"time": "10:15", "open": 100.5, "high": 103.5, "low": 98.5, "close": 101.5, "volume": 480},
    ]

    vol = int(500 * vol_mult)
    if breakout_side == "LONG":
        rows.append({"time": "10:30", "open": 101.0, "high": 106.0, "low": 100.5, "close": 105.5, "volume": vol})
    elif breakout_side == "SHORT":
        rows.append({"time": "10:30", "open": 99.0, "high": 99.5, "low": 94.0, "close": 94.5, "volume": vol})
    else:
        rows.append({"time": "10:30", "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0, "volume": vol})

    df = pd.DataFrame(rows)
    df["vol_ma20"] = 500.0  # mock 20-period volume MA
    return df


def test_engine_evaluates_long_breakout():
    cfg = StockFuturesConfig()
    engine = StockFuturesEngine(cfg)
    df = make_dummy_bars(breakout_side="LONG", vol_mult=2.0)

    signal = engine.evaluate_signal("ABB", df)
    assert signal is not None
    assert signal.symbol == "ABB"
    assert signal.side == SignalType.LONG
    assert signal.entry_price == 105.0  # max(open 101.0, h1 105.0)
    assert signal.sl_price == round(105.0 * 0.99, 2)
    assert signal.target_price == round(105.0 * 1.015, 2)
    assert signal.volume_ratio == 2.0


def test_engine_evaluates_short_breakout():
    cfg = StockFuturesConfig()
    engine = StockFuturesEngine(cfg)
    df = make_dummy_bars(breakout_side="SHORT", vol_mult=2.0)

    signal = engine.evaluate_signal("VEDL", df)
    assert signal is not None
    assert signal.symbol == "VEDL"
    assert signal.side == SignalType.SHORT
    assert signal.entry_price == 95.0  # min(open 99.0, l1 95.0)
    assert signal.sl_price == round(95.0 * 1.01, 2)
    assert signal.target_price == round(95.0 * 0.985, 2)


def test_engine_rejects_low_volume_breakout():
    cfg = StockFuturesConfig(vol_threshold=1.5)
    engine = StockFuturesEngine(cfg)
    df = make_dummy_bars(breakout_side="LONG", vol_mult=1.2)  # 1.2x < 1.5x

    signal = engine.evaluate_signal("ABB", df)
    assert signal is None


def test_engine_rejects_broken_consolidation():
    cfg = StockFuturesConfig()
    engine = StockFuturesEngine(cfg)
    # Candle at 09:30 closes at 106.0 > H1 (105.0)
    df = make_dummy_bars(breakout_side="LONG", vol_mult=2.0, break_consolidation=True)

    signal = engine.evaluate_signal("ABB", df)
    assert signal is None


def test_engine_exit_logic():
    cfg = StockFuturesConfig(squareoff_time="15:00")
    engine = StockFuturesEngine(cfg)

    pos = StockPosition(
        symbol="ABB",
        tradingsymbol="ABB_FUT",
        side="LONG",
        quantity=250,
        lots=2,
        entry_time="10:15",
        entry_price=100.0,
        sl_price=99.0,
        target_price=101.5,
        current_price=100.0,
    )

    # 1. Target Hit
    exit_res = engine.check_exit(pos, current_price=101.6, current_time="11:30")
    assert exit_res == (101.5, "TARGET_HIT")

    # 2. SL Hit
    exit_res = engine.check_exit(pos, current_price=98.8, current_time="11:30")
    assert exit_res == (99.0, "SL_HIT")

    # 3. EOD 15:00 Exit
    exit_res = engine.check_exit(pos, current_price=100.8, current_time="15:00")
    assert exit_res == (100.8, "15:00_EOD")
