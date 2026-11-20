from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

class SignalType(str, Enum):
    BULLISH_REVERSAL = "BULLISH_REVERSAL"
    BEARISH_BREAKDOWN = "BEARISH_BREAKDOWN"
    RANGE_BOUND = "RANGE_BOUND"
    NO_SIGNAL = "NO_SIGNAL"

class SpreadType(str, Enum):
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"

@dataclass
class OptionLeg:
    strike: int
    option_type: str  # "CE" or "PE"
    action: str  # "BUY" or "SELL"
    quantity: int
    premium: float
    trading_symbol: Optional[str] = None
    order_id: Optional[str] = None

@dataclass
class OptionChainProfile:
    spot_price: float
    pcr: float
    max_pain: int
    call_wall: int
    put_base: int
    atm_strike: int

@dataclass
class SpreadPosition:
    spread_type: SpreadType
    legs: List[OptionLeg]
    entry_net_debit: float
    max_profit: float
    max_loss: float
    status: str = "PENDING"  # PENDING, ACTIVE, CLOSED
    current_value: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: Optional[str] = None
