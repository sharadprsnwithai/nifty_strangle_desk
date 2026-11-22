from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import os

# Official NSE F&O Lot Sizes for Nifty 200 Momentum 30 Universe (Updated)
MOMENTUM_30_LOT_SIZES: Dict[str, int] = {
    "ABB": 125,
    "ABCAPITAL": 5400,
    "ADANIENSOL": 675,
    "ADANIGREEN": 600,
    "ADANIPOWER": 1350,
    "BHARATFORG": 500,
    "BHEL": 2625,
    "BSE": 375,
    "CGPOWER": 850,
    "CUMMINSIND": 300,
    "FEDERALBNK": 5000,
    "GLENMARK": 725,
    "GVT_D": 1000,
    "HINDALCO": 1400,
    "IDEA": 71475,
    "KEI": 150,
    "LAURUSLABS": 1700,
    "LTF": 4462,
    "MCX": 250,
    "MOTHERSON": 6150,
    "NATIONALUM": 3750,
    "NTPC": 1500,
    "POLYCAB": 125,
    "POWERINDIA": 100,
    "SAIL": 8000,
    "SHRIRAMFIN": 300,
    "SOLARINDS": 75,
    "TATASTEEL": 5500,
    "TORNTPHARM": 250,
    "VEDL": 1550,
}

# Stock display names mapping
STOCK_NAME_MAPPING: Dict[str, str] = {
    "ABB": "ABB India Limited",
    "ABCAPITAL": "Aditya Birla Capital Limited",
    "ADANIENSOL": "Adani Energy Solutions Limited",
    "ADANIGREEN": "Adani Green Energy Limited",
    "ADANIPOWER": "Adani Power Limited",
    "BHARATFORG": "Bharat Forge Limited",
    "BHEL": "Bharat Heavy Electricals Limited",
    "BSE": "BSE Limited",
    "CGPOWER": "CG Power and Industrial Solutions Limited",
    "CUMMINSIND": "Cummins India Limited",
    "FEDERALBNK": "The Federal Bank Limited",
    "GLENMARK": "Glenmark Pharmaceuticals Limited",
    "GVT_D": "GE Vernova T&D India Limited",
    "HINDALCO": "Hindalco Industries Limited",
    "IDEA": "Vodafone Idea Limited",
    "KEI": "KEI Industries Limited",
    "LAURUSLABS": "Laurus Labs Limited",
    "LTF": "L&T Finance Limited",
    "MCX": "Multi Commodity Exchange of India Limited",
    "MOTHERSON": "Samvardhana Motherson International Limited",
    "NATIONALUM": "National Aluminium Company Limited",
    "NTPC": "NTPC Limited",
    "POLYCAB": "Polycab India Limited",
    "POWERINDIA": "Hitachi Energy India Limited",
    "SAIL": "Steel Authority of India Limited",
    "SHRIRAMFIN": "Shriram Finance Limited",
    "SOLARINDS": "Solar Industries India Limited",
    "TATASTEEL": "Tata Steel Limited",
    "TORNTPHARM": "Torrent Pharmaceuticals Limited",
    "VEDL": "Vedanta Limited",
}


@dataclass
class StockFuturesConfig:
    """Configuration for Nifty 200 Momentum 30 Stock Futures Trading Desk."""
    lots_per_trade: int = 2
    max_concurrent_positions: int = 2  # Hard limit on simultaneous open futures positions
    target_pct: float = 0.0225      # +2.25% profit target (2.25:1 R:R)
    sl_pct: float = 0.010           # -1.0% hard stop loss
    vol_threshold: float = 1.5      # 1.5x of 20-period volume MA
    c1_candle_time: str = "09:15"   # Opening 15m candle (09:15 - 09:30)
    consolidation_candles: List[str] = field(default_factory=lambda: ["09:30", "09:45", "10:00", "10:15"])
    entry_start_time: str = "10:30" # Breakout scan starts at 10:30
    entry_cutoff_time: str = "13:00" # No new trades after 13:00
    squareoff_time: str = "15:00"   # Mandatory EOD square-off
    max_trades_per_stock_per_day: int = 1
    order_type: str = "LIMIT"       # "LIMIT" (marketable limit) or "MARKET"
    limit_buffer_pct: float = 0.05  # Marketable limit protection buffer percentage (0.05 = 5%)
    state_file: Path = field(default_factory=lambda: Path("data/stock_futures_trades_today.json"))
    universe_file: Path = field(default_factory=lambda: Path("data/momentum_30_universe.json"))
    symbols: List[str] = field(default_factory=lambda: list(MOMENTUM_30_LOT_SIZES.keys()))
    lot_sizes: Dict[str, int] = field(default_factory=lambda: dict(MOMENTUM_30_LOT_SIZES))

    def __post_init__(self):
        self.reload_universe()

    def reload_universe(self) -> bool:
        """Loads symbols and lot sizes from universe_file if it exists; returns True if reloaded."""
        if self.universe_file and self.universe_file.exists():
            try:
                import json
                with open(self.universe_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    file_symbols = data.get("symbols", [])
                    file_lots = data.get("lot_sizes", {})
                    if file_symbols and file_lots:
                        self.symbols = file_symbols
                        self.lot_sizes = {k: int(v) for k, v in file_lots.items()}
                        return True
            except Exception:
                pass
        return False

    def get_unit_quantity(self, symbol: str) -> int:
        """Returns total trade quantity for given symbol based on lots_per_trade."""
        lot_size = self.lot_sizes.get(symbol, 0)
        return lot_size * self.lots_per_trade
