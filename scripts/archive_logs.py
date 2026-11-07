#!/usr/bin/env python
"""
Daily Log Archiving and Retention Script.

Usage:
    python scripts/archive_logs.py
    python scripts/archive_logs.py --max-age-days 10
    python scripts/archive_logs.py --force-archive

Features:
    1. Archives active desk log files (strangle_desk.log, orb_desk.log, etc.)
       with date timestamp (YYYY-MM-DD) if stale (from previous days) or if --force-archive is specified.
    2. Deletes log files older than --max-age-days (default: 10 days).
    3. Protects active logs and .gitkeep from premature deletion.
"""
import sys
import argparse
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy.logger import (
    DEFAULT_LOG_DIR,
    DEFAULT_RETENTION_DAYS,
    ACTIVE_LOG_NAMES,
    archive_log_file,
    archive_log_if_stale,
    purge_old_logs,
)


def main():
    parser = argparse.ArgumentParser(description="Sharad's Desk Daily Log Archiving & Retention Utility")
    parser.add_argument("--log-dir", type=str, default=str(DEFAULT_LOG_DIR), help="Path to logs directory")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_RETENTION_DAYS, help="Maximum age in days before pruning (default: 10)")
    parser.add_argument("--force-archive", action="store_true", help="Force archive active log files today with timestamp")

    args = parser.parse_args()
    log_dir = Path(args.log_dir)

    if not log_dir.exists():
        print(f"Log directory '{log_dir}' does not exist.")
        return 0

    print("=" * 65)
    print(f"  SHARAD'S LOG ARCHIVER & PRUNER")
    print(f"  Directory: {log_dir.resolve()}")
    print(f"  Max Retention: {args.max_age_days} days")
    print("=" * 65)

    # 1. Archive active logs
    archived_count = 0
    for active_name in ACTIVE_LOG_NAMES:
        active_path = log_dir / active_name
        if not active_path.exists() or active_path.stat().st_size == 0:
            continue

        if args.force_archive:
            archived = archive_log_file(active_path, include_time=True)
            if archived:
                print(f"[ARCHIVED] {active_name} -> {archived.name}")
                archived_count += 1
        else:
            archived = archive_log_if_stale(active_path)
            if archived:
                print(f"[STALE ARCHIVED] {active_name} -> {archived.name}")
                archived_count += 1

    if archived_count == 0:
        print("[INFO] No active logs needed archival.")

    # 2. Prune expired logs
    deleted = purge_old_logs(log_dir, max_age_days=args.max_age_days)
    if deleted:
        print(f"\n[PRUNED] Deleted {len(deleted)} expired log files (> {args.max_age_days} days old):")
        for d in deleted:
            print(f"  - {d.name}")
    else:
        print(f"[INFO] No expired logs found older than {args.max_age_days} days.")

    print("\nLog maintenance complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
