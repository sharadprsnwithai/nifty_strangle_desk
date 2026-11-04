"""
Sharad's 15-Minute Opening Range Breakout (ORB) Trading Desk
Based on Indrajit Shantharaj's strategy with 20% wick filter,
PDH/PDL filter, NIFTY direction filter, and 3 risk guardrails.
"""

from strategy.orb_desk.config import ORBConfig, TARGET_STOCKS, NIFTY_TOKEN
from strategy.orb_desk.order_manager import ORBOrderManager
from strategy.orb_desk.desk import ORBDesk

__all__ = ["ORBConfig", "TARGET_STOCKS", "NIFTY_TOKEN", "ORBOrderManager", "ORBDesk"]
