from unittest.mock import patch, MagicMock
import pytest
from scripts.run_silver_desk import build_parser, main


def test_build_parser_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.paper is True
    assert args.live is False


def test_build_parser_live():
    parser = build_parser()
    args = parser.parse_args(["--live"])
    assert args.live is True


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


def test_main_live():
    mock_kite = MagicMock()
    with patch("scripts.run_silver_desk.get_kite_client", return_value=mock_kite):
        engine = main(["--live"])
        assert engine.is_paper is False
