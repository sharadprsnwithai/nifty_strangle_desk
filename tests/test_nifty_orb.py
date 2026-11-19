from datetime import date, datetime, time
from unittest.mock import MagicMock
import pandas as pd
import pytest
from strategy.nifty_orb.config import NiftyORBConfig
from strategy.nifty_orb.contract_resolver import resolve_active_nifty_fut_contract
from strategy.nifty_orb.desk import NiftyORBDesk
from strategy.nifty_orb.order_manager import NiftyOrderManager


def test_nifty_orb_config_defaults():
    cfg = NiftyORBConfig()
    assert cfg.lots == 2
    assert cfg.initial_sl_pts == 50.0
    assert cfg.target1_pts == 75.0
    assert cfg.ref_start_time == time(9, 15)
    assert cfg.ref_end_time == time(9, 45)
    assert cfg.trade_window_end == time(13, 30)
    assert cfg.session_cutoff_time == time(15, 15)
    assert cfg.vol_multiplier == 1.2
    assert cfg.use_trend_filter is True
    assert cfg.exchange == "NFO"
    assert cfg.product == "MIS"
    assert cfg.order_type == "LIMIT"
    assert cfg.limit_buffer_pts == 5.0
    assert cfg.state_file == "data/nifty_orb_trades_today.json"


def test_resolve_active_nifty_fut_contract_mock():
    # Test offline fallback when kite is None
    contract = resolve_active_nifty_fut_contract(None)
    assert "NIFTY" in contract["tradingsymbol"]
    assert contract["instrument_token"] > 0
    assert contract["lot_size"] in (25, 50, 65, 75)
    assert isinstance(contract["expiry"], date)


def test_resolve_active_nifty_fut_contract_live_list():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {
            "tradingsymbol": "BANKNIFTY26NOVFUT",
            "name": "BANKNIFTY",
            "instrument_type": "FUT",
            "expiry": date(2026, 11, 26),
            "instrument_token": 1001,
            "lot_size": 15,
        },
        {
            "tradingsymbol": "NIFTY26NOVFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": date(2026, 11, 26),
            "instrument_token": 1002,
            "lot_size": 65,
        },
        {
            "tradingsymbol": "NIFTY26DECFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": date(2026, 12, 31),
            "instrument_token": 1003,
            "lot_size": 65,
        },
        {
            "tradingsymbol": "NIFTY26N2624000CE",
            "name": "NIFTY",
            "instrument_type": "CE",
            "expiry": date(2026, 11, 26),
            "instrument_token": 1004,
            "lot_size": 65,
        },
    ]

    contract = resolve_active_nifty_fut_contract(mock_kite, today_d=date(2026, 11, 19))
    assert contract["tradingsymbol"] == "NIFTY26NOVFUT"
    assert contract["instrument_token"] == 1002
    assert contract["lot_size"] == 65
    assert contract["expiry"] == date(2026, 11, 26)


def test_resolve_active_nifty_fut_contract_string_expiry_and_cutoff():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {
            "tradingsymbol": "NIFTY26NOVFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": "2026-11-26",
            "instrument_token": 1002,
            "lot_size": 65,
        },
        {
            "tradingsymbol": "NIFTY26DECFUT",
            "name": "NIFTY",
            "instrument_type": "FUT",
            "expiry": "2026-12-31",
            "instrument_token": 1003,
            "lot_size": 65,
        },
    ]

    # If min_days_to_expiry is 10 and today is 2026-11-20, cutoff is 2026-11-30, so it should pick DEC contract
    contract = resolve_active_nifty_fut_contract(mock_kite, min_days_to_expiry=10, today_d=date(2026, 11, 20))
    assert contract["tradingsymbol"] == "NIFTY26DECFUT"
    assert contract["expiry"] == date(2026, 12, 31)


def test_resolve_active_nifty_fut_contract_no_candidates():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = []
    with pytest.raises(ValueError, match="No active NIFTY FUT contracts found"):
        resolve_active_nifty_fut_contract(mock_kite, today_d=date(2026, 11, 19))


def test_order_manager_paper_execution():
    om = NiftyOrderManager(mode="PAPER", limit_buffer_pts=5.0)

    # Calculate limit price with buffer
    buy_limit = om.calculate_limit_price("BUY", 24000.0)
    assert buy_limit == 24005.0

    sell_limit = om.calculate_limit_price("SELL", 24000.0)
    assert sell_limit == 23995.0

    # Place paper buy order
    res = om.place_order(
        symbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=130,
        trigger_price=24000.0,
    )
    assert res["status"] == "COMPLETE"
    assert res["order_id"].startswith("PAPER_")
    assert res["price"] == 24000.0
    assert res["quantity"] == 130


