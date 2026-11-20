import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.chain_analyzer import (
    compute_max_pain,
    compute_pcr,
    find_oi_walls,
    build_option_chain_profile,
)

@pytest.fixture
def sample_chain():
    # Mocked option chain replicating the webinar structure
    return [
        {"strike": 145000, "ce_oi": 100, "pe_oi": 1500, "ce_ltp": 9500.0, "pe_ltp": 700.0},
        {"strike": 148000, "ce_oi": 250, "pe_oi": 2200, "ce_ltp": 6800.0, "pe_ltp": 1200.0},
        {"strike": 150000, "ce_oi": 600, "pe_oi": 1800, "ce_ltp": 5000.0, "pe_ltp": 1600.0},
        {"strike": 154000, "ce_oi": 1200, "pe_oi": 1400, "ce_ltp": 2200.0, "pe_ltp": 2400.0},
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 1600.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 600.0, "pe_ltp": 6800.0},
        {"strike": 165000, "ce_oi": 1500, "pe_oi": 100, "ce_ltp": 200.0, "pe_ltp": 11000.0},
    ]

def test_pcr_calculation(sample_chain):
    # Total PE OI = 1500+2200+1800+1400+1100+400+100 = 8500
    # Total CE OI = 100+250+600+1200+2500+3800+1500 = 9950
    # PCR = 8500 / 9950 = 0.854
    pcr = compute_pcr(sample_chain)
    assert pytest.approx(pcr, 0.01) == 0.854

def test_find_oi_walls(sample_chain):
    call_wall, put_base = find_oi_walls(sample_chain)
    assert call_wall == 160000  # Max CE OI: 3800
    assert put_base == 148000   # Max PE OI: 2200

def test_compute_max_pain(sample_chain):
    max_pain = compute_max_pain(sample_chain)
    assert max_pain == 154000

def test_build_option_chain_profile(sample_chain):
    cfg = SilverConfig()
    profile = build_option_chain_profile(spot_price=153800.0, chain_data=sample_chain, config=cfg)
    assert profile.spot_price == 153800.0
    assert profile.call_wall == 160000
    assert profile.put_base == 148000
    assert profile.atm_strike == 154000
    assert 148000 <= profile.max_pain <= 160000
    assert profile.max_pain == 154000
    assert pytest.approx(profile.pcr, 0.01) == 0.854

def test_empty_chain_edge_cases():
    assert compute_pcr([]) == 1.0
    assert find_oi_walls([]) == (0, 0)
    assert compute_max_pain([]) == 0

    cfg = SilverConfig()
    profile = build_option_chain_profile(spot_price=150000.0, chain_data=[], config=cfg)
    assert profile.spot_price == 150000.0
    assert profile.pcr == 1.0
    assert profile.max_pain == 0
    assert profile.call_wall == 0
    assert profile.put_base == 0
    assert profile.atm_strike == 150000
