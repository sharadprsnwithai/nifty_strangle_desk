from unittest.mock import patch, MagicMock
from datetime import time, date
import pytest
import pandas as pd

from scripts.run_silver_desk import (
    build_parser,
    main,
    fetch_silver_market_data,
    execute_single_tick,
    display_status,
)
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.engine import SilverOptionsEngine
from strategy.silver_options.models import OptionLeg, SpreadPosition, SpreadType


def test_build_parser_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.live is False
    assert args.paper is False
    assert args.once is False
    assert args.status is False


def test_build_parser_live():
    parser = build_parser()
    args = parser.parse_args(["--live"])
    assert args.live is True
    assert args.paper is False


def test_build_parser_paper():
    parser = build_parser()
    args = parser.parse_args(["--paper"])
    assert args.paper is True
    assert args.live is False


def test_build_parser_once():
    parser = build_parser()
    args = parser.parse_args(["--once"])
    assert args.once is True
    assert args.status is False


def test_build_parser_status():
    parser = build_parser()
    args = parser.parse_args(["--status"])
    assert args.status is True
    assert args.once is False


def test_build_parser_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--paper", "--live"])


def test_main_paper_default():
    mock_kite = MagicMock()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main([])
        assert engine.is_paper is True
        assert engine.config.symbol == "SILVERM"
        assert engine.kite_client == mock_kite


def test_main_paper_explicit():
    mock_kite = MagicMock()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main(["--paper"])
        assert engine.is_paper is True
        assert engine.kite_client == mock_kite


def test_main_live():
    mock_kite = MagicMock()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main(["--live"])
        assert engine.is_paper is False
        assert engine.kite_client == mock_kite


def test_main_paper_mode_without_kite_credentials():
    with patch("scripts.run_silver_desk.get_kite_client", side_effect=ValueError("Missing required Kite credentials")):
        engine = main(["--paper"])
        assert engine.is_paper is True
        assert engine.kite_client == None
        assert engine.config.symbol == "SILVERM"


def test_main_live_mode_without_kite_credentials_raises():
    with patch("scripts.run_silver_desk.get_kite_client", side_effect=ValueError("Missing required Kite credentials")):
        with pytest.raises(ValueError, match="Missing required Kite credentials"):
            main(["--live"])


def _build_mock_kite_for_silver():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {
            "instrument_token": 1001,
            "tradingsymbol": "SILVERM26NOVFUT",
            "instrument_type": "FUT",
            "segment": "MCX-FUT",
            "name": "SILVERM",
            "expiry": "2026-11-26",
            "strike": None,
        },
        {
            "instrument_token": 1002,
            "tradingsymbol": "SILVERM26NOV75000CE",
            "instrument_type": "CE",
            "strike": 75000,
            "segment": "MCX-OPT",
            "name": "SILVERM",
            "expiry": "2026-11-26",
        },
        {
            "instrument_token": 1003,
            "tradingsymbol": "SILVERM26NOV75000PE",
            "instrument_type": "PE",
            "strike": 75000,
            "segment": "MCX-OPT",
            "name": "SILVERM",
            "expiry": "2026-11-26",
        },
    ]
    mock_kite.historical_data.return_value = [
        {
            "date": "2026-11-20 10:00:00",
            "open": 75000.0,
            "high": 75050.0,
            "low": 74950.0,
            "close": 75000.0,
            "volume": 100,
        }
        for _ in range(80)
    ]
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": 75000.0},
        "MCX:SILVERM26NOV75000CE": {"last_price": 1200.0, "oi": 500},
        "MCX:SILVERM26NOV75000PE": {"last_price": 1100.0, "oi": 600},
    }
    return mock_kite


def test_main_once_executes_single_tick_with_mock_kite():
    mock_kite = _build_mock_kite_for_silver()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main(["--once"])
        assert isinstance(engine, SilverOptionsEngine)
        assert engine.is_paper is True
        mock_kite.instruments.assert_called_once_with("MCX")
        assert mock_kite.historical_data.called
        assert mock_kite.quote.called


