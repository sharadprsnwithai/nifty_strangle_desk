from datetime import time
from typing import List, Tuple, Optional
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import OptionLeg, SpreadPosition

def sequence_entry_legs(legs: List[OptionLeg]) -> List[OptionLeg]:
    """Hedge-first: BUY leg first to secure margin discount, SELL leg second."""
    buys = [l for l in legs if l.action == "BUY"]
    sells = [l for l in legs if l.action == "SELL"]
    return buys + sells

def sequence_exit_legs(legs: List[OptionLeg]) -> List[OptionLeg]:
    """Safety-first: Close short leg first (eliminate short liability), then close long leg."""
    sells = [l for l in legs if l.action == "SELL"]
    buys = [l for l in legs if l.action == "BUY"]
    return sells + buys

def check_exit_conditions(
    position: SpreadPosition,
    current_spread_value: float,
    current_time: time,
    is_expiry_day: bool,
    config: SilverConfig,
) -> Tuple[bool, Optional[str]]:
    # 1. Mandatory Expiry Devolvement Cutoff
    if is_expiry_day and current_time >= config.devolvement_cutoff:
        return True, "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"

    # 2. Take Profit: 75% of max profit
    profit_target_value = position.entry_net_debit + (config.take_profit_ratio * position.max_profit)
    if current_spread_value >= profit_target_value:
        return True, "TAKE_PROFIT"

    # 3. Stop Loss: 50% loss of initial net debit
    stop_loss_value = position.entry_net_debit * (1.0 - config.stop_loss_ratio)
    if current_spread_value <= stop_loss_value:
        return True, "STOP_LOSS"

    return False, None
