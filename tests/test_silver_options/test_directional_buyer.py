from datetime import datetime, date, timedelta
from unittest.mock import MagicMock
import pytest

from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.desk import SilverStrangleDesk
from strategy.silver_options.directional_buyer import (
    evaluate_vwap_volume_signal,
    SilverDirectionalBuyer,
    SilverBarbellDesk,
)
from strategy.silver_options.models import SilverDirectionalPosition
from strategy.silver_options.order_manager import SilverOrderManager


def _make_bullish_bars(n: int = 14, base: float = 155000.0):
    bars = []
    for i in range(n - 1):
        px = base + i * 20.0
        bars.append({"open": px - 10, "high": px + 15, "low": px - 15, "close": px, "volume": 100})
    # Strong surge above VWAP + EMA9>EMA21 + volume 300 > 1.3 * MA
    last_px = base + n * 40.0
    bars.append({"open": last_px - 30, "high": last_px + 20, "low": last_px - 40, "close": last_px, "volume": 350})
    return bars


def _make_bearish_bars(n: int = 14, base: float = 155000.0):
    bars = []
    for i in range(n - 1):
        px = base - i * 20.0
        bars.append({"open": px + 10, "high": px + 15, "low": px - 15, "close": px, "volume": 100})
    last_px = base - n * 40.0
    bars.append({"open": last_px + 30, "high": last_px + 40, "low": last_px - 20, "close": last_px, "volume": 350})
    return bars


def test_evaluate_vwap_volume_signal_detects_ce_pe_and_rejects_low_volume():
    bull = _make_bullish_bars()
    sig_ce = evaluate_vwap_volume_signal(bull, vol_mult=1.3, max_vwap_dist_pct=0.012)
    assert sig_ce is not None
    assert sig_ce["direction"] == "CE"

    bear = _make_bearish_bars()
    sig_pe = evaluate_vwap_volume_signal(bear, vol_mult=1.3, max_vwap_dist_pct=0.012)
    assert sig_pe is not None
    assert sig_pe["direction"] == "PE"

    # Low volume on final bar -> must NOT trigger
    bull_low_vol = list(bull)
    bull_low_vol[-1] = {**bull[-1], "volume": 80}
    assert evaluate_vwap_volume_signal(bull_low_vol, vol_mult=1.3, max_vwap_dist_pct=0.012) is None

    # Overextended (>1.2% above VWAP) -> must NOT chase
    bull_far = list(bull)
    bull_far[-1] = {**bull[-1], "close": bull[-1]["close"] * 1.03, "high": bull[-1]["close"] * 1.03}
    assert evaluate_vwap_volume_signal(bull_far, vol_mult=1.3, max_vwap_dist_pct=0.012) is None


