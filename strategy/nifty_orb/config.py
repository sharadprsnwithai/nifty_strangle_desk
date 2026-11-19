from dataclasses import dataclass
from datetime import time


@dataclass
class NiftyORBConfig:
    """Configuration for NIFTY 50 Index Futures 1-Hour ORB Breakout Strategy."""

    lots: int = 2
    initial_sl_pts: float = 50.0
    target1_pts: float = 75.0

    ref_start_time: time = time(9, 15)
    ref_end_time: time = time(10, 15)       # 1-Hour Opening Range: 09:15 - 10:15 IST
    trade_window_end: time = time(13, 30)
    session_cutoff_time: time = time(15, 15)

    direction_mode: str = "BOTH"           # "BOTH", "SHORT", or "LONG"
    vol_multiplier: float = 1.2
    use_trend_filter: bool = True

    mode: str = "PAPER"
    product: str = "MIS"
    exchange: str = "NFO"
    order_type: str = "LIMIT"
    limit_buffer_pts: float = 5.0
    poll_interval_seconds: int = 5

    state_file: str = "data/nifty_orb_trades_today.json"
