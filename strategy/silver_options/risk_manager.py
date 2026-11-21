"""Risk and Exit Management for MCX Silver Options Desk.

Evaluation Priority:
1. Mandatory Expiry Devolvement Cutoff:
   Exit all open positions before physical delivery devolvement on expiry day (15:30).
2. Super Order Stop Loss (1.5x entry premium):
   For option selling, risk is rising option price. Exit if current option price >= sl_target.
3. Intraday EOD Cutoff:
   Mandatory square-off by 23:15 before market close (23:30).
4. Session End Cutoff:
   Exit at or after 18:00 (calm theta window 09:00 - 18:00).
5. Trend Reversal Exit:
   Adverse 5m EMA crossover with candle confirmation (5 EMA / 50 EMA cross).
"""

from datetime import time
from typing import List, Optional, Tuple
import pandas as pd

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import OptionLeg, SpreadPosition
from strategy.silver_options.technical_signals import check_trend_reversal


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
    current_opt_price: float = 0.0,
    current_time: time = time(0, 0),
    is_expiry_day: bool = False,
    config: Optional[SilverConfig] = None,
    df_5m: Optional[pd.DataFrame] = None,
    **kwargs,
) -> Tuple[bool, Optional[str]]:
    """Evaluate exit conditions for active Silver options positions according to priority.

    Evaluation Priority:
    a) Expiry Devolvement Cutoff:
       If is_expiry_day and current_time >= config.devolvement_cutoff:
       return (True, "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF")
    b) Super Order Stop Loss (1.5x entry premium):
       For option selling, loss occurs when current option price increases.
       Determine entry premium (e.g. position.legs[0].premium or position.entry_net_credit / config.lot_size).
       SL target = position.sl_price if position.sl_price > 0 else entry_premium * config.sl_multiplier.
       If current_opt_price >= sl_target:
       return (True, "STOP_LOSS")
    c) Intraday EOD Cutoff:
       If current_time >= config.eod_cutoff (23:15):
       return (True, "INTRADAY_EOD")
    d) Session End Cutoff (for 09:00 - 18:00 calm theta window):
       If current_time >= config.session_end (18:00):
       return (True, "SESSION_CLOSE")
    e) Trend Reversal Exit:
       If df_5m is not None and len(df_5m) >= 2:
       opt_type = position.legs[0].option_type if position.legs else "PE"
       if check_trend_reversal(df_5m, opt_type, config):
           return (True, "TREND_REVERSAL")
    f) Otherwise:
       return (False, None)
    """
    if config is None:
        config = SilverConfig()

    # Handle current_spread_value kwarg for backward compatibility
    if current_opt_price == 0.0 and "current_spread_value" in kwargs:
        current_opt_price = float(kwargs["current_spread_value"])

    # a) Mandatory Expiry Devolvement Cutoff
    if is_expiry_day and current_time >= config.devolvement_cutoff:
        return True, "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"

    # b) Daily Portfolio Max Loss Check
    daily_max_loss = getattr(config, "daily_max_loss", getattr(config, "daily_max_loss_per_lot", 15000.0) * getattr(config, "lots", 2))
    today_realized = float(kwargs.get("today_realized_pnl", 0.0))

    # c) Super Order Hybrid Stop Loss (1.5x entry premium capped at sl_per_lot)
    entry_premium = 0.0
    if position.legs:
        short_legs = [l for l in position.legs if getattr(l, "action", None) == "SELL"]
        if short_legs and short_legs[0].premium > 0:
            entry_premium = short_legs[0].premium
        elif position.legs[0].premium > 0:
            entry_premium = position.legs[0].premium

    if entry_premium <= 0.0:
        lot_size = getattr(config, "lot_size", 5) or 5
        if position.entry_net_credit > 0:
            entry_premium = position.entry_net_credit / lot_size
        elif position.entry_net_debit > 0:
            entry_premium = position.entry_net_debit / lot_size

    total_qty = getattr(config, "total_quantity", getattr(config, "lots", 2) * getattr(config, "lot_size", 5))
    if entry_premium > 0 and current_opt_price > 0:
        unrealized_pnl = (entry_premium - current_opt_price) * total_qty
        if (today_realized + unrealized_pnl) <= -daily_max_loss:
            return True, "DAILY_MAX_LOSS_LIMIT"

    sl_multiplier = getattr(config, "sl_multiplier", 1.5) or 1.5
    sl_per_lot = getattr(config, "sl_per_lot", 10000.0) or 10000.0
    lot_size = getattr(config, "lot_size", 5) or 5
    pos_sl = getattr(position, "sl_price", 0.0)
    if pos_sl is not None and pos_sl > 0:
        sl_target = pos_sl
    elif entry_premium > 0:
        dynamic_pts = entry_premium * (sl_multiplier - 1.0)
        fixed_pts = sl_per_lot / lot_size
        allowed_pts = min(dynamic_pts, fixed_pts)
        sl_target = entry_premium + allowed_pts
    else:
        sl_target = float("inf")

    if sl_target > 0 and current_opt_price >= sl_target:
        return True, "STOP_LOSS"

    # c) Intraday EOD Cutoff (23:15)
    if current_time >= config.eod_cutoff:
        return True, "INTRADAY_EOD"

    # d) Session End Cutoff (18:00)
    if current_time >= config.session_end:
        return True, "SESSION_CLOSE"

    # e) Trend Reversal Exit
    if df_5m is not None and len(df_5m) >= 2:
        opt_type = "PE"
        if position.legs:
            short_legs = [l for l in position.legs if getattr(l, "action", None) == "SELL"]
            target_leg = short_legs[0] if short_legs else position.legs[0]
            opt_type = target_leg.option_type
        if check_trend_reversal(df_5m, opt_type, config):
            return True, "TREND_REVERSAL"

    # f) Otherwise
    return False, None
