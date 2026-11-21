import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import (
    SignalType,
    SpreadType,
    OptionChainProfile,
)
from strategy.silver_options.spread_selector import (
    find_target_delta_strike,
    select_spread,
)


@pytest.fixture
def sample_chain_with_deltas():
    return [
        {
            "strike": 144000,
            "pe_oi": 1500,
            "pe_ltp": 700.0,
            "pe_delta": -0.25,
            "pe_tradingsymbol": "SILVERM26NOV144000PE",
            "ce_oi": 100,
            "ce_ltp": 7500.0,
            "ce_delta": 0.75,
            "ce_tradingsymbol": "SILVERM26NOV144000CE",
        },
        {
            "strike": 146000,
            "pe_oi": 2200,
            "pe_ltp": 950.0,
            "pe_delta": -0.35,
            "pe_tradingsymbol": "SILVERM26NOV146000PE",
            "ce_oi": 250,
            "ce_ltp": 5500.0,
            "ce_delta": 0.65,
            "ce_tradingsymbol": "SILVERM26NOV146000CE",
        },
        {
            "strike": 148000,
            "pe_oi": 2800,
            "pe_ltp": 1300.0,
            "pe_delta": -0.42,
            "pe_tradingsymbol": "SILVERM26NOV148000PE",
            "ce_oi": 600,
            "ce_ltp": 3800.0,
            "ce_delta": 0.58,
            "ce_tradingsymbol": "SILVERM26NOV148000CE",
        },
        {
            "strike": 150000,
            "pe_oi": 3000,
            "pe_ltp": 1800.0,
            "pe_delta": -0.50,
            "pe_tradingsymbol": "SILVERM26NOV150000PE",
            "ce_oi": 3000,
            "ce_ltp": 1900.0,
            "ce_delta": 0.50,
            "ce_tradingsymbol": "SILVERM26NOV150000CE",
        },
        {
            "strike": 152000,
            "pe_oi": 800,
            "pe_ltp": 2500.0,
            "pe_delta": -0.58,
            "pe_tradingsymbol": "SILVERM26NOV152000PE",
            "ce_oi": 2500,
            "ce_ltp": 1350.0,
            "ce_delta": 0.42,
            "ce_tradingsymbol": "SILVERM26NOV152000CE",
        },
        {
            "strike": 155000,
            "pe_oi": 400,
            "pe_ltp": 4200.0,
            "pe_delta": -0.66,
            "pe_tradingsymbol": "SILVERM26NOV155000PE",
            "ce_oi": 3800,
            "ce_ltp": 900.0,
            "ce_delta": 0.35,
            "ce_tradingsymbol": "SILVERM26NOV155000CE",
        },
        {
            "strike": 157000,
            "pe_oi": 200,
            "pe_ltp": 5800.0,
            "pe_delta": -0.73,
            "pe_tradingsymbol": "SILVERM26NOV157000PE",
            "ce_oi": 4200,
            "ce_ltp": 600.0,
            "ce_delta": 0.27,
            "ce_tradingsymbol": "SILVERM26NOV157000CE",
        },
    ]


@pytest.fixture
def sample_profile():
    return OptionChainProfile(
        spot_price=150000.0,
        pcr=1.0,
        max_pain=150000,
        call_wall=157000,
        put_base=146000,
        atm_strike=150000,
    )


def test_bullish_signal_selects_target_delta_pe(sample_chain_with_deltas, sample_profile):
    cfg = SilverConfig(lot_size=5, target_delta=0.35, sl_multiplier=1.5)

    # Bullish Momentum -> Sells 0.35 Delta Put (146000 PE)
    spread = select_spread(SignalType.BULLISH_MOMENTUM, sample_profile, sample_chain_with_deltas, cfg)

    assert spread is not None
    assert spread.spread_type == SpreadType.SHORT_PUT
    assert len(spread.legs) == 1

    short_leg = spread.legs[0]
    assert short_leg.strike == 146000
    assert short_leg.option_type == "PE"
    assert short_leg.action == "SELL"
    assert short_leg.quantity == 5
    assert short_leg.premium == 950.0
    assert short_leg.trading_symbol == "SILVERM26NOV146000PE"

    assert spread.entry_net_credit == 4750.0  # 950 * 5
    assert spread.entry_net_debit == 0.0
    assert spread.max_profit == 4750.0
    assert spread.sl_price == 1425.0  # 950 * 1.5
    assert spread.target_delta == 0.35
    assert spread.status == "PENDING"

    # Backward compatibility alias BULLISH_REVERSAL also produces SHORT_PUT
    spread_reversal = select_spread(SignalType.BULLISH_REVERSAL, sample_profile, sample_chain_with_deltas, cfg)
    assert spread_reversal is not None
    assert spread_reversal.spread_type == SpreadType.SHORT_PUT
    assert spread_reversal.legs[0].strike == 146000


