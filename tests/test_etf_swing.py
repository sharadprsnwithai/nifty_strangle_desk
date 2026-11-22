import pytest
import pandas as pd
import numpy as np
from strategy.etf_swing.config import ETFSwingConfig, ETFInfo
from strategy.etf_swing.scanner import ETFScanner
from strategy.etf_swing.order_manager import ETFOrderManager
from strategy.etf_swing.desk import ETFSwingDesk

def test_config_capital_per_slot():
    cfg = ETFSwingConfig()
    assert cfg.total_capital == 1000000.0
    assert round(cfg.capital_per_slot, 2) == 333333.33

def test_calculate_levels_math():
    cfg = ETFSwingConfig()
    scanner = ETFScanner(cfg)
    
    # Create synthetic daily DataFrame across 2 months
    dates = pd.date_range("2024-01-01", "2024-02-28", freq="D")
    df = pd.DataFrame({
        "High": 110.0,
        "Low": 90.0,
        "Close": 100.0
    }, index=dates)
    
    # Previous month (Jan): High=110, Low=90, Close=100
    # Expected Pivot = (110 + 90 + 100) / 3 = 100.0
    # Expected Range = 110 - 90 = 20.0
    # Expected S1 = 100 - (0.382 * 20) = 100 - 7.64 = 92.36
    P, S1, ltp, mom12 = scanner.calculate_levels(df)
    assert round(P, 2) == 100.00
    assert round(S1, 2) == 92.36
    assert ltp == 100.0

def test_order_manager_paper_sizing():
    cfg = ETFSwingConfig(mode="PAPER")
    om = ETFOrderManager(cfg)
    
    # Test Buy execution
    res = om.execute_buy("NIFTYBEES", allocated_capital=100000.0, current_price=250.0)
    assert res["status"] == "COMPLETE"
    assert res["quantity"] == 400
    assert res["cost"] == 100000.0
    
    # Test Sell execution
    res_sell = om.execute_sell("NIFTYBEES", quantity=400, current_price=280.0)
    assert res_sell["status"] == "COMPLETE"
    assert res_sell["quantity"] == 400
