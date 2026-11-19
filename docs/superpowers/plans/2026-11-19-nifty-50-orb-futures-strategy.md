# NIFTY 50 Index ORB Futures Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and containerize an automated intraday NIFTY 50 Index Futures Opening Range Breakout (ORB) trading desk with 2-lot tranche scaling (Target 1 profit booking + risk-free runner trailing to 15:15 IST), state persistence, Telegram alerts, and Docker Compose integration.

**Architecture:** A dedicated, fully decoupled strategy module `strategy/nifty_orb/` paired with `run_nifty_orb.py` and `docker-compose.yml`, mirroring the proven architecture of `strategy/silver_orb`. The desk dynamically resolves active near-month NIFTY futures, establishes a 30-minute opening range (09:15–09:45 IST), validates breakouts on closed 15-minute candles via a 50 EMA trend filter and 1.2x volume SMA confirmation, and manages a 2-lot tranche lifecycle (50-pt SL, 75-pt Target 1, runner trailed to cost, 15:15 cutoff).

**Tech Stack:** Python 3.10+, KiteConnect API, Pandas, NumPy, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-19-nifty-50-orb-futures-strategy-design.md`

## Global Constraints
- Target exchange: `NFO` (NSE Futures & Options).
- Underlying: `NIFTY` index monthly futures contract (e.g. `NIFTY26NOVFUT`).
- Contract lot size dynamically extracted from Kite instrument master.
- Reference range: 30-minute candle window from 09:15 to 09:45 IST.
- Breakout confirmation timeframe: 15-minute closed bars.
- Trend filter: 15m Close > 50 EMA for Long, Close < 50 EMA for Short.
- Volume confirmation: Volume >= 1.2 * 20-period Volume SMA.
- Position sizing: 2 lots default (Tranche 1 = 1 lot, Tranche 2 Runner = 1 lot).
- Initial Stop Loss: 50.0 points from entry.
- Target 1: +75.0 points from entry; moves Runner SL to Cost upon hit.
- Session Cutoff: Hard square-off at 15:15 IST.
- State file: `data/nifty_orb_trades_today.json`.
- Max trades: Exactly 1 trade per session.

---

### Task 1: Configuration & Module Scaffolding

**Files:**
- Create: `strategy/nifty_orb/config.py`
- Create: `strategy/nifty_orb/__init__.py`
- Test: `tests/test_nifty_orb.py`

**Interfaces:**
- Produces: `NiftyORBConfig` dataclass with all trading parameters, timing windows, order parameters, and state paths.

- [ ] **Step 1: Write the failing test for `NiftyORBConfig`**

Add to `tests/test_nifty_orb.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nifty_orb.py::test_nifty_orb_config_defaults -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.nifty_orb'`

- [ ] **Step 3: Implement `NiftyORBConfig` and module scaffolding**

Create `strategy/nifty_orb/config.py`:
```python
from dataclasses import dataclass
from datetime import time


@dataclass
class NiftyORBConfig:
    """Configuration for NIFTY 50 Index Futures 30-Min ORB Breakout Strategy."""

    lots: int = 2
    initial_sl_pts: float = 50.0
    target1_pts: float = 75.0

    ref_start_time: time = time(9, 15)
    ref_end_time: time = time(9, 45)
    trade_window_end: time = time(13, 30)
    session_cutoff_time: time = time(15, 15)

    vol_multiplier: float = 1.2
    use_trend_filter: bool = True

    mode: str = "PAPER"
    product: str = "MIS"
    exchange: str = "NFO"
    order_type: str = "LIMIT"
    limit_buffer_pts: float = 5.0
    poll_interval_seconds: int = 5

    state_file: str = "data/nifty_orb_trades_today.json"
```

Create `strategy/nifty_orb/__init__.py`:
```python
from .config import NiftyORBConfig

__all__ = ["NiftyORBConfig"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_nifty_orb.py::test_nifty_orb_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/nifty_orb/ tests/test_nifty_orb.py
git commit -m "feat(nifty_orb): add NiftyORBConfig and module scaffolding"
```

---

### Task 2: Dynamic NIFTY Futures Contract Resolver

**Files:**
- Create: `strategy/nifty_orb/contract_resolver.py`
- Modify: `strategy/nifty_orb/__init__.py`
- Test: `tests/test_nifty_orb.py`

**Interfaces:**
- Produces: `resolve_active_nifty_fut_contract(kite: Any, min_days_to_expiry: int = 0) -> Dict[str, Any]` returning `tradingsymbol`, `instrument_token`, `expiry`, and `lot_size`.

- [ ] **Step 1: Write the failing tests for contract resolution**

Add to `tests/test_nifty_orb.py`:
```python
from datetime import date
from unittest.mock import MagicMock
from strategy.nifty_orb.contract_resolver import resolve_active_nifty_fut_contract


def test_resolve_active_nifty_fut_contract_mock():
    # Test offline fallback when kite is None
    contract = resolve_active_nifty_fut_contract(None)
    assert "NIFTY" in contract["tradingsymbol"]
    assert contract["instrument_token"] > 0
    assert contract["lot_size"] in (25, 50, 75)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nifty_orb.py::test_resolve_active_nifty_fut_contract_mock -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.nifty_orb.contract_resolver'`

- [ ] **Step 3: Implement `contract_resolver.py`**

