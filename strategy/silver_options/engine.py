import logging
from datetime import time
from typing import List, Optional, Dict
import pandas as pd
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadPosition
from strategy.silver_options.technical_signals import evaluate_technical_signal
from strategy.silver_options.chain_analyzer import build_option_chain_profile
from strategy.silver_options.spread_selector import select_spread
from strategy.silver_options.risk_manager import check_exit_conditions
from strategy.silver_options.order_manager import execute_spread_entry, execute_spread_exit

logger = logging.getLogger("silver_options.engine")

class SilverOptionsEngine:
    def __init__(self, kite_client, config: Optional[SilverConfig] = None, is_paper: bool = True):
        self.kite_client = kite_client
        self.config = config or SilverConfig()
        self.is_paper = is_paper
        self.active_position: Optional[SpreadPosition] = None
        self.closed_positions: List[SpreadPosition] = []

    def on_market_tick(
        self,
        spot_price: float,
        daily_df: pd.DataFrame,
        hourly_df: pd.DataFrame,
        chain_data: List[Dict],
        current_time: time,
        is_expiry_day: bool = False,
    ):
        # 1. If we have an active position, monitor exit conditions
        if self.active_position:
            chain_by_strike = {r["strike"]: r for r in chain_data}
            current_spread_value = 0.0
            for leg in self.active_position.legs:
                row = chain_by_strike.get(leg.strike, {})
                ltp = row.get(f"{leg.option_type.lower()}_ltp")
                if ltp is None:
                    ltp = leg.premium
                if leg.action == "BUY":
                    current_spread_value += ltp
                elif leg.action == "SELL":
                    current_spread_value -= ltp

            self.active_position.current_value = current_spread_value

            should_exit, reason = check_exit_conditions(
                self.active_position,
                current_spread_value=current_spread_value,
                current_time=current_time,
                is_expiry_day=is_expiry_day,
                config=self.config,
            )
            if should_exit and reason:
                if execute_spread_exit(self.kite_client, self.active_position, reason, is_paper=self.is_paper):
                    self.active_position.realized_pnl = (current_spread_value - self.active_position.entry_net_debit) * self.config.lot_size
                    self.closed_positions.append(self.active_position)
                    self.active_position = None
                else:
                    self.active_position.status = "EXIT_FAILED"
                    logger.error("Failed to execute spread exit; holding active position for retry.")
            return

        # 2. If idle, check for entry signals
        signal = evaluate_technical_signal(daily_df, hourly_df, self.config)
        profile = build_option_chain_profile(spot_price, chain_data, self.config)
        spread = select_spread(signal, profile, chain_data, self.config)

        if spread:
            success = execute_spread_entry(self.kite_client, spread, is_paper=self.is_paper)
            if success:
                self.active_position = spread
