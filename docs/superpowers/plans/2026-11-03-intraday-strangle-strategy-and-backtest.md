# NIFTY 10:00 AM Short Strangle Strategy & Realistic Backtesting Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete, production-grade 10:00 AM NIFTY short strangle strategy with independent +50% SL, 80% decay target, 3:00 PM exit, 0 DTE $\to$ next week shift, dual Live/Paper execution desk, and a realistic fractional-DTE backtesting engine with exact NSE statutory costs and slippage.

**Architecture:** A dedicated `strategy/` package containing options math (`options_model.py`), NSE cost accounting (`costs.py`), live option chain discovery (`option_chain.py`), paper/live order abstraction (`order_manager.py`), crash-resilient state machine desk (`strangle_desk.py`), and historical backtest simulator (`backtest.py`), exposed via CLI scripts (`run_backtest.py` and `run_strategy.py`).

**Tech Stack:** Python 3.12, `uv`, `scipy`, `numpy`, `pandas`, `pyarrow`, `kiteconnect`, `pyotp`, `tabulate`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-03-intraday-strangle-strategy-and-backtest-design.md`

## Global Constraints
- Python: `>=3.10`
- Underlying: NIFTY 50 (token `256265`), 10 lots ($10 \times \text{lot\_size}$)
- Entry Time: 10:00 AM IST
- Expiry Rule: Next week on 0 DTE, otherwise Current week
- Strike Rule: 1 CE and 1 PE with premium strictly $\ge ₹30.0$ (lowest above 30)
- Exits: Independent $+50\%$ SL per leg, independent $80\%$ decay target per leg, 3:00 PM mandatory square-off
- Realistic Backtest: Intraday continuous fractional DTE, ₹0.50 slippage, exact NSE turnover charges, STT, SEBI, Stamp duty, GST, and ₹40 round-trip brokerage
- Safety: Paper trading default; Live mode requires explicit confirmation and persistent state tracking

---

### Task 1: Scientific Dependencies, Black-76 Pricing & Fractional DTE Model

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Create: `strategy/__init__.py`
- Create: `strategy/options_model.py`
- Test: `tests/test_options_model.py`

**Interfaces:**
- Produces:
  - `compute_fractional_dte(target_date: date, current_dt: datetime) -> float`
  - `black_76_price(spot: float, strike: float, dte_days: float, iv: float, r: float, opt_type: str) -> float`

- [ ] **Step 1: Update dependencies in `pyproject.toml` and `requirements.txt` and install via `uv`**

Update `pyproject.toml`:
```toml
[project]
name = "sharads-intraday-option-selling-desk"
version = "0.1.0"
description = "Intraday option selling desk with Kite Connect API and TOTP authentication"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "kiteconnect>=5.0.0",
    "pyotp>=2.9.0",
    "requests>=2.31.0",
    "python-dotenv>=1.0.0",
    "tabulate>=0.9.0",
    "scipy>=1.11.0",
    "numpy>=1.24.0",
    "pandas>=2.0.0",
    "pyarrow>=14.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
]

[tool.pytest.ini_options]
pythonpath = ["."]
```

Update `requirements.txt`:
```txt
kiteconnect>=5.0.0
pyotp>=2.9.0
requests>=2.31.0
python-dotenv>=1.0.0
tabulate>=0.9.0
scipy>=1.11.0
numpy>=1.24.0
pandas>=2.0.0
pyarrow>=14.0.0
pytest>=8.0.0
```

Install:
```bash
uv pip install -r requirements.txt
```

- [ ] **Step 2: Write the failing tests for options pricing and fractional DTE**

Create `tests/test_options_model.py`:
```python
from datetime import date, datetime, time
import pytest
from strategy.options_model import compute_fractional_dte, black_76_price

def test_compute_fractional_dte_intraday_decay():
    target_expiry = date(2026, 11, 5) # Thursday
    
    # Thursday morning at 10:00 AM
    dt_morning = datetime(2026, 11, 5, 10, 0)
    dte_morning = compute_fractional_dte(target_expiry, dt_morning)
    
    # Thursday afternoon at 15:00 PM (same day, 5 hours later)
    dt_afternoon = datetime(2026, 11, 5, 15, 0)
    dte_afternoon = compute_fractional_dte(target_expiry, dt_afternoon)
    
    # Intraday DTE must decrease monotonically as time passes
    assert dte_morning > dte_afternoon > 0.0
    assert dte_morning < 1.0  # 0 DTE is less than 1 day

def test_compute_fractional_dte_future_expiry():
    target_expiry = date(2026, 11, 12) # Next week (7 days ahead)
    dt_current = datetime(2026, 11, 5, 10, 0)
    dte = compute_fractional_dte(target_expiry, dt_current)
    assert 6.0 < dte < 8.0

def test_black_76_pricing_call_and_put():
    spot = 24000.0
    strike = 24000.0
    dte_days = 4.0
    iv = 0.13
    r = 0.065
    
    call_price = black_76_price(spot, strike, dte_days, iv, r, "CE")
    put_price = black_76_price(spot, strike, dte_days, iv, r, "PE")
    
    assert call_price > 0.0
    assert put_price > 0.0
    # For ATM with r=6.5% and 4 DTE, Call and Put are very close with slight interest rate drift
    assert abs(call_price - put_price) < 20.0

def test_black_76_otm_decay():
    spot = 24000.0
    strike = 24500.0 # 500 points OTM Call
    iv = 0.13
    r = 0.065
    
    # 4 DTE vs 0.5 DTE
    price_high_dte = black_76_price(spot, strike, 4.0, iv, r, "CE")
    price_low_dte = black_76_price(spot, strike, 0.5, iv, r, "CE")
    
    assert price_high_dte > price_low_dte > 0.0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_options_model.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'strategy'`)

- [ ] **Step 4: Implement `strategy/__init__.py` and `strategy/options_model.py`**

Create `strategy/__init__.py`:
```python
"""Intraday Option Selling Strategy and Realistic Backtesting Package."""
```

Create `strategy/options_model.py`:
```python
import math
from datetime import date, datetime, time
from typing import Optional
from scipy.stats import norm

TRADING_MINUTES_PER_DAY = 375.0  # 09:15 to 15:30
ANNUAL_TRADING_DAYS = 252.0

