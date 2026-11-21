import math
from typing import List, Optional
from scipy.stats import norm

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import (
    SignalType,
    SpreadType,
    OptionLeg,
    OptionChainProfile,
    SpreadPosition,
)


def find_target_delta_strike(
    spot: float,
    opt_type: str,
    chain_data: List[dict],
    target_delta: float = 0.35,
    dte_days: float = 20.0,
    iv: float = 0.32,
    strike_step: float = 1000.0,
    r: float = 0.05,
) -> Optional[dict]:
    """
    Finds the strike in chain_data closest to target_delta.

    - If chain rows have explicit deltas (ce_delta / pe_delta), pick strike where
      abs(row[f"{opt_type.lower()}_delta"]) is closest to target_delta.
    - Otherwise, compute analytical Black-76 delta:
      d1 = (ln(spot/strike) + 0.5 * sigma^2 * t) / (sigma * sqrt(t))
      For CE: delta = exp(-r*t) * norm.cdf(d1)
      For PE: delta = -exp(-r*t) * norm.cdf(-d1)
      Pick strike where abs(delta) is closest to target_delta.
    - Filter: For PE, strike should be OTM/ATM (strike <= spot * 1.02);
      For CE, strike should be OTM/ATM (strike >= spot * 0.98).
    - Must have valid LTP > 0.
    """
    if spot is None or spot <= 0 or not chain_data:
        return None

    opt_upper = opt_type.upper()
    if opt_upper not in ("CE", "PE"):
        return None

    delta_key = f"{opt_upper.lower()}_delta"
    ltp_key = f"{opt_upper.lower()}_ltp"

    # Filter candidate rows
    candidates = []
    for row in chain_data:
        if not isinstance(row, dict) or "strike" not in row:
            continue
        try:
            strike = float(row["strike"])
        except (ValueError, TypeError):
            continue
        if strike <= 0:
            continue

        # Must have valid LTP > 0
        ltp_val = row.get(ltp_key)
        if ltp_val is None:
            ltp_val = row.get("ltp")
        try:
            ltp = float(ltp_val)
        except (ValueError, TypeError):
            continue
        if ltp <= 0.0:
            continue

        # Filter: For PE, strike should be OTM/ATM (strike <= spot * 1.02)
        if opt_upper == "PE" and strike > spot * 1.02:
            continue
        # Filter: For CE, strike should be OTM/ATM (strike >= spot * 0.98)
        if opt_upper == "CE" and strike < spot * 0.98:
            continue

        candidates.append(row)

    if not candidates:
        return None

    # Check if explicit deltas are provided in chain
    has_explicit_delta = any(row.get(delta_key) is not None for row in candidates)

    best_row = None
    min_diff = float("inf")

    if has_explicit_delta:
        for row in candidates:
            delta_val = row.get(delta_key)
            if delta_val is None:
                continue
            try:
                cand_delta = float(delta_val)
            except (ValueError, TypeError):
                continue
            diff = abs(abs(cand_delta) - target_delta)
            if diff < min_diff:
                min_diff = diff
                best_row = row
    else:
        # Analytical Black-76 delta calculation
        t = max(dte_days / 365.0, 1e-4)
        sigma = max(iv, 1e-4)
        denom = sigma * math.sqrt(t)

        for row in candidates:
            strike = float(row["strike"])
            d1 = (math.log(spot / strike) + 0.5 * (sigma ** 2) * t) / denom
            if opt_upper == "CE":
                delta = math.exp(-r * t) * float(norm.cdf(d1))
            else:
                delta = -math.exp(-r * t) * float(norm.cdf(-d1))

            diff = abs(abs(delta) - target_delta)
            if diff < min_diff:
                min_diff = diff
                best_row = row

    return best_row


def select_spread(
    signal: SignalType,
    profile: OptionChainProfile,
    chain_data: List[dict],
    config: SilverConfig,
) -> Optional[SpreadPosition]:
    """
    Selects delta-targeted option strike for short option execution.
    - BULLISH_MOMENTUM / BULLISH_REVERSAL: Sells OTM Put (PE) with delta ~ config.target_delta.
    - BEARISH_MOMENTUM / BEARISH_BREAKDOWN: Sells OTM Call (CE) with delta ~ config.target_delta.
    - NO_SIGNAL / RANGE_BOUND: returns None.
    """
    if signal is None or signal == SignalType.NO_SIGNAL:
        return None

    if not chain_data:
        return None

    if profile is None or not hasattr(profile, "spot_price"):
        return None

    try:
        spot = float(profile.spot_price)
    except (ValueError, TypeError):
        return None

    if spot <= 0:
        return None

    cfg = config or SilverConfig()

    if signal in (SignalType.BULLISH_MOMENTUM, SignalType.BULLISH_REVERSAL):
        opt_type = "PE"
        spread_type = SpreadType.SHORT_PUT
    elif signal in (SignalType.BEARISH_MOMENTUM, SignalType.BEARISH_BREAKDOWN):
        opt_type = "CE"
        spread_type = SpreadType.SHORT_CALL
    else:
        return None

    target_delta = getattr(cfg, "target_delta", 0.35)
    strike_step = getattr(cfg, "strike_step", 1000.0)

    matched_row = find_target_delta_strike(
        spot=spot,
        opt_type=opt_type,
        chain_data=chain_data,
        target_delta=target_delta,
        strike_step=strike_step,
    )

    if matched_row is None:
        return None

    strike = int(round(float(matched_row["strike"])))
    ltp_val = matched_row.get(f"{opt_type.lower()}_ltp")
    if ltp_val is None:
        ltp_val = matched_row.get("ltp")
    try:
        premium = float(ltp_val or 0.0)
    except (ValueError, TypeError):
        return None

    if premium <= 0.0:
        return None

    trading_symbol = matched_row.get(f"{opt_type.lower()}_tradingsymbol") or matched_row.get("tradingsymbol")
    lot_size = getattr(cfg, "lot_size", 5)

    leg = OptionLeg(
        strike=strike,
        option_type=opt_type,
        action="SELL",
        quantity=lot_size,
        premium=premium,
        trading_symbol=trading_symbol,
    )

    net_credit = round(premium * lot_size, 2)
    sl_multiplier = getattr(cfg, "sl_multiplier", 1.5)
    sl_price = round(premium * sl_multiplier, 2)

    return SpreadPosition(
        spread_type=spread_type,
        legs=[leg],
        entry_net_credit=net_credit,
        entry_net_debit=0.0,
        max_profit=net_credit,
        max_loss=0.0,
        target_delta=target_delta,
        sl_price=sl_price,
        status="PENDING",
    )
