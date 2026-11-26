from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from strategy.silver_options.chain import (
    compute_silver_daily_atr,
    find_silver_atm_option,
    find_silver_strangle_strikes,
    resolve_silver_context,
)
from strategy.silver_options.config import SilverOptionsConfig
from strategy.silver_options.desk import SilverStrangleDesk
from strategy.silver_options.directional_buyer import SilverBarbellDesk, SilverDirectionalBuyer
from strategy.silver_options.order_manager import SilverOrderManager


def _build_mcx_instruments(today: date, expiry_offset_days: int = 5):
    expiry = today + timedelta(days=expiry_offset_days)
    fut_expiry = expiry + timedelta(days=2)
    return [
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOVFUT",
            "instrument_type": "FUT",
            "instrument_token": 111001,
            "expiry": fut_expiry,
            "strike": 0.0,
        },
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOV155000CE",
            "instrument_type": "CE",
            "instrument_token": 222001,
            "expiry": expiry,
            "strike": 155000.0,
        },
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOV157000CE",
            "instrument_type": "CE",
            "instrument_token": 222002,
            "expiry": expiry,
            "strike": 157000.0,
        },
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOV158000CE",
            "instrument_type": "CE",
            "instrument_token": 222003,
            "expiry": expiry,
            "strike": 158000.0,
        },
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOV153000PE",
            "instrument_type": "PE",
            "instrument_token": 222004,
            "expiry": expiry,
            "strike": 153000.0,
        },
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOV152000PE",
            "instrument_type": "PE",
            "instrument_token": 222005,
            "expiry": expiry,
            "strike": 152000.0,
        },
        {
            "name": "SILVERM",
            "tradingsymbol": "SILVERM26NOV155000PE",
            "instrument_type": "PE",
            "instrument_token": 222006,
            "expiry": expiry,
            "strike": 155000.0,
        },
    ]


def _bullish_5m_bars():
    bars = [
        {"high": 155100.0, "low": 154900.0, "close": 155000.0, "volume": 100.0}
        for _ in range(29)
    ]
    bars.append({"high": 155600.0, "low": 155300.0, "close": 155500.0, "volume": 500.0})
    return bars


def _quote_payload(last_price: float, bid: float, ask: float, oi: int = 500, vol: int = 1000):
    return {
        "last_price": last_price,
        "oi": oi,
        "volume": vol,
        "depth": {
            "buy": [{"price": bid, "quantity": 10}],
            "sell": [{"price": ask, "quantity": 10}],
        },
    }


def test_run_scheduler_exact_exit_time_no_spin_and_triggers_eod(tmp_path):
    cfg = SilverOptionsConfig(
        vwap_entry_enabled=False,
        strike_mode="PERCENT",
        entry_time="20:00",
        exit_time="23:10",
        poll_seconds=5,
        lots=2,
    )
    om = SilverOrderManager(mode="PAPER", config=cfg)
    desk = SilverStrangleDesk(order_manager=om, config=cfg, state_file=tmp_path / "strangle.json")

    entry_dt = datetime(2026, 11, 26, 20, 0, 0)
    assert desk.enter_strangle(now=entry_dt) is True
    assert desk.position is not None and desk.position.status == "OPEN"

    class _StopLoop(BaseException):
        pass

    sleep_calls = []
    exact_exit_dt = datetime(2026, 11, 26, 23, 10, 0)

    def _fake_sleep(secs):
        sleep_calls.append(secs)
        raise _StopLoop()

    with patch("strategy.silver_options.desk.datetime") as mock_dt, patch(
        "strategy.silver_options.desk.time.sleep", side_effect=_fake_sleep
    ):
        mock_dt.now.return_value = exact_exit_dt
        with pytest.raises(_StopLoop):
            desk.run_scheduler(ignore_holidays=False)

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 60
    assert desk.position.status == "CLOSED"
    assert desk.position.ce_leg.exit_reason == "EOD"
    assert desk.position.pe_leg.exit_reason == "EOD"


