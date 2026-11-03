import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from strategy.order_manager import OrderManager

logger = logging.getLogger("strategy.desk")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "trades_today.json"

class StrangleDesk:
    """Manages strategy state, persistence, and recovery."""

    def __init__(self, order_manager: OrderManager, state_file: Optional[Path] = None):
        self.order_manager = order_manager
        self.state_file = state_file or DEFAULT_STATE_FILE
        self.state: Optional[Dict[str, Any]] = None

    def save_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.state_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp_file.replace(self.state_file)

    def load_state(self) -> Optional[Dict[str, Any]]:
        if not self.state_file.exists():
            return None
        try:
            content = self.state_file.read_text(encoding="utf-8")
            self.state = json.loads(content)
            return self.state
        except Exception as e:
            logger.error("Failed to load state from %s: %s", self.state_file, e)
            return None

    def clear_state(self) -> None:
        if self.state_file.exists():
            self.state_file.unlink()
        self.state = None
