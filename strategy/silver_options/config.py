from dataclasses import dataclass
from datetime import time


@dataclass
class SilverConfig:
    symbol: str = "SILVERM"
    exchange: str = "MCX"
    lots: int = 2
    lot_size: int = 5  # kg per lot
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
    sl_per_lot: float = 10000.0  # ₹10,000 max loss per lot per trade
    daily_max_loss_per_lot: float = 15000.0  # ₹15,000 daily max loss per lot
    session_start: time = time(9, 15)
    session_end: time = time(18, 0)
    eod_cutoff: time = time(23, 15)
    slippage_buffer_ticks: int = 2

    @property
    def total_quantity(self) -> int:
        """Total quantity in kg across all configured lots."""
        return self.lots * self.lot_size

    @property
    def max_loss_per_trade(self) -> float:
        """Maximum allowed loss per trade in rupees across all lots."""
        return self.sl_per_lot * self.lots

    @property
    def daily_max_loss(self) -> float:
        """Maximum allowed daily portfolio loss in rupees across all lots."""
        return self.daily_max_loss_per_lot * self.lots