def test_bearish_signal_selects_target_delta_ce(sample_chain_with_deltas, sample_profile):
    cfg = SilverConfig(lot_size=5, target_delta=0.35, sl_multiplier=1.5)

    # Bearish Momentum -> Sells 0.35 Delta Call (155000 CE)
    spread = select_spread(SignalType.BEARISH_MOMENTUM, sample_profile, sample_chain_with_deltas, cfg)

    assert spread is not None
    assert spread.spread_type == SpreadType.SHORT_CALL
    assert len(spread.legs) == 1

    short_leg = spread.legs[0]
    assert short_leg.strike == 155000
    assert short_leg.option_type == "CE"
    assert short_leg.action == "SELL"
    assert short_leg.quantity == 5
    assert short_leg.premium == 900.0
    assert short_leg.trading_symbol == "SILVERM26NOV155000CE"

    assert spread.entry_net_credit == 4500.0  # 900 * 5
    assert spread.entry_net_debit == 0.0
    assert spread.max_profit == 4500.0
    assert spread.sl_price == 1350.0  # 900 * 1.5
    assert spread.target_delta == 0.35
    assert spread.status == "PENDING"

    # Backward compatibility alias BEARISH_BREAKDOWN also produces SHORT_CALL
    spread_breakdown = select_spread(SignalType.BEARISH_BREAKDOWN, sample_profile, sample_chain_with_deltas, cfg)
    assert spread_breakdown is not None
    assert spread_breakdown.spread_type == SpreadType.SHORT_CALL
    assert spread_breakdown.legs[0].strike == 155000


def test_no_signal_returns_none(sample_chain_with_deltas, sample_profile):
    cfg = SilverConfig()
    assert select_spread(SignalType.NO_SIGNAL, sample_profile, sample_chain_with_deltas, cfg) is None
    assert select_spread(SignalType.RANGE_BOUND, sample_profile, sample_chain_with_deltas, cfg) is None
    assert select_spread(None, sample_profile, sample_chain_with_deltas, cfg) is None


def test_empty_or_invalid_chain_data_returns_none(sample_profile):
    cfg = SilverConfig()
    assert select_spread(SignalType.BULLISH_MOMENTUM, sample_profile, [], cfg) is None
    assert select_spread(SignalType.BULLISH_MOMENTUM, sample_profile, None, cfg) is None

    # Chain with corrupt rows
    corrupt_chain = [{"invalid": 123}, {"strike": "not_a_number", "pe_ltp": 100.0}]
    assert select_spread(SignalType.BULLISH_MOMENTUM, sample_profile, corrupt_chain, cfg) is None

    # Invalid profile
    assert select_spread(SignalType.BULLISH_MOMENTUM, None, [{"strike": 146000, "pe_ltp": 100.0}], cfg) is None
    zero_spot_profile = OptionChainProfile(0.0, 1.0, 0, 0, 0, 0)
    assert select_spread(SignalType.BULLISH_MOMENTUM, zero_spot_profile, [{"strike": 146000, "pe_ltp": 100.0}], cfg) is None


def test_delta_matching_chain_provided_delta(sample_chain_with_deltas):
    # Test PE matching with exact chain deltas
    row_pe = find_target_delta_strike(
        spot=150000.0,
        opt_type="PE",
        chain_data=sample_chain_with_deltas,
        target_delta=0.35,
    )
    assert row_pe is not None
    assert row_pe["strike"] == 146000
    assert row_pe["pe_delta"] == -0.35

    # Test CE matching with exact chain deltas
    row_ce = find_target_delta_strike(
        spot=150000.0,
        opt_type="CE",
        chain_data=sample_chain_with_deltas,
        target_delta=0.35,
    )
    assert row_ce is not None
    assert row_ce["strike"] == 155000
    assert row_ce["ce_delta"] == 0.35

    # Test alternative target delta = 0.25
    row_pe_25 = find_target_delta_strike(
        spot=150000.0,
        opt_type="PE",
        chain_data=sample_chain_with_deltas,
        target_delta=0.25,
    )
    assert row_pe_25 is not None
    assert row_pe_25["strike"] == 144000


