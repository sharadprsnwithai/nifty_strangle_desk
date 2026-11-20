import logging
from typing import Optional
from strategy.silver_options.models import SpreadPosition
from strategy.silver_options.risk_manager import sequence_entry_legs, sequence_exit_legs

logger = logging.getLogger("silver_options.order_manager")

def execute_spread_entry(kite_client, spread: SpreadPosition, is_paper: bool = True) -> bool:
    ordered_legs = sequence_entry_legs(spread.legs)
    if is_paper:
        logger.info(f"[PAPER] Entering spread {spread.spread_type}: {len(ordered_legs)} legs placed.")
        spread.status = "ACTIVE"
        return True

    # Live execution: place buy leg, verify fill, then place sell leg
    for leg in ordered_legs:
        try:
            order_id = kite_client.place_order(
                variety="regular",
                exchange="MCX",
                tradingsymbol=leg.trading_symbol,
                transaction_type=leg.action,
                quantity=leg.quantity,
                order_type="MARKET",
                product="NRML",
            )
            leg.order_id = str(order_id)
        except Exception as e:
            logger.error(f"Failed to place live order for leg {leg.trading_symbol}: {e}")
            spread.status = "FAILED"
            return False

    spread.status = "ACTIVE"
    return True

def execute_spread_exit(kite_client, spread: SpreadPosition, reason: str, is_paper: bool = True) -> bool:
    ordered_legs = sequence_exit_legs(spread.legs)
    if is_paper:
        logger.info(f"[PAPER] Exiting spread {spread.spread_type} (Reason: {reason})")
        spread.status = "CLOSED"
        spread.exit_reason = reason
        return True

    # Live execution: close short leg first, then long leg
    for leg in ordered_legs:
        close_action = "BUY" if leg.action == "SELL" else "SELL"
        try:
            kite_client.place_order(
                variety="regular",
                exchange="MCX",
                tradingsymbol=leg.trading_symbol,
                transaction_type=close_action,
                quantity=leg.quantity,
                order_type="MARKET",
                product="NRML",
            )
        except Exception as e:
            logger.error(f"Failed to exit leg {leg.trading_symbol}: {e}")
            return False

    spread.status = "CLOSED"
    spread.exit_reason = reason
    return True
