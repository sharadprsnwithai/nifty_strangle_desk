from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class SignalType(str, Enum):
    BULLISH_MOMENTUM = "BULLISH_MOMENTUM"
    BEARISH_MOMENTUM = "BEARISH_MOMENTUM"
    NO_SIGNAL = "NO_SIGNAL"

    # Aliases for backward compatibility during migration
    BULLISH_REVERSAL = "BULLISH_MOMENTUM"
    BEARISH_BREAKDOWN = "BEARISH_MOMENTUM"
    RANGE_BOUND = "NO_SIGNAL"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            legacy_map = {
                "BULLISH_REVERSAL": cls.BULLISH_MOMENTUM,
                "BEARISH_BREAKDOWN": cls.BEARISH_MOMENTUM,
                "RANGE_BOUND": cls.NO_SIGNAL,
            }
            if value in legacy_map:
                return legacy_map[value]
        return super()._missing_(value)


class SpreadType(str, Enum):
    SHORT_PUT = "SHORT_PUT"
    SHORT_CALL = "SHORT_CALL"
    BULL_PUT_SPREAD = "BULL_PUT_SPREAD"
    BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"

    # Aliases for backward compatibility during migration
    BULL_CALL_SPREAD = "BULL_PUT_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_CALL_SPREAD"
    IRON_CONDOR = "SHORT_PUT"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            legacy_map = {
                "BULL_CALL_SPREAD": cls.BULL_PUT_SPREAD,
                "BEAR_PUT_SPREAD": cls.BEAR_CALL_SPREAD,
                "IRON_CONDOR": cls.SHORT_PUT,
            }
            if value in legacy_map:
                return legacy_map[value]
        return super()._missing_(value)


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
    entry_net_credit: float = 0.0
    entry_net_debit: float = 0.0
    max_profit: float = 0.0
    max_loss: float = 0.0
    target_delta: float = 0.35
    sl_price: float = 0.0
    status: str = "PENDING"  # PENDING, ACTIVE, CLOSED
    current_value: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: Optional[str] = None