def test_strangle_partial_exit_resumes_when_price_reverts(tmp_path):
    today = datetime(2026, 11, 26, 20, 15, 0)
    instruments = _build_mcx_instruments(today.date(), expiry_offset_days=5)

    kite = MagicMock()
    kite.instruments.return_value = instruments
    kite.positions.return_value = {"net": []}

    quote_sequence = [
        # Entry spot lookup (resolve_silver_context)
        {
            "MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0),
        },
        # Entry option chain lookup (find_silver_strangle_strikes)
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV158000CE": _quote_payload(400.0, 399.0, 401.0),
            "MCX:SILVERM26NOV153000PE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV152000PE": _quote_payload(400.0, 399.0, 401.0),
        },
        # Monitor poll 1: CE hits SL (>= 700), PE stays calm
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(710.0, 709.0, 711.0),
            "MCX:SILVERM26NOV153000PE": _quote_payload(450.0, 449.0, 451.0),
        },
        # Monitor poll 2: CE price reverts BELOW SL (600 < 700) -> partial exit MUST still resume!
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(600.0, 599.0, 601.0),
            "MCX:SILVERM26NOV153000PE": _quote_payload(450.0, 449.0, 451.0),
        },
    ]
    kite.quote.side_effect = quote_sequence

    order_seq = ["E_CE", "E_PE", "SL_LIMIT_1", "SL_MKT_1", "SL_RESUME_2"]
    kite.place_order.side_effect = lambda **kw: order_seq.pop(0)
    kite.cancel_order.return_value = True

    history_map = {
        "E_CE": [{"status": "COMPLETE", "filled_quantity": 2, "pending_quantity": 0, "average_price": 500.0}],
        "E_PE": [{"status": "COMPLETE", "filled_quantity": 2, "pending_quantity": 0, "average_price": 500.0}],
        # Partial fill 1/2 lots on initial SL LIMIT + 0 fill on escalation MARKET
        "SL_LIMIT_1": [{"status": "OPEN", "filled_quantity": 1, "pending_quantity": 1, "average_price": 710.0}],
        "SL_MKT_1": [{"status": "CANCELLED", "filled_quantity": 0, "pending_quantity": 0, "average_price": 0.0}],
        # Remaining 1/2 lot filled on resume poll
        "SL_RESUME_2": [{"status": "COMPLETE", "filled_quantity": 1, "pending_quantity": 0, "average_price": 600.0}],
    }
    # For SL_LIMIT_1 cancel post-check return CANCELLED with 1 filled lot
    sl_limit_calls = [0]

    def _order_history(oid):
        if oid == "SL_LIMIT_1":
            sl_limit_calls[0] += 1
            if sl_limit_calls[0] >= 2:
                return [{"status": "CANCELLED", "filled_quantity": 1, "pending_quantity": 0, "average_price": 710.0}]
        return history_map[oid]

    kite.order_history.side_effect = _order_history

    cfg = SilverOptionsConfig(
        strike_mode="PERCENT",
        vwap_entry_enabled=False,
        portfolio_sl_pct=0.0,
        sl_per_lot=10000.0,
        lots=2,
    )
    om = SilverOrderManager(mode="LIVE", kite=kite, config=cfg)
    desk = SilverStrangleDesk(order_manager=om, config=cfg, state_file=tmp_path / "strangle_live.json", kite_client=kite)

    with patch("strategy.silver_options.desk.time.sleep"):
        assert desk.enter_strangle(now=today) is True
        # Poll 1: CE hits SL, partial fill 1/2
        assert desk.monitor_cycle(now=today + timedelta(minutes=1)) is True
        assert desk.position.ce_leg.status == "OPEN"
        assert desk.position.ce_leg.exit_filled_lots == 1
        assert desk.position.ce_leg.exit_reason == "SL"

        # Poll 2: CE below SL (600 < 700), still finishes squaring off remaining 1 lot!
        assert desk.monitor_cycle(now=today + timedelta(minutes=1, seconds=5)) is True
        assert desk.position.ce_leg.status == "SL"
        assert desk.position.ce_leg.exit_filled_lots == 2
        assert desk.position.ce_leg.exit_price == 655.0


