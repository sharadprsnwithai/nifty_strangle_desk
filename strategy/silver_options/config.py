from dataclasses import dataclass
from datetime import time


@dataclass
class SilverConfig:
    symbol: str = "SILVERM"
    exchange: str = "MCX"
    lot_size: int = 5  # kg
    strike_step: int = 1000
    tick_size: float = 1.0
    market_open: time = time(9, 0)
    market_close: time = time(23, 30)
    devolvement_cutoff: time = time(15, 30)
    williams_r_period: int = 70
    ema_fast: int = 5
    ema_mid: int = 15
    ema_slow: int = 50
    target_delta: float = 0.35
    sl_multiplier: float = 1.5
    session_start: time = time(9, 15)
    session_end: time = time(18, 0)
    eod_cutoff: time = time(23, 15)
    slippage_buffer_ticks: int = 2