def test_order_manager_live_execution():
    mock_kite = MagicMock()
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.ORDER_TYPE_MARKET = "MARKET"
    mock_kite.place_order.return_value = "241119000123"

    om = NiftyOrderManager(mode="LIVE", kite=mock_kite, limit_buffer_pts=5.0)
    res = om.place_order(
        symbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=130,
        trigger_price=24000.0,
    )
    assert res["status"] == "COMPLETE"
    assert res["order_id"] == "241119000123"
    assert res["price"] == 24000.0
    assert res["quantity"] == 130
    mock_kite.place_order.assert_called_once_with(
        variety="regular",
        exchange="NFO",
        tradingsymbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=130,
        product="MIS",
        order_type="LIMIT",
        price=24005.0,
        tag="NIFTY_ORB",
    )


def test_order_manager_live_no_kite():
    om = NiftyOrderManager(mode="LIVE", kite=None)
    with pytest.raises(RuntimeError, match="Live mode enabled but KiteConnect client is None"):
        om.place_order(symbol="NIFTY26NOVFUT", transaction_type="BUY", quantity=130)


def create_sample_15m_candles(num_bars=60, base_price=24000.0, trend="UP", end_dt=None):
    if end_dt is None:
        end_dt = datetime(2026, 11, 19, 10, 0)
    start_dt = end_dt - pd.Timedelta(minutes=15 * (num_bars - 1))
    data = []
    for i in range(num_bars):
        close_p = base_price + (i * 2.0 if trend == "UP" else -i * 2.0)
        data.append({
            "date": start_dt + pd.Timedelta(minutes=15 * i),
            "open": close_p - 1.0,
            "high": close_p + 5.0,
            "low": close_p - 5.0,
            "close": close_p,
            "volume": 5000 + (10000 if i == num_bars - 1 else 0),
        })
    df = pd.DataFrame(data)
    df["time"] = df["date"].dt.time
    df["day"] = df["date"].dt.date
    return df


