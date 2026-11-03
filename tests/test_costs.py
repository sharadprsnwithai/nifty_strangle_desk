import pytest
from strategy.costs import calculate_trade_costs

def test_calculate_trade_costs_structure():
    # 10 lots NIFTY (assume 750 qty), Sell at 32.0, Buy back at 10.0
    res = calculate_trade_costs(
        entry_price=32.0,
        exit_price=10.0,
        quantity=750,
        slippage_per_share=0.50
    )
    
    assert "gross_pnl" in res
    assert "net_pnl" in res
    assert "brokerage" in res
    assert "stt" in res
    assert "exchange_turnover_fee" in res
    assert "sebi_fee" in res
    assert "stamp_duty" in res
    assert "gst" in res
    assert "statutory_taxes" in res
    assert "total_slippage" in res
    assert "total_costs" in res

    # Brokerage: 20 per order * 2 = 40.0
    assert res["brokerage"] == 40.0
    
    # STT: 0.1% on SELL turnover (only entry side for option sellers)
    # 0.001 * 32.0 * 750 = 24.0
    assert pytest.approx(res["stt"], 0.01) == 24.0
    
    # Slippage: 0.50 entry + 0.50 exit = 1.0 * 750 = 750.0
    assert res["total_slippage"] == 750.0

    # Net PnL must be strictly less than gross PnL
    assert res["net_pnl"] < res["gross_pnl"]
    assert res["net_pnl"] == pytest.approx(res["gross_pnl"] - res["total_costs"], 0.01)

def test_calculate_trade_costs_loss_trade():
    # Stop-loss triggered: Sell at 30.0, Buy back at 45.0 (+50% SL)
    res = calculate_trade_costs(
        entry_price=30.0,
        exit_price=45.0,
        quantity=750,
        slippage_per_share=0.50
    )
    
    # Gross loss: (30 - 45) * 750 = -11250
    assert res["gross_pnl"] == -11250.0
    # Net loss must be even more negative due to costs
    assert res["net_pnl"] < -11250.0
    assert res["net_pnl"] == pytest.approx(res["gross_pnl"] - res["total_costs"], 0.01)

def test_calculate_trade_costs_zero_slippage():
    # Test when slippage is 0
    res = calculate_trade_costs(
        entry_price=50.0,
        exit_price=20.0,
        quantity=100,
        slippage_per_share=0.0
    )
    assert res["total_slippage"] == 0.0
    assert res["gross_pnl"] == 3000.0
    assert res["total_costs"] == res["statutory_taxes"]
    assert res["net_pnl"] == round(res["gross_pnl"] - res["statutory_taxes"], 2)

def test_calculate_trade_costs_penny_accuracy():
    # Verify exact formulas and rounding:
    # entry = 100.0, exit = 80.0, qty = 100
    # gross_pnl = (100 - 80) * 100 = 2000.0
    # brokerage = 40.0
    # stt = 0.001 * 100.0 * 100 = 10.0
    # turnover = (100 + 80) * 100 = 18000.0
    # exchange_fee = 0.000495 * 18000 = 8.91
    # sebi_fee = 0.000001 * 18000 = 0.018 -> round to 0.02
    # stamp_duty = 0.00003 * 80.0 * 100 = 0.24
    # gst = 0.18 * (40.0 + 8.91 + 0.02) = 0.18 * 48.93 = 8.8074 -> round to 8.81
    # statutory_taxes = round(40.0 + 10.0 + 8.91 + 0.02 + 0.24 + 8.81, 2) = 67.98
    # slippage = 0.50 * 2.0 * 100 = 100.0
    # total_costs = 67.98 + 100.0 = 167.98
    # net_pnl = 2000.0 - 167.98 = 1832.02
    res = calculate_trade_costs(
        entry_price=100.0,
        exit_price=80.0,
        quantity=100,
        slippage_per_share=0.50
    )
    assert res["gross_pnl"] == 2000.0
    assert res["brokerage"] == 40.0
    assert res["stt"] == 10.0
    assert res["exchange_turnover_fee"] == 8.91
    assert res["sebi_fee"] == 0.02
    assert res["stamp_duty"] == 0.24
    assert res["gst"] == 8.81
    assert res["statutory_taxes"] == 67.98
    assert res["total_slippage"] == 100.0
    assert res["total_costs"] == 167.98
    assert res["net_pnl"] == 1832.02

def test_calculate_trade_costs_zero_quantity():
    res = calculate_trade_costs(
        entry_price=100.0,
        exit_price=80.0,
        quantity=0
    )
    assert res["gross_pnl"] == 0.0
    assert res["net_pnl"] == 0.0
    assert res["brokerage"] == 0.0
    assert res["stt"] == 0.0
    assert res["exchange_turnover_fee"] == 0.0
    assert res["sebi_fee"] == 0.0
    assert res["stamp_duty"] == 0.0
    assert res["gst"] == 0.0
    assert res["statutory_taxes"] == 0.0
    assert res["total_slippage"] == 0.0
    assert res["total_costs"] == 0.0

def test_calculate_trade_costs_negative_inputs():
    with pytest.raises(ValueError, match="Prices must be non-negative"):
        calculate_trade_costs(entry_price=-10.0, exit_price=20.0, quantity=100)

    with pytest.raises(ValueError, match="Prices must be non-negative"):
        calculate_trade_costs(entry_price=10.0, exit_price=-20.0, quantity=100)

    with pytest.raises(ValueError, match="slippage_per_share must be non-negative"):
        calculate_trade_costs(entry_price=10.0, exit_price=20.0, quantity=100, slippage_per_share=-0.5)
