from unittest.mock import patch, MagicMock
import pytest
from scripts.run_silver_desk import build_parser, main


def test_build_parser_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.live is False
    assert args.paper is False


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
        assert engine.kite_client is None
        assert engine.config.symbol == "SILVERM"


def test_main_live_mode_without_kite_credentials_raises():
    with patch("scripts.run_silver_desk.get_kite_client", side_effect=ValueError("Missing required Kite credentials")):
        with pytest.raises(ValueError, match="Missing required Kite credentials"):
            main(["--live"])
