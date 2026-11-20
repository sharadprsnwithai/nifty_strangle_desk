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
    max_debit_width_ratio: float = 0.35
    take_profit_ratio: float = 0.75
    stop_loss_ratio: float = 0.50
    daily_rsi_period: int = 14
    daily_rsi_oversold: float = 25.0
    daily_rsi_overbought: float = 75.0
    hourly_ema_period: int = 20
    slippage_buffer_ticks: int = 2