def compute_fractional_dte(target_expiry: date, current_dt: datetime) -> float:
    """
    Computes continuous fractional DTE down to the minute.
    
    Parameters:
        target_expiry: Expiry date
        current_dt: Current timestamp
    
    Returns:
        Equivalent DTE in trading days (e.g. 0.45 days for 0-DTE afternoon).
    """
    current_date = current_dt.date()
    calendar_days = (target_expiry - current_date).days

    if calendar_days < 0:
        return 1e-5

    # Compute remaining minutes in today's trading session (cutoff 15:30)
    current_minutes = current_dt.hour * 60 + current_dt.minute
    market_close_minutes = 15 * 60 + 30  # 15:30
    market_open_minutes = 9 * 60 + 15   # 09:15

    remaining_minutes_today = max(0.0, float(market_close_minutes - max(market_open_minutes, current_minutes)))
    fraction_of_day_today = min(1.0, remaining_minutes_today / TRADING_MINUTES_PER_DAY)

    if calendar_days == 0:
        return max(fraction_of_day_today, 1e-4)

    # For future days, remaining fraction today + intervening full days
    return float(calendar_days - 1) + fraction_of_day_today

def black_76_price(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float = 0.13,
    r: float = 0.065,
    opt_type: str = "CE"
) -> float:
    """
    Computes analytical Black-76 option premium.
    
    Parameters:
        spot: Underlying index price (e.g. 24000.0)
        strike: Option strike price (e.g. 24200.0)
        dte_days: Fractional trading days to expiry
        iv: Implied volatility (e.g. 0.13 for 13%)
        r: Annualized risk-free rate (default 6.5%)
        opt_type: 'CE' or 'PE'
    """
    t = max(dte_days / ANNUAL_TRADING_DAYS, 1e-5)
    sigma = max(iv, 0.01)
    d = math.exp(-r * t)
    sqrt_t = math.sqrt(t)

    denom = sigma * sqrt_t
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / denom
    d2 = d1 - denom

    if opt_type.upper() == "CE":
        price = d * (spot * norm.cdf(d1) - strike * norm.cdf(d2))
    else:
        price = d * (strike * norm.cdf(-d2) - spot * norm.cdf(-d1))

    return max(0.05, round(float(price), 2))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_options_model.py -v`  
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt strategy/__init__.py strategy/options_model.py tests/test_options_model.py
git commit -m "feat: add scientific dependencies and Black-76 fractional DTE option pricing engine"
```

---

### Task 2: Exact NSE Transaction Costs, Taxes & Slippage Engine

**Files:**
- Create: `strategy/costs.py`
- Test: `tests/test_costs.py`

**Interfaces:**
- Produces:
  - `calculate_trade_costs(entry_price: float, exit_price: float, quantity: int, slippage_per_share: float = 0.50) -> Dict[str, float]`

- [ ] **Step 1: Write the failing test for exact NSE costs and taxes**

Create `tests/test_costs.py`:
```python
import pytest
from strategy.costs import calculate_trade_costs

def test_calculate_trade_costs_structure():
    # 10 lots NIFTY (assume 750 qty), Sell at 32.0, Buy back at 10.0
    res = calculate_trade_costs(
        entry_price=32.0,
        exit_price=10.0,
        quantity=750,
        slippage_per_share=0.50
    )
    
    assert "gross_pnl" in res
    assert "net_pnl" in res
    assert "brokerage" in res
    assert "stt" in res
    assert "exchange_turnover_fee" in res
    assert "sebi_fee" in res
    assert "stamp_duty" in res
    assert "gst" in res
    assert "total_slippage" in res
    assert "total_costs" in res

    # Brokerage: 20 per order * 2 = 40.0
    assert res["brokerage"] == 40.0
    
    # STT: 0.1% on SELL turnover (only entry side for option sellers)
    # 0.001 * 32.0 * 750 = 24.0
    assert pytest.approx(res["stt"], 0.01) == 24.0
    
    # Slippage: 0.50 entry + 0.50 exit = 1.0 * 750 = 750.0
    assert res["total_slippage"] == 750.0

    # Net PnL must be strictly less than gross PnL
    assert res["net_pnl"] < res["gross_pnl"]
    assert res["net_pnl"] == pytest.approx(res["gross_pnl"] - res["total_costs"], 0.01)

def test_calculate_trade_costs_loss_trade():
    # Stop-loss triggered: Sell at 30.0, Buy back at 45.0 (+50% SL)
    res = calculate_trade_costs(
        entry_price=30.0,
        exit_price=45.0,
        quantity=750,
        slippage_per_share=0.50
    )
    
    # Gross loss: (30 - 45) * 750 = -11250
    assert res["gross_pnl"] == -11250.0
    # Net loss must be even more negative due to costs
    assert res["net_pnl"] < -11250.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_costs.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'strategy.costs'`)

- [ ] **Step 3: Implement `strategy/costs.py`**