Create `strategy/nifty_orb/contract_resolver.py`:
```python
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("nifty_orb.resolver")


def resolve_active_nifty_fut_contract(
    kite: Any,
    min_days_to_expiry: int = 0,
    today_d: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Dynamically identifies the near-month active NIFTY 50 index futures contract on NFO.
    Selects the earliest expiring valid monthly FUT contract on or after today + min_days_to_expiry.
    """
    if kite is None:
        # Offline fallback for testing or mock environments
        return {
            "tradingsymbol": "NIFTY26NOVFUT",
            "instrument_token": 256265,
            "expiry": date(2026, 11, 26),
            "lot_size": 65,
        }

    try:
        instruments = kite.instruments("NFO")
    except Exception as e:
        logger.error("Failed to fetch NFO instruments from Kite: %s", e)
        raise

    today = today_d or date.today()
    cutoff_date = today + timedelta(days=min_days_to_expiry)

    candidates = []
    for inst in instruments:
        sym = inst.get("tradingsymbol", "")
        name = inst.get("name", "")
        inst_type = inst.get("instrument_type", "")
        expiry = inst.get("expiry")

        if (name == "NIFTY" or sym.startswith("NIFTY")) and inst_type == "FUT" and expiry:
            if isinstance(expiry, str):
                try:
                    expiry_date = date.fromisoformat(expiry)
                except ValueError:
                    continue
            elif isinstance(expiry, date):
                expiry_date = expiry
            else:
                continue

            if expiry_date >= cutoff_date:
                candidates.append({
                    "tradingsymbol": sym,
                    "instrument_token": inst.get("instrument_token"),
                    "expiry": expiry_date,
                    "lot_size": int(inst.get("lot_size", 65)),
                })

    if not candidates:
        raise ValueError(f"No active NIFTY FUT contracts found on NFO on or after {cutoff_date}")

    candidates.sort(key=lambda c: c["expiry"])
    chosen = candidates[0]
    logger.info(
        "Resolved active NIFTY Futures contract: %s (Token: %s, Expiry: %s, Lot Size: %d)",
        chosen["tradingsymbol"],
        chosen["instrument_token"],
        chosen["expiry"],
        chosen["lot_size"],
    )
    return chosen
```

Update `strategy/nifty_orb/__init__.py`:
```python
from .config import NiftyORBConfig
from .contract_resolver import resolve_active_nifty_fut_contract

__all__ = ["NiftyORBConfig", "resolve_active_nifty_fut_contract"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_nifty_orb.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add strategy/nifty_orb/contract_resolver.py strategy/nifty_orb/__init__.py tests/test_nifty_orb.py
git commit -m "feat(nifty_orb): add dynamic NIFTY futures contract resolver"
```

---

### Task 3: NIFTY Order Manager (Paper & Live Execution)

**Files:**
- Create: `strategy/nifty_orb/order_manager.py`
- Modify: `strategy/nifty_orb/__init__.py`
- Test: `tests/test_nifty_orb.py`

**Interfaces:**
- Produces: `NiftyOrderManager` class with `place_order(symbol, transaction_type, quantity, trigger_price=None) -> Dict[str, Any]` and `calculate_limit_price(transaction_type, ltp) -> float`.

- [ ] **Step 1: Write the failing tests for `NiftyOrderManager`**

Add to `tests/test_nifty_orb.py`:
```python
from strategy.nifty_orb.order_manager import NiftyOrderManager


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nifty_orb.py::test_order_manager_paper_execution -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.nifty_orb.order_manager'`

- [ ] **Step 3: Implement `order_manager.py`**

Create `strategy/nifty_orb/order_manager.py`:
```python
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("nifty_orb.order_manager")


class NiftyOrderManager:
    """
    Executes and simulates orders for NIFTY 50 Futures ORB Desk.
    Supports both PAPER trading simulation and LIVE order routing via Kite Connect.
    """

    def __init__(
        self,
        mode: str = "PAPER",
        kite: Optional[Any] = None,
        exchange: str = "NFO",
        product: str = "MIS",
        order_type: str = "LIMIT",
        limit_buffer_pts: float = 5.0,
    ):
        self.mode = mode.upper()
        self.kite = kite
        self.exchange = exchange
        self.product = product
        self.order_type = order_type.upper()
        self.limit_buffer_pts = limit_buffer_pts

    def calculate_limit_price(self, transaction_type: str, ltp: float) -> float:
        """Calculates marketable limit price with slippage protection buffer."""
        tt = transaction_type.upper()
        if tt == "BUY":
            return round(ltp + self.limit_buffer_pts, 1)
        elif tt == "SELL":
            return round(ltp - self.limit_buffer_pts, 1)
        return round(ltp, 1)

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        trigger_price: Optional[float] = None,
        tag: str = "NIFTY_ORB",
    ) -> Dict[str, Any]:
        """Places a live or simulated paper order."""
        tt = transaction_type.upper()
        if self.mode == "PAPER":
            order_id = f"PAPER_{uuid.uuid4().hex[:8].upper()}"
            exec_price = trigger_price if trigger_price is not None else 0.0
            logger.info(
                "[PAPER] Order Executed: %s %d %s @ ₹%.2f (ID: %s)",
                tt, quantity, symbol, exec_price, order_id
            )
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "price": exec_price,
                "quantity": quantity,
                "symbol": symbol,
                "transaction_type": tt,
            }

        # LIVE Mode Execution
        if not self.kite:
            raise RuntimeError("Live mode enabled but KiteConnect client is None")

        exec_price = trigger_price or 0.0
        limit_p = self.calculate_limit_price(tt, exec_price) if self.order_type == "LIMIT" else None

        kite_order_type = self.kite.ORDER_TYPE_LIMIT if self.order_type == "LIMIT" else self.kite.ORDER_TYPE_MARKET
        price_arg = limit_p if self.order_type == "LIMIT" else None

        try:
            logger.info(
                "[LIVE] Placing %s %s order: %d %s (Limit: %s)",
                self.order_type, tt, quantity, symbol, price_arg
            )
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=tt,
                quantity=quantity,
                product=self.product,
                order_type=kite_order_type,
                price=price_arg,
                tag=tag[:20],
            )
            return {
                "order_id": str(order_id),
                "status": "COMPLETE",
                "price": exec_price,
                "quantity": quantity,
                "symbol": symbol,
                "transaction_type": tt,
            }
        except Exception as e:
            logger.error("Failed to place live order for %s: %s", symbol, e)
            raise
```

