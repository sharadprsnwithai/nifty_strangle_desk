import os
import re
import sys
import logging
from datetime import datetime, date, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Union, List, Set

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_LOG_FILE = "strangle_desk.log"
DEFAULT_RETENTION_DAYS = 10

ACTIVE_LOG_NAMES: Set[str] = {
    "strangle_desk.log",
    "orb_desk.log",
    "positional_condor.log",
}


def daily_log_namer(default_name: str) -> str:
    """
    Transforms default TimedRotatingFileHandler rotated filename:
    'path/to/strangle_desk.log.2026-11-06' -> 'path/to/strangle_desk_2026-11-06.log'
    """
    dir_name, base_name = os.path.split(default_name)
    parts = base_name.rsplit(".", 2)
    if len(parts) == 3:
        stem, ext, date_suffix = parts
        return os.path.join(dir_name, f"{stem}_{date_suffix}.{ext}")
    return default_name


def archive_log_file(
    log_path: Union[str, Path],
    archive_date: Optional[date] = None,
    include_time: bool = False,
) -> Optional[Path]:
    """
    Archives a log file by renaming it to `<stem>_<timestamp>.<ext>`.

    Args:
        log_path: Path to the target log file.
        archive_date: Date to embed in filename. Defaults to file's mtime date.
        include_time: If True, appends HHMMSS to the date string.

    Returns:
        The archived Path, or None if the source file was empty or missing.
    """
    path = Path(log_path)
    if not path.exists() or path.stat().st_size == 0:
        return None

    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    dt = archive_date or mtime.date()

    if include_time:
        suffix_str = mtime.strftime("%Y-%m-%d_%H%M%S")
    else:
        suffix_str = dt.strftime("%Y-%m-%d")

    target_name = f"{path.stem}_{suffix_str}{path.suffix}"
    archived_path = path.parent / target_name

    counter = 1
    while archived_path.exists():
        archived_path = path.parent / f"{path.stem}_{suffix_str}_{counter}{path.suffix}"
        counter += 1

    try:
        path.rename(archived_path)
        return archived_path
    except Exception:
        return None


def archive_log_if_stale(log_path: Path) -> Optional[Path]:
    """
    Archives log_path if it exists, is non-empty, and was last modified
    on a previous calendar day (before today).
    """
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None

    mtime = datetime.fromtimestamp(log_path.stat().st_mtime)
    if mtime.date() < date.today():
        return archive_log_file(log_path, archive_date=mtime.date())
    return None


def purge_old_logs(
    log_dir: Union[str, Path],
    max_age_days: int = DEFAULT_RETENTION_DAYS,
    protected_files: Optional[Set[str]] = None,
    reference_date: Optional[date] = None,
) -> List[Path]:
    """
    Scans log_dir and removes log files that are older than max_age_days.

    Protects active log filenames and .gitkeep from deletion.
    Determines log age first by extracting YYYY-MM-DD from the filename,
    falling back to the file modification date (st_mtime).

    Args:
        log_dir: Directory containing log files.
        max_age_days: Maximum age in days before a log file is deleted (default: 10).
        protected_files: Set of file names that must never be deleted.
        reference_date: Reference date for age calculation (default: today).

    Returns:
        List of deleted Path objects.
    """
    target_dir = Path(log_dir)
    if not target_dir.exists() or not target_dir.is_dir():
        return []

    today = reference_date or date.today()
    protected = set(protected_files) if protected_files else ACTIVE_LOG_NAMES | {".gitkeep"}

    deleted_files: List[Path] = []
    date_regex = re.compile(r"(\d{4}-\d{2}-\d{2})")

    for file_path in target_dir.iterdir():
        if not file_path.is_file():
            continue
        if file_path.name in protected:
            continue
        # Only inspect log files or rotated backups
        if not (file_path.suffix in [".log", ".txt"] or ".log" in file_path.name):
            continue

        match = date_regex.search(file_path.name)
        if match:
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                file_date = datetime.fromtimestamp(file_path.stat().st_mtime).date()
        else:
            file_date = datetime.fromtimestamp(file_path.stat().st_mtime).date()

        age_days = (today - file_date).days
        if age_days > max_age_days:
            try:
                file_path.unlink()
                deleted_files.append(file_path)
            except Exception:
                pass

    return deleted_files


