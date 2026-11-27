"""Persistent CSV trade ledger shared across trading desks."""
import csv
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("strategy.ledger")

DEFAULT_LEDGER_FILE = Path(__file__).resolve().parent.parent / "data" / "trade_ledger.csv"

LEDGER_COLUMNS = [
    "date",
    "desk",
    "mode",
    "direction",
    "instrument",
    "lots",
    "quantity",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "why_taken",
    "exit_reason",
    "realized_pnl",
]


def append_trade_ledger(
    row: Dict[str, Any],
    ledger_path: Optional[Path] = None,
) -> Path:
    """Append one completed trade row to the persistent CSV ledger."""
    path = Path(ledger_path) if ledger_path is not None else DEFAULT_LEDGER_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        normalized = {col: row.get(col, "") for col in LEDGER_COLUMNS}
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(normalized)
        logger.info(
            "Ledger recorded [%s | %s]: %s | P&L: Rs %s",
            normalized["desk"],
            normalized["date"],
            normalized["exit_reason"],
            normalized["realized_pnl"],
        )
    except Exception as e:
        logger.warning("Failed to write trade ledger entry to %s: %s", path, e)
    return path
