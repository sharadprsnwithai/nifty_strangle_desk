"""Unit tests for the FP-ALL Stock Swing Desk (1.5% risk, CNC execution, Telegram alerts)."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from strategy.stock_swing.config import StockSwingConfig
from strategy.stock_swing.desk import StockSwingDesk
from strategy.stock_swing.order_manager import StockOrderManager


def _make_nifty_df(bullish: bool = True, n: int = 210) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    trend = np.linspace(22000, 25000, n) if bullish else np.linspace(25000, 22000, n)
    return pd.DataFrame(
        {
            "Open": trend,
            "High": trend + 80,
            "Low": trend - 80,
            "Close": trend,
            "Volume": np.full(n, 1_000_000),
        },
        index=dates,
    )


def _make_fp_all_breakout_stock(n: int = 210) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = np.linspace(400.0, 500.0, n)
    # Tight 15-day base before final breakout bar
    close[-16:-1] = 500.0
    high = close + 3.0
    low = close - 3.0
    open_px = close - 1.0
    vol = np.full(n, 1_000_000.0)

    # Ensure 20D Up-Vol / Down-Vol >= 1.25 and Pocket Pivot inside 15D base
    for idx in range(n - 20, n - 1):
        if idx % 2 == 0:
            close[idx] = close[idx - 1] + 0.5
            vol[idx] = 1_400_000.0
        else:
            close[idx] = close[idx - 1] - 0.3
            vol[idx] = 400_000.0
    # Keep last 3 bars prior to breakout dry in volume
    vol[-4:-1] = 500_000.0
    high[-16:-1] = 505.0
    low[-16:-1] = 492.0

    # Breakout bar at t
    open_px[-1] = 504.0
    close[-1] = 515.0
    high[-1] = 517.0
    low[-1] = 503.0
    vol[-1] = 2_500_000.0

    return pd.DataFrame(
        {"open": open_px, "high": high, "low": low, "close": close, "volume": vol},
        index=dates,
    )


def test_risk_1_5_pct_sizing_and_25_pct_cap(tmp_path):
    cfg = StockSwingConfig(
        total_capital=1_000_000.0,
        risk_pct=0.015,
        max_alloc_pct=0.25,
        state_file=str(tmp_path / "state.json"),
    )
    desk = StockSwingDesk(config=cfg)

    # Wide SL (10%): 1.5% risk (15,000) / 50 = 300 shares (150,000 < 250,000 cap)
    assert desk.compute_position_qty(1_000_000.0, 1_000_000.0, 500.0, 450.0) == 300
    # Tight SL (3%): 1.5% risk (15,000) / 15 = 1000 shares (500,000), capped at 25% = 500 shares
    assert desk.compute_position_qty(1_000_000.0, 1_000_000.0, 500.0, 485.0) == 500


def test_paper_entry_ledger_and_telegram(tmp_path):
    cfg = StockSwingConfig(
        total_capital=1_000_000.0,
        risk_pct=0.015,
        mode="PAPER",
        state_file=str(tmp_path / "stock_swing_state.json"),
    )
    notifier = MagicMock()
    desk = StockSwingDesk(config=cfg, notifier=notifier)

    nifty_df = _make_nifty_df(bullish=True)
    stock_df = _make_fp_all_breakout_stock()

    res = desk.execute_daily_rebalance(nifty_df=nifty_df, stock_data={"TESTSTOCK": stock_df})
    assert len(res["entries"]) == 1
    assert res["entries"][0]["symbol"] == "TESTSTOCK"
    assert notifier.send_message.called
    assert "STOCK SWING - PAPER" in notifier.send_message.call_args[0][0]
    assert "TESTSTOCK" in desk.state["positions"]


def test_live_cnc_execution_and_sell_holdings_check(tmp_path):
    cfg = StockSwingConfig(
        total_capital=1_000_000.0,
        risk_pct=0.015,
        mode="LIVE",
        state_file=str(tmp_path / "stock_swing_state.json"),
    )
    kite = MagicMock()
    kite.VARIETY_REGULAR = "regular"
    kite.EXCHANGE_NSE = "NSE"
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    kite.PRODUCT_CNC = "CNC"
    kite.ORDER_TYPE_LIMIT = "LIMIT"
    kite.place_order.return_value = "ORD123"
    kite.orders.return_value = [
        {"order_id": "ORD123", "status": "COMPLETE", "filled_quantity": 100, "average_price": 515.0}
    ]
    kite.holdings.return_value = [{"tradingsymbol": "TESTSTOCK", "quantity": 100, "t1_quantity": 0}]

    om = StockOrderManager(cfg, kite_client=kite)
    buy_res = om.execute_buy("TESTSTOCK", 100, 515.0)
    assert buy_res["status"] == "COMPLETE"
    assert buy_res["mode"] == "LIVE"
    assert kite.place_order.call_args.kwargs["product"] == "CNC"
    # 515 * 1.005 = 517.575 -> rounded to 0.05 tick = 517.55
    assert kite.place_order.call_args.kwargs["price"] == 517.55

    sell_res = om.execute_sell("TESTSTOCK", 100, 540.0)
    assert sell_res["status"] == "COMPLETE"
    assert kite.place_order.call_args.kwargs["transaction_type"] == "SELL"
    assert kite.place_order.call_args.kwargs["product"] == "CNC"
    # 540 * 0.995 = 537.3 -> 537.3
    assert kite.place_order.call_args.kwargs["price"] == 537.3


def test_order_failure_notifies_telegram(tmp_path):
    cfg = StockSwingConfig(
        total_capital=1_000_000.0,
        risk_pct=0.015,
        mode="LIVE",
        state_file=str(tmp_path / "stock_swing_state.json"),
    )
    kite = MagicMock()
    kite.place_order.side_effect = RuntimeError("Insufficient funds")
    notifier = MagicMock()
    desk = StockSwingDesk(config=cfg, kite_client=kite, notifier=notifier)

    res = desk.execute_daily_rebalance(
        nifty_df=_make_nifty_df(bullish=True),
        stock_data={"TESTSTOCK": _make_fp_all_breakout_stock()},
    )
    assert len(res["entries"]) == 0
    assert len(res["errors"]) == 1
    assert "Insufficient funds" in res["errors"][0]
    assert notifier.send_message.called
    assert "Order Errors" in notifier.send_message.call_args[0][0]
