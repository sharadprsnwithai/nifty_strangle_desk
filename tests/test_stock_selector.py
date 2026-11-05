import pytest
from unittest.mock import MagicMock
from datetime import datetime, date, timedelta
from strategy.orb_desk.stock_selector import (
    get_fno_stock_symbols,
    screen_top_gainers_losers,
    is_coiling_pattern,
    screen_coiling_pattern_stocks,
    build_dynamic_watchlist,
)


def test_get_fno_stock_symbols():
    kite_mock = MagicMock()
    kite_mock.instruments.return_value = [
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "RELIANCE"},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "TCS"},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "NIFTY"},       # index excluded
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "BANKNIFTY"},   # index excluded
        {"segment": "NFO-OPT", "instrument_type": "CE", "name": "INFY"},        # not FUT
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "RELIANCE"},    # duplicate
    ]

    symbols = get_fno_stock_symbols(kite_mock)
    assert symbols == ["RELIANCE", "TCS"]


def test_screen_top_gainers_losers():
    kite_mock = MagicMock()
    symbols = ["SYM1", "SYM2", "SYM3", "SYM4", "SYM5", "SYM6", "SYM7"]
    
    # Mock quote response with previous close and current price
    kite_mock.quote.return_value = {
        "NSE:SYM1": {"instrument_token": 101, "last_price": 110.0, "ohlc": {"close": 100.0}}, # +10%
        "NSE:SYM2": {"instrument_token": 102, "last_price": 105.0, "ohlc": {"close": 100.0}}, # +5%
        "NSE:SYM3": {"instrument_token": 103, "last_price": 103.0, "ohlc": {"close": 100.0}}, # +3%
        "NSE:SYM4": {"instrument_token": 104, "last_price": 101.0, "ohlc": {"close": 100.0}}, # +1%
        "NSE:SYM5": {"instrument_token": 105, "last_price": 95.0,  "ohlc": {"close": 100.0}}, # -5%
        "NSE:SYM6": {"instrument_token": 106, "last_price": 90.0,  "ohlc": {"close": 100.0}}, # -10%
        "NSE:SYM7": {"instrument_token": 107, "last_price": 85.0,  "ohlc": {"close": 100.0}}, # -15%
    }

    gainers, losers = screen_top_gainers_losers(kite_mock, symbols, n=3)

    # Top 3 gainers: SYM1 (+10%), SYM2 (+5%), SYM3 (+3%)
    assert len(gainers) == 3
    assert gainers[0]["symbol"] == "SYM1"
    assert pytest.approx(gainers[0]["pct_change"], 0.01) == 10.0
    assert gainers[1]["symbol"] == "SYM2"
    assert gainers[2]["symbol"] == "SYM3"

    # Top 3 losers: SYM7 (-15%), SYM6 (-10%), SYM5 (-5%)
    assert len(losers) == 3
    assert losers[0]["symbol"] == "SYM7"
    assert pytest.approx(losers[0]["pct_change"], 0.01) == -15.0
    assert losers[1]["symbol"] == "SYM6"
    assert losers[2]["symbol"] == "SYM5"


def test_is_coiling_pattern():
    # Day T-3, Day T-2, Day T-1
    # Condition: H3 > H2 > H1 and L3 < L2 < L1
    
    # Valid Coiling
    candles_valid = [
        {"date": datetime(2026, 11, 2), "high": 110.0, "low": 90.0},  # T-3
        {"date": datetime(2026, 11, 3), "high": 105.0, "low": 95.0},  # T-2
        {"date": datetime(2026, 11, 4), "high": 102.0, "low": 98.0},  # T-1
    ]
    assert is_coiling_pattern(candles_valid) is True

    # Higher high on T-1 (not coiling)
    candles_invalid_high = [
        {"date": datetime(2026, 11, 2), "high": 110.0, "low": 90.0},
        {"date": datetime(2026, 11, 3), "high": 105.0, "low": 95.0},
        {"date": datetime(2026, 11, 4), "high": 106.0, "low": 98.0},
    ]
    assert is_coiling_pattern(candles_invalid_high) is False

    # Lower low on T-1 (not coiling)
    candles_invalid_low = [
        {"date": datetime(2026, 11, 2), "high": 110.0, "low": 90.0},
        {"date": datetime(2026, 11, 3), "high": 105.0, "low": 95.0},
        {"date": datetime(2026, 11, 4), "high": 102.0, "low": 94.0},
    ]
    assert is_coiling_pattern(candles_invalid_low) is False

    # Fewer than 3 candles
    assert is_coiling_pattern(candles_valid[:2]) is False