Update `strategy/nifty_orb/__init__.py`:
```python
from .config import NiftyORBConfig
from .contract_resolver import resolve_active_nifty_fut_contract
from .order_manager import NiftyOrderManager

__all__ = ["NiftyORBConfig", "resolve_active_nifty_fut_contract", "NiftyOrderManager"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_nifty_orb.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/nifty_orb/order_manager.py strategy/nifty_orb/__init__.py tests/test_nifty_orb.py
git commit -m "feat(nifty_orb): add NiftyOrderManager with paper and live routing"
```

---

### Task 4: Signal Engine, Tranche Supervisor & Desk Core

**Files:**
- Create: `strategy/nifty_orb/desk.py`
- Modify: `strategy/nifty_orb/__init__.py`
- Test: `tests/test_nifty_orb.py`

**Interfaces:**
- Produces: `NiftyORBDesk` class with:
  - `initialize_day(today_d=None)`
  - `evaluate_reference_range(df)`
  - `evaluate_breakout_signal(df, ref_high, ref_low, current_dt=None)`
  - `run_monitoring_cycle(current_time, current_dt=None, mock_ltp=None)`
  - `save_state(state)` / `load_state()`
  - Telegram broadcasts for Range, Entry, Target 1, SL, Runner exit.

- [ ] **Step 1: Write failing tests for range calculation, signals, and tranche lifecycle**

Add to `tests/test_nifty_orb.py`:
```python
import pandas as pd
from datetime import datetime, date, time
from strategy.nifty_orb.config import NiftyORBConfig
from strategy.nifty_orb.desk import NiftyORBDesk
from strategy.nifty_orb.order_manager import NiftyOrderManager


def create_sample_15m_candles(num_bars=60, base_price=24000.0, trend="UP"):
    data = []
    dt = datetime(2026, 11, 19, 9, 15)
    for i in range(num_bars):
        close_p = base_price + (i * 2.0 if trend == "UP" else -i * 2.0)
        data.append({
            "date": dt + pd.Timedelta(minutes=15 * i),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nifty_orb.py::test_evaluate_reference_range -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.nifty_orb.desk'`

- [ ] **Step 3: Implement `strategy/nifty_orb/desk.py`**

