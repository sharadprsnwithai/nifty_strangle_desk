from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List

@dataclass
class MaxOICandidate:
    """Represents a stock and its Max Call/Put Open Interest status."""
    symbol: str
    spot: float
    expiry: date
    # Call OI metrics
    max_ce_strike: float
    max_ce_oi: int
    prev_ce_oi: Optional[int] = None
    ce_oi_change_pct: Optional[float] = None
    ce_dist_pct: float = 0.0
    is_near_ce: bool = False
    # Put OI metrics
    max_pe_strike: float = 0.0
    max_pe_oi: int = 0
    prev_pe_oi: Optional[int] = None
    pe_oi_change_pct: Optional[float] = None
    pe_dist_pct: float = 0.0
    is_near_pe: bool = False


@dataclass
class OptionBuySignal:
    """Actionable trade signal generated when Max OI strike is breached with >20% unwinding."""
    symbol: str
    side: str                          # "CE" (Bullish short-covering) or "PE" (Bearish long unwinding)
    spot_price: float
    breached_strike: float
    oi_unwinding_pct: float            # e.g. -24.5%
    atm_strike: float
    atm_tradingsymbol: str
    atm_token: int
    atm_ltp: float
    lot_size: int
    sl_price: float                    # 50% of atm_ltp
    target_price: float                # e.g. 2x (100% gain)
    trigger_time: str
    expiry_date: str
    max_exit_date: str                 # 2 trading days holding period cutoff


@dataclass
class OptionPosition:
    """Tracks active or closed option buying positions with 50% SL and 2-day time stop."""
    symbol: str
    side: str                          # "CE" or "PE"
    tradingsymbol: str
    strike: float
    lot_size: int
    quantity: int
    entry_price: float
    sl_price: float                    # 50% hard stop
    target_price: float                # 2x profit target
    entry_time: str
    entry_date: str
    max_exit_date: str
    current_price: float
    status: str = "OPEN"               # "OPEN", "CLOSED"
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None  # "SL_HIT", "TARGET_HIT", "TIME_EXIT_2D", "MANUAL"
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    def update_price(self, ltp: float) -> None:
        self.current_price = ltp
        self.gross_pnl = (ltp - self.entry_price) * self.quantity

    def close(self, exit_price: float, exit_time: str, exit_date: str, exit_reason: str, exit_order_id: Optional[str] = None) -> None:
        self.status = "CLOSED"
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.exit_date = exit_date
        self.exit_reason = exit_reason
        self.exit_order_id = exit_order_id
        self.gross_pnl = (exit_price - self.entry_price) * self.quantity
        # Standard retail option buy/sell transaction charges (approx ~Rs 60 per trade)
        self.charges = 40.0 + (exit_price * self.quantity * 0.001)  # STT on option sell 0.1% + brokerage
        self.net_pnl = self.gross_pnl - self.charges
