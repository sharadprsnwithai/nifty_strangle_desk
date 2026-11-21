import json
import pytest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np

from strategy.techno_funda.config import TechnoFundaConfig
from strategy.techno_funda.models import TechnoFundaCandidate
from strategy.techno_funda.technicals import calculate_rsi, evaluate_mtf_rsi
from strategy.techno_funda.fundamentals import fetch_and_evaluate_fundamentals
from strategy.techno_funda.scanner import TechnoFundaScanner
from strategy.techno_funda.desk import TechnoFundaDesk


# ==============================================================================
# 1. Config & Technicals Tests
# ==============================================================================

def test_techno_funda_config_defaults(tmp_path):
    cfg = TechnoFundaConfig(data_dir=tmp_path)
    assert cfg.scan_time == "15:15"
    assert cfg.monthly_rsi_threshold == 60.0
    assert cfg.weekly_rsi_threshold == 60.0
    assert cfg.daily_rsi_trigger == 65.0
    assert cfg.min_ttm_ebitda_growth == 0.30
    assert cfg.max_debt_to_equity == 1.0
    assert cfg.min_operating_margin == 0.08


def test_calculate_rsi_accuracy():
    # Steadily rising series should produce high RSI (> 70)
    prices = pd.Series([100 + i * 2 for i in range(30)])
    rsi = calculate_rsi(prices, 14)
    assert float(rsi.iloc[-1]) > 80.0

    # Steadily falling series should produce low RSI (< 30)
    falling = pd.Series([200 - i * 2 for i in range(30)])
    rsi_falling = calculate_rsi(falling, 14)
    assert float(rsi_falling.iloc[-1]) < 20.0


def test_evaluate_mtf_rsi_passes_breakout():
    dates = pd.date_range("2024-01-01", periods=300, freq="B")
    np.random.seed(42)
    returns = np.random.normal(0.002, 0.012, 300)
    returns[-3:] = [0.005, 0.008, 0.015]
    prices = 100 * np.exp(np.cumsum(returns))

    df = pd.DataFrame({
        "open": prices * 0.99,
        "high": prices * 1.01,
        "low": prices * 0.98,
        "close": prices,
        "volume": np.full(300, 100000),
    }, index=dates)

    res = evaluate_mtf_rsi(df)
    assert res is not None
    assert res["daily_rsi"] >= 65.0
    assert res["weekly_rsi"] >= 60.0
    assert res["monthly_rsi"] >= 60.0
    assert res["swing_sl"] > 0
    assert res["target_1"] > res["ltp"]


def test_evaluate_mtf_rsi_fails_downtrend():
    # Declining series
    dates = pd.date_range("2026-06-01", periods=100, freq="B")
    base = np.linspace(200, 100, 100)
    df = pd.DataFrame({
        "open": base,
        "high": base * 1.01,
        "low": base * 0.99,
        "close": base,
        "volume": np.full(100, 50000),
    }, index=dates)

    res = evaluate_mtf_rsi(df)
    assert res is None


# ==============================================================================
# 2. Fundamentals Tests
# ==============================================================================

def test_fundamentals_evaluation_with_cache(tmp_path):
    cache_file = tmp_path / "test_fundamentals.json"
    cached_entry = {
        "TIPSMUSIC": {
            "symbol": "TIPSMUSIC",
            "company_name": "Tips Music Ltd",
            "sector": "Media & Entertainment",
            "ttm_ebitda_growth_pct": 38.5,
            "quarterly_ebitda_growth_pct": 32.1,
            "debt_to_equity": 0.12,
            "operating_margin_pct": 67.5,
            "passed_fundamentals": True,
            "fundamental_reason": "High EBITDA Growth (>30%) & Clean Balance Sheet (D/E < 1.0)",
            "cached_at": datetime.now().timestamp(),
        }
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cached_entry, f)

    res = fetch_and_evaluate_fundamentals("TIPSMUSIC", cache_path=cache_file)
    assert res["passed_fundamentals"] is True
    assert res["ttm_ebitda_growth_pct"] == 38.5
    assert res["debt_to_equity"] == 0.12


# ==============================================================================
# 3. Scanner Pipeline Tests
# ==============================================================================

def test_scanner_market_closed_guard():
    scanner = TechnoFundaScanner()
    with patch("strategy.techno_funda.scanner.is_market_open", return_value=(False, "Exchange closed")):
        res = scanner.scan(ignore_holidays=False)
        assert res["status"] == "MARKET_CLOSED"
        assert res["candidates"] == []