Create `strategy/nifty_orb/desk.py`:
```python
import json
import logging
import os
import tempfile
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect
from kite.client import is_token_error

from notifications.telegram import TelegramNotifier
from strategy.holidays import is_nse_holiday
from .config import NiftyORBConfig
from .contract_resolver import resolve_active_nifty_fut_contract
from .order_manager import NiftyOrderManager

logger = logging.getLogger("nifty_orb.desk")


class NiftyORBDesk:
    """
    Automated trading desk for NIFTY 50 Index Futures 30-Minute ORB Breakout Strategy.
    Executes the 2-lot tranche scaling methodology:
    - Tranche 1 booked at Target 1 (+75 pts).
    - Runner Tranche 2 SL moved to Cost (100% Risk-Free) and rides until 15:15 IST cutoff.
    """

    def __init__(
        self,
        config: Optional[NiftyORBConfig] = None,
        kite: Optional[KiteConnect] = None,
        order_manager: Optional[NiftyOrderManager] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.config = config or NiftyORBConfig()
        self.kite = kite
        self.order_manager = order_manager or NiftyOrderManager(
            mode=self.config.mode,
            kite=self.kite,
            exchange=self.config.exchange,
            product=self.config.product,
            order_type=self.config.order_type,
            limit_buffer_pts=self.config.limit_buffer_pts,
        )
        self.notifier = notifier
        self.state_file = Path(self.config.state_file).resolve()
        self.state: Optional[Dict[str, Any]] = None

    def _ensure_kite_session(self, force_login: bool = False) -> Any:
        """Validates or refreshes Kite session without crashing."""
        if self.kite is None:
            return None
        from kite.client import ensure_kite_session
        try:
            self.kite = ensure_kite_session(self.kite, force_login=force_login)
            if hasattr(self.order_manager, "kite"):
                self.order_manager.kite = self.kite
            return self.kite
        except Exception as e:
            logger.error("Failed to re-authenticate Kite session: %s", e)
            return self.kite

    def _get_fallback_file(self) -> Path:
        fallback_dir = Path(tempfile.gettempdir()) / "trading_state"
        return fallback_dir / self.state_file.name

    def save_state(self, state: Dict[str, Any]) -> None:
        """Atomically saves state to disk with fallback resilience."""
        self.state = state
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error("Primary state save failed at %s (%s). Attempting fallback...", self.state_file, e)
            try:
                fallback_file = self._get_fallback_file()
                fallback_file.parent.mkdir(parents=True, exist_ok=True)
                temp_fallback = fallback_file.with_suffix(".tmp")
                temp_fallback.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
                temp_fallback.replace(fallback_file)
            except Exception as fb_err:
                logger.critical("All state save attempts failed: %s (Fallback: %s)", e, fb_err)

    def load_state(self) -> Optional[Dict[str, Any]]:
        target_file = self.state_file
        if not target_file.exists():
            fallback_file = self._get_fallback_file()
            if fallback_file.exists():
                target_file = fallback_file
            else:
                return None
        try:
            content = target_file.read_text(encoding="utf-8")
            self.state = json.loads(content)
            return self.state
        except Exception as e:
            logger.error("Failed to parse state file %s: %s", target_file, e)
            return None

    def initialize_day(self, today_d: Optional[date] = None) -> Dict[str, Any]:
        """Initializes or resumes daily trading state."""
        today = today_d or date.today()
        today_str = today.strftime("%Y-%m-%d")

        if self.state and self.state.get("date") == today_str:
            return self.state

        existing_state = self.load_state()
        if existing_state and existing_state.get("date") == today_str:
            logger.info("Resuming existing Nifty ORB state for %s", today_str)
            self.state = existing_state
            return existing_state

        contract = resolve_active_nifty_fut_contract(self.kite, today_d=today)
        new_state = {
            "date": today_str,
            "symbol": contract["tradingsymbol"],
            "instrument_token": contract["instrument_token"],
            "lot_size": contract["lot_size"],
            "ref_high": None,
            "ref_low": None,
            "status": "INITIALIZING",
            "trades": [],
            "realized_day_pnl": 0.0,
        }
        self.save_state(new_state)
        return new_state

    # -------------------------------------------------------------------------
    # Telegram Notifications
    # -------------------------------------------------------------------------
    def _notify_range_established(self, sym: str, r_high: float, r_low: float, ema50: float) -> None:
        if not self.notifier:
            return
        rng_pts = r_high - r_low
        msg = (
            f"🔔 <b>NIFTY 30-MIN ORB RANGE ESTABLISHED [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{sym}</code>\n"
            f"Window: <b>09:15 – 09:45 IST</b>\n"
            f"High: <b>₹{r_high:,.1f}</b>\n"
            f"Low: <b>₹{r_low:,.1f}</b> (Spread: {rng_pts:.1f} pts)\n"
            f"15m 50 EMA: <b>₹{ema50:,.1f}</b>\n\n"
            f"Breakout evaluation is active until 13:30 IST."
        )
        self.notifier.send_message(msg)

    def _notify_entry(self, trade: Dict[str, Any]) -> None:
        if not self.notifier:
            return
        direction = trade["direction"]
        sym = trade["symbol"]
        entry = trade["entry_price"]
        lots = trade["lots_total"]
        sl = trade["sl_price"]
        t1 = trade["target1_price"]
        msg = (
            f"🚀 <b>NIFTY FUTURES BREAKOUT ENTRY [{self.config.mode}]</b>\n\n"
            f"Direction: <b>{direction}</b>\n"
            f"Contract: <code>{sym}</code>\n"
            f"Quantity: <b>{lots} Lots</b> ({trade['quantity_total']} units)\n"
            f"Entry Price: <b>₹{entry:,.1f}</b>\n\n"
            f"🛡️ Initial SL: <b>₹{sl:,.1f}</b> ({self.config.initial_sl_pts:.0f} pts)\n"
            f"🎯 Target 1: <b>₹{t1:,.1f}</b> (+{self.config.target1_pts:.0f} pts on Lot 1)\n"
            f"Runner Lot: Will trail to Cost upon Target 1 & ride to 15:15 IST."
        )
        self.notifier.send_message(msg)

    def _notify_target1(self, trade: Dict[str, Any], day_pnl: float) -> None:
        if not self.notifier:
            return
        msg = (
            f"🎯 <b>NIFTY TARGET 1 HIT [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{trade['symbol']}</code> ({trade['direction']})\n"
            f"Tranche 1 Exited: <b>{trade['lot1_lots']} Lots @ ₹{trade['lot1_exit_price']:,.1f}</b>\n"
            f"Booked P&L: <b>+₹{trade['lot1_pnl_rs']:+,.2f}</b>\n\n"
            f"🛡️ <b>Runner ({trade['lot2_lots']} Lots) Stop Loss Trailed to Cost (₹{trade['entry_price']:,.1f})!</b>\n"
            f"Trade is now 100% Risk-Free.\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_sl(self, trade: Dict[str, Any], exit_p: float, trade_pnl: float, day_pnl: float) -> None:
        if not self.notifier:
            return
        msg = (
            f"🛑 <b>NIFTY STOP LOSS HIT [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{trade['symbol']}</code> ({trade['direction']})\n"
            f"Exited: <b>{trade['lots_total']} Lots @ ₹{exit_p:,.1f}</b>\n"
            f"Trade P&L: <b>₹{trade_pnl:+,.2f}</b>\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    def _notify_runner_exit(self, trade: Dict[str, Any], reason: str, day_pnl: float) -> None:
        if not self.notifier:
            return
        total_pnl = trade.get("lot1_pnl_rs", 0.0) + trade.get("lot2_pnl_rs", 0.0)
        msg = (
            f"🏁 <b>NIFTY RUNNER EXITED [{self.config.mode}]</b>\n\n"
            f"Contract: <code>{trade['symbol']}</code> ({trade['direction']})\n"
            f"Runner Exited: <b>{trade['lot2_lots']} Lots @ ₹{trade['lot2_exit_price']:,.1f}</b>\n"
            f"Exit Reason: <b>{reason}</b>\n"
            f"Runner P&L: <b>₹{trade['lot2_pnl_rs']:+,.2f}</b>\n"
            f"Trade Net P&L: <b>₹{total_pnl:+,.2f}</b>\n\n"
            f"📊 <b>Total Day Realized P&L: ₹{day_pnl:+,.2f}</b>"
        )
        self.notifier.send_message(msg)

    # -------------------------------------------------------------------------
    # Data & Technical Indicators
    # -------------------------------------------------------------------------
    def fetch_recent_candles(self, token: int, num_days: int = 6) -> Optional[pd.DataFrame]:
        if not self.kite:
            return None
        to_d = datetime.now()
        from_d = to_d - timedelta(days=num_days)
        try:
            records = self.kite.historical_data(token, from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d"), "15minute")
            if not records:
                return None
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df["time"] = df["date"].dt.time
            df["day"] = df["date"].dt.date
            return df
        except Exception as e:
            if is_token_error(e):
                self._ensure_kite_session(force_login=True)
                try:
                    records = self.kite.historical_data(token, from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d"), "15minute")
                    if records:
                        df = pd.DataFrame(records)
                        df["date"] = pd.to_datetime(df["date"])
                        df["time"] = df["date"].dt.time
                        df["day"] = df["date"].dt.date
                        return df
                except Exception:
                    pass
            logger.error("Failed to fetch 15m candles for token %s: %s", token, e)
            return None

    def evaluate_reference_range(self, df: pd.DataFrame, target_day: Optional[date] = None) -> Optional[Dict[str, float]]:
        """Calculates 30-min ORB High and Low across 09:15 and 09:30 bars."""
        if df is None or len(df) < 2:
            return None
        t_day = target_day or date.today()
        day_bars = df[df["day"] == t_day]
        ref_bars = day_bars[(day_bars["time"] >= dt_time(9, 15)) & (day_bars["time"] <= dt_time(9, 30))]
        if len(ref_bars) < 2:
            return None

        r_high = float(ref_bars["high"].max())
        r_low = float(ref_bars["low"].min())
        return {"ref_high": r_high, "ref_low": r_low}

    def evaluate_breakout_signal(
        self,
        df: pd.DataFrame,
        ref_high: float,
        ref_low: float,
        current_time: dt_time,
        current_dt: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Evaluates closed 15m bar breakout with 50 EMA and 1.2x volume filters."""
        if df is None or len(df) < 50:
            return None

        eval_dt = current_dt or datetime.now()
        work_df = df.copy()

        if "date" in work_df.columns:
            try:
                candle_dates = pd.to_datetime(work_df["date"])
                if candle_dates.dt.tz is not None:
                    candle_dates = candle_dates.dt.tz_localize(None)
                eval_naive = eval_dt.replace(tzinfo=None) if hasattr(eval_dt, "tzinfo") and eval_dt.tzinfo else eval_dt
                is_closed = (candle_dates + pd.Timedelta(minutes=15)) <= (eval_naive + pd.Timedelta(seconds=5))
                closed_df = work_df[is_closed]
                if len(closed_df) >= 50:
                    work_df = closed_df
            except Exception as e:
                logger.debug("Candle filtering exception: %s", e)

        if len(work_df) < 50:
            return None

        work_df["vol_sma20"] = work_df["volume"].rolling(20).mean().shift(1)
        work_df["ema50"] = work_df["close"].ewm(span=50, adjust=False).mean()

        latest_bar = work_df.iloc[-1]
        b_high = float(latest_bar["high"])
        b_low = float(latest_bar["low"])
        b_close = float(latest_bar["close"])
        b_vol = float(latest_bar["volume"])
        vol_avg = float(latest_bar["vol_sma20"]) if not np.isnan(latest_bar["vol_sma20"]) else 100.0
        ema50_val = float(latest_bar["ema50"])

        vol_ok = b_vol >= self.config.vol_multiplier * vol_avg

        long_ok = (b_high > ref_high) and (b_close > ref_high) and vol_ok
        if self.config.use_trend_filter:
            long_ok = long_ok and (b_close > ema50_val)

        short_ok = (b_low < ref_low) and (b_close < ref_low) and vol_ok
        if self.config.use_trend_filter:
            short_ok = short_ok and (b_close < ema50_val)

        if long_ok:
            return {
                "direction": "LONG",
                "trigger_price": b_close,
                "ref_high": ref_high,
                "ref_low": ref_low,
                "candle_time": latest_bar["time"],
            }
        elif short_ok:
            return {
                "direction": "SHORT",
                "trigger_price": b_close,
                "ref_high": ref_high,
                "ref_low": ref_low,
                "candle_time": latest_bar["time"],
            }
        return None

    # -------------------------------------------------------------------------
    # Core Monitoring Cycle
    # -------------------------------------------------------------------------
    def run_monitoring_cycle(
        self,
        current_time: dt_time,
        current_dt: Optional[datetime] = None,
        mock_ltp: Optional[float] = None,
        mock_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Main iteration evaluating range setup, entry trigger, and open trade management."""
        state = self.state
        if not state:
            return

        status = state.get("status", "INITIALIZING")
        today = current_dt.date() if current_dt else date.today()

        if is_nse_holiday(today):
            logger.info("NSE Holiday today (%s). Desk resting.", today)
            return

        token = state["instrument_token"]
        sym = state["symbol"]
        lot_size = state["lot_size"]

        # Fetch market data
        df = mock_df if mock_df is not None else self.fetch_recent_candles(token)
        ltp = mock_ltp
        if ltp is None and self.kite:
            try:
                quote = self.kite.ltp([f"{self.config.exchange}:{sym}"])
                ltp = float(quote.get(f"{self.config.exchange}:{sym}", {}).get("last_price", 0.0))
            except Exception as e:
                logger.error("Failed to fetch LTP: %s", e)

        # 1. INITIALIZING -> Establish 09:15 - 09:45 Range
        if status == "INITIALIZING":
            if current_time >= self.config.ref_end_time:
                ref = self.evaluate_reference_range(df, target_day=today)
                if ref:
                    state["ref_high"] = ref["ref_high"]
                    state["ref_low"] = ref["ref_low"]
                    state["status"] = "SCANNING"
                    self.save_state(state)
                    ema50 = float(df["close"].ewm(span=50).mean().iloc[-1]) if df is not None and len(df) > 50 else 0.0
                    self._notify_range_established(sym, ref["ref_high"], ref["ref_low"], ema50)

        # 2. SCANNING -> Evaluate Breakout Signal
        elif status == "SCANNING":
            if current_time > self.config.trade_window_end:
                logger.info("Trade window cutoff reached (13:30 IST) without breakout. Session completed.")
                state["status"] = "COMPLETED"
                self.save_state(state)
                return

            sig = self.evaluate_breakout_signal(
                df, state["ref_high"], state["ref_low"], current_time, current_dt
            )
            if sig:
                direction = sig["direction"]
                trigger_p = sig["trigger_price"]
                lots = self.config.lots
                lot1_lots = max(1, lots // 2)
                lot2_lots = lots - lot1_lots
                total_qty = lots * lot_size

                tx_type = "BUY" if direction == "LONG" else "SELL"
                ord_res = self.order_manager.place_order(
                    symbol=sym,
                    transaction_type=tx_type,
                    quantity=total_qty,
                    trigger_price=trigger_p,
                )

                entry_p = ord_res["price"]
                sl_p = (entry_p - self.config.initial_sl_pts) if direction == "LONG" else (entry_p + self.config.initial_sl_pts)
                t1_p = (entry_p + self.config.target1_pts) if direction == "LONG" else (entry_p - self.config.target1_pts)

                trade = {
                    "symbol": sym,
                    "direction": direction,
                    "entry_time": str(current_time),
                    "entry_price": entry_p,
                    "lots_total": lots,
                    "quantity_total": total_qty,
                    "lot1_lots": lot1_lots,
                    "lot2_lots": lot2_lots,
                    "sl_price": sl_p,
                    "target1_price": t1_p,
                    "lot1_status": "OPEN",
                    "lot2_status": "OPEN",
                    "lot1_exit_price": None,
                    "lot1_pnl_rs": 0.0,
                    "lot2_exit_price": None,
                    "lot2_pnl_rs": 0.0,
                }
                state["trades"].append(trade)
                state["status"] = "IN_TRADE"
                self.save_state(state)
                self._notify_entry(trade)

        # 3. IN_TRADE & TARGET1_HIT -> Manage Stop Loss, Target 1, Runner & Cutoff
        elif status in ("IN_TRADE", "TARGET1_HIT") and state["trades"]:
            trade = state["trades"][-1]
            direction = trade["direction"]
            if ltp is None or ltp <= 0:
                return

            # A. Check Stop Loss
            sl_hit = (ltp <= trade["sl_price"]) if direction == "LONG" else (ltp >= trade["sl_price"])
            if sl_hit:
                if status == "IN_TRADE":
                    # Full position SL hit
                    exit_tx = "SELL" if direction == "LONG" else "BUY"
                    self.order_manager.place_order(sym, exit_tx, trade["quantity_total"], trigger_price=ltp)
                    pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                    trade_pnl = pts * trade["quantity_total"]
                    state["realized_day_pnl"] += trade_pnl
                    trade["lot1_status"] = "STOPPED_OUT"
                    trade["lot2_status"] = "STOPPED_OUT"
                    trade["lot1_exit_price"] = ltp
                    trade["lot2_exit_price"] = ltp
                    trade["lot1_pnl_rs"] = pts * (trade["lot1_lots"] * lot_size)
                    trade["lot2_pnl_rs"] = pts * (trade["lot2_lots"] * lot_size)
                    state["status"] = "COMPLETED"
                    self.save_state(state)
                    self._notify_sl(trade, ltp, trade_pnl, state["realized_day_pnl"])
                    return
                elif status == "TARGET1_HIT":
                    # Runner hit cost / breakeven
                    runner_qty = trade["lot2_lots"] * lot_size
                    exit_tx = "SELL" if direction == "LONG" else "BUY"
                    self.order_manager.place_order(sym, exit_tx, runner_qty, trigger_price=ltp)
                    pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                    runner_pnl = pts * runner_qty
                    trade["lot2_status"] = "CLOSED"
                    trade["lot2_exit_price"] = ltp
                    trade["lot2_pnl_rs"] = runner_pnl
                    state["realized_day_pnl"] += runner_pnl
                    state["status"] = "COMPLETED"
                    self.save_state(state)
                    self._notify_runner_exit(trade, "Cost/Breakeven Reversal", state["realized_day_pnl"])
                    return

            # B. Check Target 1 (When IN_TRADE)
            if status == "IN_TRADE":
                t1_hit = (ltp >= trade["target1_price"]) if direction == "LONG" else (ltp <= trade["target1_price"])
                if t1_hit:
                    t1_qty = trade["lot1_lots"] * lot_size
                    exit_tx = "SELL" if direction == "LONG" else "BUY"
                    self.order_manager.place_order(sym, exit_tx, t1_qty, trigger_price=ltp)
                    pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                    t1_pnl = pts * t1_qty
                    trade["lot1_status"] = "TARGET_HIT"
                    trade["lot1_exit_price"] = ltp
                    trade["lot1_pnl_rs"] = t1_pnl
                    state["realized_day_pnl"] += t1_pnl

                    # Trail runner SL to Cost
                    trade["sl_price"] = trade["entry_price"]
                    state["status"] = "TARGET1_HIT"
                    self.save_state(state)
                    self._notify_target1(trade, state["realized_day_pnl"])

            # C. Check Session Cutoff (15:15 IST)
            if current_time >= self.config.session_cutoff_time:
                runner_qty = trade["lot2_lots"] * lot_size
                exit_tx = "SELL" if direction == "LONG" else "BUY"
                self.order_manager.place_order(sym, exit_tx, runner_qty, trigger_price=ltp)
                pts = (ltp - trade["entry_price"]) if direction == "LONG" else (trade["entry_price"] - ltp)
                runner_pnl = pts * runner_qty
                trade["lot2_status"] = "CLOSED"
                trade["lot2_exit_price"] = ltp
                trade["lot2_pnl_rs"] = runner_pnl
                state["realized_day_pnl"] += runner_pnl
                state["status"] = "COMPLETED"
                self.save_state(state)
                self._notify_runner_exit(trade, "15:15 EOD Session Cutoff", state["realized_day_pnl"])
```