def test_buyer_partial_exit_resumes_when_price_reverts(tmp_path):
    today = datetime(2026, 11, 26, 18, 30, 0)
    instruments = _build_mcx_instruments(today.date(), expiry_offset_days=5)

    kite = MagicMock()
    kite.instruments.return_value = instruments
    kite.positions.return_value = {"net": []}
    kite.historical_data.return_value = [
        {"date": today.date() - timedelta(days=2), "high": 156000.0, "low": 154000.0, "close": 155000.0},
        {"date": today.date() - timedelta(days=1), "high": 156500.0, "low": 154500.0, "close": 155200.0},
    ]

    kite.quote.side_effect = [
        # Context + ATM lookup + entry quotes
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {
            "MCX:SILVERM26NOV155000CE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV157000CE": _quote_payload(400.0, 399.0, 401.0),
            "MCX:SILVERM26NOV158000CE": _quote_payload(300.0, 299.0, 301.0),
        },
        # Poll 1: LTP drops to 340 (<= SL 350)
        {"MCX:SILVERM26NOV155000CE": _quote_payload(340.0, 339.0, 341.0)},
        # Poll 2: LTP rebounds above SL to 400 (> 350)
        {"MCX:SILVERM26NOV155000CE": _quote_payload(400.0, 399.0, 401.0)},
    ]

    order_seq = ["B_ENTRY", "B_SL_LIM", "B_SL_MKT", "B_SL_RESUME"]
    kite.place_order.side_effect = lambda **kw: order_seq.pop(0)
    kite.cancel_order.return_value = True

    lim_calls = [0]

    def _history(oid):
        if oid == "B_ENTRY":
            return [{"status": "COMPLETE", "filled_quantity": 2, "pending_quantity": 0, "average_price": 500.0}]
        if oid == "B_SL_LIM":
            lim_calls[0] += 1
            if lim_calls[0] == 1:
                return [{"status": "OPEN", "filled_quantity": 1, "pending_quantity": 1, "average_price": 340.0}]
            return [{"status": "CANCELLED", "filled_quantity": 1, "pending_quantity": 0, "average_price": 340.0}]
        if oid == "B_SL_MKT":
            return [{"status": "CANCELLED", "filled_quantity": 0, "pending_quantity": 0, "average_price": 0.0}]
        return [{"status": "COMPLETE", "filled_quantity": 1, "pending_quantity": 0, "average_price": 400.0}]

    kite.order_history.side_effect = _history

    cfg = SilverOptionsConfig(lots=2, buyer_sl_pct=0.30, buyer_sl_per_lot=3000.0)
    om = SilverOrderManager(mode="LIVE", kite=kite, config=cfg)
    buyer = SilverDirectionalBuyer(order_manager=om, config=cfg, state_file=tmp_path / "buyer.json", kite_client=kite)

    with patch("strategy.silver_options.directional_buyer.time.sleep"):
        assert buyer.evaluate_entry(now=today, bars_5m=_bullish_5m_bars()) is True
        assert buyer.position.stop_loss_price == 350.0

        # Poll 1: hits SL, partial 1/2 fill
        assert buyer.monitor_cycle(now=today + timedelta(minutes=2)) is True
        assert buyer.position.status == "OPEN"
        assert buyer.position.exit_filled_lots == 1
        assert buyer.position.exit_reason == "SL"

        # Poll 2: rebounds above SL -> resumes and completes remaining 1/2 lot!
        assert buyer.monitor_cycle(now=today + timedelta(minutes=2, seconds=5)) is False
        assert buyer.position.status == "SL"
        assert buyer.position.exit_filled_lots == 2
        assert buyer.position.exit_price == 370.0