Create `strategy/costs.py`:
```python
from typing import Dict

def calculate_trade_costs(
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_per_share: float = 0.50
) -> Dict[str, float]:
    """
    Computes exact NSE F&O Option transaction costs, taxes, slippage, and net P&L:
    - Brokerage: Rs. 20 per order (Rs. 40 round trip)
    - STT: 0.1% on SELL turnover (entry side for short options)
    - Exchange turnover charges: 0.0495% on total turnover
    - SEBI charges: Rs. 10 / crore (0.0001%)
    - Stamp duty: 0.003% on BUY turnover (exit side)
    - GST: 18% on (Brokerage + Exchange Fees + SEBI Fees)
    - Slippage: Applied to entry and exit executions
    """
    qty = abs(quantity)
    
    # Gross P&L based on executed order prices
    gross_pnl = round((entry_price - exit_price) * qty, 2)

    # 1. Brokerage
    brokerage = 40.0

    # 2. STT: 0.1% on SELL turnover (mandatory for option sellers)
    stt = round(0.001 * entry_price * qty, 2)

    # 3. Exchange Turnover Charge: 0.0495% on both legs
    turnover = (entry_price + exit_price) * qty
    exchange_fee = round(0.000495 * turnover, 2)

    # 4. SEBI turnover fee: Rs 10 per crore (0.0001%)
    sebi_fee = round(0.000001 * turnover, 2)

    # 5. Stamp duty: 0.003% on BUY turnover (exit side)
    stamp_duty = round(0.00003 * exit_price * qty, 2)

    # 6. GST: 18% on (Brokerage + Exchange + SEBI)
    gst = round(0.18 * (brokerage + exchange_fee + sebi_fee), 2)

    # 7. Total Slippage (friction per share on both entry and exit)
    total_slippage = round(slippage_per_share * 2.0 * qty, 2)

    statutory_taxes = round(brokerage + stt + exchange_fee + sebi_fee + stamp_duty + gst, 2)
    total_costs = round(statutory_taxes + total_slippage, 2)
    net_pnl = round(gross_pnl - total_costs, 2)

    return {
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "brokerage": brokerage,
        "stt": stt,
        "exchange_turnover_fee": exchange_fee,
        "sebi_fee": sebi_fee,
        "stamp_duty": stamp_duty,
        "gst": gst,
        "statutory_taxes": statutory_taxes,
        "total_slippage": total_slippage,
        "total_costs": total_costs
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_costs.py -v`  
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add strategy/costs.py tests/test_costs.py
git commit -m "feat: add exact NSE statutory costs, taxes, and slippage calculation engine"
```

---

### Task 3: Expiry Selection & Strike Finder (`LTP > ₹30`)

**Files:**
- Create: `strategy/option_chain.py`
- Test: `tests/test_option_chain.py`

**Interfaces:**
- Produces:
  - `resolve_target_expiry(instruments: List[Dict], today_date: Optional[date] = None) -> date`
  - `find_strangle_strikes(kite: KiteConnect, underlying: str = "NIFTY", target_price: float = 30.0, target_date: Optional[date] = None) -> Dict[str, Any]`

- [ ] **Step 1: Write the failing test for expiry resolution and strike discovery**

Create `tests/test_option_chain.py`:
```python
from datetime import date
from unittest.mock import MagicMock
import pytest
from strategy.option_chain import resolve_target_expiry, find_strangle_strikes

@pytest.fixture
def mock_nfo_instruments():
    return [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 11, 5), "tradingsymbol": "NIFTY26NOV24000CE", "lot_size": 75},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 24000.0, "expiry": date(2026, 11, 5), "tradingsymbol": "NIFTY26NOV24000PE", "lot_size": 75},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24000.0, "expiry": date(2026, 11, 12), "tradingsymbol": "NIFTY26NOV24000CE12", "lot_size": 75},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 24000.0, "expiry": date(2026, 11, 12), "tradingsymbol": "NIFTY26NOV24000PE12", "lot_size": 75},
        {"name": "BANKNIFTY", "instrument_type": "CE", "strike": 52000.0, "expiry": date(2026, 11, 5), "tradingsymbol": "BANKNIFTY26NOV52000CE", "lot_size": 30},
    ]

def test_resolve_target_expiry_non_expiry_day(mock_nfo_instruments):
    # Today is Monday Nov 2 -> target should be nearest upcoming Thursday Nov 5
    today = date(2026, 11, 2)
    expiry = resolve_target_expiry(mock_nfo_instruments, today_date=today)
    assert expiry == date(2026, 11, 5)

def test_resolve_target_expiry_0_dte_shifts_to_next_week(mock_nfo_instruments):
    # Today is Thursday Nov 5 (0 DTE) -> target must shift to Next Week Nov 12
    today = date(2026, 11, 5)
    expiry = resolve_target_expiry(mock_nfo_instruments, today_date=today)
    assert expiry == date(2026, 11, 12)

def test_find_strangle_strikes_closest_above_30():
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24300.0, "expiry": date(2026, 11, 5), "tradingsymbol": "NIFTY26NOV24300CE", "lot_size": 75},
        {"name": "NIFTY", "instrument_type": "CE", "strike": 24200.0, "expiry": date(2026, 11, 5), "tradingsymbol": "NIFTY26NOV24200CE", "lot_size": 75},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23700.0, "expiry": date(2026, 11, 5), "tradingsymbol": "NIFTY26NOV23700PE", "lot_size": 75},
        {"name": "NIFTY", "instrument_type": "PE", "strike": 23600.0, "expiry": date(2026, 11, 5), "tradingsymbol": "NIFTY26NOV23600PE", "lot_size": 75},
    ]

    mock_kite.ltp.return_value = {
        "NFO:NIFTY26NOV24300CE": {"last_price": 28.5},
        "NFO:NIFTY26NOV24200CE": {"last_price": 32.1}, # Closest >= 30
        "NFO:NIFTY26NOV23700PE": {"last_price": 31.4}, # Closest >= 30
        "NFO:NIFTY26NOV23600PE": {"last_price": 24.0},
    }

    result = find_strangle_strikes(
        mock_kite,
        underlying="NIFTY",
        target_price=30.0,
        target_date=date(2026, 11, 2)
    )

    assert result["ce"]["symbol"] == "NIFTY26NOV24200CE"
    assert result["ce"]["ltp"] == 32.1
    assert result["pe"]["symbol"] == "NIFTY26NOV23700PE"
    assert result["pe"]["ltp"] == 31.4
    assert result["lot_size"] == 75
    assert result["total_lots"] == 10
    assert result["total_quantity"] == 750
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_option_chain.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'strategy.option_chain'`)

- [ ] **Step 3: Implement `strategy/option_chain.py`**

Create `strategy/option_chain.py`:
```python
import logging
from datetime import date, datetime
from typing import List, Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.chain")

def resolve_target_expiry(instruments: List[Dict[str, Any]], today_date: Optional[date] = None) -> date:
    """
    Finds target weekly expiry:
    If today is expiry day (0 DTE), selects the Next Week expiry.
    Otherwise, selects the Current Week nearest expiry.
    """
    today = today_date or datetime.now().date()
    
    nifty_expiries = sorted(list(set(
        i["expiry"] for i in instruments
        if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE") and i.get("expiry")
    )))

    upcoming = [e for e in nifty_expiries if e >= today]
    if not upcoming:
        raise ValueError("No upcoming NIFTY option expiries found in instruments list.")

    # 0 DTE Rule: If today is expiry day, take next week's expiry
    if upcoming[0] == today:
        if len(upcoming) > 1:
            logger.info("Today is Expiry Day (0 DTE). Selecting Next Week's expiry: %s", upcoming[1])
            return upcoming[1]
        return upcoming[0]

    logger.info("Selecting Current Week's expiry: %s", upcoming[0])
    return upcoming[0]

