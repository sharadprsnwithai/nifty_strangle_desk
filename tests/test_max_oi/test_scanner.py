import json
import pytest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from strategy.max_oi.config import MaxOIConfig
from strategy.max_oi.models import MaxOICandidate, OptionBuySignal, OptionPosition
from strategy.max_oi.scanner import MaxOIScanner
from strategy.max_oi.desk import MaxOIDesk


# ==============================================================================
# 1. Config & Model Tests
# ==============================================================================

def test_max_oi_config_defaults(tmp_path):
    cfg = MaxOIConfig(snapshots_dir=tmp_path / "snaps", state_file=tmp_path / "state.json")
    assert cfg.proximity_pct == 0.02
    assert cfg.unwinding_pct == 0.20
    assert cfg.sl_pct == 0.50
    assert cfg.max_holding_days == 2
    assert cfg.post_15_only is True
    assert (tmp_path / "snaps").exists()


def test_option_position_lifecycle():
    pos = OptionPosition(
        symbol="RELIANCE",
        side="CE",
        tradingsymbol="RELIANCE26NOVCALL",
        strike=1300.0,
        lot_size=250,
        quantity=250,
        entry_price=40.0,
        sl_price=20.0,
        target_price=80.0,
        entry_time="09:35",
        entry_date="2026-11-20",
        max_exit_date="2026-11-22",
        current_price=40.0,
    )
    assert pos.status == "OPEN"
    assert pos.gross_pnl == 0.0

    # Price update
    pos.update_price(50.0)
    assert pos.current_price == 50.0
    assert pos.gross_pnl == (50.0 - 40.0) * 250

    # Close position on target
    pos.close(exit_price=80.0, exit_time="14:00", exit_date="2026-11-20", exit_reason="TARGET_HIT")
    assert pos.status == "CLOSED"
    assert pos.exit_price == 80.0
    assert pos.exit_reason == "TARGET_HIT"
    assert pos.gross_pnl == (80.0 - 40.0) * 250
    assert pos.charges > 0
    assert pos.net_pnl == pos.gross_pnl - pos.charges


# ==============================================================================
# 2. Scanner Logic Tests
# ==============================================================================

def test_resolve_target_monthly_expiry():
    scanner = MaxOIScanner(kite=None)
    mock_instruments = [
        {"name": "RELIANCE", "instrument_type": "CE", "expiry": date(2026, 11, 26)},
        {"name": "RELIANCE", "instrument_type": "PE", "expiry": date(2026, 12, 31)},
        {"name": "RELIANCE", "instrument_type": "FUT", "expiry": date(2026, 11, 26)},
        {"name": "RELIANCE", "instrument_type": "CE", "expiry": date(2026, 10, 29)},  # Past
    ]
    resolved = scanner.resolve_target_monthly_expiry(mock_instruments, check_date=date(2026, 11, 15))
    assert resolved == date(2026, 11, 26)


def test_oi_snapshot_save_and_load(tmp_path):
    snap_dir = tmp_path / "snaps"
    cfg = MaxOIConfig(snapshots_dir=snap_dir, state_file=tmp_path / "state.json")
    scanner = MaxOIScanner(kite=None, config=cfg)

    # Save day 1 snapshot
    snap_data = {"RELIANCE26NOV1300CE": 1000000, "RELIANCE26NOV1200PE": 800000}
    scanner.save_oi_snapshot(snap_data, snap_date=date(2026, 11, 19))

    # Save day 2 snapshot
    snap_data2 = {"RELIANCE26NOV1300CE": 900000, "RELIANCE26NOV1200PE": 850000}
    scanner.save_oi_snapshot(snap_data2, snap_date=date(2026, 11, 20))

    # Loading prior to day 2 (Nov 20) should return day 1 snapshot
    loaded = scanner.load_previous_oi_snapshot(current_date=date(2026, 11, 20))
    assert loaded.get("RELIANCE26NOV1300CE") == 1000000

    # Loading prior to day 3 (Nov 21) should return day 2 snapshot
    loaded2 = scanner.load_previous_oi_snapshot(current_date=date(2026, 11, 21))
    assert loaded2.get("RELIANCE26NOV1300CE") == 900000


