from datetime import date, datetime
import pytest
from strategy.options_model import compute_fractional_dte, black_76_price

def test_compute_fractional_dte_intraday_decay():
    target_expiry = date(2026, 11, 5) # Thursday
    
    # Thursday morning at 10:00 AM
    dt_morning = datetime(2026, 11, 5, 10, 0)
    dte_morning = compute_fractional_dte(target_expiry, dt_morning)
    
    # Thursday afternoon at 15:00 PM (same day, 5 hours later)
    dt_afternoon = datetime(2026, 11, 5, 15, 0)
    dte_afternoon = compute_fractional_dte(target_expiry, dt_afternoon)
    
    # Intraday DTE must decrease monotonically as time passes
    assert dte_morning > dte_afternoon > 0.0
    assert dte_morning < 1.0  # 0 DTE is less than 1 day

def test_compute_fractional_dte_future_expiry():
    target_expiry = date(2026, 11, 12) # Next week Thursday
    dt_current = datetime(2026, 11, 5, 10, 0) # Today Thursday 10:00 AM
    dte = compute_fractional_dte(target_expiry, dt_current)
    # 5 full future trading days (Fri, Mon, Tue, Wed, Thu) + 0.88 fraction today = 5.88
    assert 5.5 < dte < 6.0

def test_compute_fractional_dte_continuity_across_sessions():
    target_expiry = date(2026, 11, 5) # Thursday
    
    # Wednesday close at 15:30
    wed_close = datetime(2026, 11, 4, 15, 30)
    dte_wed_close = compute_fractional_dte(target_expiry, wed_close)
    
    # Thursday open at 09:15
    thu_open = datetime(2026, 11, 5, 9, 15)
    dte_thu_open = compute_fractional_dte(target_expiry, thu_open)
    
    # Both should represent exactly 1.0 trading day remaining
    assert pytest.approx(dte_wed_close, abs=1e-3) == 1.0
    assert pytest.approx(dte_thu_open, abs=1e-3) == 1.0

def test_black_76_pricing_call_and_put():
    spot = 24000.0
    strike = 24000.0
    dte_days = 4.0
    iv = 0.13
    r = 0.065
    
    call_price = black_76_price(spot, strike, dte_days, iv, r, "CE")
    put_price = black_76_price(spot, strike, dte_days, iv, r, "PE")
    
    assert call_price > 0.0
    assert put_price > 0.0
    # For ATM with r=6.5% and 4 DTE, Call and Put are very close with slight interest rate drift
    assert abs(call_price - put_price) < 20.0

def test_black_76_otm_decay():
    spot = 24000.0
    strike = 24500.0 # 500 points OTM Call
    iv = 0.13
    r = 0.065
    
    # 4 DTE vs 0.5 DTE
    price_high_dte = black_76_price(spot, strike, 4.0, iv, r, "CE")
    price_low_dte = black_76_price(spot, strike, 0.5, iv, r, "CE")
    
    assert price_high_dte > price_low_dte > 0.0

def test_black_76_input_validation():
    # Invalid opt_type
    with pytest.raises(ValueError, match="Invalid opt_type"):
        black_76_price(24000.0, 24000.0, 4.0, 0.13, 0.065, "INVALID")

    # Invalid non-positive spot
    with pytest.raises(ValueError, match="spot and strike must be positive"):
        black_76_price(0.0, 24000.0, 4.0, 0.13, 0.065, "CE")
    with pytest.raises(ValueError, match="spot and strike must be positive"):
        black_76_price(-24000.0, 24000.0, 4.0, 0.13, 0.065, "CE")

    # Invalid non-positive strike
    with pytest.raises(ValueError, match="spot and strike must be positive"):
        black_76_price(24000.0, 0.0, 4.0, 0.13, 0.065, "PE")
    with pytest.raises(ValueError, match="spot and strike must be positive"):
        black_76_price(24000.0, -24000.0, 4.0, 0.13, 0.065, "PE")
