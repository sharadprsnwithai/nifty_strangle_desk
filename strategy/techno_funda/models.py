from dataclasses import dataclass
from typing import Optional


@dataclass
class TechnoFundaCandidate:
    """Represents a shortlisted stock meeting Technical and Fundamental Breakout criteria."""

    symbol: str
    company_name: str
    sector: str
    ltp: float

    # Technical Momentum Metrics
    daily_rsi: float
    prev_daily_rsi: float
    weekly_rsi: float
    monthly_rsi: float
    rsi_cross_confirmed: bool
    volume_ratio: float
    chart_pattern: str

    # Fundamental Growth Metrics
    ttm_ebitda_growth_pct: Optional[float] = None
    quarterly_ebitda_growth_pct: Optional[float] = None
    debt_to_equity: Optional[float] = None
    operating_margin_pct: Optional[float] = None
    passed_fundamentals: bool = False
    fundamental_reason: str = ""

    # Trade Reference Levels
    swing_sl: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    scan_date: str = ""
