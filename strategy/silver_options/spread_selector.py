from typing import List, Optional
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import (
    SignalType,
    SpreadType,
    OptionLeg,
    OptionChainProfile,
    SpreadPosition,
)

def select_spread(
    signal: SignalType,
    profile: OptionChainProfile,
    chain_data: List[dict],
    config: SilverConfig,
) -> Optional[SpreadPosition]:
    chain_by_strike = {row["strike"]: row for row in chain_data}

    if signal == SignalType.BULLISH_REVERSAL:
        # Long Leg: Max Pain pivot or nearest strike above spot
        long_strike = profile.max_pain if profile.max_pain in chain_by_strike else profile.atm_strike
        short_strike = profile.call_wall

        if long_strike >= short_strike or long_strike not in chain_by_strike or short_strike not in chain_by_strike:
            return None

        long_row = chain_by_strike[long_strike]
        short_row = chain_by_strike[short_strike]
        long_ltp = long_row.get("ce_ltp", 0.0)
        short_ltp = short_row.get("ce_ltp", 0.0)

        net_debit = long_ltp - short_ltp
        spread_width = short_strike - long_strike

        if net_debit <= 0 or net_debit > (config.max_debit_width_ratio * spread_width):
            return None

        leg1 = OptionLeg(strike=long_strike, option_type="CE", action="BUY", quantity=config.lot_size, premium=long_ltp)
        leg2 = OptionLeg(strike=short_strike, option_type="CE", action="SELL", quantity=config.lot_size, premium=short_ltp)

        return SpreadPosition(
            spread_type=SpreadType.BULL_CALL_SPREAD,
            legs=[leg1, leg2],
            entry_net_debit=round(net_debit, 2),
            max_profit=round(spread_width - net_debit, 2),
            max_loss=round(net_debit, 2),
        )

    elif signal == SignalType.BEARISH_BREAKDOWN:
        long_strike = profile.max_pain if profile.max_pain in chain_by_strike else profile.atm_strike
        short_strike = profile.put_base

        if long_strike <= short_strike or long_strike not in chain_by_strike or short_strike not in chain_by_strike:
            return None

        long_row = chain_by_strike[long_strike]
        short_row = chain_by_strike[short_strike]
        long_ltp = long_row.get("pe_ltp", 0.0)
        short_ltp = short_row.get("pe_ltp", 0.0)

        net_debit = long_ltp - short_ltp
        spread_width = long_strike - short_strike

        if net_debit <= 0 or net_debit > (config.max_debit_width_ratio * spread_width):
            return None

        leg1 = OptionLeg(strike=long_strike, option_type="PE", action="BUY", quantity=config.lot_size, premium=long_ltp)
        leg2 = OptionLeg(strike=short_strike, option_type="PE", action="SELL", quantity=config.lot_size, premium=short_ltp)

        return SpreadPosition(
            spread_type=SpreadType.BEAR_PUT_SPREAD,
            legs=[leg1, leg2],
            entry_net_debit=round(net_debit, 2),
            max_profit=round(spread_width - net_debit, 2),
            max_loss=round(net_debit, 2),
        )

    return None
