from dataclasses import dataclass
from datetime import time
from typing import Optional


@dataclass
class SilverORBConfig:
    """Configuration for MCX Silver Mini (SILVERM) 18:00 US Session Breakout Strategy."""

    lots: int = 2
    lot_size_kg: float = 5.0  # 1 lot of SILVERM = 5 kg (Rs 5 per point move)
    point_value_per_lot: float = 5.0  # Rs 5 per lot per point (Rs 10 for 2 lots)

    initial_sl_pts: float = 500.0  # Rs 2,500 risk per lot (Rs 5,000 for 2 lots)
    target1_pts: float = 800.0     # Rs 4,000 profit on Lot 1

    ref_candle_time: time = time(17, 45)       # 17:45 - 18:00 15m reference candle
    trade_window_start: time = time(18, 0)     # Start evaluating breakout at 18:00 IST
    trade_window_end: time = time(19, 30)       # Cutoff to enter new trades
    session_cutoff_time: time = time(22, 45)   # Hard EOD exit for runner lot

    vol_multiplier: float = 1.2    # Volume confirmation factor vs 20-period SMA
    use_trend_filter: bool = True  # Long > 50 EMA, Short < 50 EMA on 15m

    mode: str = "PAPER"            # "PAPER" or "LIVE"
    product: str = "MIS"           # "MIS" for intraday, "NRML" for carry/margin
    exchange: str = "MCX"
    order_type: str = "LIMIT"      # "LIMIT" or "MARKET"
    limit_buffer_pts: float = 15.0 # Slippage buffer in points for marketable LIMIT orders
    poll_interval_seconds: int = 5

    state_file: str = "data/silver_trades_today.json"