def test_buyer_combo2a_gate_skips_high_dte_low_atr_and_allows_high_atr(tmp_path):
    today = datetime.now().date()
    far_expiry = today + timedelta(days=20)
    instruments = [
        {"name": "SILVERM", "instrument_type": "FUT", "tradingsymbol": "SILVERM_FUT", "expiry": far_expiry, "instrument_token": 1001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM_155000CE", "strike": 155000.0, "expiry": far_expiry, "instrument_token": 2001},
    ]

    # Case 1: DTE=20 (>10) AND ATR=2000 (~1.29% < 3.0%) -> SKIPPED
    mock_kite_calm = MagicMock()
    mock_kite_calm.instruments.return_value = instruments
    mock_kite_calm.quote.return_value = {"MCX:SILVERM_FUT": {"last_price": 155000.0}}
    mock_kite_calm.historical_data.return_value = [
        {"date": today - timedelta(days=2), "high": 156000.0, "low": 154000.0, "close": 155000.0},
        {"date": today - timedelta(days=1), "high": 156000.0, "low": 154000.0, "close": 155000.0},
    ]
    cfg = SilverOptionsConfig(lots=2, buyer_max_dte_days=10, buyer_min_atr_pct=3.0)
    om = SilverOrderManager(mode="PAPER", config=cfg)
    buyer = SilverDirectionalBuyer(om, config=cfg, state_file=tmp_path / "buyer_calm.json", kite_client=mock_kite_calm)
    now = datetime.combine(today, datetime.strptime("18:30", "%H:%M").time())
    assert buyer.evaluate_entry(now=now, bars_5m=_make_bullish_bars()) is False
    assert buyer.position is not None
    assert buyer.position.status == "SKIPPED"

    # Case 2: DTE=20 (>10) BUT ATR=5500 (~3.55% >= 3.0%) -> ENTERS CE!
    mock_kite_vol = MagicMock()
    mock_kite_vol.instruments.return_value = instruments
    mock_kite_vol.quote.side_effect = [
        {"MCX:SILVERM_FUT": {"last_price": 155000.0}},
        {"MCX:SILVERM_FUT": {"last_price": 155000.0}},
        {"MCX:SILVERM_155000CE": {"last_price": 3000.0, "oi": 200}},
    ]
    mock_kite_vol.historical_data.return_value = [
        {"date": today - timedelta(days=2), "high": 158000.0, "low": 152500.0, "close": 155000.0},
        {"date": today - timedelta(days=1), "high": 158000.0, "low": 152500.0, "close": 155000.0},
    ]
    buyer_vol = SilverDirectionalBuyer(om, config=cfg, state_file=tmp_path / "buyer_vol.json", kite_client=mock_kite_vol)
    assert buyer_vol.evaluate_entry(now=now, bars_5m=_make_bullish_bars()) is True
    assert buyer_vol.position is not None
    assert buyer_vol.position.status == "OPEN"
    assert buyer_vol.position.option_type == "CE"
    # Entry=3000, mult=10, cap=6000 -> hard_floor=2400 (> 30% SL 2100)!
    assert buyer_vol.position.stop_loss_price == 2400.0
    assert buyer_vol.position.target_price == 4800.0


def test_buyer_breakeven_trail_target_and_sl_lifecycle(tmp_path):
    today = datetime.now().date()
    mock_kite = MagicMock()
    cfg = SilverOptionsConfig(lots=2, buyer_sl_pct=0.30, buyer_sl_per_lot=3000.0, buyer_target_pct=0.60, buyer_trail_be_pct=0.25)
    om = SilverOrderManager(mode="PAPER", config=cfg)
    buyer = SilverDirectionalBuyer(om, config=cfg, state_file=tmp_path / "buyer_life.json", kite_client=mock_kite)
    buyer.position = SilverDirectionalPosition(
        trade_date=str(today), underlying_spot=155000.0, symbol="SILVERM_155000PE",
        strike=155000.0, option_type="PE", lots=2, multiplier=10.0,
        entry_price=1000.0, entry_time="18:15:00", stop_loss_price=700.0, initial_sl_price=700.0,
        target_price=1600.0, current_price=1000.0, max_loss_cap=6000.0, status="OPEN", mode="PAPER",
    )

    # Tick 1: +25% gain (1260 >= 1250) -> arms breakeven trail (SL -> 1000.0)
    mock_kite.quote.return_value = {"MCX:SILVERM_155000PE": {"last_price": 1260.0}}
    assert buyer.monitor_cycle(datetime.combine(today, datetime.strptime("19:00", "%H:%M").time())) is True
    assert buyer.position.breakeven_armed is True
    assert buyer.position.stop_loss_price == 1000.0

    # Tick 2: hits +60% target (1610 >= 1600) -> closes TARGET
    mock_kite.quote.return_value = {"MCX:SILVERM_155000PE": {"last_price": 1610.0}}
    assert buyer.monitor_cycle(datetime.combine(today, datetime.strptime("19:30", "%H:%M").time())) is False
    assert buyer.position.status == "TARGET"
    assert buyer.position.exit_price == 1610.0
    assert buyer.position.total_pnl == 6100.0


def test_barbell_desk_runs_buyer_and_strangle_concurrently_without_conflict(tmp_path):
    today = datetime.now().date()
    exp = today + timedelta(days=4)
    instruments = [
        {"name": "SILVERM", "instrument_type": "FUT", "tradingsymbol": "SILVERM_FUT", "expiry": exp, "instrument_token": 1001},
        {"name": "SILVERM", "instrument_type": "CE", "tradingsymbol": "SILVERM_158000CE", "strike": 158000.0, "expiry": exp, "instrument_token": 2001},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM_152000PE", "strike": 152000.0, "expiry": exp, "instrument_token": 3001},
        {"name": "SILVERM", "instrument_type": "PE", "tradingsymbol": "SILVERM_155000PE", "strike": 155000.0, "expiry": exp, "instrument_token": 3002},
    ]

    mock_kite = MagicMock()
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.EXCHANGE_MCX = "MCX"
    mock_kite.TRANSACTION_TYPE_SELL = "SELL"
    mock_kite.TRANSACTION_TYPE_BUY = "BUY"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.ORDER_TYPE_MARKET = "MARKET"
    mock_kite.PRODUCT_MIS = "MIS"
    mock_kite.VALIDITY_DAY = "DAY"
    mock_kite.instruments.return_value = instruments
    mock_kite.historical_data.return_value = [
        {"date": today - timedelta(days=2), "high": 157000.0, "low": 153000.0, "close": 155000.0},
        {"date": today - timedelta(days=1), "high": 157000.0, "low": 153000.0, "close": 155000.0},
    ]
    # Simulate Buyer already OPEN LONG (+2) on SILVERM_155000PE at 20:00 when Strangle tries to enter LIVE
    mock_kite.positions.return_value = {
        "net": [{"exchange": "MCX", "tradingsymbol": "SILVERM_155000PE", "quantity": 2}]
    }
    mock_kite.quote.return_value = {
        "MCX:SILVERM_FUT": {"last_price": 155000.0},
        "MCX:SILVERM_158000CE": {"last_price": 500.0, "oi": 250, "depth": {"buy": [{"price": 499.0}], "sell": [{"price": 501.0}]}},
        "MCX:SILVERM_152000PE": {"last_price": 500.0, "oi": 250, "depth": {"buy": [{"price": 499.0}], "sell": [{"price": 501.0}]}},
        "MCX:SILVERM_155000PE": {"last_price": 1200.0, "oi": 300, "depth": {"buy": [{"price": 1198.0}], "sell": [{"price": 1202.0}]}},
    }
    mock_kite.place_order.side_effect = ["SELL_CE_1", "SELL_PE_1"]
    mock_kite.order_history.return_value = [{"status": "COMPLETE", "filled_quantity": 2, "pending_quantity": 0, "average_price": 500.0}]

    cfg = SilverOptionsConfig(lots=2, vwap_entry_enabled=False, strike_mode="ATR", atr_multiplier=0.75, max_dte_days=10)
    om = SilverOrderManager(mode="LIVE", kite=mock_kite, config=cfg)
    buyer = SilverDirectionalBuyer(om, config=cfg, state_file=tmp_path / "b.json", kite_client=mock_kite)
    buyer.position = SilverDirectionalPosition(
        trade_date=str(today), underlying_spot=155000.0, symbol="SILVERM_155000PE",
        strike=155000.0, option_type="PE", lots=2, multiplier=10.0,
        entry_price=1200.0, entry_time="18:30:00", stop_loss_price=840.0, initial_sl_price=840.0,
        target_price=1920.0, current_price=1200.0, max_loss_cap=6000.0, status="OPEN", mode="LIVE",
    )
    strangle = SilverStrangleDesk(om, config=cfg, state_file=tmp_path / "s.json", kite_client=mock_kite)
    barbell = SilverBarbellDesk(buyer=buyer, strangle=strangle, config=cfg)

    status = barbell.step(now=datetime.combine(today, datetime.strptime("20:00", "%H:%M").time()))
    # Strangle MUST NOT abort due to Buyer's open LONG SILVERM_155000PE!
    assert status["buyer_active"] is True
    assert status["strangle_active"] is True
    assert strangle.position.status == "OPEN"
