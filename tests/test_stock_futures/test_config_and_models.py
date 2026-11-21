import pytest
from strategy.stock_futures.config import StockFuturesConfig, MOMENTUM_30_LOT_SIZES
from strategy.stock_futures.models import (
    SignalType,
    TradeSignal,
    StockPosition,
    calculate_futures_charges,
)


def test_momentum_30_config(tmp_path):
    cfg = StockFuturesConfig(lots_per_trade=2, universe_file=tmp_path / "empty.json")
    assert len(cfg.symbols) == 30
    assert "IDEA" in cfg.symbols
    assert "MCX" in cfg.symbols
    assert "ABB" in cfg.symbols
    assert cfg.get_unit_quantity("ABB") == MOMENTUM_30_LOT_SIZES["ABB"] * 2
    assert cfg.get_unit_quantity("IDEA") == MOMENTUM_30_LOT_SIZES["IDEA"] * 2
    assert cfg.get_unit_quantity("BSE") == MOMENTUM_30_LOT_SIZES["BSE"] * 2


def test_stock_position_long_pnl():
    pos = StockPosition(
        symbol="ABB",
        tradingsymbol="ABB_FUT",
        side="LONG",
        quantity=250,
        lots=2,
        entry_time="10:15",
        entry_price=1000.0,
        sl_price=990.0,
        target_price=1015.0,
        current_price=1000.0,
    )
    pos.update_current_price(1010.0)
    assert pos.gross_pnl == (1010.0 - 1000.0) * 250  # +2500.0

    pos.close(exit_price=1015.0, exit_time="11:30", exit_reason="TARGET_HIT")
    assert pos.status == "CLOSED"
    assert pos.exit_reason == "TARGET_HIT"
    assert pos.gross_pnl == 3750.0
    assert pos.charges > 0
    assert pos.net_pnl == 3750.0 - pos.charges


def test_stock_position_short_pnl():
    pos = StockPosition(
        symbol="MCX",
        tradingsymbol="MCX_FUT",
        side="SHORT",
        quantity=500,
        lots=2,
        entry_time="10:30",
        entry_price=2000.0,
        sl_price=2020.0,
        target_price=1970.0,
        current_price=2000.0,
    )
    pos.close(exit_price=2020.0, exit_time="11:00", exit_reason="SL_HIT")
    assert pos.gross_pnl == (2000.0 - 2020.0) * 500  # -10000.0
    assert pos.net_pnl < -10000.0  # includes charges


def test_calculate_futures_charges():
    charges = calculate_futures_charges(
        entry_price=1000.0,
        exit_price=1015.0,
        quantity=250,
        side="LONG",
        orders_count=2
    )
    assert charges > 40.0  # At least 20*2 brokerage plus STT and turnover charges
