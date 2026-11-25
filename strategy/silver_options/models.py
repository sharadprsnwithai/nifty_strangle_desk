from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, Any


@dataclass
class SilverStrangleLeg:
    """Represents a single option leg (Call or Put) of the Silver Strangle."""
    symbol: str
    strike: float
    option_type: str           # 'CE' or 'PE'
    entry_price: float
    entry_time: str
    stop_loss_price: float
    current_price: float
    status: str = "OPEN"       # OPEN, STOPPED, TARGET, EOD, CLOSED
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    @property
    def points_captured(self) -> float:
        """Points won or lost on this short option leg."""
        exit_p = self.exit_price if self.exit_price is not None else self.current_price
        return round(self.entry_price - exit_p, 2)

    def pnl(self, multiplier: float) -> float:
        """Calculates P&L in Rupees for this leg given total kg multiplier."""
        return round(self.points_captured * multiplier, 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SilverStrangleLeg":
        return cls(**data)


@dataclass
class SilverStranglePosition:
    """Represents the complete dual-leg evening strangle trade state."""
    trade_date: str
    underlying_spot: float
    lots: int
    multiplier: float
    max_loss_cap: float
    ce_leg: Optional[SilverStrangleLeg] = None
    pe_leg: Optional[SilverStrangleLeg] = None
    status: str = "OPEN"       # OPEN, PARTIAL, CLOSED, ABORTED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    closed_at: Optional[str] = None
    abort_reason: Optional[str] = None

    @property
    def total_points(self) -> float:
        """Combined points captured across both legs."""
        ce_pts = self.ce_leg.points_captured if self.ce_leg else 0.0
        pe_pts = self.pe_leg.points_captured if self.pe_leg else 0.0
        return round(ce_pts + pe_pts, 2)

    @property
    def total_pnl(self) -> float:
        """Combined gross P&L in Rs across both legs."""
        return round(self.total_points * self.multiplier, 2)

    def is_fully_closed(self) -> bool:
        ce_closed = self.ce_leg is None or self.ce_leg.status != "OPEN"
        pe_closed = self.pe_leg is None or self.pe_leg.status != "OPEN"
        return ce_closed and pe_closed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "underlying_spot": self.underlying_spot,
            "lots": self.lots,
            "multiplier": self.multiplier,
            "max_loss_cap": self.max_loss_cap,
            "status": self.status,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "abort_reason": self.abort_reason,
            "ce_leg": self.ce_leg.to_dict() if self.ce_leg else None,
            "pe_leg": self.pe_leg.to_dict() if self.pe_leg else None,
            "total_points": self.total_points,
            "total_pnl": self.total_pnl,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SilverStranglePosition":
        ce_data = data.get("ce_leg")
        pe_data = data.get("pe_leg")
        return cls(
            trade_date=data["trade_date"],
            underlying_spot=data["underlying_spot"],
            lots=data["lots"],
            multiplier=data["multiplier"],
            max_loss_cap=data.get("max_loss_cap", 6000.0),
            ce_leg=SilverStrangleLeg.from_dict(ce_data) if ce_data else None,
            pe_leg=SilverStrangleLeg.from_dict(pe_data) if pe_data else None,
            status=data.get("status", "OPEN"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            closed_at=data.get("closed_at"),
            abort_reason=data.get("abort_reason"),
        )


def calculate_silver_mcx_costs(
    premium_points: float,
    multiplier: float,
    is_sell: bool = True,
    brokerage_per_order: float = 20.0,
) -> float:
    """
    Computes statutory regulatory charges & brokerage for MCX Commodity Options.
    Turnover = premium * multiplier (total kg).
    """
    turnover = abs(premium_points) * multiplier
    if turnover <= 0:
        return 0.0

    # Brokerage: Rs 20 flat per executed order
    brokerage = brokerage_per_order

    # Exchange transaction fee: 0.05% of turnover (MCX Commodity Options)
    exchange_fee = turnover * 0.0005

    # STT on commodity options: 0.0625% on sell side premium
    stt = (turnover * 0.000625) if is_sell else 0.0

    # SEBI turnover fee: Rs 10 per crore (0.0001%)
    sebi_fee = turnover * 0.000001

    # Stamp duty: 0.003% on buy side premium
    stamp_duty = (turnover * 0.00003) if not is_sell else 0.0

    # GST: 18% on (Brokerage + Exchange fee + SEBI fee)
    gst = (brokerage + exchange_fee + sebi_fee) * 0.18

    total = brokerage + exchange_fee + stt + sebi_fee + stamp_duty + gst
    return round(total, 2)