def test_clear_state_resets_vwap_state_and_spread_check_blocks_wide_vwap_entry(tmp_path):
    today = datetime(2026, 11, 26, 20, 0, 0)
    instruments = _build_mcx_instruments(today.date(), expiry_offset_days=5)

    kite = MagicMock()
    kite.instruments.return_value = instruments
    kite.positions.return_value = {"net": []}
    kite.quote.side_effect = [
        # Poll 1a (20:00): spot lookup
        {
            "MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0),
        },
        # Poll 1b (20:00): lock strikes with tight spreads
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV158000CE": _quote_payload(400.0, 399.0, 401.0),
            "MCX:SILVERM26NOV153000PE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV152000PE": _quote_payload(400.0, 399.0, 401.0),
        },
        # Poll 2 (20:05): Comb < VWAP (900 < ~966), BUT CE spread widened to ~28.6% (bid=350, ask=467)
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(420.0, 350.0, 467.0, vol=1500),
            "MCX:SILVERM26NOV153000PE": _quote_payload(480.0, 479.0, 481.0, vol=1500),
        },
        # Poll 3 (21:45 cutoff): still wide spread -> must SKIP at cutoff!
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(420.0, 350.0, 467.0, vol=1700),
            "MCX:SILVERM26NOV153000PE": _quote_payload(480.0, 479.0, 481.0, vol=1700),
        },
    ]

    cfg = SilverOptionsConfig(
        strike_mode="PERCENT",
        vwap_entry_enabled=True,
        vwap_entry_cutoff="21:45",
        max_bid_ask_spread_pct=0.03,
        lots=2,
    )
    om = SilverOrderManager(mode="LIVE", kite=kite, config=cfg)
    desk = SilverStrangleDesk(order_manager=om, config=cfg, state_file=tmp_path / "vwap_spread.json", kite_client=kite)

    # Sample 1
    assert desk.enter_strangle(now=today) is False
    assert desk._vwap_state.get("samples") == 1
    # Sample 2: Comb < VWAP but wide spread blocks entry
    assert desk.enter_strangle(now=today + timedelta(minutes=5)) is False
    assert desk.position is None
    assert kite.place_order.call_count == 0
    # Sample 3: Cutoff reached -> cleanly SKIPPED
    assert desk.enter_strangle(now=datetime(2026, 11, 26, 21, 45, 0)) is False
    assert desk.position is not None and desk.position.status == "SKIPPED"

    desk.clear_state()
    assert desk.position is None
    assert desk._vwap_state == {}


def test_buyer_incomplete_5m_bar_filtered_and_unconfirmed_cancel_blocks_duplicate_buy(tmp_path):
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 11, 26, 18, 32, 0)
    kite = MagicMock()
    kite.historical_data.return_value = [
        {"date": datetime(2026, 11, 26, 18, 25, 0, tzinfo=ist), "high": 155100, "low": 154900, "close": 155050, "volume": 120},
        {"date": datetime(2026, 11, 26, 18, 30, 0, tzinfo=ist), "high": 156000, "low": 154800, "close": 155900, "volume": 999},
    ]

    cfg = SilverOptionsConfig(lots=2)
    om = SilverOrderManager(mode="LIVE", kite=kite, config=cfg)
    buyer = SilverDirectionalBuyer(order_manager=om, config=cfg, state_file=tmp_path / "buyer_dup.json", kite_client=kite)

    completed = buyer._fetch_today_5m_bars(fut_token=111001, now=now)
    assert len(completed) == 1
    assert completed[0]["volume"] == 120

    # Now test LIVE BUY timeout where cancel_order fails -> ABORTED prevents duplicate BUY
    instruments = _build_mcx_instruments(now.date(), expiry_offset_days=5)
    kite.instruments.return_value = instruments
    kite.positions.return_value = {"net": []}
    kite.historical_data.return_value = [
        {"date": now.date() - timedelta(days=2), "high": 156000.0, "low": 154000.0, "close": 155000.0},
        {"date": now.date() - timedelta(days=1), "high": 156500.0, "low": 154500.0, "close": 155200.0},
    ]
    kite.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {"MCX:SILVERM26NOV155000CE": _quote_payload(500.0, 499.0, 501.0)},
    ]
    kite.place_order.return_value = "UNCONFIRMED_BUY"
    kite.cancel_order.side_effect = RuntimeError("Network timeout cancelling")
    kite.order_history.return_value = [
        {"status": "OPEN", "filled_quantity": 0, "pending_quantity": 2, "average_price": 0.0}
    ]

    with patch("strategy.silver_options.directional_buyer.time.sleep"):
        assert buyer.evaluate_entry(now=now, bars_5m=_bullish_5m_bars()) is False
        assert buyer.position is not None and buyer.position.status == "ABORTED"
        # Next poll must NOT place a duplicate BUY order
        assert buyer.evaluate_entry(now=now + timedelta(seconds=20), bars_5m=_bullish_5m_bars()) is False
        assert kite.place_order.call_count == 1