def test_delta_matching_black_76_computed_fallback():
    # Chain with NO deltas provided (fallback to Black-76)
    chain_no_deltas = [
        {"strike": 142000, "pe_ltp": 450.0, "ce_ltp": 9200.0},
        {"strike": 144000, "pe_ltp": 680.0, "ce_ltp": 7400.0},
        {"strike": 145000, "pe_ltp": 820.0, "ce_ltp": 6500.0},
        {"strike": 146000, "pe_ltp": 1000.0, "ce_ltp": 5600.0},
        {"strike": 148000, "pe_ltp": 1450.0, "ce_ltp": 4100.0},
        {"strike": 150000, "pe_ltp": 2000.0, "ce_ltp": 2100.0},
        {"strike": 152000, "pe_ltp": 2800.0, "ce_ltp": 1500.0},
        {"strike": 154000, "pe_ltp": 3800.0, "ce_ltp": 1100.0},
        {"strike": 155000, "pe_ltp": 4400.0, "ce_ltp": 920.0},
        {"strike": 156000, "pe_ltp": 5100.0, "ce_ltp": 780.0},
    ]

    spot = 150000.0
    # Spot 150k, 20 DTE, 32% IV:
    # Black-76 PE delta at 146k is ~ -0.344 (closest to 0.35)
    row_pe = find_target_delta_strike(
        spot=spot,
        opt_type="PE",
        chain_data=chain_no_deltas,
        target_delta=0.35,
        dte_days=20.0,
        iv=0.32,
    )
    assert row_pe is not None
    assert row_pe["strike"] == 146000

    # Black-76 CE delta at 155k is ~ 0.344 (closest to 0.35)
    row_ce = find_target_delta_strike(
        spot=spot,
        opt_type="CE",
        chain_data=chain_no_deltas,
        target_delta=0.35,
        dte_days=20.0,
        iv=0.32,
    )
    assert row_ce is not None
    assert row_ce["strike"] == 155000


def test_otm_atm_filtering():
    # Verify OTM/ATM bounds:
    # For PE: strike <= spot * 1.02
    # For CE: strike >= spot * 0.98
    spot = 150000.0

    # ITM Put (strike 160,000 > 150,000 * 1.02 = 153,000) should be excluded even with target delta
    chain_with_itm = [
        {"strike": 160000, "pe_ltp": 11000.0, "pe_delta": -0.35},
        {"strike": 146000, "pe_ltp": 950.0, "pe_delta": -0.30},
    ]
    row_pe = find_target_delta_strike(spot, "PE", chain_with_itm, target_delta=0.35)
    assert row_pe is not None
    assert row_pe["strike"] == 146000  # 160000 rejected because ITM

    # ITM Call (strike 140,000 < 150,000 * 0.98 = 147,000) should be excluded even with target delta
    chain_with_itm_ce = [
        {"strike": 140000, "ce_ltp": 11500.0, "ce_delta": 0.35},
        {"strike": 155000, "ce_ltp": 900.0, "ce_delta": 0.30},
    ]
    row_ce = find_target_delta_strike(spot, "CE", chain_with_itm_ce, target_delta=0.35)
    assert row_ce is not None
    assert row_ce["strike"] == 155000  # 140000 rejected because ITM


def test_reject_zero_or_non_positive_ltp():
    spot = 150000.0
    zero_ltp_chain = [
        {"strike": 146000, "pe_ltp": 0.0, "pe_delta": -0.35},
        {"strike": 145000, "pe_ltp": -10.0, "pe_delta": -0.35},
    ]
    assert find_target_delta_strike(spot, "PE", zero_ltp_chain, target_delta=0.35) is None


def test_trading_symbol_fallback():
    # Test that tradingsymbol is used when specific pe/ce_tradingsymbol is missing
    chain = [
        {"strike": 146000, "pe_ltp": 950.0, "pe_delta": -0.35, "tradingsymbol": "SILVERM_FALLBACK_PE"},
    ]
    profile = OptionChainProfile(150000.0, 1.0, 150000, 155000, 145000, 150000)
    cfg = SilverConfig()

    spread = select_spread(SignalType.BULLISH_MOMENTUM, profile, chain, cfg)
    assert spread is not None
    assert spread.legs[0].trading_symbol == "SILVERM_FALLBACK_PE"