def test_evaluate_reference_range():
    cfg = NiftyORBConfig()
    desk = NiftyORBDesk(config=cfg)

    # Construct 09:15 and 09:30 bars
    df = pd.DataFrame([
        {"date": datetime(2026, 11, 19, 9, 15), "high": 24100.0, "low": 24000.0, "close": 24050.0, "volume": 1000},
        {"date": datetime(2026, 11, 19, 9, 30), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
        {"date": datetime(2026, 11, 19, 9, 45), "high": 24150.0, "low": 24060.0, "close": 24140.0, "volume": 1500},
    ])
    df["time"] = df["date"].dt.time
    df["day"] = df["date"].dt.date

    ref = desk.evaluate_reference_range(df, target_day=date(2026, 11, 19))
    assert ref is not None
    assert ref["ref_high"] == 24120.0
    assert ref["ref_low"] == 23980.0

    # Test edge case: < 2 bars
    ref_empty = desk.evaluate_reference_range(df.iloc[:1], target_day=date(2026, 11, 19))
    assert ref_empty is None


def test_evaluate_breakout_signal_long():
    cfg = NiftyORBConfig()
    desk = NiftyORBDesk(config=cfg)

    df = create_sample_15m_candles(num_bars=55, base_price=24000.0, trend="UP")
    # Make the last closed bar break above ref_high 24100
    df.loc[df.index[-1], "high"] = 24150.0
    df.loc[df.index[-1], "close"] = 24130.0
    df.loc[df.index[-1], "volume"] = 15000.0

    eval_time = df.iloc[-1]["date"] + pd.Timedelta(minutes=15, seconds=10)
    sig = desk.evaluate_breakout_signal(
        df,
        ref_high=24100.0,
        ref_low=23900.0,
        current_time=eval_time.time(),
        current_dt=eval_time,
    )
    assert sig is not None
    assert sig["direction"] == "LONG"
    assert sig["trigger_price"] == 24130.0


def test_evaluate_breakout_signal_short():
    cfg = NiftyORBConfig()
    desk = NiftyORBDesk(config=cfg)

    df = create_sample_15m_candles(num_bars=55, base_price=24000.0, trend="DOWN")
    # Make the last closed bar break below ref_low 23850
    df.loc[df.index[-1], "low"] = 23800.0
    df.loc[df.index[-1], "close"] = 23820.0
    df.loc[df.index[-1], "volume"] = 15000.0

    eval_time = df.iloc[-1]["date"] + pd.Timedelta(minutes=15, seconds=10)
    sig = desk.evaluate_breakout_signal(
        df,
        ref_high=24100.0,
        ref_low=23850.0,
        current_time=eval_time.time(),
        current_dt=eval_time,
    )
    assert sig is not None
    assert sig["direction"] == "SHORT"
    assert sig["trigger_price"] == 23820.0


def test_evaluate_breakout_signal_filters():
    cfg = NiftyORBConfig()
    desk = NiftyORBDesk(config=cfg)

    # 1. Volume filter check: volume is below 1.2x average
    df = create_sample_15m_candles(num_bars=55, base_price=24000.0, trend="UP")
    df.loc[df.index[-1], "high"] = 24150.0
    df.loc[df.index[-1], "close"] = 24130.0
    df.loc[df.index[-1], "volume"] = 4000.0  # < 1.2 * 5000 = 6000

    eval_time = df.iloc[-1]["date"] + pd.Timedelta(minutes=15, seconds=10)
    sig_low_vol = desk.evaluate_breakout_signal(
        df,
        ref_high=24100.0,
        ref_low=23900.0,
        current_time=eval_time.time(),
        current_dt=eval_time,
    )
    assert sig_low_vol is None

    # 2. Trend filter check: close < 50 EMA during long breakout
    df.loc[df.index[-1], "volume"] = 15000.0
    # Artificially set all previous close to 25000 so 50 EMA is ~25000, while close is 24130
    df.loc[df.index[:-1], "close"] = 25000.0
    sig_trend_fail = desk.evaluate_breakout_signal(
        df,
        ref_high=24100.0,
        ref_low=23900.0,
        current_time=eval_time.time(),
        current_dt=eval_time,
    )
    assert sig_trend_fail is None


def test_desk_initialize_and_state_persistence(tmp_path):
    state_file = tmp_path / "nifty_state.json"
    cfg = NiftyORBConfig(state_file=str(state_file))
    desk = NiftyORBDesk(config=cfg)

    today = date(2026, 11, 19)
    state = desk.initialize_day(today_d=today)

    assert state["date"] == "2026-11-19"
    assert state["status"] == "INITIALIZING"
    assert state_file.exists()

    # Load state directly
    desk2 = NiftyORBDesk(config=cfg)
    loaded = desk2.initialize_day(today_d=today)
    assert loaded["date"] == "2026-11-19"
    assert loaded["status"] == "INITIALIZING"


def test_desk_lifecycle_target1_then_cost(tmp_path):
    state_file = tmp_path / "nifty_state_t1.json"
    cfg = NiftyORBConfig(state_file=str(state_file))
    mock_notifier = MagicMock()
    desk = NiftyORBDesk(config=cfg, notifier=mock_notifier)

    t_day = date(2026, 11, 19)
    desk.initialize_day(today_d=t_day)

    # 09:30: Range not yet formed (ref_end_time is 09:45)
    df_range = pd.DataFrame([
        {"date": datetime(2026, 11, 19, 9, 15), "high": 24100.0, "low": 24000.0, "close": 24050.0, "volume": 1000},
        {"date": datetime(2026, 11, 19, 9, 30), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
    ])
    desk.run_monitoring_cycle(
        current_time=time(9, 30),
        current_dt=datetime(2026, 11, 19, 9, 30),
        mock_df=df_range,
    )
    assert desk.state["status"] == "INITIALIZING"

    # 09:45: Range is established
    desk.run_monitoring_cycle(
        current_time=time(9, 45),
        current_dt=datetime(2026, 11, 19, 9, 45),
        mock_df=df_range,
    )
    assert desk.state["status"] == "SCANNING"
    assert desk.state["ref_high"] == 24120.0
    assert desk.state["ref_low"] == 23980.0
    mock_notifier.send_message.assert_called()  # Range notification

    # 10:00: Breakout candle closes above ref_high
    df_breakout = create_sample_15m_candles(num_bars=55, base_price=24000.0, trend="UP")
    df_breakout.loc[df_breakout.index[-1], "high"] = 24150.0
    df_breakout.loc[df_breakout.index[-1], "close"] = 24130.0
    df_breakout.loc[df_breakout.index[-1], "volume"] = 15000.0
    breakout_dt = df_breakout.iloc[-1]["date"] + pd.Timedelta(minutes=15, seconds=5)

    desk.run_monitoring_cycle(
        current_time=breakout_dt.time(),
        current_dt=breakout_dt,
        mock_df=df_breakout,
        mock_ltp=24130.0,
    )
    assert desk.state["status"] == "IN_TRADE"
    assert len(desk.state["trades"]) == 1
    trade = desk.state["trades"][0]
    assert trade["entry_price"] == 24130.0
    assert trade["sl_price"] == 24080.0  # -50 pts
    assert trade["target1_price"] == 24205.0  # +75 pts

    # 10:30: LTP hits Target 1 (+75 pts -> 24205.0)
    desk.run_monitoring_cycle(
        current_time=time(10, 30),
        current_dt=datetime(2026, 11, 19, 10, 30),
        mock_df=df_breakout,
        mock_ltp=24205.0,
    )
    assert desk.state["status"] == "TARGET1_HIT"
    assert trade["lot1_status"] == "TARGET_HIT"
    assert trade["sl_price"] == 24130.0  # Trailed to cost
    assert desk.state["realized_day_pnl"] > 0

    # 11:00: LTP falls back to entry price (24130.0) -> Cost exit for runner
    desk.run_monitoring_cycle(
        current_time=time(11, 0),
        current_dt=datetime(2026, 11, 19, 11, 0),
        mock_df=df_breakout,
        mock_ltp=24130.0,
    )
    assert desk.state["status"] == "COMPLETED"
    assert trade["lot2_status"] == "CLOSED"
    assert trade["lot2_exit_price"] == 24130.0


def test_desk_lifecycle_target1_then_session_cutoff(tmp_path):
    state_file = tmp_path / "nifty_state_cutoff.json"
    cfg = NiftyORBConfig(state_file=str(state_file))
    desk = NiftyORBDesk(config=cfg)

    t_day = date(2026, 11, 19)
    desk.initialize_day(today_d=t_day)

    df_range = pd.DataFrame([
        {"date": datetime(2026, 11, 19, 9, 15), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
        {"date": datetime(2026, 11, 19, 9, 30), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
    ])
    desk.run_monitoring_cycle(
        current_time=time(9, 45),
        current_dt=datetime(2026, 11, 19, 9, 45),
        mock_df=df_range,
    )

    df_breakout = create_sample_15m_candles(num_bars=55, base_price=24000.0, trend="UP")
    df_breakout.loc[df_breakout.index[-1], "high"] = 24150.0
    df_breakout.loc[df_breakout.index[-1], "close"] = 24130.0
    df_breakout.loc[df_breakout.index[-1], "volume"] = 15000.0
    breakout_dt = df_breakout.iloc[-1]["date"] + pd.Timedelta(minutes=15, seconds=5)

    desk.run_monitoring_cycle(
        current_time=breakout_dt.time(),
        current_dt=breakout_dt,
        mock_df=df_breakout,
        mock_ltp=24130.0,
    )

    # Hit Target 1
    desk.run_monitoring_cycle(
        current_time=time(10, 30),
        current_dt=datetime(2026, 11, 19, 10, 30),
        mock_df=df_breakout,
        mock_ltp=24205.0,
    )
    assert desk.state["status"] == "TARGET1_HIT"

    # 15:15: Session cutoff reached while runner is at 24230 (+100 pts)
    desk.run_monitoring_cycle(
        current_time=time(15, 15),
        current_dt=datetime(2026, 11, 19, 15, 15),
        mock_df=df_breakout,
        mock_ltp=24230.0,
    )
    assert desk.state["status"] == "COMPLETED"
    trade = desk.state["trades"][0]
    assert trade["lot2_status"] == "CLOSED"
    assert trade["lot2_exit_price"] == 24230.0
    assert trade["lot2_pnl_rs"] > 0


def test_desk_lifecycle_initial_stop_loss(tmp_path):
    state_file = tmp_path / "nifty_state_sl.json"
    cfg = NiftyORBConfig(state_file=str(state_file))
    desk = NiftyORBDesk(config=cfg)

    t_day = date(2026, 11, 19)
    desk.initialize_day(today_d=t_day)

    df_range = pd.DataFrame([
        {"date": datetime(2026, 11, 19, 9, 15), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
        {"date": datetime(2026, 11, 19, 9, 30), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
    ])
    desk.run_monitoring_cycle(
        current_time=time(9, 45),
        current_dt=datetime(2026, 11, 19, 9, 45),
        mock_df=df_range,
    )

    df_breakout = create_sample_15m_candles(num_bars=55, base_price=24000.0, trend="UP")
    df_breakout.loc[df_breakout.index[-1], "high"] = 24150.0
    df_breakout.loc[df_breakout.index[-1], "close"] = 24130.0
    df_breakout.loc[df_breakout.index[-1], "volume"] = 15000.0
    breakout_dt = df_breakout.iloc[-1]["date"] + pd.Timedelta(minutes=15, seconds=5)

    desk.run_monitoring_cycle(
        current_time=breakout_dt.time(),
        current_dt=breakout_dt,
        mock_df=df_breakout,
        mock_ltp=24130.0,
    )
    assert desk.state["status"] == "IN_TRADE"

    # LTP drops to 24080 (SL hit: 24130 - 50 = 24080)
    desk.run_monitoring_cycle(
        current_time=time(10, 15),
        current_dt=datetime(2026, 11, 19, 10, 15),
        mock_df=df_breakout,
        mock_ltp=24080.0,
    )
    assert desk.state["status"] == "COMPLETED"
    trade = desk.state["trades"][0]
    assert trade["lot1_status"] == "STOPPED_OUT"
    assert trade["lot2_status"] == "STOPPED_OUT"
    assert desk.state["realized_day_pnl"] < 0


def test_desk_scanning_cutoff(tmp_path):
    state_file = tmp_path / "nifty_state_scan_cutoff.json"
    cfg = NiftyORBConfig(state_file=str(state_file))
    desk = NiftyORBDesk(config=cfg)

    t_day = date(2026, 11, 19)
    desk.initialize_day(today_d=t_day)

    df_range = pd.DataFrame([
        {"date": datetime(2026, 11, 19, 9, 15), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
        {"date": datetime(2026, 11, 19, 9, 30), "high": 24120.0, "low": 23980.0, "close": 24070.0, "volume": 1200},
    ])
    desk.run_monitoring_cycle(
        current_time=time(9, 45),
        current_dt=datetime(2026, 11, 19, 9, 45),
        mock_df=df_range,
    )
    assert desk.state["status"] == "SCANNING"

    # Time exceeds 13:30 (e.g. 13:31) without breakout
    desk.run_monitoring_cycle(
        current_time=time(13, 31),
        current_dt=datetime(2026, 11, 19, 13, 31),
        mock_df=df_range,
    )
    assert desk.state["status"] == "COMPLETED"


def test_parse_args_defaults():
    from run_nifty_orb import parse_args

    args = parse_args([])
    assert args.paper is True
    assert args.lots == 2
    assert args.sl == 50.0
    assert args.target1 == 75.0
    assert args.product == "MIS"
    assert args.order_type == "LIMIT"
    assert args.limit_buffer_pts == 5.0


def test_parse_args_custom():
    from run_nifty_orb import parse_args

    args = parse_args([
        "--live",
        "--lots", "4",
        "--sl", "60.0",
        "--target1", "100.0",
        "--poll", "10",
        "--product", "NRML",
        "--order-type", "MARKET",
        "--limit-buffer-pts", "3.0",
        "--dry-run",
        "--once",
    ])
    assert args.live is True
    assert args.paper is False
    assert args.lots == 4
    assert args.sl == 60.0
    assert args.target1 == 100.0
    assert args.poll == 10
    assert args.product == "NRML"
    assert args.order_type == "MARKET"
    assert args.limit_buffer_pts == 3.0
    assert args.dry_run is True
    assert args.once is True


def test_run_nifty_orb_main_dry_run(monkeypatch, tmp_path):
    from run_nifty_orb import main

    monkeypatch.setattr("run_nifty_orb.load_config", lambda: MagicMock(telegram_bot_token=None, chat_id=None))
    monkeypatch.setattr("run_nifty_orb.get_kite_client", lambda config: None)
    monkeypatch.setattr("run_nifty_orb.TelegramNotifier", MagicMock)
    state_file = str(tmp_path / "nifty_dry_run_state.json")

    code = main(["--paper", "--dry-run", "--state-file", state_file, "--log-dir", str(tmp_path)])
    assert code == 0


def test_run_nifty_orb_main_once(monkeypatch, tmp_path):
    from run_nifty_orb import main

    monkeypatch.setattr("run_nifty_orb.load_config", lambda: MagicMock(telegram_bot_token=None, chat_id=None))
    monkeypatch.setattr("run_nifty_orb.get_kite_client", lambda config: None)
    monkeypatch.setattr("run_nifty_orb.TelegramNotifier", MagicMock)
    state_file = str(tmp_path / "nifty_once_state.json")

    code = main(["--paper", "--once", "--state-file", state_file, "--log-dir", str(tmp_path)])
    assert code == 0




