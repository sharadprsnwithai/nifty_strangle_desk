from datetime import date, datetime
from unittest.mock import MagicMock
import pytest
from strategy.option_chain import resolve_target_expiry, find_strangle_strikes, fetch_daily_atr

@pytest.fixture
def mock_nfo_instruments():
    return [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24000CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 24000.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24000PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 11, 10), "tradingsymbol": "NIFTY26NOV24000CE10", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 24000.0, "expiry": date(2026, 11, 10), "tradingsymbol": "NIFTY26NOV24000PE10", "lot_size": 65},
        {"name": "BANKNIFTY", "instrument_type": "CE", "strike": 52000.0, "expiry": date(2026, 11, 3), "tradingsymbol": "BANKNIFTY26NOV52000CE", "lot_size": 30},
    ]

def test_resolve_target_expiry_non_expiry_day(mock_nfo_instruments):
    # Today is Monday Nov 2 -> target should be nearest upcoming Tuesday Nov 3
    today = date(2026, 11, 2)
    expiry = resolve_target_expiry(mock_nfo_instruments, today_date=today)
    assert expiry == date(2026, 11, 3)

def test_resolve_target_expiry_0_dte_shifts_to_next_week(mock_nfo_instruments):
    # Today is Tuesday Nov 3 (0 DTE) -> target must shift to Next Week Nov 10
    today = date(2026, 11, 3)
    expiry = resolve_target_expiry(mock_nfo_instruments, today_date=today)
    assert expiry == date(2026, 11, 10)

def test_resolve_target_expiry_no_upcoming():
    instruments = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 10, 1), "tradingsymbol": "NIFTYOLDCE"},
    ]
    today = date(2026, 11, 2)
    with pytest.raises(ValueError, match="No upcoming NIFTY option expiries"):
        resolve_target_expiry(instruments, today_date=today)

def test_resolve_target_expiry_0_dte_only_one_expiry():
    # If today is expiry day but there is only 1 expiry in instruments, fall back to that expiry
    instruments = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24000CE"},
    ]
    today = date(2026, 11, 3)
    expiry = resolve_target_expiry(instruments, today_date=today)
    assert expiry == date(2026, 11, 3)

def test_find_strangle_strikes_closest_above_30():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24300.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24300CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24200.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24200CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23600.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV23600PE", "lot_size": 65},
    ]

    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24300CE": {"last_price": 28.5},
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.1},  # Closest >= 30
        "NFO:NIFTY26NOV23700PE": {"last_price": 31.4},  # Closest >= 30
        "NFO:NIFTY26NOV23600PE": {"last_price": 24.0},
    }

    result = find_strangle_strikes(
        mock_kite,
        underlying="NIFTY",
        target_price=30.0,
        target_date=date(2026, 11, 2)
    )

    assert result["ce"]["symbol"] == "NIFTY26NOV24200CE"
    assert result["ce"]["ltp"] == 32.1
    assert result["ce"]["strike"] == 24200.0
    assert result["ce"]["sl_price"] == 48.15  # 32.1 * 1.5
    assert result["ce"]["target_price"] == 6.42  # 32.1 * 0.2
    assert result["pe"]["symbol"] == "NIFTY26NOV23700PE"
    assert result["pe"]["ltp"] == 31.4
    assert result["pe"]["strike"] == 23700.0
    assert result["pe"]["sl_price"] == 47.1  # 31.4 * 1.5
    assert result["pe"]["target_price"] == 6.28  # 31.4 * 0.2
    assert result["lot_size"] == 65
    assert result["total_lots"] == 10
    assert result["total_quantity"] == 650

def test_find_strangle_strikes_no_contract_raises():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24000CE", "lot_size": 65},
    ]
    with pytest.raises(ValueError, match="No CE/PE option contracts found"):
        find_strangle_strikes(mock_kite, target_date=date(2026, 11, 2))

def test_find_strangle_strikes_no_strike_above_target_price():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24200.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24200CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 65},
    ]
    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24200CE": {"last_price": 25.0},
        "NFO:NIFTY26NOV23700PE": {"last_price": 35.0},
    }
    with pytest.raises(ValueError, match="No CE strike found with LTP >= 30.0"):
        find_strangle_strikes(mock_kite, target_date=date(2026, 11, 2))


def test_find_strangle_strikes_auth_failure_raises():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24200.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24200CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 65},
    ]
    mock_kite.ltp.side_effect = Exception("Incorrect `api_key` or `access_token`.")
    with pytest.raises(Exception, match="Incorrect `api_key` or `access_token`"):
        find_strangle_strikes(mock_kite, target_date=date(2026, 11, 2))


def test_fetch_daily_atr_success():
    mock_kite = MagicMock()
    # 5 daily candles with high-low of ~160 points
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 11, 1), "open": 24000.0, "high": 24150.0, "low": 23990.0, "close": 24100.0},
        {"date": datetime(2026, 11, 2), "open": 24100.0, "high": 24260.0, "low": 24080.0, "close": 24200.0},
        {"date": datetime(2026, 11, 3), "open": 24200.0, "high": 24350.0, "low": 24180.0, "close": 24300.0},
    ]
    atr = fetch_daily_atr(mock_kite, instrument_token=256265, period=14)
    assert 160.0 <= atr <= 180.0


def test_fetch_daily_atr_fallback_on_error():
    mock_kite = MagicMock()
    mock_kite.historical_data.side_effect = RuntimeError("Historical API down")
    atr = fetch_daily_atr(mock_kite, instrument_token=256265, period=14)
    assert atr == 150.0  # safe fallback default


def test_find_strangle_strikes_atr_mode_success():
    mock_kite = MagicMock()
    # Spot is 24000.0, ATR is 160.0 -> target offset = 160 * 1.25 = 200 pts
    # Target CE strike = 24200.0, Target PE strike = 23800.0
    mock_kite.ltp.side_effect = lambda query: {
        "NSE:NIFTY 50": {"last_price": 24000.0},
        "NFO:NIFTY26NOV24200CE": {"last_price": 28.5},
        "NFO:NIFTY26NOV23800PE": {"last_price": 31.0},
    } if any("NSE:" in q for q in query) else {
        "NFO:NIFTY26NOV24200CE": {"last_price": 28.5},
        "NFO:NIFTY26NOV23800PE": {"last_price": 31.0},
    }
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 11, 1), "open": 24000.0, "high": 24160.0, "low": 24000.0, "close": 24100.0},
        {"date": datetime(2026, 11, 2), "open": 24100.0, "high": 24260.0, "low": 24100.0, "close": 24200.0},
    ]
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24200.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV24200CE", "lot_size": 65},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23800.0, "expiry": date(2026, 11, 3), "tradingsymbol": "NIFTY26NOV23800PE", "lot_size": 65},
    ]

    result = find_strangle_strikes(
        mock_kite,
        underlying="NIFTY",
        strike_mode="ATR",
        atr_multiplier=1.25,
        target_date=date(2026, 11, 2)
    )

    assert result["ce"]["strike"] == 24200.0
    assert result["pe"]["strike"] == 23800.0
    assert result["ce"]["symbol"] == "NIFTY26NOV24200CE"
    assert result["pe"]["symbol"] == "NIFTY26NOV23800PE"
    assert result["ce"]["ltp"] == 28.5
    assert result["pe"]["ltp"] == 31.0

