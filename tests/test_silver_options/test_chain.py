from datetime import date
from unittest.mock import MagicMock
from strategy.silver_options.chain import (
    get_strike_step,
    round_to_strike_step,
    find_silver_strangle_strikes,
)


def test_strike_step_calculation():
    assert get_strike_step(150000.0) == 1000.0
    assert get_strike_step(90000.0) == 500.0
    assert round_to_strike_step(153620.0, 1000.0) == 154000.0
    assert round_to_strike_step(153400.0, 1000.0) == 153000.0


def test_find_silver_strangle_strikes_with_mock_data():
    today = date(2026, 11, 23)
    expiry = date(2026, 11, 27)

    mock_instruments = [
        {"name": "SILVERM", "instrument_type": "FUT", "tradingsymbol": "SILVERM26NOVFUT", "expiry": expiry, "instrument_token": 1001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM26NOV157000CE", "strike": 157000.0, "expiry": expiry, "instrument_token": 2001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM26NOV158000CE", "strike": 158000.0, "expiry": expiry, "instrument_token": 2002},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM26NOV153000PE", "strike": 153000.0, "expiry": expiry, "instrument_token": 3001},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM26NOV152000PE", "strike": 152000.0, "expiry": expiry, "instrument_token": 3002},
    ]

    mock_kite = MagicMock()
    mock_kite.instruments.return_value = mock_instruments
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": 155000.0},
        "MCX:SILVERM26NOV157000CE": {"last_price": 450.0},
        "MCX:SILVERM26NOV153000PE": {"last_price": 480.0},
    }

    result = find_silver_strangle_strikes(
        kite=mock_kite,
        underlying="SILVERM",
        strike_otm_pct=0.015,  # 1.5% of 155k is ~2325 -> CE 157000, PE 153000
        exchange="MCX",
        instruments=mock_instruments,
    )

    assert result["underlying"] == "SILVERM"
    assert result["spot_price"] == 155000.0
    assert result["ce_strike"] == 157000.0
    assert result["pe_strike"] == 153000.0
    assert result["ce_symbol"] == "SILVERM26NOV157000CE"
    assert result["pe_symbol"] == "SILVERM26NOV153000PE"
    assert result["ce_ltp"] == 450.0
    assert result["pe_ltp"] == 480.0


def test_find_silver_strangle_strikes_fallback():
    # Without Kite or instruments, falls back gracefully to analytical model
    result = find_silver_strangle_strikes(
        kite=None,
        underlying="SILVERM",
        strike_otm_pct=0.015,
        mock_spot=150000.0,
    )
    assert result["spot_price"] == 150000.0
    assert result["ce_strike"] > 150000.0
    assert result["pe_strike"] < 150000.0
    assert result["ce_ltp"] > 0
    assert result["pe_ltp"] > 0


def test_find_silver_strangle_strikes_skips_low_oi_and_wide_spread():
    expiry = date(2026, 11, 27)
    mock_instruments = [
        {"name": "SILVERM", "instrument_type": "FUT", "tradingsymbol": "SILVERM26NOVFUT", "expiry": expiry, "instrument_token": 1001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM26NOV157000CE", "strike": 157000.0, "expiry": expiry, "instrument_token": 2001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM26NOV158000CE", "strike": 158000.0, "expiry": expiry, "instrument_token": 2002},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM26NOV153000PE", "strike": 153000.0, "expiry": expiry, "instrument_token": 3001},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM26NOV152000PE", "strike": 152000.0, "expiry": expiry, "instrument_token": 3002},
    ]

    mock_kite = MagicMock()
    mock_kite.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": {"last_price": 155000.0}},
        {
            # Target CE (157000CE) has OI < 100 -> must skip!
            "MCX:SILVERM26NOV157000CE": {
                "last_price": 450.0, "oi": 35,
                "depth": {"buy": [{"price": 448.0}], "sell": [{"price": 452.0}]},
            },
            # Next CE (158000CE) has OI >= 100 and tight spread -> select!
            "MCX:SILVERM26NOV158000CE": {
                "last_price": 390.0, "oi": 240,
                "depth": {"buy": [{"price": 389.0}], "sell": [{"price": 392.0}]},
            },
            # Target PE (153000PE) has OI >= 100 but huge 12% spread -> must skip!
            "MCX:SILVERM26NOV153000PE": {
                "last_price": 480.0, "oi": 150,
                "depth": {"buy": [{"price": 450.0}], "sell": [{"price": 510.0}]},
            },
            # Next PE (152000PE) has OI >= 100 and tight spread -> select!
            "MCX:SILVERM26NOV152000PE": {
                "last_price": 410.0, "oi": 180,
                "depth": {"buy": [{"price": 408.0}], "sell": [{"price": 412.0}]},
            },
        },
    ]

    result = find_silver_strangle_strikes(
        kite=mock_kite,
        underlying="SILVERM",
        strike_otm_pct=0.015,
        exchange="MCX",
        instruments=mock_instruments,
        allow_fallback=False,
        min_oi=100,
        max_spread_pct=0.03,
    )
    assert result["ce_symbol"] == "SILVERM26NOV158000CE"
    assert result["ce_oi"] == 240
    assert result["ce_bid"] == 389.0
    assert result["pe_symbol"] == "SILVERM26NOV152000PE"
    assert result["pe_oi"] == 180
    assert result["pe_bid"] == 408.0


def test_find_silver_strangle_strikes_raises_when_no_liquid_strike_in_live():
    import pytest
    expiry = date(2026, 11, 27)
    mock_instruments = [
        {"name": "SILVERM", "instrument_type": "FUT", "tradingsymbol": "SILVERM26NOVFUT", "expiry": expiry, "instrument_token": 1001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM26NOV157000CE", "strike": 157000.0, "expiry": expiry, "instrument_token": 2001},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM26NOV153000PE", "strike": 153000.0, "expiry": expiry, "instrument_token": 3001},
    ]
    mock_kite = MagicMock()
    mock_kite.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": {"last_price": 155000.0}},
        {
            "MCX:SILVERM26NOV157000CE": {"last_price": 450.0, "oi": 15},
            "MCX:SILVERM26NOV153000PE": {"last_price": 480.0, "oi": 20},
        },
    ]
    with pytest.raises(RuntimeError, match="No liquid CE strike"):
        find_silver_strangle_strikes(
            kite=mock_kite,
            underlying="SILVERM",
            instruments=mock_instruments,
            allow_fallback=False,
            min_oi=100,
        )