def test_atr_token_retry_partial_day_guard_and_prev_close_atr_pct():
    today = date(2026, 11, 26)
    kite_old = MagicMock()
    kite_new = MagicMock()

    # TokenException on first ATR call -> refreshed session succeeds; ignores today's incomplete candle
    kite_old.historical_data.side_effect = Exception("TokenException: Invalid `api_key` or `access_token`.")
    kite_new.historical_data.return_value = [
        {"date": today - timedelta(days=3), "high": 103000.0, "low": 99000.0, "close": 100000.0},
        {"date": today - timedelta(days=2), "high": 104000.0, "low": 100000.0, "close": 102000.0},
        {"date": today - timedelta(days=1), "high": 105000.0, "low": 101000.0, "close": 104000.0},
        {"date": today, "high": 120000.0, "low": 95000.0, "close": 115000.0},
    ]

    with patch("kite.client.ensure_kite_session", return_value=kite_new):
        atr = compute_silver_daily_atr(kite=kite_old, fut_token=111001, period=14, as_of_date=today)
    # TRs: bar0=4000, bar1=4000, bar2=4000 -> ATR=4000 (today's 25,000 TR is excluded)
    assert atr == 4000.0

    # If ONLY today's incomplete bar exists -> returns None (no partial-day fallback)
    kite_partial = MagicMock()
    kite_partial.historical_data.return_value = [
        {"date": today, "high": 120000.0, "low": 95000.0, "close": 115000.0},
    ]
    assert compute_silver_daily_atr(kite=kite_partial, fut_token=111001, period=14, as_of_date=today) is None

    # resolve_silver_context computes atr_pct using prev_close (104000), NOT oscillating intraday spot (200000)
    instruments = _build_mcx_instruments(today, expiry_offset_days=15)
    kite_new.instruments.return_value = instruments
    kite_new.quote.return_value = {
        "MCX:SILVERM26NOVFUT": _quote_payload(200000.0, 199990.0, 200010.0),
    }
    ctx = resolve_silver_context(kite=kite_new, as_of_date=today, allow_fallback=False)
    assert ctx["prev_close"] == 104000.0
    assert ctx["atr_pct"] == round((4000.0 / 104000.0) * 100.0, 2)

    # find_silver_atm_option reuses ctx (0 extra instruments() calls) while refreshing live futures spot to 158000
    instruments_calls_before = kite_new.instruments.call_count
    kite_new.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": _quote_payload(158000.0, 157990.0, 158010.0)},
        {"MCX:SILVERM26NOV158000CE": _quote_payload(600.0, 599.0, 601.0)},
    ]
    atm = find_silver_atm_option(kite=kite_new, option_type="CE", allow_fallback=False, context=ctx, as_of_date=today)
    assert kite_new.instruments.call_count == instruments_calls_before
    assert atm["spot_price"] == 158000.0
    assert atm["strike"] == 158000.0


