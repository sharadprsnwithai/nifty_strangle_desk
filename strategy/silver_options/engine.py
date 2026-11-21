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
        self.today_realized_pnl: float = 0.0
        self.current_trading_date = None
        self.trading_halted_for_day: bool = False

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

        # Track and reset daily PnL on new calendar date
        tick_date = kwargs.get("current_date")
        if tick_date is None and df_5m is not None and not df_5m.empty and "date" in df_5m.columns:
            tick_date = pd.to_datetime(df_5m["date"].iloc[-1]).date()

        if tick_date is not None and tick_date != self.current_trading_date:
            self.current_trading_date = tick_date
            self.today_realized_pnl = 0.0
            self.trading_halted_for_day = False

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
                today_realized_pnl=self.today_realized_pnl,
            )

            if should_exit and reason:
                if execute_spread_exit(self.kite_client, self.active_position, reason, is_paper=self.is_paper):
                    short_legs = [l for l in self.active_position.legs if getattr(l, "action", None) == "SELL"]
                    pos_qty = sum(l.quantity for l in short_legs) if short_legs else 0
                    if pos_qty == 0 and self.active_position.legs:
                        pos_qty = sum(l.quantity for l in self.active_position.legs)
                    if pos_qty == 0:
                        pos_qty = getattr(self.config, "total_quantity", getattr(self.config, "lots", 2) * self.config.lot_size)

                    if short_legs and short_legs[0].premium > 0:
                        entry_premium = short_legs[0].premium
                    elif self.active_position.legs and self.active_position.legs[0].premium > 0:
                        entry_premium = self.active_position.legs[0].premium
                    elif pos_qty > 0:
                        entry_premium = self.active_position.entry_net_credit / pos_qty
                    else:
                        entry_premium = 0.0

                    realized_pnl = (entry_premium - current_opt_price) * pos_qty
                    self.active_position.realized_pnl = round(realized_pnl, 2)
                    self.active_position.status = "CLOSED"
                    self.active_position.exit_reason = reason
                    self.closed_positions.append(self.active_position)
                    self.today_realized_pnl += self.active_position.realized_pnl

                    daily_max_loss = getattr(self.config, "daily_max_loss", self.config.daily_max_loss_per_lot * getattr(self.config, "lots", 2))
                    if self.today_realized_pnl <= -daily_max_loss:
                        self.trading_halted_for_day = True
                        logger.warning(
                            "Daily max loss reached: ₹%.2f <= -₹%.2f. Trading halted for today.",
                            self.today_realized_pnl,
                            daily_max_loss,
                        )

                    self.active_position = None
                else:
                    self.active_position.status = "EXIT_FAILED"
                    logger.error("Failed to execute spread exit; holding active position for retry.")
            return

        # 2. Idle state: check daily max loss circuit breaker before evaluating entries
        daily_max_loss = getattr(self.config, "daily_max_loss", self.config.daily_max_loss_per_lot * getattr(self.config, "lots", 2))
        if self.trading_halted_for_day or self.today_realized_pnl <= -daily_max_loss:
            logger.warning("Daily max loss limit reached. Trading halted for today.")
            return

        # 3. Idle position signal generation
        signal = evaluate_technical_signal(df_5m, self.config, current_time=current_time)
        if signal != SignalType.NO_SIGNAL:
            profile = build_option_chain_profile(spot_price, chain_data, self.config)
            spread = select_spread(signal, profile, chain_data, self.config)
            if spread:
                success = execute_spread_entry(self.kite_client, spread, is_paper=self.is_paper)
                if success:
                    spread.status = "ACTIVE"
                    self.active_position = spread
