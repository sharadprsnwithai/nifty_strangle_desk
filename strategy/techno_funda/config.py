from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List


@dataclass
class TechnoFundaConfig:
    """Configuration for Milind Upasani's MTF Techno-Funda Breakout Strategy."""

    # Scheduling
    scan_time: str = "15:15"                  # Daily scan time IST (before market close)
    market_close_time: str = "15:30"

    # Technical Thresholds (Multi-Timeframe Momentum)
    monthly_rsi_threshold: float = 60.0       # Primary trend monthly RSI > 60
    weekly_rsi_threshold: float = 60.0        # Secondary trend weekly RSI > 60
    daily_rsi_trigger: float = 65.0           # Minor trend daily RSI cross above 65
    daily_rsi_max: float = 75.0               # Max daily RSI to avoid over-extended entries
    volume_surge_ratio: float = 1.25          # Breakout volume vs 20-MA

    # Fundamental Thresholds (Operational High Growth)
    min_ttm_ebitda_growth: float = 0.30       # TTM YoY EBITDA Growth > 30%
    min_quarterly_ebitda_growth: float = 0.30 # Quarterly YoY EBITDA Growth > 30%
    max_debt_to_equity: float = 1.0           # Solvency filter: D/E < 1.0
    min_operating_margin: float = 0.08        # Minimum operating margin 8%

    # Paths and Caching
    data_dir: Path = field(default_factory=lambda: Path("data"))
    daily_data_path: Path = field(default_factory=lambda: Path("data/swing_daily_2y.parquet"))
    fundamentals_cache_file: Path = field(default_factory=lambda: Path("data/techno_funda_fundamentals.json"))
    state_file: Path = field(default_factory=lambda: Path("data/techno_funda_alerts.json"))

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
