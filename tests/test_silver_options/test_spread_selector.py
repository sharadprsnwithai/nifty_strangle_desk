import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType, SpreadType
from strategy.silver_options.chain_analyzer import build_option_chain_profile
from strategy.silver_options.spread_selector import select_spread

@pytest.fixture
def mock_chain():
    return [
        {"strike": 145000, "ce_oi": 100, "pe_oi": 1500, "ce_ltp": 9500.0, "pe_ltp": 700.0},
        {"strike": 148000, "ce_oi": 250, "pe_oi": 2200, "ce_ltp": 6800.0, "pe_ltp": 1200.0},
        {"strike": 154000, "ce_oi": 1200, "pe_oi": 1400, "ce_ltp": 2200.0, "pe_ltp": 2400.0},
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1200, "ce_ltp": 1400.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 600.0, "pe_ltp": 6800.0},
    ]

def test_select_bull_call_spread(mock_chain):
    cfg = SilverConfig(max_debit_width_ratio=0.35)
    profile = build_option_chain_profile(153800.0, mock_chain, cfg)
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, mock_chain, cfg)

    assert spread is not None
    assert spread.spread_type == SpreadType.BULL_CALL_SPREAD
    assert len(spread.legs) == 2
    long_leg, short_leg = spread.legs[0], spread.legs[1]

    # Matching webinar: Buy 155,000 CE (pivot) / Sell 160,000 CE (call wall)
    assert long_leg.action == "BUY" and long_leg.strike == 155000 and long_leg.option_type == "CE"
    assert short_leg.action == "SELL" and short_leg.strike == 160000 and short_leg.option_type == "CE"

    # Net debit = 1400 - 600 = 800 pts. Spread width = 5000 pts.
    # 800 <= 0.35 * 5000 (1750) -> Valid!
    assert spread.entry_net_debit == 800.0
    assert spread.max_profit == 4200.0
    assert spread.max_loss == 800.0

def test_reject_unfavorable_risk_reward():
    # If net debit exceeds 35% of spread width, reject the trade
    expensive_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 2500.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 500.0, "pe_ltp": 6800.0},
    ]
    cfg = SilverConfig(max_debit_width_ratio=0.35)
    profile = build_option_chain_profile(153800.0, expensive_chain, cfg)
    # Net debit = 2000 > 0.35 * 5000 (1750)
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, expensive_chain, cfg)
    assert spread is None

def test_select_bear_put_spread(mock_chain):
    cfg = SilverConfig(max_debit_width_ratio=0.35)
    profile = build_option_chain_profile(153800.0, mock_chain, cfg)
    spread = select_spread(SignalType.BEARISH_BREAKDOWN, profile, mock_chain, cfg)

    assert spread is not None
    assert spread.spread_type == SpreadType.BEAR_PUT_SPREAD
    assert len(spread.legs) == 2
    long_leg, short_leg = spread.legs[0], spread.legs[1]

    # Bear Put: Buy 155,000 PE (pivot) / Sell 148,000 PE (put base)
    assert long_leg.action == "BUY" and long_leg.strike == 155000 and long_leg.option_type == "PE"
    assert short_leg.action == "SELL" and short_leg.strike == 148000 and short_leg.option_type == "PE"

    # Net debit = 2800 - 1200 = 1600 pts. Spread width = 7000 pts.
    # 1600 <= 0.35 * 7000 (2450) -> Valid!
    assert spread.entry_net_debit == 1600.0
    assert spread.max_profit == 5400.0
    assert spread.max_loss == 1600.0

def test_reject_negative_or_zero_net_debit(mock_chain):
    # If net debit <= 0, trade should be rejected
    inverted_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 500.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 600.0, "pe_ltp": 6800.0},
    ]
    cfg = SilverConfig(max_debit_width_ratio=0.35)
    profile = build_option_chain_profile(153800.0, inverted_chain, cfg)
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, inverted_chain, cfg)
    assert spread is None

def test_reject_invalid_strike_relationship(mock_chain):
    cfg = SilverConfig()
    profile = build_option_chain_profile(153800.0, mock_chain, cfg)
    # Tamper profile so long_strike >= short_strike for Bull Call
    profile.call_wall = 150000
    profile.max_pain = 155000
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, mock_chain, cfg)
    assert spread is None

    # Tamper profile so long_strike <= short_strike for Bear Put
    profile.put_base = 158000
    profile.max_pain = 155000
    spread = select_spread(SignalType.BEARISH_BREAKDOWN, profile, mock_chain, cfg)
    assert spread is None

def test_reject_missing_strikes():
    cfg = SilverConfig()
    mock_chain = [
        {"strike": 150000, "ce_oi": 100, "pe_oi": 100, "ce_ltp": 500.0, "pe_ltp": 500.0}
    ]
    profile = build_option_chain_profile(150000.0, mock_chain, cfg)
    profile.call_wall = 160000  # not in chain
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, mock_chain, cfg)
    assert spread is None

def test_no_spread_on_unsupported_signal(mock_chain):
    cfg = SilverConfig()
    profile = build_option_chain_profile(153800.0, mock_chain, cfg)
    assert select_spread(SignalType.NO_SIGNAL, profile, mock_chain, cfg) is None
    assert select_spread(SignalType.RANGE_BOUND, profile, mock_chain, cfg) is None