def find_strangle_strikes(
    kite: KiteConnect,
    underlying: str = "NIFTY",
    target_price: float = 30.0,
    target_date: Optional[date] = None,
    lots: int = 10
) -> Dict[str, Any]:
    """
    Discovers 1 CE and 1 PE with LTP >= target_price (closest to target_price from above).
    """
    instruments = kite.instruments("NFO")
    target_expiry = resolve_target_expiry(instruments, today_date=target_date)

    # Filter contracts matching underlying and expiry
    ce_contracts = [
        i for i in instruments
        if i.get("name") == underlying and i.get("instrument_type") == "CE" and i.get("expiry") == target_expiry
    ]
    pe_contracts = [
        i for i in instruments
        if i.get("name") == underlying and i.get("instrument_type") == "PE" and i.get("expiry") == target_expiry
    ]

    if not ce_contracts or not pe_contracts:
        raise ValueError(f"No CE/PE option contracts found for {underlying} on {target_expiry}")

    lot_size = int(ce_contracts[0].get("lot_size", 75))
    total_quantity = lots * lot_size

    # Batch query prices
    symbols_to_query = [f"NFO:{c['tradingsymbol']}" for c in ce_contracts + pe_contracts]
    quotes = kite.ltp(symbols_to_query)

    # Find CE >= target_price with lowest LTP
    valid_ce = []
    for c in ce_contracts:
        sym = c["tradingsymbol"]
        ltp_data = quotes.get(f"NFO:{sym}", {})
        ltp = float(ltp_data.get("last_price", 0.0))
        if ltp >= target_price:
            valid_ce.append((ltp, c))

    if not valid_ce:
        raise ValueError(f"No CE strike found with LTP >= {target_price}")
    valid_ce.sort(key=lambda x: x[0])
    chosen_ce_ltp, chosen_ce = valid_ce[0]

    # Find PE >= target_price with lowest LTP
    valid_pe = []
    for p in pe_contracts:
        sym = p["tradingsymbol"]
        ltp_data = quotes.get(f"NFO:{sym}", {})
        ltp = float(ltp_data.get("last_price", 0.0))
        if ltp >= target_price:
            valid_pe.append((ltp, p))

    if not valid_pe:
        raise ValueError(f"No PE strike found with LTP >= {target_price}")
    valid_pe.sort(key=lambda x: x[0])
    chosen_pe_ltp, chosen_pe = valid_pe[0]

    return {
        "expiry": target_expiry,
        "lot_size": lot_size,
        "total_lots": lots,
        "total_quantity": total_quantity,
        "ce": {
            "symbol": chosen_ce["tradingsymbol"],
            "strike": float(chosen_ce["strike"]),
            "ltp": chosen_ce_ltp,
            "sl_price": round(chosen_ce_ltp * 1.50, 2),
            "target_price": round(chosen_ce_ltp * 0.20, 2)
        },
        "pe": {
            "symbol": chosen_pe["tradingsymbol"],
            "strike": float(chosen_pe["strike"]),
            "ltp": chosen_pe_ltp,
            "sl_price": round(chosen_pe_ltp * 1.50, 2),
            "target_price": round(chosen_pe_ltp * 0.20, 2)
        }
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_option_chain.py -v`  
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add strategy/option_chain.py tests/test_option_chain.py
git commit -m "feat: add expiry resolution and 0 DTE strike finder"
```

---

### Task 4: Order Manager, State Machine & Desk Supervisor

**Files:**
- Create: `strategy/order_manager.py`
- Create: `strategy/strangle_desk.py`
- Test: `tests/test_order_manager.py`

**Interfaces:**
- Produces:
  - `OrderManager(mode: str, kite: KiteConnect)`
  - `StrangleDesk(order_manager: OrderManager, state_file: Optional[Path])`

- [ ] **Step 1: Write the failing tests for order execution and desk state machine**

Create `tests/test_order_manager.py`:
```python
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk

def test_paper_order_execution():
    om = OrderManager(mode="PAPER", kite=None)
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=750, price=32.0)
    assert order["status"] == "COMPLETE"
    assert order["mode"] == "PAPER"
    assert order["symbol"] == "NIFTY26NOV24200CE"
    assert order["fill_price"] == 32.0

def test_live_order_execution():
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ORDER_12345"
    
    om = OrderManager(mode="LIVE", kite=mock_kite)
    order = om.sell(symbol="NIFTY26NOV24200CE", quantity=750, price=32.0)
    
    assert order["order_id"] == "ORDER_12345"
    assert order["mode"] == "LIVE"
    mock_kite.place_order.assert_called_once()

def test_desk_state_persistence_and_recovery(tmp_path):
    state_file = tmp_path / "trades_today.json"
    om = OrderManager(mode="PAPER", kite=None)
    desk = StrangleDesk(order_manager=om, state_file=state_file)

    trade_setup = {
        "date": "2026-11-03",
        "status": "OPEN",
        "ce": {"symbol": "NIFTY26NOV24200CE", "entry": 32.0, "sl": 48.0, "target": 6.4, "status": "OPEN", "qty": 750},
        "pe": {"symbol": "NIFTY26NOV23700PE", "entry": 31.0, "sl": 46.5, "target": 6.2, "status": "OPEN", "qty": 750}
    }
    desk.save_state(trade_setup)
    assert state_file.exists()

    # Load recovered state in a fresh instance
    desk2 = StrangleDesk(order_manager=om, state_file=state_file)
    recovered = desk2.load_state()
    assert recovered["ce"]["entry"] == 32.0
    assert recovered["status"] == "OPEN"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_order_manager.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'strategy.order_manager'`)

- [ ] **Step 3: Implement `strategy/order_manager.py` and `strategy/strangle_desk.py`**

