from datetime import datetime, timedelta, date
import pandas as pd
import pytest
from strategy.backtest import run_backtest_simulation, get_weekly_expiry_for_date, select_strike_above_target

def test_get_weekly_expiry_for_date():
    # Thursday Nov 5 -> 0 DTE, shifts to Thursday Nov 12
    expiry, is_0dte = get_weekly_expiry_for_date(date(2026, 11, 5))
    assert is_0dte is True
    assert expiry == date(2026, 11, 12)

    # Monday Nov 2 -> Not 0 DTE, nearest is Thursday Nov 5
    expiry_mon, is_0dte_mon = get_weekly_expiry_for_date(date(2026, 11, 2))
    assert is_0dte_mon is False
    assert expiry_mon == date(2026, 11, 5)

    # Wednesday Nov 4 -> Not 0 DTE, nearest is Thursday Nov 5
    expiry_wed, is_0dte_wed = get_weekly_expiry_for_date(date(2026, 11, 4))
    assert is_0dte_wed is False
    assert expiry_wed == date(2026, 11, 5)

    # Friday Nov 6 -> Not 0 DTE, nearest is next Thursday Nov 12
    expiry_fri, is_0dte_fri = get_weekly_expiry_for_date(date(2026, 11, 6))
    assert is_0dte_fri is False
    assert expiry_fri == date(2026, 11, 12)

def test_select_strike_above_target():
    spot = 24000.0
    dte = 4.0
    iv = 0.13
    
    ce_strike, ce_price = select_strike_above_target(spot, dte, iv, "CE", target_price=30.0)
    pe_strike, pe_price = select_strike_above_target(spot, dte, iv, "PE", target_price=30.0)
    
    assert ce_strike >= spot
    assert pe_strike <= spot
    assert ce_price >= 30.0
    assert pe_price >= 30.0

@pytest.fixture
def sample_5min_bars():
    # 2 trading days: day 1 normal range, day 2 trending up
    rows = []
    base_time_day1 = datetime(2026, 11, 2, 9, 15)
    for i in range(75): # 375 mins
        t = base_time_day1 + timedelta(minutes=i * 5)
        rows.append({"timestamps": t, "open": 24000.0, "high": 24020.0, "low": 23980.0, "close": 24005.0})

    base_time_day2 = datetime(2026, 11, 3, 9, 15)
    for i in range(75):
        t = base_time_day2 + timedelta(minutes=i * 5)
        # Sharp rally from 24000 to 24400
        spot = 24000.0 + (i * 5.5)
        rows.append({"timestamps": t, "open": spot, "high": spot + 10.0, "low": spot - 5.0, "close": spot + 5.0})

    return pd.DataFrame(rows)

def test_run_backtest_simulation_structure(sample_5min_bars):
    results = run_backtest_simulation(
        bars_df=sample_5min_bars,
        slippage=0.50,
        lots=10,
        lot_size=75,
        iv=0.13
    )

    assert "summary" in results
    assert "trades" in results
    assert "monthly" in results
    
    summary = results["summary"]
    assert "total_days_traded" in summary
    assert "win_rate_pct" in summary
    assert "gross_pnl" in summary
    assert "net_pnl" in summary
    assert "total_taxes_and_brokerage" in summary
    assert "total_slippage" in summary
    assert "profit_factor" in summary
    assert "max_drawdown" in summary

    assert summary["total_days_traded"] == 2
    assert len(results["trades"]) == 2

    # Check Day 2 CE triggered stop loss due to sharp rally
    day2_trade = results["trades"][1]
    assert day2_trade["ce_reason"] == "STOP_LOSS"

def test_run_backtest_simulation_empty_df():
    empty_df = pd.DataFrame(columns=["timestamps", "open", "high", "low", "close"])
    results = run_backtest_simulation(bars_df=empty_df)
    assert results["summary"]["total_days_traded"] == 0
    assert results["trades"] == []
    assert results["monthly"] == []

def test_run_backtest_simulation_column_mappings(sample_5min_bars):
    # Test with "date" column instead of "timestamps"
    df_date = sample_5min_bars.rename(columns={"timestamps": "date"})
    results = run_backtest_simulation(bars_df=df_date)
    assert results["summary"]["total_days_traded"] == 2

    # Test with DatetimeIndex
    df_idx = sample_5min_bars.set_index("timestamps")
    results_idx = run_backtest_simulation(bars_df=df_idx)
    assert results_idx["summary"]["total_days_traded"] == 2

def test_run_backtest_simulation_thursday_0dte():
    # Thursday trading day: Nov 5, 2026
    rows = []
    base_time = datetime(2026, 11, 5, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        rows.append({"timestamps": t, "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0})
    df = pd.DataFrame(rows)

    results = run_backtest_simulation(bars_df=df)
    assert results["summary"]["total_days_traded"] == 1
    trade = results["trades"][0]
    assert trade["is_0dte"] is True
    # Target expiry should be next week Thursday: Nov 12, 2026
    assert trade["expiry"] == "2026-11-12"
