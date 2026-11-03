import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Union

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_LOG_FILE = "strangle_desk.log"


def setup_logging(
    log_dir: Optional[Union[str, Path]] = None,
    log_level: str = "INFO",
    log_filename: str = DEFAULT_LOG_FILE
) -> logging.Logger:
    """
    Configures application-wide logging with both stdout stream handler and
    a rotating file handler on disk.
    
    Args:
        log_dir: Directory where log files are stored. Defaults to ./logs.
        log_level: Logging severity ('DEBUG', 'INFO', 'WARNING', 'ERROR').
        log_filename: Filename for the active log file (default: strangle_desk.log).
    
    Returns:
        The configured root logger.
    """
    target_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / log_filename

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

    # 1. Console Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    root_logger.addHandler(console_handler)

    # 2. Rotating File Handler (persisted to host machine disk)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=10,             # Keep up to 10 backups
        encoding="utf-8"
    )
    file_handler.setLevel(numeric_level)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    root_logger.addHandler(file_handler)

    root_logger.info("Logging configured at level %s. Log file: %s", logging.getLevelName(numeric_level), log_path)
    return root_logger
