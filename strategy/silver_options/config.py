import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SilverOptionsConfig:
    """Configuration for MCX SILVERM Evening Intraday Strangle Desk."""

    # Strategy Mode: BARBELL (Combo 2A: Buyer + Primary Strangle), STRANGLE, or BUYER
    strategy_mode: str = os.getenv("SILVER_STRATEGY_MODE", "BARBELL")

    # Underlying and Contract Specifications
    underlying: str = os.getenv("SILVER_UNDERLYING", "SILVERM")
    exchange: str = "MCX"
    lot_size_kg: int = int(os.getenv("SILVER_LOT_SIZE_KG", "5"))  # 1 lot of SILVERM = 5 kg (SILVER = 30 kg)
    tick_size: float = 0.5       # MCX Silver option tick size is 0.50

    def __post_init__(self):
        if self.underlying.upper() == "SILVER" and self.lot_size_kg == 5 and "SILVER_LOT_SIZE_KG" not in os.environ:
            self.lot_size_kg = 30

    # Trade Sizing & Risk Management
    # Lot size is fully configurable via CLI / environment
    lots: int = int(os.getenv("SILVER_LOTS", "2"))
    # Per-lot loss cap in Rs (e.g. Rs 3,000 per lot)
    sl_per_lot: float = float(os.getenv("SILVER_SL_PER_LOT", "3000.0"))
    # Percentage stop-loss for individual legs (40% above entry premium)
    sl_pct: float = float(os.getenv("SILVER_SL_PCT", "0.40"))
    
    # Strike Selection: ATR-scaled primary setup (with PERCENT fallback)
    strike_otm_pct: float = float(os.getenv("SILVER_STRIKE_OTM_PCT", "0.015"))
    strike_mode: str = os.getenv("SILVER_STRIKE_MODE", "ATR")  # ATR or PERCENT
    atr_period: int = int(os.getenv("SILVER_ATR_PERIOD", "14"))
    atr_multiplier: float = float(os.getenv("SILVER_ATR_MULTIPLIER", "0.75"))
    atr_min_otm_pct: float = float(os.getenv("SILVER_ATR_MIN_OTM_PCT", "0.01"))
    min_option_oi: int = int(os.getenv("SILVER_MIN_OI", "100"))
    max_bid_ask_spread_pct: float = float(os.getenv("SILVER_MAX_SPREAD_PCT", "0.03"))
    strike_scan_steps: int = int(os.getenv("SILVER_STRIKE_SCAN_STEPS", "5"))

    # Execution Timing (IST) & Entry Filters
    entry_time: str = os.getenv("SILVER_ENTRY_TIME", "20:00")
    exit_time: str = os.getenv("SILVER_EXIT_TIME", "23:10")
    poll_seconds: int = int(os.getenv("SILVER_POLL_SECONDS", "5"))
    skip_0dte: bool = os.getenv("SILVER_SKIP_0DTE", "true").lower() in ("true", "1", "yes")
    max_dte_days: int = int(os.getenv("SILVER_MAX_DTE_DAYS", "10"))  # 0 = disabled; 10 restricts to <=10 DTE
    vwap_entry_enabled: bool = os.getenv("SILVER_VWAP_ENTRY", "true").lower() in ("true", "1", "yes")
    vwap_entry_cutoff: str = os.getenv("SILVER_VWAP_CUTOFF", "21:45")

    # Protective features
    single_leg_exit: bool = os.getenv("SILVER_SINGLE_LEG_EXIT", "true").lower() in ("true", "1", "yes")
    trail_to_cost: bool = os.getenv("SILVER_TRAIL_COST", "true").lower() in ("true", "1", "yes")
    trail_activation_decay_pct: float = float(os.getenv("SILVER_TRAIL_ACTIVATION_PCT", "0.30"))
    target_decay_pct: float = float(os.getenv("SILVER_TARGET_DECAY_PCT", "0.70"))
    portfolio_sl_pct: float = float(os.getenv("SILVER_PORTFOLIO_SL_PCT", "0.25"))  # 0 = use sl_per_lot; >0 = % of combined credit

    # Order execution settings
    order_type: str = os.getenv("SILVER_ORDER_TYPE", "LIMIT")
    limit_buffer_pct: float = float(os.getenv("SILVER_LIMIT_BUFFER_PCT", "0.05"))
    max_limit_slippage_pct: float = float(os.getenv("SILVER_MAX_LIMIT_SLIPPAGE_PCT", "0.01"))
    product: str = os.getenv("SILVER_PRODUCT", "MIS")  # MIS for intraday margin
    order_timeout_seconds: int = int(os.getenv("SILVER_ORDER_TIMEOUT", "180"))

    # State persistence
    state_file: str = os.getenv("SILVER_STATE_FILE", "data/silver_trades_today.json")

    # Directional Option Buyer (18:00-20:00 VWAP + Volume-Surge, Combo 2A)
    buyer_entry_start: str = os.getenv("SILVER_BUYER_ENTRY_START", "18:00")
    buyer_entry_cutoff: str = os.getenv("SILVER_BUYER_ENTRY_CUTOFF", "20:00")
    buyer_max_dte_days: int = int(os.getenv("SILVER_BUYER_MAX_DTE_DAYS", "10"))
    buyer_min_atr_pct: float = float(os.getenv("SILVER_BUYER_MIN_ATR_PCT", "3.0"))
    buyer_vol_mult: float = float(os.getenv("SILVER_BUYER_VOL_MULT", "1.3"))
    buyer_max_vwap_dist_pct: float = float(os.getenv("SILVER_BUYER_MAX_VWAP_DIST_PCT", "0.012"))
    buyer_sl_pct: float = float(os.getenv("SILVER_BUYER_SL_PCT", "0.30"))
    buyer_sl_per_lot: float = float(os.getenv("SILVER_BUYER_SL_PER_LOT", "3000.0"))
    buyer_target_pct: float = float(os.getenv("SILVER_BUYER_TARGET_PCT", "0.60"))
    buyer_trail_be_pct: float = float(os.getenv("SILVER_BUYER_TRAIL_BE_PCT", "0.25"))
    buyer_state_file: str = os.getenv("SILVER_BUYER_STATE_FILE", "data/silver_buyer_trades_today.json")

    @property
    def max_portfolio_loss(self) -> float:
        """
        Dynamically calculates the total portfolio max loss cap from number of lots.
        Formula: sl_per_lot * lots
        Example: 2 lots @ Rs 3,000/lot = Rs 6,000 total loss limit.
        """
        return round(self.sl_per_lot * self.lots, 2)

    def effective_max_portfolio_loss(self, combined_entry_points: float) -> float:
        """
        Returns portfolio max loss cap in Rs: uses `portfolio_sl_pct * combined_entry_points * multiplier`
        when `portfolio_sl_pct > 0`, otherwise falls back to `max_portfolio_loss`.
        """
        if self.portfolio_sl_pct > 0 and combined_entry_points > 0:
            return round(combined_entry_points * self.portfolio_sl_pct * self.multiplier, 2)
        return self.max_portfolio_loss

    @property
    def multiplier(self) -> float:
        """Total kg traded across all lots. For SILVERM: lots * 5 kg."""
        return float(self.lots * self.lot_size_kg)

    @property
    def buyer_max_loss_cap(self) -> float:
        """Hard rupee loss cap per trade for the directional buyer (`buyer_sl_per_lot * lots`)."""
        return round(self.buyer_sl_per_lot * self.lots, 2)
