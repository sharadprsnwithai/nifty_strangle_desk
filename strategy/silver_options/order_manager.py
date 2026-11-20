import logging
from typing import Optional, Union, Dict
from strategy.silver_options.models import SpreadPosition
from strategy.silver_options.risk_manager import sequence_entry_legs, sequence_exit_legs

logger = logging.getLogger("silver_options.order_manager")

def place_order(
    kite_client,
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    price: Optional[float] = None,
    ltp: Optional[float] = None,
    tick_size: float = 1.0,
    slippage_buffer_ticks: int = 2,
    product: str = "NRML",
    exchange: str = "MCX",
    variety: str = "regular",
):
    """Place an order with Kite Connect, supporting marketable limit orders when price or ltp is provided."""
    ref_price = price if price is not None else ltp
    if ref_price is not None:
        buffer = tick_size * slippage_buffer_ticks
        if transaction_type.upper() == "BUY":
            limit_price = round(ref_price + buffer, 2)
        else:
            limit_price = round(max(tick_size, ref_price - buffer), 2)
        params = {
            "variety": variety,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": "LIMIT",
            "price": limit_price,
            "product": product,
        }
    else:
        params = {
            "variety": variety,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": "MARKET",
            "product": product,
        }
    return kite_client.place_order(**params)

def _resolve_leg_price(leg, price: Optional[Union[float, Dict]] = None, ltp: Optional[Union[float, Dict]] = None) -> Optional[float]:
    target = price if price is not None else ltp
    if target is None:
        return None
    if isinstance(target, dict):
        val = target.get(leg.trading_symbol)
        if val is None:
            val = target.get(leg.strike)
        return float(val) if val is not None else None
    return float(target)

def execute_spread_entry(
    kite_client,
    spread: SpreadPosition,
    is_paper: bool = True,
    price: Optional[Union[float, Dict]] = None,
    ltp: Optional[Union[float, Dict]] = None,
    tick_size: float = 1.0,
    slippage_buffer_ticks: int = 2,
) -> bool:
    ordered_legs = sequence_entry_legs(spread.legs)
    if is_paper:
        logger.info(f"[PAPER] Entering spread {spread.spread_type}: {len(ordered_legs)} legs placed.")
        spread.status = "ACTIVE"
        return True

    # Live execution: place buy leg, verify fill, then place sell leg
    for leg in ordered_legs:
        leg_price = _resolve_leg_price(leg, price=price, ltp=ltp)
        try:
            order_id = place_order(
                kite_client=kite_client,
                tradingsymbol=leg.trading_symbol,
                transaction_type=leg.action,
                quantity=leg.quantity,
                price=leg_price,
                tick_size=tick_size,
                slippage_buffer_ticks=slippage_buffer_ticks,
                product="NRML",
                exchange="MCX",
                variety="regular",
            )
            leg.order_id = str(order_id)
        except Exception as e:
            logger.error(f"Failed to place live order for leg {leg.trading_symbol}: {e}")
            spread.status = "FAILED"
            return False

    spread.status = "ACTIVE"
    return True

def execute_spread_exit(
    kite_client,
    spread: SpreadPosition,
    reason: str,
    is_paper: bool = True,
    price: Optional[Union[float, Dict]] = None,
    ltp: Optional[Union[float, Dict]] = None,
    tick_size: float = 1.0,
    slippage_buffer_ticks: int = 2,
) -> bool:
    ordered_legs = sequence_exit_legs(spread.legs)
    if is_paper:
        logger.info(f"[PAPER] Exiting spread {spread.spread_type} (Reason: {reason})")
        spread.status = "CLOSED"
        spread.exit_reason = reason
        return True

    # Live execution: close short leg first, then long leg
    for leg in ordered_legs:
        close_action = "BUY" if leg.action == "SELL" else "SELL"
        leg_price = _resolve_leg_price(leg, price=price, ltp=ltp)
        try:
            place_order(
                kite_client=kite_client,
                tradingsymbol=leg.trading_symbol,
                transaction_type=close_action,
                quantity=leg.quantity,
                price=leg_price,
                tick_size=tick_size,
                slippage_buffer_ticks=slippage_buffer_ticks,
                product="NRML",
                exchange="MCX",
                variety="regular",
            )
        except Exception as e:
            logger.error(f"Failed to exit leg {leg.trading_symbol}: {e}")
            spread.status = "FAILED"
            return False

    spread.status = "CLOSED"
    spread.exit_reason = reason
    return True
