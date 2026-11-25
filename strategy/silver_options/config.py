import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SilverOptionsConfig:
    """Configuration for MCX SILVERM Evening Intraday Strangle Desk."""

    # Underlying and Contract Specifications
    underlying: str = "SILVERM"
    exchange: str = "MCX"
    lot_size_kg: int = 5         # 1 lot of SILVERM = 5 kg
    tick_size: float = 0.5       # MCX Silver option tick size is 0.50

    # Trade Sizing & Risk Management
    # Lot size is fully configurable via CLI / environment
    lots: int = int(os.getenv("SILVER_LOTS", "2"))
    # Per-lot loss cap in Rs (e.g. Rs 3,000 per lot)
    sl_per_lot: float = float(os.getenv("SILVER_SL_PER_LOT", "3000.0"))
    # Percentage stop-loss for individual legs (40% above entry premium)
    sl_pct: float = float(os.getenv("SILVER_SL_PCT", "0.40"))
    
    # Strike Selection: 1.5% OTM (rounded to 500 / 1000 strike steps)
    strike_otm_pct: float = float(os.getenv("SILVER_STRIKE_OTM_PCT", "0.015"))
    strike_mode: str = os.getenv("SILVER_STRIKE_MODE", "PERCENT")  # PERCENT or ATR
    atr_multiplier: float = float(os.getenv("SILVER_ATR_MULTIPLIER", "1.5"))
    min_option_oi: int = int(os.getenv("SILVER_MIN_OI", "100"))
    max_bid_ask_spread_pct: float = float(os.getenv("SILVER_MAX_SPREAD_PCT", "0.03"))
    strike_scan_steps: int = int(os.getenv("SILVER_STRIKE_SCAN_STEPS", "5"))

    # Execution Timing (IST)
    entry_time: str = os.getenv("SILVER_ENTRY_TIME", "20:00")
    exit_time: str = os.getenv("SILVER_EXIT_TIME", "23:15")
    poll_seconds: int = int(os.getenv("SILVER_POLL_SECONDS", "5"))

    # Protective features
    single_leg_exit: bool = os.getenv("SILVER_SINGLE_LEG_EXIT", "true").lower() in ("true", "1", "yes")
    trail_to_cost: bool = os.getenv("SILVER_TRAIL_COST", "true").lower() in ("true", "1", "yes")
    trail_activation_decay_pct: float = float(os.getenv("SILVER_TRAIL_ACTIVATION_PCT", "0.30"))
    target_decay_pct: float = float(os.getenv("SILVER_TARGET_DECAY_PCT", "0.70"))

    # Order execution settings
    order_type: str = os.getenv("SILVER_ORDER_TYPE", "LIMIT")
    limit_buffer_pct: float = float(os.getenv("SILVER_LIMIT_BUFFER_PCT", "0.05"))
    max_limit_slippage_pct: float = float(os.getenv("SILVER_MAX_LIMIT_SLIPPAGE_PCT", "0.01"))
    product: str = os.getenv("SILVER_PRODUCT", "MIS")  # MIS for intraday margin
    order_timeout_seconds: int = int(os.getenv("SILVER_ORDER_TIMEOUT", "180"))

    # State persistence
    state_file: str = os.getenv("SILVER_STATE_FILE", "data/silver_trades_today.json")

    @property
    def max_portfolio_loss(self) -> float:
        """
        Dynamically calculates the total portfolio max loss cap from number of lots.
        Formula: sl_per_lot * lots
        Example: 2 lots @ Rs 3,000/lot = Rs 6,000 total loss limit.
        """
        return round(self.sl_per_lot * self.lots, 2)

    @property
    def multiplier(self) -> float:
        """Total kg traded across all lots. For SILVERM: lots * 5 kg."""
        return float(self.lots * self.lot_size_kg)
