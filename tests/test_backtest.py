from datetime import datetime, timedelta, date
import pandas as pd
import pytest
from strategy.backtest import run_backtest_simulation, get_weekly_expiry_for_date, select_strike_above_target

def test_get_weekly_expiry_for_date():
    # Tuesday Nov 3 -> 0 DTE, shifts to Tuesday Nov 10
    expiry, is_0dte = get_weekly_expiry_for_date(date(2026, 11, 3))
    assert is_0dte is True
    assert expiry == date(2026, 11, 10)

    # Monday Nov 2 -> Not 0 DTE, nearest is Tuesday Nov 3
    expiry_mon, is_0dte_mon = get_weekly_expiry_for_date(date(2026, 11, 2))
    assert is_0dte_mon is False
    assert expiry_mon == date(2026, 11, 3)

    # Wednesday Nov 4 -> Not 0 DTE, nearest is next Tuesday Nov 10
    expiry_wed, is_0dte_wed = get_weekly_expiry_for_date(date(2026, 11, 4))
    assert is_0dte_wed is False
    assert expiry_wed == date(2026, 11, 10)

    # Friday Nov 6 -> Not 0 DTE, nearest is next Tuesday Nov 10
    expiry_fri, is_0dte_fri = get_weekly_expiry_for_date(date(2026, 11, 6))
    assert is_0dte_fri is False
    assert expiry_fri == date(2026, 11, 10)

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
        lot_size=65,
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

    # Check Day 2 CE hit 70% STOP_LOSS by default
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