def test_scanner_market_closed_guard():
    """Verifies that no Kite API calls are made if market is closed and ignore_holidays is False."""
    mock_kite = MagicMock()
    scanner = MaxOIScanner(kite=mock_kite)

    with patch("strategy.max_oi.scanner.is_market_open", return_value=(False, "Post-market")):
        result = scanner.scan(ignore_holidays=False)
        assert result["status"] == "MARKET_CLOSED"
        # Zero API calls to kite!
        mock_kite.instruments.assert_not_called()
        mock_kite.quote.assert_not_called()


def test_scanner_pre_15th_idle():
    """Verifies that scanner stays idle in the first half of the month."""
    mock_kite = MagicMock()
    scanner = MaxOIScanner(kite=mock_kite)

    with patch("strategy.max_oi.scanner.is_market_open", return_value=(True, "Market OPEN")):
        result = scanner.scan(check_date=date(2026, 11, 10), ignore_date_filter=False, ignore_holidays=True)
        assert result["status"] == "IDLE_PRE_15TH"
        mock_kite.instruments.assert_not_called()


def test_scanner_proximity_and_signal_generation(tmp_path):
    """
    Tests complete scanning logic:
    - RELIANCE spot is 1300.
    - Max CE is 1300 (0% distance, tested).
    - Previous CE OI was 1,000,000. Live CE OI is 750,000 (-25% panic unwinding).
    - Result: Candidate found + Actionable Call Buy Signal triggered!
    """
    snap_dir = tmp_path / "snaps"
    cfg = MaxOIConfig(
        proximity_pct=0.02,
        unwinding_pct=0.20,
        snapshots_dir=snap_dir,
        state_file=tmp_path / "state.json",
        post_15_only=True,
    )

    mock_kite = MagicMock()
    target_exp = date(2026, 11, 26)

    # Mock instruments
    mock_instruments = [
        {"name": "RELIANCE", "instrument_type": "CE", "expiry": target_exp, "strike": 1300.0, "tradingsymbol": "RELIANCE26NOV1300CE", "lot_size": 250, "instrument_token": 101},
        {"name": "RELIANCE", "instrument_type": "CE", "expiry": target_exp, "strike": 1320.0, "tradingsymbol": "RELIANCE26NOV1320CE", "lot_size": 250, "instrument_token": 102},
        {"name": "RELIANCE", "instrument_type": "PE", "expiry": target_exp, "strike": 1200.0, "tradingsymbol": "RELIANCE26NOV1200PE", "lot_size": 250, "instrument_token": 103},
        {"name": "RELIANCE", "instrument_type": "PE", "expiry": target_exp, "strike": 1250.0, "tradingsymbol": "RELIANCE26NOV1250PE", "lot_size": 250, "instrument_token": 104},
    ]
    mock_kite.instruments.return_value = mock_instruments

    # Mock quotes
    def mock_quote(symbols):
        quotes = {}
        for s in symbols:
            if s == "NSE:RELIANCE":
                quotes[s] = {"last_price": 1305.0}  # Breached above 1300 CE!
            elif s == "NFO:RELIANCE26NOV1300CE":
                quotes[s] = {"oi": 750000, "last_price": 35.0}  # -25% unwinding vs 1,000,000
            elif s == "NFO:RELIANCE26NOV1320CE":
                quotes[s] = {"oi": 200000, "last_price": 20.0}
            elif s == "NFO:RELIANCE26NOV1200PE":
                quotes[s] = {"oi": 900000, "last_price": 10.0}
            elif s == "NFO:RELIANCE26NOV1250PE":
                quotes[s] = {"oi": 500000, "last_price": 18.0}
        return quotes

    mock_kite.quote.side_effect = mock_quote

    scanner = MaxOIScanner(kite=mock_kite, config=cfg)

    # Seed previous day OI snapshot (Nov 19)
    prev_snapshot = {
        "RELIANCE26NOV1300CE": 1000000,
        "RELIANCE26NOV1200PE": 900000,
    }
    scanner.save_oi_snapshot(prev_snapshot, snap_date=date(2026, 11, 19))

    # Scan on Nov 20 (post 15th)
    result = scanner.scan(
        stocks=["RELIANCE"],
        check_date=date(2026, 11, 20),
        ignore_holidays=True,
    )

    assert result["status"] == "COMPLETED"
    assert result["candidates_count"] == 1
    cand = result["candidates"][0]
    assert cand.symbol == "RELIANCE"
    assert cand.is_near_ce is True
    assert cand.ce_oi_change_pct == -25.0

    # Actionable signal verified
    assert result["signals_count"] == 1
    sig = result["signals"][0]
    assert sig.symbol == "RELIANCE"
    assert sig.side == "CE"
    assert sig.breached_strike == 1300.0
    assert sig.oi_unwinding_pct == -25.0
    assert sig.atm_ltp == 35.0
    assert sig.sl_price == 17.50  # 50% SL
    assert sig.target_price == 70.0  # 2x target


