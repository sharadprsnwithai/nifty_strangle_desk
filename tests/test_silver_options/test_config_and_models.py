import pytest
from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.models import (
    SilverStrangleLeg,
    SilverStranglePosition,
    calculate_silver_mcx_costs,
)


def test_silver_config_defaults():
    config = SilverOptionsConfig()
    assert config.underlying == "SILVERM"
    assert config.exchange == "MCX"
    assert config.lot_size_kg == 5
    assert config.lots == 2
    assert config.multiplier == 10.0
    assert config.sl_per_lot == 3000.0
    # Stop loss calculated from number of lots
    assert config.max_portfolio_loss == 6000.0
    assert config.sl_pct == 0.40
    assert config.entry_time == "20:00"
    assert config.exit_time == "23:15"


def test_silver_config_dynamic_sl_from_lots():
    # 5 lots @ Rs 3000/lot -> Rs 15,000 max portfolio loss
    c5 = SilverOptionsConfig(lots=5, sl_per_lot=3000.0)
    assert c5.multiplier == 25.0
    assert c5.max_portfolio_loss == 15000.0

    # 10 lots @ Rs 2500/lot -> Rs 25,000 max portfolio loss
    c10 = SilverOptionsConfig(lots=10, sl_per_lot=2500.0)
    assert c10.multiplier == 50.0
    assert c10.max_portfolio_loss == 25000.0

    # 1 lot @ Rs 4000/lot -> Rs 4,000 max portfolio loss
    c1 = SilverOptionsConfig(lots=1, sl_per_lot=4000.0)
    assert c1.multiplier == 5.0
    assert c1.max_portfolio_loss == 4000.0


def test_silver_strangle_leg():
    leg = SilverStrangleLeg(
        symbol="SILVERM26NOV160000CE",
        strike=160000.0,
        option_type="CE",
        entry_price=1500.0,
        entry_time="20:00:00",
        stop_loss_price=2100.0,
        current_price=1200.0,
    )
    # 1500 entry - 1200 current = +300 points
    assert leg.points_captured == 300.0
    # Multiplier for 2 lots of SILVERM = 10 kg -> +300 pts * 10 kg = Rs 3,000
    assert leg.pnl(10.0) == 3000.0

    # Simulate exit
    leg.exit_price = 1100.0
    leg.status = "EOD"
    assert leg.points_captured == 400.0
    assert leg.pnl(10.0) == 4000.0


def test_silver_strangle_position_lifecycle():
    ce = SilverStrangleLeg(
        symbol="SILVERM26NOV160000CE",
        strike=160000.0,
        option_type="CE",
        entry_price=1500.0,
        entry_time="20:00:00",
        stop_loss_price=2100.0,
        current_price=1200.0,
    )
    pe = SilverStrangleLeg(
        symbol="SILVERM26NOV150000PE",
        strike=150000.0,
        option_type="PE",
        entry_price=1400.0,
        entry_time="20:00:00",
        stop_loss_price=1960.0,
        current_price=1100.0,
    )
    pos = SilverStranglePosition(
        trade_date="2026-11-23",
        underlying_spot=155000.0,
        lots=2,
        multiplier=10.0,
        max_loss_cap=6000.0,
        ce_leg=ce,
        pe_leg=pe,
        status="OPEN",
    )

    # CE +300 pts, PE +300 pts -> Total +600 pts
    assert pos.total_points == 600.0
    # 600 pts * 10 kg = Rs 6,000 P&L
    assert pos.total_pnl == 6000.0
    assert not pos.is_fully_closed()

    # Serialization roundtrip
    pos_dict = pos.to_dict()
    reloaded = SilverStranglePosition.from_dict(pos_dict)
    assert reloaded.trade_date == pos.trade_date
    assert reloaded.lots == 2
    assert reloaded.multiplier == 10.0
    assert reloaded.max_loss_cap == 6000.0
    assert reloaded.ce_leg.symbol == "SILVERM26NOV160000CE"
    assert reloaded.pe_leg.symbol == "SILVERM26NOV150000PE"


def test_calculate_silver_mcx_costs():
    # 200 points premium on 10 kg = Rs 2,000 turnover
    costs = calculate_silver_mcx_costs(premium_points=200.0, multiplier=10.0, is_sell=True)
    assert costs > 20.0  # includes brokerage (Rs 20) + exchange fee + STT + GST
    # Zero premium gives zero cost
    assert calculate_silver_mcx_costs(0.0, 10.0) == 0.0
