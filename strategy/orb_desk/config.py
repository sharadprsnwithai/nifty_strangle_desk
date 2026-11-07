from dataclasses import dataclass, field
from typing import List, Dict, Any

# Target universe of 30 high-momentum stocks
TARGET_STOCKS: List[Dict[str, Any]] = [
    {"symbol": "ABB", "token": 3329, "lot": 125},
    {"symbol": "ADANIENSOL", "token": 2615553, "lot": 675},
    {"symbol": "ADANIGREEN", "token": 912129, "lot": 600},
    {"symbol": "ADANIPOWER", "token": 4451329, "lot": 3550},
    {"symbol": "ABCAPITAL", "token": 5533185, "lot": 1550},
    {"symbol": "BSE", "token": 5013761, "lot": 200},
    {"symbol": "BHARATFORG", "token": 108033, "lot": 500},
    {"symbol": "BHEL", "token": 112129, "lot": 1250},
    {"symbol": "CGPOWER", "token": 194561, "lot": 850},
    {"symbol": "CUMMINSIND", "token": 486657, "lot": 100},
    {"symbol": "FEDERALBNK", "token": 261889, "lot": 2500},
    {"symbol": "GVT&D", "token": 4296449, "lot": 125},
    {"symbol": "GLENMARK", "token": 1895937, "lot": 375},
    {"symbol": "HINDALCO", "token": 348929, "lot": 700},
    {"symbol": "POWERINDIA", "token": 4724993, "lot": 25},
    {"symbol": "KEI", "token": 3407361, "lot": 175},
    {"symbol": "LTF", "token": 6386689, "lot": 2250},
    {"symbol": "LAURUSLABS", "token": 4923905, "lot": 425},
    {"symbol": "MCX", "token": 7982337, "lot": 225},
    {"symbol": "NTPC", "token": 2977281, "lot": 1600},
    {"symbol": "NATIONALUM", "token": 1629185, "lot": 1875},
    {"symbol": "POLYCAB", "token": 2455041, "lot": 75},
    {"symbol": "MOTHERSON", "token": 1076225, "lot": 6150},
    {"symbol": "SHRIRAMFIN", "token": 1102337, "lot": 825},
    {"symbol": "SOLARINDS", "token": 3412993, "lot": 25},
    {"symbol": "SAIL", "token": 758529, "lot": 4700},
    {"symbol": "TATASTEEL", "token": 895745, "lot": 2750},
    {"symbol": "TORNTPHARM", "token": 900609, "lot": 125},
    {"symbol": "VEDL", "token": 784129, "lot": 1975},
    {"symbol": "IDEA", "token": 3677697, "lot": 36125},
]

NIFTY_TOKEN: int = 256265
NIFTY_SYMBOL: str = "NSE:NIFTY 50"


@dataclass
class ORBConfig:
    """Configuration parameters for the ORB Desk."""
    # Sizing & Risk Guardrails (Option 3: Risk-Parity ₹2,500 Risk)
    fixed_risk_per_trade: float = 2500.0    # Fixed Rs Risk per trade
    max_trades_per_day: int = 5             # Maximum trades allowed in a single day
    daily_stop_loss_limit: float = 50000.0  # Daily Portfolio Stop-Loss Circuit Breaker (for 2 lots)
    sizing_mode: str = "LOTS"               # "FIXED_RISK" or "LOTS" (2 lots production)
    lots: int = 2                           # Number of lots (used if sizing_mode="LOTS")

    # Strategy Filters
    wick_max_pct: float = 0.20              # Max allowed rejection wick (20%)
    max_sl_pct: float = 0.025               # Max stop-loss distance as % of stock price (2.5%)
    use_pdh_pdl_filter: bool = True         # Require Long > PDH, Short < PDL
    use_nifty_filter: bool = True           # Require Long when NIFTY > Day Open, Short when NIFTY < Day Open

    # Trade Lifecycle Rules (Option 3 Canonical Logic)
    use_armed_candle_trigger: bool = True   # Require confirmation break of armed candle (high for long, low for short)
    target_1_r: float = 1.0                 # Target 1 R-multiple (1.0 R)
    book_target_1_pct: float = 0.50         # Book 50% position size at Target 1 (1.0 R)
    trail_1r_to_be: bool = True             # Move Stop-Loss to Cost / Breakeven at 1:1 R
    eod_squareoff_time: str = "15:00"       # Exit remaining 50% runner at 3:00 PM IST (15:00)

    # Execution Parameters
    mode: str = "PAPER"                     # "PAPER" or "LIVE"
    order_type: str = "LIMIT"               # "LIMIT" (marketable limit with buffer) or "MARKET"
    limit_buffer_pct: float = 0.002         # 0.2% buffer for marketable limit execution
    product: str = "MIS"                    # "MIS" for intraday
    exchange: str = "NSE"                   # "NSE" for cash equity, "NFO" for futures
    poll_interval_seconds: int = 5          # Position monitoring interval in seconds
    state_file: str = "data/orb_trades_today.json"

    # Universe: Strictly Static 30 High-Momentum F&O Stocks (Option 3 Desk)

