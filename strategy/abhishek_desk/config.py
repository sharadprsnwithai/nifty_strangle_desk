from dataclasses import dataclass
from typing import Optional

@dataclass
class AbhishekConfig:
    """
    Configuration parameters for Abhishek's 15-Minute PDH/PDL Pullback-Trap Strategy.
    """
    # Timeframe and Execution Time
    timeframe: str = "15minute"
    eod_squareoff_time: str = "15:15"     # 3:15 PM IST square-off for runner
    
    # Levels and Bias
    use_pdh_pdl_filter: bool = True       # Require Long > PDH, Short < PDL; between is No-Trade
    
    # Pullback Rules
    max_pullback_candles: int = 2         # 1 or 2 candles allowed. 3 consecutive cancels/invalidates
    sl_buffer_points: float = 5.0         # 5 NIFTY points buffer beyond candle extreme for SL
    
    # Trade Lifecycle & Risk
    target_1_r: float = 1.0               # First profit target at 1:1 R
    book_target_1_pct: float = 0.50       # Book 50% position size at Target 1
    trail_runner_to_be: bool = True       # Whether to trail runner SL to Cost after 1:1 R
    
    # Sizing & Position Limits
    lots: int = 2                         # Number of lots (2 lots = 130 quantity)
    lot_size: int = 65                    # NIFTY lot size
    max_trades_per_day: int = 2           # Maximum trades allowed in a day
    allow_reentry_after_sl_only: bool = True  # Re-entry permitted only if SL hit before 1:1 R
    
    # Options Simulation Parameters (Option Buying)
    option_target_delta: float = 0.50     # ATM strike target (closest strike to spot)
    assumed_iv: float = 0.13              # 13% Nifty baseline implied volatility
    risk_free_rate: float = 0.065         # 6.5% risk free rate
    slippage_per_share: float = 0.50      # 0.50 Rs execution slippage per leg