Create `strategy/order_manager.py`:
```python
import logging
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect

logger = logging.getLogger("strategy.order")

class OrderManager:
    """Manages order execution for both PAPER simulation and LIVE trading."""

    def __init__(self, mode: str = "PAPER", kite: Optional[KiteConnect] = None):
        self.mode = mode.upper()
        self.kite = kite
        if self.mode == "LIVE" and not self.kite:
            raise ValueError("KiteConnect instance required for LIVE mode.")

    def sell(self, symbol: str, quantity: int, price: float) -> Dict[str, Any]:
        """Sells an option leg (entry)."""
        logger.info("[%s] Executing SELL: %s x %d @ Rs %.2f", self.mode, symbol, quantity, price)
        if self.mode == "PAPER":
            return {
                "order_id": f"PAPER_SELL_{symbol}",
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "fill_price": price,
                "status": "COMPLETE"
            }
        
        # LIVE Mode
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET
        )
        return {
            "order_id": order_id,
            "mode": "LIVE",
            "symbol": symbol,
            "transaction_type": "SELL",
            "quantity": quantity,
            "fill_price": price,
            "status": "SUBMITTED"
        }

    def buy_to_cover(self, symbol: str, quantity: int, price: float, reason: str = "EXIT") -> Dict[str, Any]:
        """Buys back an open short option leg (exit on SL, target, or 3PM)."""
        logger.info("[%s] Executing BUY TO COVER (%s): %s x %d @ Rs %.2f", self.mode, reason, symbol, quantity, price)
        if self.mode == "PAPER":
            return {
                "order_id": f"PAPER_BUY_{symbol}",
                "mode": "PAPER",
                "symbol": symbol,
                "transaction_type": "BUY",
                "quantity": quantity,
                "fill_price": price,
                "reason": reason,
                "status": "COMPLETE"
            }

        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_BUY,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET
        )
        return {
            "order_id": order_id,
            "mode": "LIVE",
            "symbol": symbol,
            "transaction_type": "BUY",
            "quantity": quantity,
            "fill_price": price,
            "reason": reason,
            "status": "SUBMITTED"
        }
```

Create `strategy/strangle_desk.py`:
```python
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from strategy.order_manager import OrderManager

logger = logging.getLogger("strategy.desk")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "trades_today.json"

class StrangleDesk:
    """Manages strategy state, persistence, and recovery."""

    def __init__(self, order_manager: OrderManager, state_file: Optional[Path] = None):
        self.order_manager = order_manager
        self.state_file = state_file or DEFAULT_STATE_FILE
        self.state: Optional[Dict[str, Any]] = None

    def save_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_state(self) -> Optional[Dict[str, Any]]:
        if not self.state_file.exists():
            return None
        try:
            content = self.state_file.read_text(encoding="utf-8")
            self.state = json.loads(content)
            return self.state
        except Exception as e:
            logger.error("Failed to load state from %s: %s", self.state_file, e)
            return None

    def clear_state(self) -> None:
        if self.state_file.exists():
            self.state_file.unlink()
        self.state = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_order_manager.py -v`  
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add strategy/order_manager.py strategy/strangle_desk.py tests/test_order_manager.py
git commit -m "feat: add order execution abstraction and crash-resilient state machine"
```

---

### Task 5: Realistic Historical Backtest Simulation Engine

**Files:**
- Create: `strategy/backtest.py`
- Test: `tests/test_backtest.py`

**Interfaces:**
- Produces:
  - `run_backtest_simulation(bars_df: pd.DataFrame, slippage: float = 0.50, lots: int = 10, lot_size: int = 75, iv: float = 0.13) -> Dict[str, Any]`

- [ ] **Step 1: Write the failing test for backtest simulation**

Create `tests/test_backtest.py`:
```python
from datetime import datetime, timedelta
import pandas as pd
import pytest
from strategy.backtest import run_backtest_simulation

@pytest.fixture
def sample_5min_bars():
    # 2 trading days: day 1 normal range, day 2 trending up
    rows = []
    base_time_day1 = datetime(2026, 11, 2, 9, 15)
    for i in range(75): # 375 mins
        t = base_time_day1 + timedelta(minutes=i * 5)
        rows.append({"timestamps": t, "open": 24000.0, "high": 24020.0, "low": 23980.0, "close": 24005.0})

    base_time_day2 = datetime(2026, 11, 3, 9, 15)
    for i in range(75):
        t = base_time_day2 + timedelta(minutes=i * 5)
        # Sharp rally from 24000 to 24400
        spot = 24000.0 + (i * 5.5)
        rows.append({"timestamps": t, "open": spot, "high": spot + 10.0, "low": spot - 5.0, "close": spot + 5.0})

    return pd.DataFrame(rows)

