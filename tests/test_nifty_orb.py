from datetime import time
from strategy.nifty_orb.config import NiftyORBConfig


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
