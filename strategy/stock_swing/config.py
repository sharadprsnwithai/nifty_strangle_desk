"""Configuration for the FP-ALL NSE Stock Swing Trading Desk."""

from dataclasses import dataclass
import json
from pathlib import Path
import os


@dataclass
class StockSwingConfig:
    """Configuration for FP-ALL Stock Swing Desk (1.5% risk, 25% cap, max 5 slots)."""

    total_capital: float = 1_000_000.0
    risk_pct: float = 0.015          # 1.5% equity risk per trade
    max_positions: int = 5
    max_alloc_pct: float = 0.25      # 25% max capital per stock
    mode: str = "PAPER"              # PAPER or LIVE
    product: str = "CNC"             # Delivery
    order_type: str = "LIMIT"        # LIMIT with buffer or MARKET
    limit_buffer_pct: float = 0.005  # 0.5% marketable limit buffer
    exec_time: str = "15:20"         # Daily EOD scan & execution time (IST)
    state_file: str = os.path.join("data", "stock_swing_state.json")
    universe_file: str = os.path.join("data", "stock_swing_universe.json")
    log_dir: str = os.path.join("logs")

    @staticmethod
    def default_universe_symbols(universe_file: str) -> list[str]:
        """Load the 248 NSE symbols from JSON (or parquet fallback)."""
        path = Path(universe_file)
        if not path.exists():
            return []
        if path.suffix.lower() == ".json":
            return sorted(json.loads(path.read_text(encoding="utf-8")))
        import pandas as pd

        df = pd.read_parquet(path, columns=["symbol"])
        return sorted(df["symbol"].dropna().unique().tolist())