def test_barbell_scheduler_buyer_only_token_refresh_and_order_validation(tmp_path):
    cfg = SilverOptionsConfig(strategy_mode="BUYER", lots=2)
    om = SilverOrderManager(mode="LIVE", kite=MagicMock(), config=cfg)
    buyer = SilverDirectionalBuyer(order_manager=om, config=cfg, state_file=tmp_path / "b_sched.json", kite_client=om.kite)
    barbell = SilverBarbellDesk(buyer=buyer, strangle=None, config=cfg)

    class _StopScheduler(BaseException):
        pass

    # Sunday (holiday normally) at 17:50: ignore_holidays=True runs token refresh & enters idle sleep (30s)
    sunday_dt = datetime(2026, 11, 29, 17, 50, 0)
    with patch("strategy.silver_options.directional_buyer.datetime") as mock_dt, patch(
        "strategy.silver_options.directional_buyer.time.sleep", side_effect=_StopScheduler()
    ), patch.object(buyer, "_ensure_active_kite") as mock_refresh:
        mock_dt.now.return_value = sunday_dt
        with pytest.raises(_StopScheduler):
            barbell.run_scheduler(ignore_holidays=True)
        mock_refresh.assert_called_once_with(force_login=True)

    # OrderManager input validation
    paper_om = SilverOrderManager(mode="PAPER", config=cfg)
    with pytest.raises(ValueError):
        paper_om.sell("SILVERM26NOV157000CE", quantity=0, price=100.0)
    with pytest.raises(ValueError):
        paper_om.buy("SILVERM26NOV157000CE", quantity=2, price=0.0)


def test_enter_strangle_refuses_at_or_after_exit_time_and_excludes_buyer_symbol(tmp_path):
    today = datetime(2026, 11, 26, 20, 0, 0)
    instruments = _build_mcx_instruments(today.date(), expiry_offset_days=5)

    kite = MagicMock()
    kite.instruments.return_value = instruments
    kite.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV158000CE": _quote_payload(400.0, 399.0, 401.0),
            "MCX:SILVERM26NOV153000PE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV152000PE": _quote_payload(400.0, 399.0, 401.0),
        },
    ]

    cfg = SilverOptionsConfig(strike_mode="PERCENT", vwap_entry_enabled=False, portfolio_sl_pct=0.0, lots=2)
    om = SilverOrderManager(mode="PAPER", config=cfg)
    desk_late = SilverStrangleDesk(order_manager=om, config=cfg, state_file=tmp_path / "late.json", kite_client=kite)
    assert desk_late.enter_strangle(now=datetime(2026, 11, 26, 23, 10, 0)) is False
    assert desk_late.position is not None and desk_late.position.status == "SKIPPED"

    # When Buyer holds SILVERM26NOV157000CE, Strangle must exclude it and pick SILVERM26NOV158000CE
    desk = SilverStrangleDesk(order_manager=om, config=cfg, state_file=tmp_path / "excl.json", kite_client=kite)
    assert desk.enter_strangle(now=today, ignore_symbols={"SILVERM26NOV157000CE"}) is True
    assert desk.position.ce_leg.symbol == "SILVERM26NOV158000CE"


