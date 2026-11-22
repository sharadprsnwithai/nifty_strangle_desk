"""
Configuration for the Weekly Monthly-Fibonacci ETF Swing Trading Desk.
"""

from dataclasses import dataclass, field
from typing import Dict, List
import os

@dataclass
class ETFInfo:
    symbol: str             # NSE trading symbol (e.g. NIFTYBEES)
    yf_ticker: str          # Yahoo finance ticker (e.g. NIFTYBEES.NS)
    name: str               # Human readable name
    min_lot: int = 1        # Minimum order quantity

DEFAULT_ETF_UNIVERSE: Dict[str, ETFInfo] = {
    "NIFTYBEES": ETFInfo("NIFTYBEES", "NIFTYBEES.NS", "Nippon India Nifty 50 ETF"),
    "BANKBEES": ETFInfo("BANKBEES", "BANKBEES.NS", "Nippon India Bank Nifty ETF"),
    "PSUBNKBEES": ETFInfo("PSUBNKBEES", "PSUBNKBEES.NS", "Nippon India Nifty PSU Bank ETF"),
    "AUTOBEES": ETFInfo("AUTOBEES", "AUTOBEES.NS", "Nippon India Nifty Auto ETF"),
    "CPSEETF": ETFInfo("CPSEETF", "CPSEETF.NS", "CPSE ETF / Defense & PSU"),
    "SILVERBEES": ETFInfo("SILVERBEES", "SILVERBEES.NS", "Nippon India Silver ETF"),
    "GOLDBEES": ETFInfo("GOLDBEES", "GOLDBEES.NS", "Nippon India Gold ETF"),
    "MON100": ETFInfo("MON100", "MON100.NS", "Motilal Oswal Nasdaq 100 ETF"),
    "HNGSNGBEES": ETFInfo("HNGSNGBEES", "HNGSNGBEES.NS", "Nippon India Hang Seng ETF")
}

@dataclass
class ETFSwingConfig:
    # Capital and Allocation (Configured for Rs 10 Lakhs)
    total_capital: float = float(os.getenv("ETF_SWING_CAPITAL", "1000000.0"))
    max_slots: int = int(os.getenv("ETF_SWING_MAX_SLOTS", "3"))
    
    # Execution Mode: PAPER or LIVE
    mode: str = os.getenv("ETF_SWING_MODE", "PAPER").upper()
    
    # Timing
    scan_time_friday: str = "15:20"      # Time to preview weekend rebalance
    exec_time_monday: str = "09:16"      # Execution time on Monday market open
    
    # Product and Order settings
    product: str = "CNC"                 # Cash and Carry (delivery)
    order_type: str = "LIMIT"            # LIMIT with marketable buffer or MARKET
    limit_buffer_pct: float = 0.005      # 0.5% marketable buffer to ensure immediate fill
    
    # File storage paths
    state_file: str = os.path.join("data", "etf_swing_state.json")
    log_dir: str = os.path.join("logs")
    
    # Universe
    universe: Dict[str, ETFInfo] = field(default_factory=lambda: DEFAULT_ETF_UNIVERSE)

    @property
    def capital_per_slot(self) -> float:
        return self.total_capital / self.max_slots
