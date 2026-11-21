import logging
from datetime import time
from typing import List, Optional, Dict
import pandas as pd
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadPosition, SignalType
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
        df_5m: Optional[pd.DataFrame] = None,
        chain_data: Optional[List[Dict]] = None,
        current_time: Optional[time] = None,
        is_expiry_day: bool = False,
        **kwargs,
    ):
        # Backward compatibility with callers/tests passing legacy parameters
        if df_5m is None:
            df_5m = kwargs.get("daily_df")
            if df_5m is None:
                df_5m = kwargs.get("df_5m")

        if current_time is None:
            current_time = time(10, 0)

        if chain_data is None:
            chain_data = []

        # 1. Active position management
        if self.active_position:
            current_opt_price = 0.0
            target_leg = None
            if self.active_position.legs:
                short_legs = [l for l in self.active_position.legs if getattr(l, "action", None) == "SELL"]
                target_leg = short_legs[0] if short_legs else self.active_position.legs[0]

            if target_leg is not None:
                chain_by_strike = {r["strike"]: r for r in chain_data if isinstance(r, dict) and "strike" in r}
                row = chain_by_strike.get(target_leg.strike, {})
                ltp = row.get(f"{target_leg.option_type.lower()}_ltp")
                if ltp is None:
                    ltp = row.get("ltp")
                if ltp is not None:
                    current_opt_price = float(ltp)
                elif target_leg.premium is not None and target_leg.premium > 0:
                    current_opt_price = float(target_leg.premium)
                else:
                    current_opt_price = float(getattr(self.active_position, "current_value", 0.0) or 0.0)
            else:
                current_opt_price = float(getattr(self.active_position, "current_value", 0.0) or 0.0)

            self.active_position.current_value = current_opt_price

            should_exit, reason = check_exit_conditions(
                position=self.active_position,
                current_opt_price=current_opt_price,
                current_time=current_time,
                is_expiry_day=is_expiry_day,
                config=self.config,
                df_5m=df_5m,
            )

            if should_exit and reason:
                if execute_spread_exit(self.kite_client, self.active_position, reason, is_paper=self.is_paper):
                    short_legs = [l for l in self.active_position.legs if getattr(l, "action", None) == "SELL"]
                    if short_legs and short_legs[0].premium > 0:
                        entry_premium = short_legs[0].premium
                    elif self.active_position.legs and self.active_position.legs[0].premium > 0:
                        entry_premium = self.active_position.legs[0].premium
                    elif self.config.lot_size > 0:
                        entry_premium = self.active_position.entry_net_credit / self.config.lot_size
                    else:
                        entry_premium = 0.0

                    realized_pnl = (entry_premium - current_opt_price) * self.config.lot_size
                    self.active_position.realized_pnl = round(realized_pnl, 2)
                    self.active_position.status = "CLOSED"
                    self.active_position.exit_reason = reason
                    self.closed_positions.append(self.active_position)
                    self.active_position = None
                else:
                    self.active_position.status = "EXIT_FAILED"
                    logger.error("Failed to execute spread exit; holding active position for retry.")
            return

        # 2. Idle position signal generation
        signal = evaluate_technical_signal(df_5m, self.config, current_time=current_time)
        if signal != SignalType.NO_SIGNAL:
            profile = build_option_chain_profile(spot_price, chain_data, self.config)
            spread = select_spread(signal, profile, chain_data, self.config)
            if spread:
                success = execute_spread_entry(self.kite_client, spread, is_paper=self.is_paper)
                if success:
                    spread.status = "ACTIVE"
                    self.active_position = spread