Update `strategy/nifty_orb/__init__.py`:
```python
from .config import NiftyORBConfig
from .contract_resolver import resolve_active_nifty_fut_contract
from .desk import NiftyORBDesk
from .order_manager import NiftyOrderManager

__all__ = [
    "NiftyORBConfig",
    "resolve_active_nifty_fut_contract",
    "NiftyOrderManager",
    "NiftyORBDesk",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_nifty_orb.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/nifty_orb/desk.py strategy/nifty_orb/__init__.py tests/test_nifty_orb.py
git commit -m "feat(nifty_orb): add NiftyORBDesk with signal evaluation and tranche management"
```

---

### Task 5: Standalone CLI Runner (`run_nifty_orb.py`)

**Files:**
- Create: `run_nifty_orb.py`
- Test: `tests/test_nifty_orb.py`

**Interfaces:**
- Produces: CLI interface with `--live`, `--paper`, `--lots`, `--sl`, `--target1`, `--poll`, `--dry-run`, `--once`.

- [ ] **Step 1: Write test for argument parsing in `run_nifty_orb.py`**

Add to `tests/test_nifty_orb.py`:
```python
from run_nifty_orb import parse_args


def test_parse_args_defaults():
    args = parse_args([])
    assert args.paper is True
    assert args.lots == 2
    assert args.sl == 50.0
    assert args.target1 == 75.0
    assert args.product == "MIS"
    assert args.order_type == "LIMIT"
    assert args.limit_buffer_pts == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nifty_orb.py::test_parse_args_defaults -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'run_nifty_orb'`