def test_screen_coiling_pattern_stocks():
    kite_mock = MagicMock()
    symbols_data = [
        {"symbol": "COIL1", "token": 1001},
        {"symbol": "NOCOIL", "token": 1002},
    ]

    def mock_hist(instrument_token, *args, **kwargs):
        if instrument_token == 1001:
            return [
                {"date": datetime(2026, 11, 2), "high": 200.0, "low": 150.0},
                {"date": datetime(2026, 11, 3), "high": 190.0, "low": 160.0},
                {"date": datetime(2026, 11, 4), "high": 180.0, "low": 170.0},
                {"date": datetime(2026, 11, 5), "high": 175.0, "low": 172.0}, # today's incomplete candle
            ]
        else:
            return [
                {"date": datetime(2026, 11, 2), "high": 200.0, "low": 150.0},
                {"date": datetime(2026, 11, 3), "high": 210.0, "low": 140.0}, # expansion
                {"date": datetime(2026, 11, 4), "high": 205.0, "low": 145.0},
                {"date": datetime(2026, 11, 5), "high": 202.0, "low": 148.0},
            ]

    kite_mock.historical_data.side_effect = mock_hist
    today = date(2026, 11, 5)

    coiling_stocks = screen_coiling_pattern_stocks(kite_mock, symbols_data, today_date=today)
    assert len(coiling_stocks) == 1
    assert coiling_stocks[0]["symbol"] == "COIL1"
    assert coiling_stocks[0]["t1_high"] == 180.0
    assert coiling_stocks[0]["t1_low"] == 170.0


def test_build_dynamic_watchlist():
    kite_mock = MagicMock()
    
    # Mock symbols and instruments
    kite_mock.instruments.return_value = [
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM1", "lot_size": 100},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM2", "lot_size": 200},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM3", "lot_size": 300},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM4", "lot_size": 400},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM5", "lot_size": 500},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM6", "lot_size": 600},
        {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "SYM_COIL", "lot_size": 700},
    ]

    kite_mock.quote.return_value = {
        "NSE:SYM1": {"instrument_token": 1, "last_price": 120.0, "ohlc": {"close": 100.0}}, # +20% (Gainer)
        "NSE:SYM2": {"instrument_token": 2, "last_price": 115.0, "ohlc": {"close": 100.0}}, # +15% (Gainer)
        "NSE:SYM3": {"instrument_token": 3, "last_price": 110.0, "ohlc": {"close": 100.0}}, # +10% (Gainer)
        "NSE:SYM4": {"instrument_token": 4, "last_price": 90.0,  "ohlc": {"close": 100.0}}, # -10% (Loser)
        "NSE:SYM5": {"instrument_token": 5, "last_price": 85.0,  "ohlc": {"close": 100.0}}, # -15% (Loser)
        "NSE:SYM6": {"instrument_token": 6, "last_price": 80.0,  "ohlc": {"close": 100.0}}, # -20% (Loser)
        "NSE:SYM_COIL": {"instrument_token": 7, "last_price": 100.0, "ohlc": {"close": 100.0}}, # 0% (Coiling)
    }

    # Pre-scanned coiling stocks
    coiling_stocks = [
        {"symbol": "SYM_COIL", "token": 7, "t1_high": 105.0, "t1_low": 95.0, "lot": 700},
        # SYM1 also happens to be coiling as well
        {"symbol": "SYM1", "token": 1, "t1_high": 108.0, "t1_low": 98.0, "lot": 100},
    ]

    watchlist = build_dynamic_watchlist(
        kite=kite_mock,
        pre_scanned_coiling=coiling_stocks,
        top_gainers_n=3,
        top_losers_n=3
    )

    # Total unique symbols: SYM1, SYM2, SYM3, SYM4, SYM5, SYM6, SYM_COIL (7 total)
    assert len(watchlist) == 7
    symbols_in_watchlist = {item["symbol"] for item in watchlist}
    assert symbols_in_watchlist == {"SYM1", "SYM2", "SYM3", "SYM4", "SYM5", "SYM6", "SYM_COIL"}

    # SYM1 should have both TOP_GAINER and COILING in reasons
    sym1_entry = next(item for item in watchlist if item["symbol"] == "SYM1")
    assert "TOP_GAINER" in sym1_entry["reasons"]
    assert "COILING" in sym1_entry["reasons"]

    # SYM_COIL should have COILING in reasons
    coil_entry = next(item for item in watchlist if item["symbol"] == "SYM_COIL")
    assert "COILING" in coil_entry["reasons"]

    # SYM6 should have TOP_LOSER in reasons
    sym6_entry = next(item for item in watchlist if item["symbol"] == "SYM6")
    assert "TOP_LOSER" in sym6_entry["reasons"]