class DailyRetentionRotatingFileHandler(TimedRotatingFileHandler):
    """
    TimedRotatingFileHandler that rotates daily at midnight,
    applies clean timestamp naming (<name>_YYYY-MM-DD.log),
    and enforces maximum retention period (deleting logs older than max_age_days).
    """

    def __init__(
        self,
        filename: Union[str, Path],
        when: str = "midnight",
        interval: int = 1,
        backupCount: int = DEFAULT_RETENTION_DAYS,
        encoding: str = "utf-8",
        delay: bool = False,
        utc: bool = False,
        atTime=None,
        errors=None,
        max_age_days: int = DEFAULT_RETENTION_DAYS,
    ):
        super().__init__(
            str(filename),
            when=when,
            interval=interval,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
            utc=utc,
            atTime=atTime,
            errors=errors,
        )
        self.max_age_days = max_age_days
        self.namer = daily_log_namer

    def doRollover(self):
        super().doRollover()
        try:
            purge_old_logs(Path(self.baseFilename).parent, max_age_days=self.max_age_days)
        except Exception:
            pass


def setup_logging(
    log_dir: Optional[Union[str, Path]] = None,
    log_level: str = "INFO",
    log_filename: str = DEFAULT_LOG_FILE,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> logging.Logger:
    """
    Configures application-wide logging with both stdout stream handler and
    a daily-rotating, timestamp-archiving, retention-managed file handler.

    Args:
        log_dir: Directory where log files are stored. Defaults to ./logs.
        log_level: Logging severity ('DEBUG', 'INFO', 'WARNING', 'ERROR').
        log_filename: Filename for the active log file (default: strangle_desk.log).
        retention_days: Number of days to retain rotated log files before deletion (default: 10).

    Returns:
        The configured root logger.
    """
    target_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / log_filename

    # 1. If active log exists from a previous day, archive it with timestamp
    archive_log_if_stale(log_path)

    # 2. Prune any logs older than retention_days
    protected = ACTIVE_LOG_NAMES | {log_filename, ".gitkeep"}
    purge_old_logs(target_dir, max_age_days=retention_days, protected_files=protected)

    numeric_level = getattr(logging, str(log_level).upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers to avoid duplicates on re-configuration
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    # Console Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    root_logger.addHandler(console_handler)

    # Daily Rotating File Handler with Timestamp & Retention
    file_handler = DailyRetentionRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        max_age_days=retention_days,
    )
    file_handler.setLevel(numeric_level)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    root_logger.addHandler(file_handler)

    root_logger.info(
        "Logging configured at level %s. Log file: %s (Daily rollover, %d-day retention)",
        logging.getLevelName(numeric_level),
        log_path,
        retention_days,
    )
    return root_logger


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sharad's Desk Log Archiver & Retention Pruner")
    parser.add_argument("--log-dir", type=str, default=str(DEFAULT_LOG_DIR), help="Directory containing logs")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_RETENTION_DAYS, help="Max retention age in days")
    parser.add_argument("--force-archive", action="store_true", help="Force archive active log files today")

    args = parser.parse_args()
    log_dir = Path(args.log_dir)

    print(f"Inspecting logs in: {log_dir} (Retention: {args.max_age_days} days)")

    if args.force_archive:
        for active_name in ACTIVE_LOG_NAMES:
            p = log_dir / active_name
            archived = archive_log_file(p, include_time=True)
            if archived:
                print(f"Force archived: {p.name} -> {archived.name}")

    deleted = purge_old_logs(log_dir, max_age_days=args.max_age_days)
    if deleted:
        print(f"Deleted {len(deleted)} expired log files (> {args.max_age_days} days old):")
        for d in deleted:
            print(f"  - {d.name}")
    else:
        print(f"No expired logs found older than {args.max_age_days} days.")
