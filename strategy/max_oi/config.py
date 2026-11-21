import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

@dataclass
class MaxOIConfig:
    """Configuration for Saurabh's Max OI Unwinding Option Buying Strategy."""
    # Strategy Filters
    proximity_pct: float = 0.02         # Within 2.0% range of Max OI strike
    unwinding_pct: float = 0.20         # At least 20% OI decrease (panic unwinding)
    sl_pct: float = 0.50                # 50% stop loss on option premium
    max_holding_days: int = 2           # 2-day maximum holding period (avoids theta death)
    post_15_only: bool = True           # Restrict scanning to second half of expiry (after 15th)
    capital_allocation_pct: float = 0.02 # Maximum 2% capital allocated to option buying

    # Scheduling & Timing
    scan_time: str = "09:30"            # Primary daily scan time IST
    entry_cutoff_time: str = "14:30"    # Cutoff for intraday scan entries
    market_close_time: str = "15:30"    # Regular market close

    # Data persistence
    snapshots_dir: Path = field(default_factory=lambda: Path("data/oi_snapshots"))
    state_file: Path = field(default_factory=lambda: Path("data/max_oi_trades.json"))

    def __post_init__(self):
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