def test_vwap_quote_failure_does_not_pollute_samples_and_clears_stale_depth(tmp_path):
    today = datetime(2026, 11, 26, 20, 0, 0)
    instruments = _build_mcx_instruments(today.date(), expiry_offset_days=5)

    kite = MagicMock()
    kite.instruments.return_value = instruments
    kite.positions.return_value = {"net": []}
    kite.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(500.0, 499.0, 501.0, vol=1000),
            "MCX:SILVERM26NOV153000PE": _quote_payload(500.0, 499.0, 501.0, vol=1000),
        },
        # Poll 2 throws transient error -> samples MUST remain 1 (no dead sample added)
        RuntimeError("Transient Kite timeout"),
        # Poll 3 returns lower Comb with EMPTY depth -> clears stale ce_bid/ce_ask from locked strikes!
        {
            "MCX:SILVERM26NOV157000CE": {"last_price": 350.0, "volume": 1200, "depth": {"buy": [], "sell": []}},
            "MCX:SILVERM26NOV153000PE": _quote_payload(450.0, 449.0, 451.0, vol=1200),
        },
    ]

    cfg = SilverOptionsConfig(strike_mode="PERCENT", vwap_entry_enabled=True, max_bid_ask_spread_pct=0.03, lots=2)
    om = SilverOrderManager(mode="PAPER", config=cfg)
    desk = SilverStrangleDesk(order_manager=om, config=cfg, state_file=tmp_path / "vwap_err.json", kite_client=kite)

    assert desk.enter_strangle(now=today) is False
    assert desk._vwap_state["samples"] == 1

    assert desk.enter_strangle(now=today + timedelta(minutes=5)) is False
    assert desk._vwap_state["samples"] == 1

    assert desk.enter_strangle(now=today + timedelta(minutes=10)) is True
    assert desk._vwap_state["strikes"]["ce_bid"] is None
    assert desk._vwap_state["strikes"]["ce_ask"] is None


def test_buyer_zero_sl_pct_uses_hard_loss_cap_instead_of_entry_price(tmp_path):
    today = datetime(2026, 11, 26, 18, 30, 0)
    instruments = _build_mcx_instruments(today.date(), expiry_offset_days=5)

    kite = MagicMock()
    kite.instruments.return_value = instruments
    kite.historical_data.return_value = [
        {"date": today.date() - timedelta(days=2), "high": 156000.0, "low": 154000.0, "close": 155000.0},
        {"date": today.date() - timedelta(days=1), "high": 156500.0, "low": 154500.0, "close": 155200.0},
    ]
    kite.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {"MCX:SILVERM26NOV155000CE": _quote_payload(2000.0, 1998.0, 2002.0)},
    ]

    cfg = SilverOptionsConfig(lots=2, buyer_sl_pct=0.0, buyer_sl_per_lot=3000.0)
    om = SilverOrderManager(mode="PAPER", config=cfg)
    buyer = SilverDirectionalBuyer(order_manager=om, config=cfg, state_file=tmp_path / "zero_sl.json", kite_client=kite)

    assert buyer.evaluate_entry(now=today, bars_5m=_bullish_5m_bars()) is True
    # Entry=2000, cap=6000 / 10kg = 600 pts -> SL MUST be 1400.0 (not 2000.0!)
    assert buyer.position.stop_loss_price == 1400.0


def test_chain_propagates_refreshed_kite_after_token_error():
    today = date(2026, 11, 26)
    instruments = _build_mcx_instruments(today, expiry_offset_days=5)

    kite_old = MagicMock()
    kite_new = MagicMock()
    kite_old.instruments.side_effect = Exception("TokenException: Invalid `api_key` or `access_token`.")
    kite_new.instruments.return_value = instruments
    kite_new.quote.side_effect = [
        {"MCX:SILVERM26NOVFUT": _quote_payload(155000.0, 154990.0, 155010.0)},
        {
            "MCX:SILVERM26NOV157000CE": _quote_payload(500.0, 499.0, 501.0),
            "MCX:SILVERM26NOV153000PE": _quote_payload(500.0, 499.0, 501.0),
        },
    ]

    with patch("kite.client.ensure_kite_session", return_value=kite_new):
        res = find_silver_strangle_strikes(
            kite=kite_old,
            strike_mode="PERCENT",
            allow_fallback=False,
            as_of_date=today,
        )
    assert res["ce_symbol"] == "SILVERM26NOV157000CE"
    assert res["pe_symbol"] == "SILVERM26NOV153000PE"
    assert kite_old.quote.call_count == 0
    assert kite_new.quote.call_count == 2

