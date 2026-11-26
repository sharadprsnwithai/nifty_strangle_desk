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
    cost_trail_pending: bool = False
    exit_filled_lots: int = 0

    @property
    def points_captured(self) -> float:
        """Points won or lost on this short option leg."""
        exit_p = self.exit_price if self.exit_price is not None else self.current_price
        return round(self.entry_price - exit_p, 2)

    def effective_points(self, total_lots: int) -> float:
        """
        Accurate per-lot average points even when partially squared off
        (`status == "OPEN"` and `0 < exit_filled_lots < total_lots`).
        """
        if self.status != "OPEN" or self.exit_filled_lots <= 0 or total_lots <= 0:
            return self.points_captured
        closed_lots = min(self.exit_filled_lots, total_lots)
        open_lots = max(0, total_lots - closed_lots)
        closed_px = self.exit_price if self.exit_price is not None else self.current_price
        closed_pts = self.entry_price - closed_px
        open_pts = self.entry_price - self.current_price
        return round(((closed_lots * closed_pts) + (open_lots * open_pts)) / total_lots, 2)

    def pnl(self, multiplier: float) -> float:
        """Calculates P&L in Rupees for this leg given total kg multiplier."""
        return round(self.points_captured * multiplier, 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SilverStrangleLeg":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


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
    mode: str = "LIVE"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    closed_at: Optional[str] = None
    abort_reason: Optional[str] = None

    @property
    def total_points(self) -> float:
        """Combined points captured across both legs (accounting for partial exits)."""
        ce_pts = self.ce_leg.effective_points(self.lots) if self.ce_leg else 0.0
        pe_pts = self.pe_leg.effective_points(self.lots) if self.pe_leg else 0.0
        return round(ce_pts + pe_pts, 2)

    @property
    def total_pnl(self) -> float:
        """Combined gross P&L in Rs across both legs."""
        return round(self.total_points * self.multiplier, 2)

    @property
    def total_estimated_costs(self) -> float:
        """Estimated round-trip MCX brokerage & statutory taxes across both legs."""
        total = 0.0
        for leg in (self.ce_leg, self.pe_leg):
            if leg is None:
                continue
            exit_p = leg.exit_price if leg.exit_price is not None else leg.current_price
            total += calculate_silver_mcx_costs(leg.entry_price, self.multiplier, is_sell=True)
            total += calculate_silver_mcx_costs(exit_p, self.multiplier, is_sell=False)
        return round(total, 2)

    @property
    def estimated_net_pnl(self) -> float:
        """Estimated net P&L in Rs after MCX brokerage & statutory taxes."""
        return round(self.total_pnl - self.total_estimated_costs, 2)

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
            "mode": self.mode,
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
            mode=data.get("mode", "LIVE"),
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


@dataclass
class SilverDirectionalPosition:
    """Represents a single-leg LONG ATM option trade for the 18:00-20:00 Directional Buyer."""
    trade_date: str
    underlying_spot: float
    symbol: str = ""
    strike: float = 0.0
    option_type: str = "CE"       # 'CE' or 'PE'
    lots: int = 2
    multiplier: float = 10.0
    entry_price: float = 0.0
    entry_time: str = ""
    stop_loss_price: float = 0.0
    initial_sl_price: float = 0.0
    target_price: float = 0.0
    current_price: float = 0.0
    max_loss_cap: float = 6000.0
    status: str = "OPEN"          # OPEN, TARGET, SL, MAX_LOSS, EOD, BROKER_RECONCILED, SKIPPED, ABORTED
    mode: str = "LIVE"
    order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    exit_filled_lots: int = 0
    breakeven_armed: bool = False
    abort_reason: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    closed_at: Optional[str] = None

    @property
    def points_captured(self) -> float:
        """Points won or lost on this LONG option position (accounting for partial exits)."""
        if self.entry_price <= 0:
            return 0.0
        if self.status == "OPEN" and 0 < self.exit_filled_lots < self.lots and self.lots > 0:
            closed_lots = min(self.exit_filled_lots, self.lots)
            open_lots = max(0, self.lots - closed_lots)
            closed_px = self.exit_price if self.exit_price is not None else self.current_price
            return round(((closed_lots * (closed_px - self.entry_price)) + (open_lots * (self.current_price - self.entry_price))) / self.lots, 2)
        exit_p = self.exit_price if self.exit_price is not None else self.current_price
        return round(exit_p - self.entry_price, 2)

    @property
    def total_pnl(self) -> float:
        """Gross P&L in Rupees for this long option trade."""
        return round(self.points_captured * self.multiplier, 2)

    @property
    def total_estimated_costs(self) -> float:
        """Estimated MCX round-trip charges (buy entry + sell exit)."""
        if self.entry_price <= 0:
            return 0.0
        exit_p = self.exit_price if self.exit_price is not None else self.current_price
        total = (
            calculate_silver_mcx_costs(self.entry_price, self.multiplier, is_sell=False)
            + calculate_silver_mcx_costs(exit_p, self.multiplier, is_sell=True)
        )
        return round(total, 2)

    @property
    def estimated_net_pnl(self) -> float:
        return round(self.total_pnl - self.total_estimated_costs, 2)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["points_captured"] = self.points_captured
        d["total_pnl"] = self.total_pnl
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SilverDirectionalPosition":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)
