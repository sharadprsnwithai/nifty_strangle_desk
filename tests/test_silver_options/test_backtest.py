import pytest
from strategy.silver_options.backtest import calculate_mcx_costs, run_silver_backtest


def test_mcx_cost_calculation():
    # Turnover of 1 lot (5kg * 150000 = 7.5L)
    costs = calculate_mcx_costs(turnover=750000.0)
    # Expected brokerage (40) + CTT + exchange fee + GST > 50
    assert costs > 50.0


def test_run_silver_backtest_metrics():
    mock_trades = [
        {"pnl": 15000.0, "holding_days": 2},
        {"pnl": -4000.0, "holding_days": 1},
        {"pnl": 18000.0, "holding_days": 3},
        {"pnl": -3500.0, "holding_days": 1},
    ]
    summary = run_silver_backtest(mock_trades)
    assert summary["total_trades"] == 4
    assert summary["win_rate"] == 50.0
    assert summary["net_pnl"] > 20000.0
    assert summary["profit_factor"] > 3.0


def test_run_silver_backtest_empty():
    summary = run_silver_backtest([])
    assert summary["total_trades"] == 0
    assert summary["net_pnl"] == 0.0
    assert summary["win_rate"] == 0.0
    assert summary["profit_factor"] == 0.0
    assert summary["max_drawdown"] == 0.0


def test_mcx_cost_custom_legs():
    # turnover = 100,000, legs = 1
    # brokerage = 20 * 1 * 2 = 40
    # mcx_turnover_fee = 100000 * 0.000021 = 2.1
    # ctt = 100000 * 0.0001 = 10.0
    # sebi_fee = 100000 * 0.000001 = 0.1
    # gst = (40 + 2.1 + 0.1) * 0.18 = 42.2 * 0.18 = 7.596
    # stamp_duty = 100000 * 0.00003 = 3.0
    # total = 40 + 2.1 + 10.0 + 0.1 + 7.596 + 3.0 = 62.796 -> 62.8
    cost = calculate_mcx_costs(turnover=100000.0, legs=1)
    assert cost == 62.8