# ==============================================================================
# 3. Desk & Alerting Tests
# ==============================================================================

def test_desk_formatting():
    desk = MaxOIDesk()

    # Candidate formatting
    cand = MaxOICandidate(
        symbol="TATASTEEL",
        spot=150.0,
        expiry=date(2026, 11, 26),
        max_ce_strike=155.0,
        max_ce_oi=5000000,
        ce_oi_change_pct=-5.2,
        ce_dist_pct=3.3,
        is_near_ce=True,
        max_pe_strike=140.0,
        max_pe_oi=4000000,
        pe_dist_pct=-6.6,
        is_near_pe=False,
    )
    summary_msg = desk.format_candidate_summary_message([cand], "2026-11-26")
    assert "MAX OI PROXIMITY WATCHLIST" in summary_msg
    assert "TATASTEEL" in summary_msg
    assert "155 CALL" in summary_msg

    # Signal formatting
    sig = OptionBuySignal(
        symbol="POLYCAB",
        side="CE",
        spot_price=7150.0,
        breached_strike=7100.0,
        oi_unwinding_pct=-26.4,
        atm_strike=7100.0,
        atm_tradingsymbol="POLYCAB26NOV7100CE",
        atm_token=201,
        atm_ltp=120.0,
        lot_size=125,
        sl_price=60.0,
        target_price=240.0,
        trigger_time="09:35",
        expiry_date="2026-11-26",
        max_exit_date="2026-11-23",
    )
    signal_msg = desk.format_signal_message(sig)
    assert "MAX OI BREAKOUT SIGNAL: BULLISH CALL BUY" in signal_msg
    assert "POLYCAB" in signal_msg
    assert "-26.4%" in signal_msg
    assert "Rs 60.00" in signal_msg
    assert "Rs 240.00" in signal_msg


def test_desk_monitoring_sl_and_target(tmp_path):
    mock_kite = MagicMock()
    mock_notifier = MagicMock()
    state_file = tmp_path / "desk_state.json"

    cfg = MaxOIConfig(state_file=state_file)
    desk = MaxOIDesk(config=cfg, notifier=mock_notifier, kite=mock_kite, ignore_holidays=True)

    # Position 1: Target hit
    pos1 = OptionPosition(
        symbol="RELIANCE",
        side="CE",
        tradingsymbol="RELIANCE26NOV1300CE",
        strike=1300.0,
        lot_size=250,
        quantity=250,
        entry_price=40.0,
        sl_price=20.0,
        target_price=80.0,
        entry_time="09:35",
        entry_date="2026-11-20",
        max_exit_date="2026-11-25",
        current_price=40.0,
    )
    desk.positions[pos1.tradingsymbol] = pos1

    mock_kite.quote.return_value = {
        "NFO:RELIANCE26NOV1300CE": {"last_price": 85.0}  # Target hit!
    }

    with patch("strategy.max_oi.desk.is_market_open", return_value=(True, "Market OPEN")):
        closed = desk.monitor_open_positions()
        assert len(closed) == 1
        assert closed[0].status == "CLOSED"
        assert closed[0].exit_reason == "TARGET_HIT"
        assert closed[0].exit_price == 85.0
        mock_notifier.send_message.assert_called_once()
        assert "OPTION BUY TARGET HIT" in mock_notifier.send_message.call_args[0][0]
