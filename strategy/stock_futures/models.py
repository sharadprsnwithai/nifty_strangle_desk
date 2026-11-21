from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Dict, Any


class SignalType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


@dataclass
class TradeSignal:
    symbol: str
    side: SignalType
    trigger_time: str
    entry_price: float
    sl_price: float
    target_price: float
    volume_ratio: float
    c1_high: float
    c1_low: float


@dataclass
class StockPosition:
    symbol: str
    tradingsymbol: str
    side: str                          # "LONG" or "SHORT"
    quantity: int
    lots: int
    entry_time: str
    entry_price: float
    sl_price: float
    target_price: float
    current_price: float
    status: str = "OPEN"               # "OPEN" or "CLOSED"
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "TARGET_HIT", "SL_HIT", "15:00_EOD"
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    def update_current_price(self, price: float) -> None:
        self.current_price = price
        if self.side == "LONG":
            self.gross_pnl = (self.current_price - self.entry_price) * self.quantity
        else:
            self.gross_pnl = (self.entry_price - self.current_price) * self.quantity

    def close(self, exit_price: float, exit_time: str, exit_reason: str, exit_order_id: Optional[str] = None) -> None:
        self.status = "CLOSED"
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.exit_reason = exit_reason
        self.exit_order_id = exit_order_id
        if self.side == "LONG":
            self.gross_pnl = (exit_price - self.entry_price) * self.quantity
        else:
            self.gross_pnl = (self.entry_price - exit_price) * self.quantity
        self.charges = calculate_futures_charges(self.entry_price, exit_price, self.quantity, self.side)
        self.net_pnl = self.gross_pnl - self.charges

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def calculate_futures_charges(
    entry_price: float,
    exit_price: float,
    quantity: int,
    side: str = "LONG",
    orders_count: int = 2
) -> float:
    """
    Calculates total institutional transaction costs for NSE Equity Futures:
    - Brokerage: Rs 20 per executed order (Entry + Exit = Rs 40)
    - STT: 0.02% on sell turnover
    - Exchange Transaction Charges: 0.0019% on total turnover
    - GST: 18% on (Brokerage + Exchange Charges)
    - Stamp Duty: 0.002% on buy turnover
    - SEBI Turnover Fees: Rs 10 / Crore (0.0001%) on total turnover
    """
    buy_turnover = entry_price * quantity if side == "LONG" else exit_price * quantity
    sell_turnover = exit_price * quantity if side == "LONG" else entry_price * quantity
    total_turnover = buy_turnover + sell_turnover

    brokerage = 20.0 * orders_count
    stt = sell_turnover * 0.0002
    exchange_txn = total_turnover * 0.000019
    gst = (brokerage + exchange_txn) * 0.18
    stamp_duty = buy_turnover * 0.00002
    sebi = total_turnover * 0.000001

    return brokerage + stt + exchange_txn + gst + stamp_duty + sebi