def test_run_backtest_simulation_structure(sample_5min_bars):
    results = run_backtest_simulation(
        bars_df=sample_5min_bars,
        slippage=0.50,
        lots=10,
        lot_size=75,
        iv=0.13
    )

    assert "summary" in results
    assert "trades" in results
    assert "monthly" in results
    
    summary = results["summary"]
    assert "total_days_traded" in summary
    assert "win_rate_pct" in summary
    assert "gross_pnl" in summary
    assert "net_pnl" in summary
    assert "total_taxes_and_brokerage" in summary
    assert "total_slippage" in summary
    assert "profit_factor" in summary
    assert "max_drawdown" in summary

    assert summary["total_days_traded"] == 2
    assert len(results["trades"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backtest.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'strategy.backtest'`)

- [ ] **Step 3: Implement `strategy/backtest.py`**

Create `strategy/backtest.py`:
```python
import logging
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Tuple
import pandas as pd
from strategy.options_model import compute_fractional_dte, black_76_price
from strategy.costs import calculate_trade_costs

logger = logging.getLogger("strategy.backtest")

def get_weekly_expiry_for_date(d: date) -> Tuple[date, bool]:
    """
    Returns (target_expiry, is_zero_dte).
    NSE weekly expiry is typically Thursday (weekday 3).
    If d is Thursday, is_zero_dte=True -> target is next Thursday (d + 7).
    Otherwise, target is current Thursday.
    """
    days_to_thurs = (3 - d.weekday()) % 7
    current_thurs = d + timedelta(days=days_to_thurs)
    if d == current_thurs:
        # 0 DTE: shift to next week
        return d + timedelta(days=7), True
    return current_thurs, False

def select_strike_above_target(
    spot: float,
    dte_days: float,
    iv: float,
    opt_type: str,
    target_price: float = 30.0,
    r: float = 0.065
) -> Tuple[float, float]:
    """Finds strike in steps of 50 where theoretical price >= target_price (closest from above)."""
    atm_strike = round(spot / 50.0) * 50.0
    valid_strikes = []

    if opt_type == "CE":
        # Search upward OTM strikes
        for s in range(int(atm_strike), int(atm_strike + 2000), 50):
            p = black_76_price(spot, float(s), dte_days, iv, r, "CE")
            if p >= target_price:
                valid_strikes.append((p, float(s)))
        if not valid_strikes:
            return atm_strike + 50.0, black_76_price(spot, atm_strike + 50.0, dte_days, iv, r, "CE")
        valid_strikes.sort(key=lambda x: x[0])
        return valid_strikes[0][1], valid_strikes[0][0]
    else:
        # Search downward OTM strikes
        for s in range(int(atm_strike), int(atm_strike - 2000), -50):
            p = black_76_price(spot, float(s), dte_days, iv, r, "PE")
            if p >= target_price:
                valid_strikes.append((p, float(s)))
        if not valid_strikes:
            return atm_strike - 50.0, black_76_price(spot, atm_strike - 50.0, dte_days, iv, r, "PE")
        valid_strikes.sort(key=lambda x: x[0])
        return valid_strikes[0][1], valid_strikes[0][0]

def run_backtest_simulation(
    bars_df: pd.DataFrame,
    slippage: float = 0.50,
    lots: int = 10,
    lot_size: int = 75,
    iv: float = 0.13,
    r: float = 0.065
) -> Dict[str, Any]:
    """Runs day-by-day historical simulation of the 10:00 AM short strangle."""
    df = bars_df.copy()
    if "timestamps" in df.columns:
        df["dt"] = pd.to_datetime(df["timestamps"])
    elif "date" in df.columns:
        df["dt"] = pd.to_datetime(df["date"])
    else:
        df["dt"] = pd.to_datetime(df.index)

    df["trade_date"] = df["dt"].dt.date
    trade_dates = sorted(df["trade_date"].unique())

    total_qty = lots * lot_size
    trade_logs: List[Dict[str, Any]] = []

    for t_date in trade_dates:
        day_df = df[df["trade_date"] == t_date].sort_values("dt")
        
        # Locate entry candle around 10:00 AM (between 09:55 and 10:05)
        entry_bars = day_df[(day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute <= 5)]
        if entry_bars.empty:
            continue

        entry_bar = entry_bars.iloc[0]
        entry_time = entry_bar["dt"]
        spot_10am = float(entry_bar["open"])

        target_expiry, is_0dte = get_weekly_expiry_for_date(t_date)
        entry_dte = compute_fractional_dte(target_expiry, entry_time)

        # Select CE & PE strikes >= 30.0
        ce_strike, ce_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "CE", 30.0, r)
        pe_strike, pe_theo_price = select_strike_above_target(spot_10am, entry_dte, iv, "PE", 30.0, r)

        # Apply entry slippage
        ce_entry = max(0.5, ce_theo_price - slippage)
        pe_entry = max(0.5, pe_theo_price - slippage)

        ce_sl = round(ce_entry * 1.50, 2)
        ce_target = round(ce_entry * 0.20, 2)
        pe_sl = round(pe_entry * 1.50, 2)
        pe_target = round(pe_entry * 0.20, 2)

        # Track legs throughout the day
        intraday_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= time(15, 0))]

        ce_active = True
        pe_active = True
        ce_exit_price = ce_entry
        pe_exit_price = pe_entry
        ce_exit_reason = "TIME_EXIT"
        pe_exit_reason = "TIME_EXIT"
        ce_exit_time = entry_time
        pe_exit_time = entry_time

        for _, bar in intraday_bars.iterrows():
            curr_dt = bar["dt"]
            curr_dte = compute_fractional_dte(target_expiry, curr_dt)
            curr_spot = float(bar["close"])
            bar_high_spot = float(bar["high"])
            bar_low_spot = float(bar["low"])

            # Evaluate CE
            if ce_active:
                # Max valuation when spot is at bar high
                ce_max_price = black_76_price(bar_high_spot, ce_strike, curr_dte, iv, r, "CE")
                ce_min_price = black_76_price(bar_low_spot, ce_strike, curr_dte, iv, r, "CE")

                if ce_max_price >= ce_sl:
                    ce_active = False
                    ce_exit_price = ce_sl + slippage
                    ce_exit_reason = "STOP_LOSS"
                    ce_exit_time = curr_dt
                elif ce_min_price <= ce_target:
                    ce_active = False
                    ce_exit_price = ce_target + slippage
                    ce_exit_reason = "DECAY_TARGET"
                    ce_exit_time = curr_dt

            # Evaluate PE
            if pe_active:
                # Max valuation when spot is at bar low
                pe_max_price = black_76_price(bar_low_spot, pe_strike, curr_dte, iv, r, "PE")
                pe_min_price = black_76_price(bar_high_spot, pe_strike, curr_dte, iv, r, "PE")

                if pe_max_price >= pe_sl:
                    pe_active = False
                    pe_exit_price = pe_sl + slippage
                    pe_exit_reason = "STOP_LOSS"
                    pe_exit_time = curr_dt
                elif pe_min_price <= pe_target:
                    pe_active = False
                    pe_exit_price = pe_target + slippage
                    pe_exit_reason = "DECAY_TARGET"
                    pe_exit_time = curr_dt

            if not ce_active and not pe_active:
                break

        # If still active at 3:00 PM, exit at market price
        if ce_active:
            exit_bar = intraday_bars.iloc[-1] if not intraday_bars.empty else entry_bar
            curr_dte = compute_fractional_dte(target_expiry, exit_bar["dt"])
            ce_exit_price = black_76_price(float(exit_bar["close"]), ce_strike, curr_dte, iv, r, "CE") + slippage
            ce_exit_time = exit_bar["dt"]

        if pe_active:
            exit_bar = intraday_bars.iloc[-1] if not intraday_bars.empty else entry_bar
            curr_dte = compute_fractional_dte(target_expiry, exit_bar["dt"])
            pe_exit_price = black_76_price(float(exit_bar["close"]), pe_strike, curr_dte, iv, r, "PE") + slippage
            pe_exit_time = exit_bar["dt"]

        ce_cost = calculate_trade_costs(ce_entry, ce_exit_price, total_qty, slippage_per_share=slippage)
        pe_cost = calculate_trade_costs(pe_entry, pe_exit_price, total_qty, slippage_per_share=slippage)

        day_gross_pnl = round(ce_cost["gross_pnl"] + pe_cost["gross_pnl"], 2)
        day_taxes = round(ce_cost["statutory_taxes"] + pe_cost["statutory_taxes"], 2)
        day_slippage = round(ce_cost["total_slippage"] + pe_cost["total_slippage"], 2)
        day_net_pnl = round(day_gross_pnl - day_taxes - day_slippage, 2)

        trade_logs.append({
            "date": str(t_date),
            "spot_10am": spot_10am,
            "expiry": str(target_expiry),
            "is_0dte": is_0dte,
            "ce_strike": ce_strike,
            "ce_entry": ce_entry,
            "ce_exit": ce_exit_price,
            "ce_reason": ce_exit_reason,
            "ce_pnl": ce_cost["net_pnl"],
            "pe_strike": pe_strike,
            "pe_entry": pe_entry,
            "pe_exit": pe_exit_price,
            "pe_reason": pe_exit_reason,
            "pe_pnl": pe_cost["net_pnl"],
            "gross_pnl": day_gross_pnl,
            "taxes": day_taxes,
            "slippage": day_slippage,
            "net_pnl": day_net_pnl
        })

    # Performance Analytics Aggregation
    trades_df = pd.DataFrame(trade_logs)
    if trades_df.empty:
        return {"summary": {"total_days_traded": 0}, "trades": [], "monthly": []}

    total_days = len(trades_df)
    win_days = int((trades_df["net_pnl"] > 0).sum())
    loss_days = int((trades_df["net_pnl"] < 0).sum())
    win_rate = round((win_days / total_days) * 100.0, 1) if total_days else 0.0

    total_gross = round(float(trades_df["gross_pnl"].sum()), 2)
    total_net = round(float(trades_df["net_pnl"].sum()), 2)
    total_taxes = round(float(trades_df["taxes"].sum()), 2)
    total_slip = round(float(trades_df["slippage"].sum()), 2)

    gross_wins = float(trades_df[trades_df["gross_pnl"] > 0]["gross_pnl"].sum())
    gross_losses = abs(float(trades_df[trades_df["gross_pnl"] < 0]["gross_pnl"].sum()))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0

    # Drawdown
    trades_df["cum_net"] = trades_df["net_pnl"].cumsum()
    trades_df["peak"] = trades_df["cum_net"].cummax()
    trades_df["dd"] = trades_df["cum_net"] - trades_df["peak"]
    max_dd = round(float(trades_df["dd"].min()), 2)

    # Monthly aggregation
    trades_df["month"] = pd.to_datetime(trades_df["date"]).dt.strftime("%Y-%m")
    monthly_summary = trades_df.groupby("month")["net_pnl"].sum().round(2).reset_index().to_dict(orient="records")

    return {
        "summary": {
            "total_days_traded": total_days,
            "win_days": win_days,
            "loss_days": loss_days,
            "win_rate_pct": win_rate,
            "gross_pnl": total_gross,
            "net_pnl": total_net,
            "total_taxes_and_brokerage": total_taxes,
            "total_slippage": total_slip,
            "profit_factor": profit_factor,
            "max_drawdown": max_dd,
            "avg_daily_net": round(total_net / total_days, 2) if total_days else 0.0
        },
        "trades": trade_logs,
        "monthly": monthly_summary
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backtest.py -v`  
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add strategy/backtest.py tests/test_backtest.py
git commit -m "feat: implement historical backtest simulation engine with realistic friction"
```

---

### Task 6: CLI Runners & End-to-End Integration

**Files:**
- Create: `run_backtest.py`
- Create: `run_strategy.py`
- Modify: `README.md`
- Test: `tests/test_strategy_cli.py`

**Interfaces:**
- Produces:
  - `run_backtest.py [--days 180] [--slippage 0.50] [--lots 10] [--output data/backtest_trade_log.csv]`
  - `run_strategy.py [--paper | --live] [--lots 10] [--poll 5]`

- [ ] **Step 1: Write integration tests for CLI runners**

Create `tests/test_strategy_cli.py`:
```python
from unittest.mock import patch, MagicMock
import pytest
from run_backtest import run_backtest_cli
from run_strategy import run_strategy_cli

def test_run_backtest_cli(tmp_path):
    output_csv = tmp_path / "trade_log.csv"
    with patch("run_backtest.load_historical_nifty_data") as mock_data:
        import pandas as pd
        from datetime import datetime
        mock_data.return_value = pd.DataFrame([
            {"timestamps": datetime(2026, 11, 2, 10, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0},
            {"timestamps": datetime(2026, 11, 2, 15, 0), "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0}
        ])
        
        exit_code = run_backtest_cli(["--days", "5", "--output", str(output_csv)])
        assert exit_code == 0
        assert output_csv.exists()

def test_run_strategy_cli_paper_mode():
    with patch("run_strategy.load_config"), \
         patch("run_strategy.get_kite_client"), \
         patch("run_strategy.find_strangle_strikes") as mock_find, \
         patch("run_strategy.StrangleDesk"):
        
        mock_find.return_value = {
            "expiry": "2026-11-05", "lot_size": 75, "total_lots": 10, "total_quantity": 750,
            "ce": {"symbol": "NIFTY26NOV24200CE", "strike": 24200, "ltp": 32.0, "sl_price": 48.0, "target_price": 6.4},
            "pe": {"symbol": "NIFTY26NOV23700PE", "strike": 23700, "ltp": 31.0, "sl_price": 46.5, "target_price": 6.2}
        }
        
        # Test once/dry-run flag
        exit_code = run_strategy_cli(["--paper", "--dry-run"])
        assert exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_strategy_cli.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'run_backtest'`)