- [ ] **Step 3: Implement `run_nifty_orb.py`**

Create `run_nifty_orb.py`:
```python
import argparse
import logging
import os
import sys
import time
from datetime import datetime

# Ensure stdout and stderr handle UTF-8 characters gracefully
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import load_config
from kite import get_kite_client
from notifications.telegram import TelegramNotifier
from strategy.logger import setup_logging
from strategy.nifty_orb import NiftyORBConfig, NiftyORBDesk, NiftyOrderManager

logger = logging.getLogger("run_nifty_orb")


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Sharad's NIFTY 50 Index 30-Min ORB Futures Trading Desk"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--paper", action="store_true", default=True, help="Run simulated PAPER trading (default)"
    )
    mode_group.add_argument(
        "--live", action="store_true", help="Execute real LIVE orders via Kite Connect"
    )

    parser.add_argument("--lots", type=int, default=2, help="Number of Nifty Futures lots (default: 2)")
    parser.add_argument("--sl", type=float, default=50.0, help="Initial Stop Loss in points (default: 50.0)")
    parser.add_argument("--target1", type=float, default=75.0, help="Tranche 1 Target in points (default: 75.0)")
    parser.add_argument("--poll", type=int, default=5, help="Polling interval in seconds (default: 5)")
    parser.add_argument("--product", choices=["MIS", "NRML"], default="MIS", help="Product type (default: MIS)")
    parser.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT", help="Order execution type (default: LIMIT)")
    parser.add_argument("--limit-buffer-pts", type=float, default=5.0, help="Marketable limit price buffer in points (default: 5.0)")
    parser.add_argument("--state-file", type=str, default=os.getenv("NIFTY_STATE_FILE", "data/nifty_orb_trades_today.json"), help="State file path")
    parser.add_argument("--dry-run", action="store_true", help="Inspect active contract and day state without trading")
    parser.add_argument("--once", action="store_true", help="Run single evaluation cycle and exit")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=os.getenv("LOG_LEVEL", "INFO"), help="Logging verbosity level (default: INFO)")
    parser.add_argument("--log-dir", type=str, default=os.getenv("LOG_DIR", "logs"), help="Directory for host log files (default: logs)")

    return parser.parse_args(args)


def main(args=None) -> int:
    parsed = parse_args(args)
    setup_logging(log_dir=parsed.log_dir, log_level=parsed.log_level, log_filename="nifty_orb_desk.log")
    mode = "LIVE" if parsed.live else "PAPER"

    t1_lots = max(1, parsed.lots // 2)
    t2_lots = parsed.lots - t1_lots

    logger.info("=" * 75)
    logger.info("  STARTING NIFTY 50 INDEX 30-MIN ORB FUTURES DESK [%s MODE]", mode)
    logger.info("  Lots: %d | Initial SL: %.1f pts | Tranche 1 Target: %.1f pts", parsed.lots, parsed.sl, parsed.target1)
    logger.info("  Plan: Book Tranche 1 (%d lots) @ +%.1f pts & Move SL to Cost; Runner (%d lots) rides to 15:15 IST", t1_lots, parsed.target1, t2_lots)
    logger.info("=" * 75)

    config = load_config()
    kite = get_kite_client(config=config)
    notifier = TelegramNotifier(bot_token=config.telegram_bot_token, chat_id=config.telegram_chat_id)

    nifty_cfg = NiftyORBConfig(
        lots=parsed.lots,
        initial_sl_pts=parsed.sl,
        target1_pts=parsed.target1,
        mode=mode,
        product=parsed.product,
        order_type=parsed.order_type,
        limit_buffer_pts=parsed.limit_buffer_pts,
        poll_interval_seconds=parsed.poll,
        state_file=parsed.state_file,
    )

    om = NiftyOrderManager(
        mode=mode,
        kite=kite if mode == "LIVE" else None,
        product=parsed.product,
        order_type=parsed.order_type,
        limit_buffer_pts=parsed.limit_buffer_pts,
    )

    desk = NiftyORBDesk(
        config=nifty_cfg,
        kite=kite,
        order_manager=om,
        notifier=notifier,
    )

    state = desk.initialize_day()
    logger.info("Active Contract: %s (Token: %s, Lot Size: %d)", state["symbol"], state["instrument_token"], state["lot_size"])

    if parsed.dry_run:
        logger.info("Dry-run complete. Exiting.")
        return 0

    if parsed.once:
        desk.run_monitoring_cycle(datetime.now().time(), datetime.now())
        return 0

    logger.info("Entering daemon monitoring loop (polling every %ds)...", parsed.poll)
    try:
        while True:
            now = datetime.now()
            desk.run_monitoring_cycle(now.time(), now)
            if desk.state and desk.state.get("status") == "COMPLETED":
                logger.info("Session complete. Desk finished for today.")
                break
            time.sleep(parsed.poll)
    except KeyboardInterrupt:
        logger.info("Shutdown requested. State preserved.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_nifty_orb.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add run_nifty_orb.py tests/test_nifty_orb.py
git commit -m "feat(nifty_orb): add standalone CLI runner run_nifty_orb.py"
```

