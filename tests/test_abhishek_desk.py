from datetime import datetime, date, time as dt_time
import pandas as pd
import pytest

from strategy.abhishek_desk.config import AbhishekConfig
from strategy.abhishek_desk.engine import AbhishekStrategyEngine, get_nearest_weekly_expiry


def test_abhishek_config_defaults():
    cfg = AbhishekConfig()
    assert cfg.timeframe == "15minute"
    assert cfg.eod_squareoff_time == "15:15"
    assert cfg.use_pdh_pdl_filter is True
    assert cfg.max_pullback_candles == 2
    assert cfg.target_1_r == 1.0
    assert cfg.book_target_1_pct == 0.50
    assert cfg.lots == 2
    assert cfg.lot_size == 65
    assert cfg.max_trades_per_day == 2


def test_nearest_weekly_expiry():
    # Tuesday 2026-10-06 is expiry
    exp_tue = get_nearest_weekly_expiry(date(2026, 10, 6), expiry_weekday=1)
    assert exp_tue == date(2026, 10, 6)
    # Wednesday 2026-10-07 -> next Tuesday is 2026-10-13
    exp_wed = get_nearest_weekly_expiry(date(2026, 10, 7), expiry_weekday=1)
    assert exp_wed == date(2026, 10, 13)


def test_chop_zone_no_trade():
    engine = AbhishekStrategyEngine()
    t_date = date(2026, 10, 10)
    pdh = 24500.0
    pdl = 24000.0

    # Bars trading between 24100 and 24300 (inside PDL-PDH chop zone)
    bars = [
        {"dt": datetime(2026, 10, 10, 9, 15), "open": 24100, "high": 24200, "low": 24050, "close": 24150},
        {"dt": datetime(2026, 10, 10, 9, 30), "open": 24150, "high": 24250, "low": 24120, "close": 24200},
        {"dt": datetime(2026, 10, 10, 9, 45), "open": 24200, "high": 24250, "low": 24150, "close": 24180},
    ]
    df_15m = pd.DataFrame(bars)
    trades = engine.simulate_day(trade_date=t_date, pdh=pdh, pdl=pdl, day_15m_bars=df_15m)
    assert len(trades) == 0


def test_3_candle_pullback_invalidation():
    engine = AbhishekStrategyEngine()
    t_date = date(2026, 10, 10)
    pdh = 24000.0
    pdl = 23500.0

    # Price is above PDH (24000)
    # 3 consecutive red candles appear -> Setup must be disqualified!
    bars = [
        {"dt": datetime(2026, 10, 10, 9, 15), "open": 24100, "high": 24200, "low": 24080, "close": 24180}, # Green
        {"dt": datetime(2026, 10, 10, 9, 30), "open": 24180, "high": 24190, "low": 24120, "close": 24140}, # Red 1
        {"dt": datetime(2026, 10, 10, 9, 45), "open": 24140, "high": 24150, "low": 24090, "close": 24100}, # Red 2
        {"dt": datetime(2026, 10, 10, 10, 0), "open": 24100, "high": 24110, "low": 24050, "close": 24060}, # Red 3 -> Invalidates!
        {"dt": datetime(2026, 10, 10, 10, 15), "open": 24060, "high": 24150, "low": 24050, "close": 24140}, # Green closing high
    ]
    df_15m = pd.DataFrame(bars)
    trades = engine.simulate_day(trade_date=t_date, pdh=pdh, pdl=pdl, day_15m_bars=df_15m)
    # Should not take trade because 3 red candles disqualified the pullback
    assert len(trades) == 0


def test_long_trigger_and_target1_booking():
    engine = AbhishekStrategyEngine()
    t_date = date(2026, 10, 10)
    pdh = 24000.0
    pdl = 23500.0

    bars = [
        # Bar 1: Above PDH (Green)
        {"dt": datetime(2026, 10, 10, 9, 15), "open": 24100, "high": 24200, "low": 24080, "close": 24180},
        # Bar 2: Red pullback (Open 24180, Close 24120, Low 24110)
        {"dt": datetime(2026, 10, 10, 9, 30), "open": 24180, "high": 24190, "low": 24110, "close": 24120},
        # Bar 3: Green trigger! Closes at 24210 > Red Open (24180)!
        {"dt": datetime(2026, 10, 10, 9, 45), "open": 24120, "high": 24220, "low": 24115, "close": 24210},
        # Bar 4: Reaches Target 1 (SL = 24110 - 5 = 24105. Risk = 24210 - 24105 = 105. Target 1 = 24315)
        {"dt": datetime(2026, 10, 10, 10, 0), "open": 24215, "high": 24330, "low": 24215, "close": 24320},
        # Bar 5: End of day 15:15 squareoff
        {"dt": datetime(2026, 10, 10, 15, 15), "open": 24320, "high": 24400, "low": 24300, "close": 24380},
    ]
    df_15m = pd.DataFrame(bars)
    trades = engine.simulate_day(trade_date=t_date, pdh=pdh, pdl=pdl, day_15m_bars=df_15m, mode="FUTURES")
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "LONG"
    assert t["entry_price"] == 24210.0
    assert t["initial_sl"] == 24105.0  # 24110 - 5 buffer
    assert t["t1_hit"] is True
    assert t["target_1_price"] == 24315.0
    assert t["exit_reason"] == "315_RUNNER_EXIT"
    assert t["exit_price"] == 24380.0
    assert t["net_pnl"] > 0


def test_short_trigger_and_sl_reentry_flow():
    engine = AbhishekStrategyEngine()
    t_date = date(2026, 10, 10)
    pdh = 24500.0
    pdl = 24000.0

    # Below PDL (24000)
    bars = [
        # Bar 1: Below PDL (Red)
        {"dt": datetime(2026, 10, 10, 9, 15), "open": 23950, "high": 23960, "low": 23880, "close": 23900},
        # Bar 2: Green pullback (Open 23900, Close 23940, High 23950)
        {"dt": datetime(2026, 10, 10, 9, 30), "open": 23900, "high": 23950, "low": 23890, "close": 23940},
        # Bar 3: Red trigger! Closes at 23880 < Green Open (23900)!
        {"dt": datetime(2026, 10, 10, 9, 45), "open": 23940, "high": 23945, "low": 23875, "close": 23880},
        # Bar 4: Stopped out immediately (SL = 23950 + 5 = 23955)
        {"dt": datetime(2026, 10, 10, 10, 0), "open": 23880, "high": 23970, "low": 23860, "close": 23920},
    ]
    df_15m = pd.DataFrame(bars)
    trades = engine.simulate_day(trade_date=t_date, pdh=pdh, pdl=pdl, day_15m_bars=df_15m, mode="FUTURES")
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "SHORT"
    assert t["entry_price"] == 23880.0
    assert t["initial_sl"] == 23955.0  # 23950 + 5
    assert t["t1_hit"] is False
    assert t["exit_reason"] == "FULL_STOP_LOSS"
    assert t["net_pnl"] < 0