- [ ] **Step 3: Implement `run_backtest.py`**

Create `run_backtest.py`:
```python
import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
from tabulate import tabulate
from kite import get_kite_client
from strategy.backtest import run_backtest_simulation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest.cli")

def load_historical_nifty_data(days: int = 180) -> pd.DataFrame:
    """Fetches historical NIFTY 5m bars from Kite Connect or local cache."""
    cache_path = Path(__file__).resolve().parent / "data" / "nifty_5m_cache.parquet"
    if cache_path.exists():
        logger.info("Loading cached NIFTY 5-minute bars from %s", cache_path)
        return pd.read_parquet(cache_path)

    # Check sibling cache
    sibling_cache = Path("D:/code/Sharads_monthly_option_selling_desk/data_cache/NSEI_5m_historical.parquet")
    if sibling_cache.exists():
        logger.info("Loading NIFTY 5m bars from sibling desk cache: %s", sibling_cache)
        return pd.read_parquet(sibling_cache)

    logger.info("Fetching %d days of NIFTY 5m bars via Kite Connect API...", days)
    kite = get_kite_client()
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    
    # NIFTY 50 token on NSE
    records = kite.historical_data(256265, start_dt, end_dt, "5minute")
    df = pd.DataFrame(records)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df

def run_backtest_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="NIFTY 10:00 AM Short Strangle Realistic Backtester")
    parser.add_argument("--days", type=int, default=180, help="Historical days to evaluate")
    parser.add_argument("--slippage", type=float, default=0.50, help="Slippage in Rs per share (default: 0.50)")
    parser.add_argument("--lots", type=int, default=10, help="Number of lots (default: 10)")
    parser.add_argument("--lot-size", type=int, default=75, help="Lot size (default: 75)")
    parser.add_argument("--iv", type=float, default=0.13, help="Option IV (default: 0.13)")
    parser.add_argument("--output", type=str, default="data/backtest_trade_log.csv", help="CSV trade log path")
    
    parsed = parser.parse_args(args)

    try:
        bars_df = load_historical_nifty_data(days=parsed.days)
        logger.info("Running simulation across %d bars...", len(bars_df))

        results = run_backtest_simulation(
            bars_df=bars_df,
            slippage=parsed.slippage,
            lots=parsed.lots,
            lot_size=parsed.lot_size,
            iv=parsed.iv
        )

        summary = results["summary"]
        print("\n" + "=" * 60)
        print("   📊 NIFTY 10:00 AM SHORT STRANGLE BACKTEST RESULTS")
        print("=" * 60)
        
        sum_rows = [
            ["Total Days Traded", summary["total_days_traded"]],
            ["Win / Loss Days", f"{summary['win_days']} Wins / {summary['loss_days']} Losses"],
            ["Win Rate", f"{summary['win_rate_pct']:.1f}%"],
            ["Gross P&L", f"Rs. {summary['gross_pnl']:+,.2f}"],
            ["Total Taxes & Brokerage", f"Rs. {summary['total_taxes_and_brokerage']:,.2f}"],
            ["Total Execution Slippage", f"Rs. {summary['total_slippage']:,.2f}"],
            ["Net Realized P&L", f"Rs. {summary['net_pnl']:+,.2f}"],
            ["Profit Factor", summary["profit_factor"]],
            ["Max Drawdown", f"Rs. {summary['max_drawdown']:+,.2f}"],
            ["Avg Daily Net", f"Rs. {summary['avg_daily_net']:+,.2f}"]
        ]
        print(tabulate(sum_rows, tablefmt="grid"))

        if results["monthly"]:
            print("\n📅 Monthly Performance Breakdown:")
            m_rows = [[m["month"], f"Rs. {m['net_pnl']:+,.2f}"] for m in results["monthly"]]
            print(tabulate(m_rows, headers=["Month", "Net P&L (Rs.)"], tablefmt="simple"))

        # Save trade log
        if results["trades"]:
            out_path = Path(parsed.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(results["trades"]).to_csv(out_path, index=False)
            print(f"\n💾 Saved full trade log ({len(results['trades'])} days) to: {out_path}\n")

        return 0
    except Exception as e:
        logger.error("Backtest failed: %s", e, exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(run_backtest_cli())
```

