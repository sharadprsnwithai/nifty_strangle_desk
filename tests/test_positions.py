from unittest.mock import MagicMock
import pytest
from kite.positions import fetch_positions, summarize_positions, format_positions_table

@pytest.fixture
def sample_positions_payload():
    return {
        "net": [
            {
                "tradingsymbol": "NIFTY26NOV24500CE",
                "exchange": "NFO",
                "instrument_token": 123456,
                "product": "NRML",
                "quantity": -50,
                "buy_quantity": 0,
                "buy_price": 0.0,
                "sell_quantity": 50,
                "sell_price": 120.0,
                "last_price": 95.0,
                "pnl": 1250.0,
                "m2m": 1250.0,
                "realised": 0.0,
                "unrealised": 1250.0,
            },
            {
                "tradingsymbol": "BANKNIFTY26NOV52000PE",
                "exchange": "NFO",
                "instrument_token": 654321,
                "product": "MIS",
                "quantity": 0,
                "buy_quantity": 30,
                "buy_price": 200.0,
                "sell_quantity": 30,
                "sell_price": 220.0,
                "last_price": 215.0,
                "pnl": 600.0,
                "m2m": 0.0,
                "realised": 600.0,
                "unrealised": 0.0,
            }
        ],
        "day": [
            {
                "tradingsymbol": "BANKNIFTY26NOV52000PE",
                "exchange": "NFO",
                "instrument_token": 654321,
                "product": "MIS",
                "quantity": 0,
                "buy_quantity": 30,
                "buy_price": 200.0,
                "sell_quantity": 30,
                "sell_price": 220.0,
                "last_price": 215.0,
                "pnl": 600.0,
                "m2m": 0.0,
                "realised": 600.0,
                "unrealised": 0.0,
            }
        ]
    }

def test_fetch_positions_calls_kite():
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {"net": [], "day": []}
    res = fetch_positions(mock_kite)
    assert res == {"net": [], "day": []}
    mock_kite.positions.assert_called_once()

def test_summarize_positions(sample_positions_payload):
    summary = summarize_positions(sample_positions_payload)
    
    assert summary["totals"]["total_realised"] == 600.0
    assert summary["totals"]["total_unrealised"] == 1250.0
    assert summary["totals"]["total_pnl"] == 1850.0
    assert summary["totals"]["open_positions_count"] == 1
    assert summary["totals"]["closed_positions_count"] == 1
    assert len(summary["net_positions"]) == 2
    assert len(summary["day_positions"]) == 1

def test_format_positions_table(sample_positions_payload):
    summary = summarize_positions(sample_positions_payload)
    table_text = format_positions_table(summary, view="net", open_only=False)
    
    assert "NIFTY26NOV24500CE" in table_text
    assert "BANKNIFTY26NOV52000PE" in table_text
    assert "Total Net P&L" in table_text
    assert "+1850.00" in table_text

def test_format_positions_table_open_only(sample_positions_payload):
    summary = summarize_positions(sample_positions_payload)
    table_text = format_positions_table(summary, view="net", open_only=True)
    
    assert "NIFTY26NOV24500CE" in table_text
    # Closed position should be filtered out
    assert "BANKNIFTY26NOV52000PE" not in table_text

def test_empty_positions():
    empty_payload = {"net": [], "day": []}
    summary = summarize_positions(empty_payload)
    assert summary["totals"]["total_pnl"] == 0.0
    table_text = format_positions_table(summary, view="net", open_only=False)
    assert "No positions found" in table_text
