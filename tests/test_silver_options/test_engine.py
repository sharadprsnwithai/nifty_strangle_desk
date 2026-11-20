from datetime import time
from unittest.mock import MagicMock
import pandas as pd
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.engine import SilverOptionsEngine

@pytest.fixture
def sample_market_data():
    daily_df = pd.DataFrame({
        "close": [165000, 162000, 158000, 154000, 150000, 152000, 151000, 148000],
        "low": [164000, 161000, 157000, 153000, 149000, 151000, 150000, 147500],
        "rsi": [35.0, 30.0, 24.0, 21.0, 19.0, 23.0, 22.0, 25.0],
    })
    hourly_df = pd.DataFrame({
        "close": [148000, 148500, 149200, 150500],
        "ema_20": [149000, 149000, 149100, 149200],
    })
    chain_data = [
        {"strike": 148000, "ce_oi": 250, "pe_oi": 2200, "ce_ltp": 6800.0, "pe_ltp": 1200.0},
        {"strike": 154000, "ce_oi": 1200, "pe_oi": 1400, "ce_ltp": 2200.0, "pe_ltp": 2400.0},
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 1400.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 600.0, "pe_ltp": 6800.0},
    ]
    return daily_df, hourly_df, chain_data

def test_engine_lifecycle_paper_mode(sample_market_data):
    daily_df, hourly_df, chain_data = sample_market_data
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # Process entry step
    engine.on_market_tick(
        spot_price=153800.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(14, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is not None
    assert engine.active_position.status == "ACTIVE"

    # Simulate price expansion to target (155k CE 4500, 160k CE 500 -> spread value 4000)
    updated_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 4500.0, "pe_ltp": 800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 500.0, "pe_ltp": 4800.0},
    ]
    engine.on_market_tick(
        spot_price=159000.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=updated_chain,
        current_time=time(15, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None  # Closed on take-profit!
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].exit_reason == "TAKE_PROFIT"
    assert engine.closed_positions[0].realized_pnl > 0

def test_engine_stop_loss_exit(sample_market_data):
    daily_df, hourly_df, chain_data = sample_market_data
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # Entry step
    engine.on_market_tick(
        spot_price=153800.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(14, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None
    initial_net_debit = engine.active_position.entry_net_debit

    # Drop premium so spread collapses (e.g. 155k CE 500, 160k CE 300 -> spread 200, which is < 50% of debit 800)
    stop_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 500.0, "pe_ltp": 3800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 300.0, "pe_ltp": 6000.0},
    ]
    engine.on_market_tick(
        spot_price=151000.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=stop_chain,
        current_time=time(15, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].exit_reason == "STOP_LOSS"
    assert engine.closed_positions[0].realized_pnl < 0

def test_engine_mandatory_devolvement_cutoff(sample_market_data):
    daily_df, hourly_df, chain_data = sample_market_data
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # Entry step on expiry day before cutoff
    engine.on_market_tick(
        spot_price=153800.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(14, 0),
        is_expiry_day=True,
    )
    assert engine.active_position is not None

    # Tick at or past devolvement cutoff (17:00)
    engine.on_market_tick(
        spot_price=154000.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(17, 5),
        is_expiry_day=True,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 1
    assert engine.closed_positions[0].exit_reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"

def test_engine_position_held_when_no_exit_condition(sample_market_data):
    daily_df, hourly_df, chain_data = sample_market_data
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    engine.on_market_tick(
        spot_price=153800.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(14, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None

    # Moderate price movement: 155k CE 1600, 160k CE 700 -> spread 900 (entry 800)
    mod_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 1600.0, "pe_ltp": 2500.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 700.0, "pe_ltp": 6200.0},
    ]
    engine.on_market_tick(
        spot_price=154500.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=mod_chain,
        current_time=time(14, 30),
        is_expiry_day=False,
    )

    assert engine.active_position is not None
    assert engine.active_position.current_value == 900.0
    assert len(engine.closed_positions) == 0

def test_engine_no_signal_no_entry():
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)

    # Flat market data -> NO_SIGNAL
    daily_df = pd.DataFrame({
        "close": [150000.0] * 10,
        "low": [149000.0] * 10,
        "high": [151000.0] * 10,
        "rsi": [50.0] * 10,
    })
    hourly_df = pd.DataFrame({
        "close": [150000.0] * 10,
        "ema_20": [150000.0] * 10,
    })
    chain_data = [
        {"strike": 150000, "ce_oi": 1000, "pe_oi": 1000, "ce_ltp": 1000.0, "pe_ltp": 1000.0},
    ]

    engine.on_market_tick(
        spot_price=150000.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(10, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 0

def test_engine_live_execution_success(sample_market_data):
    daily_df, hourly_df, chain_data = sample_market_data
    cfg = SilverConfig()
    mock_kite = MagicMock()
    mock_kite.place_order.side_effect = ["order_buy_1", "order_sell_1", "order_exit_short", "order_exit_long"]
    engine = SilverOptionsEngine(kite_client=mock_kite, config=cfg, is_paper=False)

    # Entry
    engine.on_market_tick(
        spot_price=153800.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(14, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is not None
    assert mock_kite.place_order.call_count == 2

    # Exit on take profit
    updated_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 4500.0, "pe_ltp": 800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 500.0, "pe_ltp": 4800.0},
    ]
    engine.on_market_tick(
        spot_price=159000.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=updated_chain,
        current_time=time(15, 0),
        is_expiry_day=False,
    )
    assert engine.active_position is None
    assert mock_kite.place_order.call_count == 4
    assert len(engine.closed_positions) == 1

def test_engine_live_execution_entry_failure(sample_market_data):
    daily_df, hourly_df, chain_data = sample_market_data
    cfg = SilverConfig()
    mock_kite = MagicMock()
    mock_kite.place_order.side_effect = Exception("Margin limit reached")
    engine = SilverOptionsEngine(kite_client=mock_kite, config=cfg, is_paper=False)

    engine.on_market_tick(
        spot_price=153800.0,
        daily_df=daily_df,
        hourly_df=hourly_df,
        chain_data=chain_data,
        current_time=time(14, 0),
        is_expiry_day=False,
    )

    assert engine.active_position is None
    assert len(engine.closed_positions) == 0