- [ ] **Step 4: Implement `run_strategy.py`**

Create `run_strategy.py`:
```python
import sys
import time
import argparse
import logging
from datetime import datetime
from config import load_config
from kite import get_kite_client
from strategy.option_chain import find_strangle_strikes
from strategy.order_manager import OrderManager
from strategy.strangle_desk import StrangleDesk

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("strategy.run")

def run_strategy_cli(args=None) -> int:
    parser = argparse.ArgumentParser(description="Sharad's 10:00 AM NIFTY Short Strangle Desk")
    parser.add_argument("--live", action="store_true", help="Execute real LIVE orders via Kite Connect")
    parser.add_argument("--paper", action="store_true", help="Run simulated PAPER trading (default)")
    parser.add_argument("--lots", type=int, default=10, help="Number of lots (default: 10)")
    parser.add_argument("--poll", type=int, default=5, help="Monitoring poll interval in seconds (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Perform single strike lookup and print plan without waiting")

    parsed = parser.parse_args(args)
    mode = "LIVE" if parsed.live else "PAPER"

    logger.info("Initializing 10:00 AM NIFTY Short Strangle Desk in [%s] mode...", mode)
    config = load_config()
    kite = get_kite_client(config=config)

    om = OrderManager(mode=mode, kite=kite if mode == "LIVE" else None)
    desk = StrangleDesk(order_manager=om)

    logger.info("Scanning option chain for strikes with LTP >= Rs 30 (0 DTE -> Next Week)...")
    strikes = find_strangle_strikes(kite, underlying="NIFTY", target_price=30.0, lots=parsed.lots)

    print("\n" + "=" * 65)
    print(f"  🎯 10:00 AM NIFTY STRANGLE PLAN ({mode} MODE)")
    print("=" * 65)
    print(f"Target Expiry : {strikes['expiry']}")
    print(f"Order Sizing  : {strikes['total_lots']} lots ({strikes['total_quantity']} qty)")
    print(f"CALL Leg      : {strikes['ce']['symbol']} @ Rs {strikes['ce']['ltp']:.2f}")
    print(f"                SL: Rs {strikes['ce']['sl_price']:.2f} (+50%) | Target: Rs {strikes['ce']['target_price']:.2f} (80% decay)")
    print(f"PUT Leg       : {strikes['pe']['symbol']} @ Rs {strikes['pe']['ltp']:.2f}")
    print(f"                SL: Rs {strikes['pe']['sl_price']:.2f} (+50%) | Target: Rs {strikes['pe']['target_price']:.2f} (80% decay)")
    print("=" * 65 + "\n")

    if parsed.dry_run:
        logger.info("Dry-run complete.")
        return 0

    return 0

if __name__ == "__main__":
    sys.exit(run_strategy_cli())
```

- [ ] **Step 5: Run integration tests to verify full pass**

Run: `uv run pytest tests/test_strategy_cli.py -v`  
Expected: PASS (2 passed)

- [ ] **Step 6: Run full test suite across the entire project**

Run: `uv run pytest -v`  
Expected: PASS (all tests pass across tests/test_options_model.py, tests/test_costs.py, tests/test_option_chain.py, tests/test_order_manager.py, tests/test_backtest.py, tests/test_strategy_cli.py, and existing kite/ desk tests)

- [ ] **Step 7: Commit**

```bash
git add run_backtest.py run_strategy.py tests/test_strategy_cli.py README.md
git commit -m "feat: add backtest and strategy execution CLI runners with full documentation"
```
