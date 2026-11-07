import os
import sys
import logging
import datetime
from pathlib import Path
import pytest

from strategy.logger import (
    setup_logging,
    archive_log_file,
    archive_log_if_stale,
    purge_old_logs,
    daily_log_namer,
    DailyRetentionRotatingFileHandler,
    DEFAULT_RETENTION_DAYS,
)


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


def test_daily_log_namer():
    default_name = "/var/log/strangle_desk.log.2026-11-06"
    named = daily_log_namer(default_name)
    assert named.endswith("strangle_desk_2026-11-06.log")


def test_daily_rotation_handler_rollover_with_timestamp(tmp_path):
    log_path = tmp_path / "desk.log"
    handler = DailyRetentionRotatingFileHandler(
        filename=log_path,
        when="midnight",
        backupCount=10,
        max_age_days=10,
    )

    test_logger = logging.getLogger("test_daily_rot")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)

    test_logger.info("Message day 1")
    handler.doRollover()
    test_logger.info("Message day 2")

    handler.close()
    test_logger.removeHandler(handler)

    files = [f.name for f in tmp_path.iterdir()]
    assert "desk.log" in files
    # Should have an archived file with date
    archived = [f for f in files if f != "desk.log"]
    assert len(archived) == 1
    assert archived[0].startswith("desk_") and archived[0].endswith(".log")


def test_archive_log_if_stale(tmp_path):
    log_path = tmp_path / "strangle_desk.log"
    log_path.write_text("Old log data from yesterday", encoding="utf-8")

    # Set mtime to 2 days ago
    past_time = (datetime.datetime.now() - datetime.timedelta(days=2)).timestamp()
    os.utime(log_path, (past_time, past_time))

    archived = archive_log_if_stale(log_path)
    assert archived is not None
    assert archived.exists()
    assert not log_path.exists()
    assert "Old log data" in archived.read_text(encoding="utf-8")


def test_archive_log_if_stale_ignores_today(tmp_path):
    log_path = tmp_path / "strangle_desk.log"
    log_path.write_text("Today log data", encoding="utf-8")

    # mtime is today
    archived = archive_log_if_stale(log_path)
    assert archived is None
    assert log_path.exists()


def test_purge_old_logs_deletes_older_than_10_days(tmp_path):
    today = datetime.date(2026, 11, 7)

    # 1. Active file (must be preserved)
    active = tmp_path / "strangle_desk.log"
    active.write_text("active")

    # 2. .gitkeep (must be preserved)
    gitkeep = tmp_path / ".gitkeep"
    gitkeep.write_text("")

    # 3. Recent log (5 days old, must be preserved)
    recent = tmp_path / "strangle_desk_2026-11-02.log"
    recent.write_text("recent")

    # 4. 10 days old log (boundary case, preserved)
    boundary = tmp_path / "strangle_desk_2026-10-28.log"
    boundary.write_text("boundary 10 days")

    # 5. 11 days old log (older than 10 days, MUST be deleted)
    expired1 = tmp_path / "strangle_desk_2026-10-27.log"
    expired1.write_text("expired 11 days")

    # 6. 30 days old log (older than 10 days, MUST be deleted)
    expired2 = tmp_path / "strangle_desk_2026-10-08.log"
    expired2.write_text("expired 30 days")

    # 7. Old file without date in name, but mtime 15 days ago (MUST be deleted)
    expired_mtime = tmp_path / "old_backup.log"
    expired_mtime.write_text("expired mtime")
    past_ts = (datetime.datetime(2026, 11, 7) - datetime.timedelta(days=15)).timestamp()
    os.utime(expired_mtime, (past_ts, past_ts))

    deleted = purge_old_logs(tmp_path, max_age_days=10, reference_date=today)

    deleted_names = {d.name for d in deleted}
    assert "strangle_desk_2026-10-27.log" in deleted_names
    assert "strangle_desk_2026-10-08.log" in deleted_names
    assert "old_backup.log" in deleted_names

    remaining_names = {f.name for f in tmp_path.iterdir()}
    assert "strangle_desk.log" in remaining_names
    assert ".gitkeep" in remaining_names
    assert "strangle_desk_2026-11-02.log" in remaining_names
    assert "strangle_desk_2026-10-28.log" in remaining_names
    assert "strangle_desk_2026-10-27.log" not in remaining_names
    assert "strangle_desk_2026-10-08.log" not in remaining_names
    assert "old_backup.log" not in remaining_names
