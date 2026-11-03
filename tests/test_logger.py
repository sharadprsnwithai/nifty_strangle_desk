import logging
from pathlib import Path
import pytest
from strategy.logger import setup_logging


def test_setup_logging_creates_file_and_directory(tmp_path):
    log_dir = tmp_path / "custom_logs"
    log_file = "test_run.log"

    logger = setup_logging(log_dir=log_dir, log_level="DEBUG", log_filename=log_file)
    target_path = log_dir / log_file

    assert log_dir.exists()
    assert target_path.exists()

    logger.info("Test info message from strategy")
    logger.debug("Test debug message with details")

    content = target_path.read_text(encoding="utf-8")
    assert "Test info message from strategy" in content
    assert "Test debug message with details" in content
    assert "[DEBUG]" in content
    assert "[INFO]" in content


def test_setup_logging_respects_log_level(tmp_path):
    log_dir = tmp_path / "logs_level"
    log_file = "level_test.log"

    logger = setup_logging(log_dir=log_dir, log_level="WARNING", log_filename=log_file)
    target_path = log_dir / log_file

    logger.info("This should be suppressed")
    logger.warning("This warning should appear")

    content = target_path.read_text(encoding="utf-8")
    assert "This should be suppressed" not in content
    assert "This warning should appear" in content
    assert "[WARNING]" in content


def test_setup_logging_logs_exception_traceback(tmp_path):
    log_dir = tmp_path / "logs_exc"
    log_file = "exc_test.log"

    logger = setup_logging(log_dir=log_dir, log_level="INFO", log_filename=log_file)
    target_path = log_dir / log_file

    try:
        raise ValueError("Critical execution failure occurred")
    except Exception:
        logger.exception("Handled unexpected error in monitoring loop")

    content = target_path.read_text(encoding="utf-8")
    assert "Handled unexpected error in monitoring loop" in content
    assert "Critical execution failure occurred" in content
    assert "Traceback (most recent call last):" in content