def test_main_once_paper_mode_without_credentials():
    with patch("scripts.run_silver_desk.get_kite_client", side_effect=ValueError("Missing credentials")):
        engine = main(["--once"])
        assert isinstance(engine, SilverOptionsEngine)
        assert engine.is_paper is True
        assert engine.kite_client is None


def test_main_status_with_mock_kite():
    mock_kite = _build_mock_kite_for_silver()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main(["--status"])
        assert isinstance(engine, SilverOptionsEngine)
        assert engine.is_paper is True


def test_main_once_and_status_combined():
    mock_kite = _build_mock_kite_for_silver()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main(["--once", "--status"])
        assert isinstance(engine, SilverOptionsEngine)
        assert mock_kite.instruments.called


def test_display_status_output():
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # Initially empty
    display_status(engine)
    assert engine.active_position is None

    # Populate active and closed positions
    leg = OptionLeg(
        strike=75000,
        option_type="PE",
        action="SELL",
        quantity=5,
        premium=1000.0,
        trading_symbol="SILVERM26NOV75000PE",
    )
    active_pos = SpreadPosition(
        spread_type=SpreadType.SHORT_PUT,
        legs=[leg],
        entry_net_credit=5000.0,
        sl_price=1500.0,
        status="ACTIVE",
    )
    engine.active_position = active_pos

    closed_pos = SpreadPosition(
        spread_type=SpreadType.SHORT_CALL,
        legs=[leg],
        entry_net_credit=4000.0,
        realized_pnl=1250.0,
        status="CLOSED",
        exit_reason="PROFIT_TARGET",
    )
    engine.closed_positions.append(closed_pos)

    # Should execute without exceptions and log positions
    display_status(engine)
    assert len(engine.closed_positions) == 1


def test_fetch_silver_market_data_fallback():
    mock_kite = MagicMock()
    mock_kite.instruments.side_effect = RuntimeError("Kite API connection timeout")
    cfg = SilverConfig()
    spot, df_5m, chain = fetch_silver_market_data(mock_kite, cfg)
    assert spot == 0.0
    assert df_5m.empty
    assert chain == []


def test_execute_single_tick_entry_flow():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {
            "instrument_token": 2001,
            "tradingsymbol": "SILVERM26NOVFUT",
            "instrument_type": "FUT",
            "segment": "MCX-FUT",
            "name": "SILVERM",
            "expiry": "2026-11-26",
        },
        {
            "instrument_token": 2002,
            "tradingsymbol": "SILVERM26NOV74000PE",
            "instrument_type": "PE",
            "strike": 74000,
            "segment": "MCX-OPT",
            "name": "SILVERM",
            "expiry": "2026-11-26",
        },
    ]
    # Generate uptrend bars (Williams %R > -20, 5 > 15 > 50 EMA, high breakout)
    n_bars = 80
    base = 75000.0
    closes = [base + i * 15.0 for i in range(n_bars)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    mock_candles = [
        {"date": f"2026-11-20 10:{i:02d}:00", "open": closes[i], "high": highs[i], "low": lows[i], "close": closes[i], "volume": 100}
        for i in range(n_bars)
    ]
    mock_kite.historical_data.return_value = mock_candles
    mock_kite.quote.return_value = {
        "MCX:SILVERM26NOVFUT": {"last_price": closes[-1]},
        "MCX:SILVERM26NOV74000PE": {"last_price": 1000.0, "oi": 2500},
    }

    cfg = SilverConfig(session_start=time(9, 15), session_end=time(18, 0))
    engine = SilverOptionsEngine(kite_client=mock_kite, config=cfg, is_paper=True)

    execute_single_tick(engine, mock_kite, cfg, current_time=time(10, 0))
    assert engine.active_position is not None
    assert engine.active_position.spread_type == SpreadType.SHORT_PUT
