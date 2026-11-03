import math
from datetime import date, datetime
import numpy as np
from scipy.stats import norm

TRADING_MINUTES_PER_DAY = 375.0  # 09:15 to 15:30
ANNUAL_TRADING_DAYS = 252.0

def compute_fractional_dte(target_expiry: date, current_dt: datetime) -> float:
    """
    Computes continuous fractional DTE down to the minute using business trading days.
    
    Parameters:
        target_expiry: Expiry date
        current_dt: Current timestamp
    
    Returns:
        Equivalent DTE in trading days (e.g. 0.45 days for 0-DTE afternoon).
    """
    current_date = current_dt.date()
    calendar_days = (target_expiry - current_date).days

    if calendar_days < 0:
        return 1e-5

    # Compute remaining minutes in today's trading session (cutoff 15:30)
    current_minutes = current_dt.hour * 60 + current_dt.minute
    market_close_minutes = 15 * 60 + 30  # 15:30
    market_open_minutes = 9 * 60 + 15   # 09:15

    remaining_minutes_today = max(0.0, float(market_close_minutes - max(market_open_minutes, current_minutes)))
    fraction_of_day_today = min(1.0, remaining_minutes_today / TRADING_MINUTES_PER_DAY)

    if calendar_days == 0:
        return max(fraction_of_day_today, 1e-4)

    if np.is_busday(current_date):
        trading_days_ahead = int(np.busday_count(current_date, target_expiry))
        return max(float(trading_days_ahead) + fraction_of_day_today, 1e-4)
    else:
        trading_days_ahead = int(np.busday_count(current_date, target_expiry))
        return max(float(trading_days_ahead + 1), 1e-4)

def black_76_price(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float = 0.13,
    r: float = 0.065,
    opt_type: str = "CE"
) -> float:
    """
    Computes analytical Black-76 option premium.
    
    Parameters:
        spot: Underlying index price (e.g. 24000.0)
        strike: Option strike price (e.g. 24200.0)
        dte_days: Fractional trading days to expiry
        iv: Implied volatility (e.g. 0.13 for 13%)
        r: Annualized risk-free rate (default 6.5%)
        opt_type: 'CE' or 'PE'
    """
    if spot <= 0 or strike <= 0:
        raise ValueError(f"spot and strike must be positive numbers. Got spot={spot}, strike={strike}")

    opt_type_upper = opt_type.upper()
    if opt_type_upper not in ("CE", "PE"):
        raise ValueError(f"Invalid opt_type: {opt_type}. Must be 'CE' or 'PE'.")

    t = max(dte_days / ANNUAL_TRADING_DAYS, 1e-5)
    sigma = max(iv, 0.01)
    d = math.exp(-r * t)
    sqrt_t = math.sqrt(t)

    denom = sigma * sqrt_t
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / denom
    d2 = d1 - denom

    if opt_type_upper == "CE":
        price = d * (spot * norm.cdf(d1) - strike * norm.cdf(d2))
    else:
        price = d * (strike * norm.cdf(-d2) - spot * norm.cdf(-d1))

    return max(0.05, round(float(price), 2))
