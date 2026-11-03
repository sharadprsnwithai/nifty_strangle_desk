import json
from unittest.mock import patch, MagicMock
import pytest
from main import run_cli, build_parser

@pytest.fixture
def mock_kite_summary():
    return {
        "net_positions": [{"symbol": "NIFTY26NOV24500CE", "quantity": 50, "pnl": 500.0, "exchange": "NFO", "product": "NRML", "buy_price": 100.0, "sell_price": 0.0, "last_price": 110.0}],
        "day_positions": [{"symbol": "NIFTY26NOV24500CE", "quantity": 50, "pnl": 500.0, "exchange": "NFO", "product": "MIS", "buy_price": 100.0, "sell_price": 0.0, "last_price": 110.0}],
        "totals": {"total_pnl": 500.0, "total_realised": 0.0, "total_unrealised": 500.0, "total_m2m": 500.0, "open_positions_count": 1, "closed_positions_count": 0}
    }

def test_cli_json_output(capsys, mock_kite_summary):
    with patch("main.load_config"), \
         patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions") as mock_fetch, \
         patch("main.summarize_positions", return_value=mock_kite_summary):
        
        mock_fetch.return_value = {"net": [], "day": []}
        
        exit_code = run_cli(["--json"])
        assert exit_code == 0
        
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["totals"]["total_pnl"] == 500.0

def test_cli_table_output(capsys, mock_kite_summary):
    with patch("main.load_config"), \
         patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions") as mock_fetch, \
         patch("main.summarize_positions", return_value=mock_kite_summary):
        
        mock_fetch.return_value = {"net": [], "day": []}
        
        exit_code = run_cli([])
        assert exit_code == 0
        
        captured = capsys.readouterr()
        assert "NIFTY26NOV24500CE" in captured.out
        assert "PORTFOLIO SUMMARY" in captured.out

def test_cli_day_view_output(capsys, mock_kite_summary):
    with patch("main.load_config"), \
         patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions") as mock_fetch, \
         patch("main.summarize_positions", return_value=mock_kite_summary):
        
        mock_fetch.return_value = {"net": [], "day": []}
        
        exit_code = run_cli(["--day"])
        assert exit_code == 0
        
        captured = capsys.readouterr()
        assert "DAY POSITIONS (Intraday)" in captured.out

def test_cli_force_login_flag():
    with patch("main.load_config"), \
         patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions", return_value={"net": [], "day": []}), \
         patch("main.summarize_positions", return_value={"net_positions": [], "day_positions": [], "totals": {"total_realised": 0, "total_unrealised": 0, "total_m2m": 0, "total_pnl": 0, "open_positions_count": 0, "closed_positions_count": 0}}):
        
        exit_code = run_cli(["--force-login"])
        assert exit_code == 0
        mock_client.assert_called_once()
        assert mock_client.call_args.kwargs.get("force_login") is True

def test_cli_config_error(capsys):
    with patch("main.load_config", side_effect=ValueError("Missing credentials")):
        exit_code = run_cli([])
        assert exit_code == 1

def test_cli_open_only_and_net_flags(capsys, mock_kite_summary):
    with patch("main.load_config"), \
         patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions") as mock_fetch, \
         patch("main.summarize_positions", return_value=mock_kite_summary), \
         patch("main.format_positions_table", return_value="FORMATTED_TABLE") as mock_fmt:
        
        mock_fetch.return_value = {"net": [], "day": []}
        
        exit_code = run_cli(["--net", "--open-only"])
        assert exit_code == 0
        mock_fmt.assert_called_once_with(mock_kite_summary, view="net", open_only=True)
        captured = capsys.readouterr()
        assert "FORMATTED_TABLE" in captured.out

def test_cli_positions_fetch_error(capsys):
    with patch("main.load_config"), \
         patch("main.get_kite_client"), \
         patch("main.fetch_positions", side_effect=RuntimeError("Kite network error")):
        exit_code = run_cli([])
        assert exit_code == 1
