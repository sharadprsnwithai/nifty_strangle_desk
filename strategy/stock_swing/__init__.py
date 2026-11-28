"""FP-ALL Stock Swing Trading Desk (15D Consolidation + Institutional Footprints)."""

from strategy.stock_swing.config import StockSwingConfig
from strategy.stock_swing.desk import StockSwingDesk
from strategy.stock_swing.order_manager import StockOrderManager
from strategy.stock_swing.scanner import StockSwingScanner

__all__ = [
    "StockSwingConfig",
    "StockSwingDesk",
    "StockOrderManager",
    "StockSwingScanner",
]