---

### Task 6: Docker Compose Service Integration

**Files:**
- Modify: `docker-compose.yml`
- Test: Syntax check and dry-run CLI test

**Interfaces:**
- Produces: `nifty-orb-desk` service in `docker-compose.yml`.

- [ ] **Step 1: Add `nifty-orb-desk` service block to `docker-compose.yml`**

Add the service block under `services:` in `docker-compose.yml`:
```yaml
  nifty-orb-desk:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: nifty_orb_desk
    image: ${DOCKER_USER:-sharadprsn}/nifty_strangle_desk:latest
    restart: unless-stopped
    entrypoint: ["python", "run_nifty_orb.py"]
    env_file:
      - .env
    environment:
      - TZ=Asia/Kolkata
      - PYTHONUNBUFFERED=1
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-10}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "10"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./.env:/app/.env
    # -----------------------------------------------------------------------------------
    # NIFTY 50 30-Min ORB Futures Desk (2-Lot Tranche Scaling):
    # - Reference: 09:15 - 09:45 IST, 15m confirmation bars
    # - Risk: 2 Lots, SL: 50 pts
    # - Tranche: Tranche 1 (1 Lot) booked @ +75 pts; Runner (1 Lot) trailed to Cost & rides to 15:15
    #
    # 1. Paper Trading Mode (Default Simulation):
    command: ["--paper", "--lots", "2", "--sl", "50", "--target1", "75", "--order-type", "LIMIT", "--limit-buffer-pts", "5.0", "--poll", "5"]
    #
    # 2. Live Production Mode (Live Broker Execution via Kite Connect):
    # command: ["--live", "--lots", "2", "--sl", "50", "--target1", "75", "--order-type", "LIMIT", "--limit-buffer-pts", "5.0", "--poll", "5"]
    # -----------------------------------------------------------------------------------
```

- [ ] **Step 2: Validate docker-compose syntax**

Run: `python -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"`
Expected: Clean load without YAML parsing errors.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(nifty_orb): add nifty-orb-desk service to docker-compose.yml"
```

---

### Task 7: Full End-to-End Test Suite Execution

**Files:**
- Modify/Run: `tests/test_nifty_orb.py`

- [ ] **Step 1: Run complete test suite**

Run: `pytest tests/test_nifty_orb.py -v`
Expected: All tests PASS.

- [ ] **Step 2: Verify dry-run with CLI**

Run: `python run_nifty_orb.py --dry-run`
Expected: Initializes day, resolves active contract, logs config, exits code 0.

- [ ] **Step 3: Commit final test validations**

```bash
git commit --allow-empty -m "test(nifty_orb): verify complete end-to-end test suite passes"
```