def test_run_backtest_simulation_tuesday_0dte():
    # Tuesday trading day: Nov 3, 2026
    rows = []
    base_time = datetime(2026, 11, 3, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        rows.append({"timestamps": t, "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0})
    df = pd.DataFrame(rows)

    results = run_backtest_simulation(bars_df=df)
    assert results["summary"]["total_days_traded"] == 1
    trade = results["trades"][0]
    assert trade["is_0dte"] is True
    # Target expiry should be next week Tuesday: Nov 10, 2026
    assert trade["expiry"] == "2026-11-10"


def test_run_backtest_simulation_70pct_sl_default():
    # Verify default is 70% single-leg SL (strategy_max_loss=0.0, use_leg_sl=True, leg_sl_pct=0.70)
    rows = []
    base_time = datetime(2026, 11, 2, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        spot = 24000.0 if i < 15 else 24000.0 + ((i - 15) * 8.0)
        rows.append({"timestamps": t, "open": spot, "high": spot + 5.0, "low": spot - 5.0, "close": spot})
    df = pd.DataFrame(rows)

    results = run_backtest_simulation(bars_df=df, lots=10, lot_size=65)
    assert results["summary"]["total_days_traded"] == 1
    trade = results["trades"][0]
    assert trade["ce_reason"] == "STOP_LOSS"


def test_run_backtest_simulation_strategy_max_loss_exit():
    # Construct a trading day with steady strong rally where combined loss hits 10,000,
    # triggering an immediate simultaneous portfolio exit on both legs.
    rows = []
    base_time = datetime(2026, 11, 2, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        if i < 15:
            spot = 24000.0
        else:
            spot = 24000.0 + ((i - 14) * 15.0)  # Steady strong rally
        rows.append({"timestamps": t, "open": spot, "high": spot + 1.0, "low": spot - 1.0, "close": spot})
    df = pd.DataFrame(rows)

    results = run_backtest_simulation(
        bars_df=df,
        lots=10,
        lot_size=65,
        strategy_max_loss=10000.0,
        use_leg_sl=False
    )
    assert results["summary"]["total_days_traded"] == 1
    trade = results["trades"][0]

    # Both legs should have exited simultaneously with STRATEGY_MAX_LOSS_10K
    assert trade["ce_reason"] == "STRATEGY_MAX_LOSS_10K"
    assert trade["pe_reason"] == "STRATEGY_MAX_LOSS_10K"
    # Gross loss should be capped near -10,000 (within 5-minute candle granularity)
    assert trade["gross_pnl"] <= -10000.0
    assert trade["gross_pnl"] >= -12500.0


def test_run_backtest_simulation_single_leg_exit():
    # Construct a trading day with steady strong rally where CE hits SL,
    # but PE stays open to harvest decay.
    rows = []
    base_time = datetime(2026, 11, 2, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        if i < 15:
            spot = 24000.0
        else:
            spot = 24000.0 + ((i - 14) * 15.0)  # Steady strong rally
        rows.append({"timestamps": t, "open": spot, "high": spot + 1.0, "low": spot - 1.0, "close": spot})
    df = pd.DataFrame(rows)

    results = run_backtest_simulation(
        bars_df=df,
        lots=10,
        lot_size=65,
        strategy_max_loss=10000.0,
        use_leg_sl=True,
        trail_opposite_to_cost=True
    )
    assert results["summary"]["total_days_traded"] == 1
    trade = results["trades"][0]

    # CE stopped out on STOP_LOSS or SINGLE_LEG_10K_EXIT
    assert trade["ce_reason"] in ("STOP_LOSS", "SINGLE_LEG_10K_EXIT")
    # PE remained active or exited at TIME_EXIT / DECAY_TARGET (not STRATEGY_MAX_LOSS_10K)
    assert trade["pe_reason"] in ("TIME_EXIT", "DECAY_TARGET")


def test_calculate_daily_atr_has_no_same_day_lookahead():
    from strategy.backtest import calculate_daily_atr
    rows = [
        {"timestamps": datetime(2026, 11, 2, 10, 0), "open": 24000.0, "high": 24100.0, "low": 23950.0, "close": 24050.0},
        {"timestamps": datetime(2026, 11, 3, 10, 0), "open": 24050.0, "high": 24650.0, "low": 24050.0, "close": 24600.0},
    ]
    atr = calculate_daily_atr(pd.DataFrame(rows), period=14)
    assert pd.isna(atr.iloc[0])
    # Day 2 10:00 AM ATR must reflect Day 1 range (150.0), not Day 2 afternoon range (600.0)
    assert pytest.approx(float(atr.iloc[1]), abs=1e-6) == 150.0


def test_backtest_pending_cost_trail_uses_correct_favorable_extremum():
    rows = []
    t0 = datetime(2026, 11, 2, 9, 15)
    for i in range(75):
        t = t0 + timedelta(minutes=i * 5)
        if i == 10:  # 10:05: PE hits 1.5x STOP_LOSS on bar_low=23900 (CE > 0.2x target) while close=24030 leaves CE underwater -> cost_trail_pending=True
            rows.append({"timestamps": t, "open": 24000.0, "high": 24035.0, "low": 23900.0, "close": 24030.0})
        elif i == 11:  # 10:10: bar_low=23980 dips CE < ce_entry (locks trail) while bar_high=24020 stays < 1.5x SL
            rows.append({"timestamps": t, "open": 24015.0, "high": 24020.0, "low": 23980.0, "close": 23990.0})
        elif i == 12:  # 10:15: spot rallies to 24035 -> triggers trailed CE SL at ce_entry (< 1.5x SL)
            rows.append({"timestamps": t, "open": 24000.0, "high": 24035.0, "low": 23995.0, "close": 24030.0})
        else:
            rows.append({"timestamps": t, "open": 24000.0, "high": 24002.0, "low": 23998.0, "close": 24000.0})
    res = run_backtest_simulation(
        pd.DataFrame(rows),
        lots=1,
        lot_size=65,
        strategy_max_loss=50000.0,
        use_leg_sl=True,
        trail_opposite_to_cost=True,
    )
    assert res["summary"]["total_days_traded"] == 1
    trade = res["trades"][0]
    assert trade["pe_reason"] == "STOP_LOSS"
    assert trade["ce_reason"] == "STOP_LOSS"
    assert trade["ce_exit"] == trade["ce_entry"]


def test_run_backtest_simulation_no_sl():
    # Sharp rally that would normally trigger STOP_LOSS or STRATEGY_MAX_LOSS_10K
    # must stay open until TIME_EXIT (or DECAY_TARGET) when SL is disabled.
    rows = []
    base_time = datetime(2026, 11, 2, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        spot = 24000.0 if i < 15 else 24000.0 + ((i - 14) * 15.0)
        rows.append({"timestamps": t, "open": spot, "high": spot + 1.0, "low": spot - 1.0, "close": spot})
    df = pd.DataFrame(rows)

    results = run_backtest_simulation(
        bars_df=df,
        lots=10,
        lot_size=65,
        strategy_max_loss=0.0,
        use_leg_sl=False,
        trail_opposite_to_cost=False,
    )
    assert results["summary"]["total_days_traded"] == 1
    trade = results["trades"][0]
    assert trade["ce_reason"] in ("TIME_EXIT", "CO_TARGET_EXIT")
    assert trade["pe_reason"] in ("TIME_EXIT", "DECAY_TARGET")
    assert trade["ce_reason"] not in ("STOP_LOSS", "STRATEGY_MAX_LOSS_10K", "SINGLE_LEG_10K_EXIT")
    assert trade["pe_reason"] not in ("STOP_LOSS", "STRATEGY_MAX_LOSS_10K", "SINGLE_LEG_10K_EXIT")
    assert trade["gross_pnl"] < -10000.0


def test_run_backtest_simulation_roll_untested_on_trend():
    # Sharp rally where PE hits 80% DECAY_TARGET while combined P&L <= 0:
    # roll_untested=True rolls PE to ATM once instead of CO_TARGET_EXIT on CE.
    rows = []
    base_time = datetime(2026, 11, 2, 9, 15)
    for i in range(75):
        t = base_time + timedelta(minutes=i * 5)
        spot = 24000.0 if i < 15 else 24000.0 + ((i - 14) * 15.0)
        rows.append({"timestamps": t, "open": spot, "high": spot + 1.0, "low": spot - 1.0, "close": spot})
    df = pd.DataFrame(rows)

    res_rolled = run_backtest_simulation(
        bars_df=df,
        lots=10,
        lot_size=65,
        strategy_max_loss=0.0,
        use_leg_sl=False,
        roll_untested=True,
    )
    assert res_rolled["summary"]["total_days_traded"] == 1
    assert res_rolled["trades"][0]["rolled"] is True

    res_no_roll = run_backtest_simulation(
        bars_df=df,
        lots=10,
        lot_size=65,
        strategy_max_loss=0.0,
        use_leg_sl=False,
        roll_untested=False,
    )
    assert res_no_roll["trades"][0]["rolled"] is False
    assert res_no_roll["trades"][0]["ce_reason"] == "CO_TARGET_EXIT"


