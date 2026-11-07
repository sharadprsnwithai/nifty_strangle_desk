import os
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from strategy.fo_ban import (
    filter_banned_stocks,
    get_nse_fo_ban_list,
    parse_fo_ban_csv,
)
from strategy.orb_desk.config import ORBConfig, TARGET_STOCKS
from strategy.orb_desk.desk import ORBDesk
from strategy.orb_desk.order_manager import ORBOrderManager


def test_parse_fo_ban_csv_standard():
    csv_text = """
Securities in Ban For Trade Date 08-NOV-2026:
1,KAYNES
2,IDEA
3,MANAPPURAM
"""
    result = parse_fo_ban_csv(csv_text)
    assert result == {"KAYNES", "IDEA", "MANAPPURAM"}


def test_parse_fo_ban_csv_empty_or_header_only():
    assert parse_fo_ban_csv("") == set()
    assert parse_fo_ban_csv("Securities in Ban For Trade Date 08-NOV-2026:\n") == set()
    assert parse_fo_ban_csv("No Securities in Ban Period") == set()


def test_filter_banned_stocks():
    stocks = [
        {"symbol": "NSE:RELIANCE", "token": 738561},
        {"symbol": "NSE:KAYNES", "token": 12345},
        {"symbol": "NSE:INFY", "token": 408065},
    ]
    banned = {"KAYNES"}
    allowed, excluded = filter_banned_stocks(stocks, banned)

    assert len(allowed) == 2
    assert [s["symbol"] for s in allowed] == ["NSE:RELIANCE", "NSE:INFY"]
    assert excluded == ["NSE:KAYNES"]


def test_filter_banned_stocks_none_banned():
    stocks = [{"symbol": "NSE:RELIANCE", "token": 738561}]
    allowed, excluded = filter_banned_stocks(stocks, set())
    assert allowed == stocks
    assert excluded == []


def test_get_nse_fo_ban_list_caching(tmp_path):
    today = date(2026, 11, 7)
    cache_file = tmp_path / "fo_secban_2026-11-07.txt"
    cache_file.write_text("KAYNES\nIDEA\n", encoding="utf-8")

    with patch("requests.get") as mock_get:
        banned = get_nse_fo_ban_list(check_date=today, cache_dir=tmp_path, use_cache=True)
        # Should read from cache without making network request
        mock_get.assert_not_called()
        assert banned == {"KAYNES", "IDEA"}


def test_get_nse_fo_ban_list_network_failure_fallback(tmp_path):
    today = date(2026, 11, 7)

    with patch("requests.get", side_effect=Exception("Connection timed out")):
        banned = get_nse_fo_ban_list(check_date=today, cache_dir=tmp_path, use_cache=False)
        assert banned == set()


def test_get_nse_fo_ban_list_env_override(tmp_path):
    today = date(2026, 11, 7)
    with patch.dict(os.environ, {"FO_BAN_OVERRIDE": "TATAMOTORS,SBIN"}):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "1,KAYNES\n"
            mock_get.return_value = mock_resp

            banned = get_nse_fo_ban_list(check_date=today, cache_dir=tmp_path, use_cache=False)
            assert "KAYNES" in banned
            assert "TATAMOTORS" in banned
            assert "SBIN" in banned


def test_orb_desk_filters_banned_stocks(tmp_path):
    cfg = ORBConfig(state_file=str(tmp_path / "orb_state.json"))
    order_mgr = ORBOrderManager(mode="PAPER")
    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"ohlc": {"open": 24500.0}}}

    with patch("strategy.orb_desk.desk.get_nse_fo_ban_list", return_value={"IDEA", "SAIL"}):
        desk = ORBDesk(kite=mock_kite, order_manager=order_mgr, config=cfg)
        desk.initialize_day(force=True)

        # IDEA and SAIL should be excluded from target_stocks
        symbols = [s["symbol"] for s in desk.target_stocks]
        assert "IDEA" not in symbols
        assert "SAIL" not in symbols
        assert sorted(desk.banned_stocks_today) == ["IDEA", "SAIL"]
        assert len(desk.target_stocks) == len(TARGET_STOCKS) - 2