def test_scanner_end_to_end_custom_data(tmp_path):
    cache_file = tmp_path / "funda_cache.json"
    cache_data = {
        "STRONG_CO": {
            "symbol": "STRONG_CO",
            "company_name": "Strong Growth Co",
            "sector": "Industrial",
            "ttm_ebitda_growth_pct": 45.0,
            "quarterly_ebitda_growth_pct": 35.0,
            "debt_to_equity": 0.20,
            "operating_margin_pct": 18.0,
            "passed_fundamentals": True,
            "fundamental_reason": "High Growth & Low Debt",
            "cached_at": datetime.now().timestamp(),
        }
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache_data, f)

    cfg = TechnoFundaConfig(data_dir=tmp_path, fundamentals_cache_file=cache_file)
    scanner = TechnoFundaScanner(config=cfg)

    # Synthetic strong uptrend dataset
    dates = pd.date_range("2024-01-01", periods=300, freq="B")
    np.random.seed(42)
    returns = np.random.normal(0.002, 0.012, 300)
    returns[-3:] = [0.005, 0.008, 0.015]
    prices = 100 * np.exp(np.cumsum(returns))
    df = pd.DataFrame({
        "open": prices * 0.99,
        "high": prices * 1.01,
        "low": prices * 0.98,
        "close": prices,
        "volume": np.full(300, 100000),
    }, index=dates)

    custom_universe = {"STRONG_CO": df}

    res = scanner.scan(custom_data=custom_universe, ignore_holidays=True)
    assert res["status"] == "COMPLETED"
    assert res["candidates_count"] == 1
    cand = res["candidates"][0]
    assert cand.symbol == "STRONG_CO"
    assert cand.daily_rsi >= 65.0
    assert cand.passed_fundamentals is True


# ==============================================================================
# 4. Desk & Formatting Tests
# ==============================================================================

def test_desk_telegram_alert_formatting():
    desk = TechnoFundaDesk()
    cand = TechnoFundaCandidate(
        symbol="TIPSMUSIC",
        company_name="Tips Music Ltd",
        sector="Media",
        ltp=808.05,
        daily_rsi=65.5,
        prev_daily_rsi=62.0,
        weekly_rsi=70.5,
        monthly_rsi=70.7,
        rsi_cross_confirmed=True,
        volume_ratio=1.6,
        chart_pattern="BOX_BREAKOUT_WITH_VOLUME",
        ttm_ebitda_growth_pct=38.5,
        quarterly_ebitda_growth_pct=32.1,
        debt_to_equity=0.12,
        operating_margin_pct=67.5,
        passed_fundamentals=True,
        swing_sl=755.0,
        target_1=880.0,
        target_2=940.0,
        scan_date="2026-11-21",
    )
    msg = desk.format_candidate_alert(cand)
    assert "TECHNO-FUNDA BREAKOUT ALERT" in msg
    assert "TIPSMUSIC" in msg
    assert "Monthly RSI: <b>70.7</b>" in msg
    assert "Crossed 65 🚀" in msg
    assert "TTM YoY EBITDA Growth: <b>+38.5%</b>" in msg
    assert "Rs 755.00" in msg


def test_desk_run_scan_cycle_dispatches_telegram(tmp_path):
    mock_notifier = MagicMock()
    mock_scanner = MagicMock()

    cand = TechnoFundaCandidate(
        symbol="MCX",
        company_name="Multi Commodity Exchange",
        sector="Financials",
        ltp=6500.0,
        daily_rsi=66.2,
        prev_daily_rsi=61.0,
        weekly_rsi=72.0,
        monthly_rsi=71.0,
        rsi_cross_confirmed=True,
        volume_ratio=1.8,
        chart_pattern="VCP_CONTRACTION_BREAKOUT",
        ttm_ebitda_growth_pct=52.0,
        quarterly_ebitda_growth_pct=40.0,
        debt_to_equity=0.0,
        operating_margin_pct=45.0,
        passed_fundamentals=True,
        swing_sl=6200.0,
        target_1=6950.0,
        target_2=7250.0,
        scan_date="2026-11-21",
    )

    mock_scanner.scan.return_value = {
        "status": "COMPLETED",
        "candidates": [cand],
    }

    desk = TechnoFundaDesk(scanner=mock_scanner, notifier=mock_notifier, ignore_holidays=True)
    res = desk.run_scan_cycle()

    assert res["status"] == "COMPLETED"
    assert mock_notifier.send_message.called
    assert "TECHNO-FUNDA BREAKOUT ALERT" in mock_notifier.send_message.call_args[0][0]
