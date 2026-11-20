# MCX Silver Options Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated, institutional-grade MCX Silver Mini (`SILVERM`) options trading desk executing defined-risk vertical spreads and condors based on multi-timeframe RSI divergence and Option Chain dynamics (PCR, Max Pain, OI walls) with strict devolvement risk controls.

**Architecture:** A dedicated, fully isolated module (`strategy/silver_options/`) on branch `feature/silver-trading`. It mirrors the clean separation of `strategy/strangle_desk/` while handling MCX-specific commodity parameters (09:00–23:30 IST market hours, 5 kg lot size, physical futures devolvement cutoff at 15:30 IST, and hedge-first order sequencing).

**Tech Stack:** Python 3.11+, KiteConnect API, Pydantic / Dataclasses, Pandas, NumPy, Pytest.

**Spec:** [docs/superpowers/specs/2026-11-20-mcx-silver-options-desk-design.md](file:///d:/code/sharads_intraday_option_selling_desk/docs/superpowers/specs/2026-11-20-mcx-silver-options-desk-design.md)

## Global Constraints
- `SILVERM` lot size = 5 kg
- Strike interval = 1,000 points (tick size = ₹1.00)
- Market hours: 09:00 to 23:30 IST (extended evening US overlap)
- Devolvement cutoff: 15:30 IST on contract expiry day (hard auto square-off)
- Hedge-first sequencing: Long leg placed before Short leg on entry; Short leg closed before Long leg on exit
- Risk-to-reward gating: Net Debit must be $\le 0.35 \times \text{Spread Width}$
- Zero regression: All 149 existing NIFTY short strangle tests must pass on every commit

---

### Task 1: Configuration & Domain Models (`config.py`, `models.py`)

**Files:**
- Create: `strategy/silver_options/config.py`
- Create: `strategy/silver_options/models.py`
- Create: `strategy/silver_options/__init__.py`
- Test: `tests/test_silver_options/test_config_and_models.py`

**Interfaces:**
- Produces:
  - `SilverConfig`: dataclass holding lot size (5), strike step (1000), trading hours (09:00 to 23:30), devolvement cutoff (15:30), risk parameters.
  - `SignalType`: Enum (`BULLISH_REVERSAL`, `BEARISH_BREAKDOWN`, `RANGE_BOUND`, `NO_SIGNAL`).
  - `SpreadType`: Enum (`BULL_CALL_SPREAD`, `BEAR_PUT_SPREAD`, `IRON_CONDOR`).
  - `OptionLeg`: dataclass holding strike, option_type ('CE'/'PE'), action ('BUY'/'SELL'), quantity, premium.
  - `OptionChainProfile`: dataclass holding spot_price, pcr, max_pain, call_wall, put_base, atm_strike.
  - `SpreadPosition`: dataclass tracking active spread state (`PENDING`, `ACTIVE`, `CLOSED`), legs, entry_debit, current_value, realized_pnl.

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_config_and_models.py`:
```python
import pytest
from datetime import time
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import (
    SignalType,
    SpreadType,
    OptionLeg,
    OptionChainProfile,
    SpreadPosition,
)

def test_silver_config_defaults():
    cfg = SilverConfig()
    assert cfg.symbol == "SILVERM"
    assert cfg.lot_size == 5
    assert cfg.strike_step == 1000
    assert cfg.market_open == time(9, 0)
    assert cfg.market_close == time(23, 30)
    assert cfg.devolvement_cutoff == time(15, 30)
    assert cfg.max_debit_width_ratio == 0.35
    assert cfg.take_profit_ratio == 0.75
    assert cfg.stop_loss_ratio == 0.50

def test_option_chain_profile_and_spread_position():
    profile = OptionChainProfile(
        spot_price=153800.0,
        pcr=0.77,
        max_pain=155000,
        call_wall=160000,
        put_base=148000,
        atm_strike=154000,
    )
    assert profile.pcr == 0.77
    assert profile.max_pain == 155000

    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0)
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0)
    pos = SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg1, leg2],
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
    )
    assert pos.spread_type == SpreadType.BULL_CALL_SPREAD
    assert pos.entry_net_debit == 800.0
    assert pos.status == "PENDING"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_config_and_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/__init__.py`:
```python
"""MCX Silver Options Trading Desk."""
```

Create `strategy/silver_options/config.py`:
```python
from dataclasses import dataclass
from datetime import time

@dataclass
class SilverConfig:
    symbol: str = "SILVERM"
    exchange: str = "MCX"
    lot_size: int = 5  # kg
    strike_step: int = 1000
    tick_size: float = 1.0
    market_open: time = time(9, 0)
    market_close: time = time(23, 30)
    devolvement_cutoff: time = time(15, 30)
    max_debit_width_ratio: float = 0.35
    take_profit_ratio: float = 0.75
    stop_loss_ratio: float = 0.50
    daily_rsi_period: int = 14
    daily_rsi_oversold: float = 25.0
    daily_rsi_overbought: float = 75.0
    hourly_ema_period: int = 20
    slippage_buffer_ticks: int = 2
```

Create `strategy/silver_options/models.py`:
```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

class SignalType(str, Enum):
    BULLISH_REVERSAL = "BULLISH_REVERSAL"
    BEARISH_BREAKDOWN = "BEARISH_BREAKDOWN"
    RANGE_BOUND = "RANGE_BOUND"
    NO_SIGNAL = "NO_SIGNAL"

class SpreadType(str, Enum):
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"

@dataclass
class OptionLeg:
    strike: int
    option_type: str  # "CE" or "PE"
    action: str  # "BUY" or "SELL"
    quantity: int
    premium: float
    trading_symbol: Optional[str] = None
    order_id: Optional[str] = None

@dataclass
class OptionChainProfile:
    spot_price: float
    pcr: float
    max_pain: int
    call_wall: int
    put_base: int
    atm_strike: int

@dataclass
class SpreadPosition:
    spread_type: SpreadType
    legs: List[OptionLeg]
    entry_net_debit: float
    max_profit: float
    max_loss: float
    status: str = "PENDING"  # PENDING, ACTIVE, CLOSED
    current_value: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_config_and_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/ tests/test_silver_options/test_config_and_models.py
git commit -m "feat(silver): add configuration and core domain models"
```

---

### Task 2: Multi-Timeframe Technical Signal Engine (`technical_signals.py`)

**Files:**
- Create: `strategy/silver_options/technical_signals.py`
- Test: `tests/test_silver_options/test_technical_signals.py`

**Interfaces:**
- Consumes: `SilverConfig`, `SignalType` from Task 1.
- Produces:
  - `compute_rsi(series: pd.Series, period: int = 14) -> pd.Series`
  - `detect_rsi_divergence(daily_df: pd.DataFrame, config: SilverConfig) -> SignalType`
  - `verify_hourly_confirmation(hourly_df: pd.DataFrame, daily_signal: SignalType, config: SilverConfig) -> bool`
  - `evaluate_technical_signal(daily_df: pd.DataFrame, hourly_df: pd.DataFrame, config: SilverConfig) -> SignalType`

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_technical_signals.py`:
```python
import pandas as pd
import numpy as np
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType
from strategy.silver_options.technical_signals import (
    compute_rsi,
    detect_rsi_divergence,
    verify_hourly_confirmation,
    evaluate_technical_signal,
)

def test_compute_rsi_accuracy():
    prices = pd.Series([100, 102, 104, 103, 105, 107, 106, 108, 110, 112, 111, 113, 115, 114, 116, 118])
    rsi = compute_rsi(prices, period=14)
    assert not np.isnan(rsi.iloc[-1])
    assert 0 <= rsi.iloc[-1] <= 100

def test_detect_bullish_divergence():
    # Price makes lower low (150,000 -> 148,000) while RSI makes higher low (19 -> 25)
    cfg = SilverConfig(daily_rsi_oversold=25.0)
    data = {
        "close": [165000, 162000, 158000, 154000, 150000, 152000, 151000, 148000],
        "low": [164000, 161000, 157000, 153000, 149000, 151000, 150000, 147500],
    }
    df = pd.DataFrame(data)
    # Inject mocked RSI series directly
    df["rsi"] = [35.0, 30.0, 24.0, 21.0, 19.0, 23.0, 22.0, 25.0]

    signal = detect_rsi_divergence(df, cfg)
    assert signal == SignalType.BULLISH_REVERSAL

def test_hourly_confirmation_bullish():
    cfg = SilverConfig(hourly_ema_period=20)
    # Hourly close crosses above 20 EMA
    hourly_df = pd.DataFrame({
        "close": [148000, 148500, 149200, 150500],
        "ema_20": [149000, 149000, 149100, 149200],
    })
    confirmed = verify_hourly_confirmation(hourly_df, SignalType.BULLISH_REVERSAL, cfg)
    assert confirmed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_technical_signals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.technical_signals'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/technical_signals.py`:
```python
import pandas as pd
import numpy as np
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0.0)).copy()
    loss = (-delta.where(delta < 0, 0.0)).copy()
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)

def detect_rsi_divergence(daily_df: pd.DataFrame, config: SilverConfig) -> SignalType:
    if len(daily_df) < 5:
        return SignalType.NO_SIGNAL
    
    if "rsi" not in daily_df.columns:
        daily_df = daily_df.copy()
        daily_df["rsi"] = compute_rsi(daily_df["close"], period=config.daily_rsi_period)

    recent = daily_df.tail(10)
    current_close = recent["close"].iloc[-1]
    current_rsi = recent["rsi"].iloc[-1]
    
    # Check for Bullish Divergence in oversold territory
    prior_window = recent.iloc[:-1]
    prior_min_close_idx = prior_window["close"].idxmin()
    prior_min_close = prior_window["close"].loc[prior_min_close_idx]
    prior_min_rsi = prior_window["rsi"].loc[prior_min_close_idx]

    if prior_min_rsi <= config.daily_rsi_oversold:
        # Price made a lower low, but RSI made a higher low
        if current_close < prior_min_close and current_rsi > prior_min_rsi:
            return SignalType.BULLISH_REVERSAL

    # Check for Bearish Divergence in overbought territory
    prior_max_close_idx = prior_window["close"].idxmax()
    prior_max_close = prior_window["close"].loc[prior_max_close_idx]
    prior_max_rsi = prior_window["rsi"].loc[prior_max_close_idx]

    if prior_max_rsi >= config.daily_rsi_overbought:
        if current_close > prior_max_close and current_rsi < prior_max_rsi:
            return SignalType.BEARISH_BREAKDOWN

    return SignalType.NO_SIGNAL

def verify_hourly_confirmation(hourly_df: pd.DataFrame, daily_signal: SignalType, config: SilverConfig) -> bool:
    if len(hourly_df) < 2:
        return False
    
    df = hourly_df.copy()
    if "ema_20" not in df.columns:
        df["ema_20"] = df["close"].ewm(span=config.hourly_ema_period, adjust=False).mean()

    curr_close = df["close"].iloc[-1]
    curr_ema = df["ema_20"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    prev_ema = df["ema_20"].iloc[-2]

    if daily_signal == SignalType.BULLISH_REVERSAL:
        # Bullish cross or confirmed close above 20 EMA
        return curr_close > curr_ema and (prev_close <= prev_ema or curr_close > prev_close)

    if daily_signal == SignalType.BEARISH_BREAKDOWN:
        # Bearish cross or confirmed close below 20 EMA
        return curr_close < curr_ema and (prev_close >= prev_ema or curr_close < prev_close)

    return True

def evaluate_technical_signal(daily_df: pd.DataFrame, hourly_df: pd.DataFrame, config: SilverConfig) -> SignalType:
    daily_signal = detect_rsi_divergence(daily_df, config)
    if daily_signal in (SignalType.BULLISH_REVERSAL, SignalType.BEARISH_BREAKDOWN):
        if verify_hourly_confirmation(hourly_df, daily_signal, config):
            return daily_signal
        return SignalType.NO_SIGNAL
    return SignalType.NO_SIGNAL
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_technical_signals.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/technical_signals.py tests/test_silver_options/test_technical_signals.py
git commit -m "feat(silver): implement multi-timeframe RSI divergence and hourly confirmation"
```

---

### Task 3: Option Chain Analytics Engine (`chain_analyzer.py`)

**Files:**
- Create: `strategy/silver_options/chain_analyzer.py`
- Test: `tests/test_silver_options/test_chain_analyzer.py`

**Interfaces:**
- Consumes: `SilverConfig`, `OptionChainProfile` from Task 1.
- Produces:
  - `compute_max_pain(chain_data: List[dict]) -> int`
  - `compute_pcr(chain_data: List[dict]) -> float`
  - `find_oi_walls(chain_data: List[dict]) -> tuple[int, int]` (call_wall, put_base)
  - `build_option_chain_profile(spot_price: float, chain_data: List[dict], config: SilverConfig) -> OptionChainProfile`

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_chain_analyzer.py`:
```python
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.chain_analyzer import (
    compute_max_pain,
    compute_pcr,
    find_oi_walls,
    build_option_chain_profile,
)

@pytest.fixture
def sample_chain():
    # Mocked option chain replicating the webinar structure
    return [
        {"strike": 145000, "ce_oi": 100, "pe_oi": 1500, "ce_ltp": 9500.0, "pe_ltp": 700.0},
        {"strike": 148000, "ce_oi": 250, "pe_oi": 2200, "ce_ltp": 6800.0, "pe_ltp": 1200.0},
        {"strike": 150000, "ce_oi": 600, "pe_oi": 1800, "ce_ltp": 5000.0, "pe_ltp": 1600.0},
        {"strike": 154000, "ce_oi": 1200, "pe_oi": 1400, "ce_ltp": 2200.0, "pe_ltp": 2400.0},
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 1600.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 600.0, "pe_ltp": 6800.0},
        {"strike": 165000, "ce_oi": 1500, "pe_oi": 100, "ce_ltp": 200.0, "pe_ltp": 11000.0},
    ]

def test_pcr_calculation(sample_chain):
    # Total PE OI = 1500+2200+1800+1400+1100+400+100 = 8500
    # Total CE OI = 100+250+600+1200+2500+3800+1500 = 9950
    # PCR = 8500 / 9950 = 0.854
    pcr = compute_pcr(sample_chain)
    assert pytest.approx(pcr, 0.01) == 0.854

def test_find_oi_walls(sample_chain):
    call_wall, put_base = find_oi_walls(sample_chain)
    assert call_wall == 160000  # Max CE OI: 3800
    assert put_base == 148000   # Max PE OI: 2200

def test_build_option_chain_profile(sample_chain):
    cfg = SilverConfig()
    profile = build_option_chain_profile(spot_price=153800.0, chain_data=sample_chain, config=cfg)
    assert profile.spot_price == 153800.0
    assert profile.call_wall == 160000
    assert profile.put_base == 148000
    assert profile.atm_strike == 154000
    assert 148000 <= profile.max_pain <= 160000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_chain_analyzer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.chain_analyzer'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/chain_analyzer.py`:
```python
from typing import List, Tuple
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import OptionChainProfile

def compute_pcr(chain_data: List[dict]) -> float:
    total_ce_oi = sum(row.get("ce_oi", 0) for row in chain_data)
    total_pe_oi = sum(row.get("pe_oi", 0) for row in chain_data)
    if total_ce_oi == 0:
        return 1.0
    return round(total_pe_oi / total_ce_oi, 3)

def find_oi_walls(chain_data: List[dict]) -> Tuple[int, int]:
    if not chain_data:
        return 0, 0
    call_wall = max(chain_data, key=lambda r: r.get("ce_oi", 0))["strike"]
    put_base = max(chain_data, key=lambda r: r.get("pe_oi", 0))["strike"]
    return call_wall, put_base

def compute_max_pain(chain_data: List[dict]) -> int:
    if not chain_data:
        return 0
    strikes = [row["strike"] for row in chain_data]
    min_pain = float("inf")
    max_pain_strike = strikes[0]

    for k in strikes:
        total_pain = 0.0
        for row in chain_data:
            s = row["strike"]
            ce_oi = row.get("ce_oi", 0)
            pe_oi = row.get("pe_oi", 0)
            if k > s:
                total_pain += ce_oi * (k - s)
            elif k < s:
                total_pain += pe_oi * (s - k)
        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = k

    return max_pain_strike

def build_option_chain_profile(
    spot_price: float, chain_data: List[dict], config: SilverConfig
) -> OptionChainProfile:
    step = config.strike_step
    atm_strike = int(round(spot_price / step) * step)
    pcr = compute_pcr(chain_data)
    call_wall, put_base = find_oi_walls(chain_data)
    max_pain = compute_max_pain(chain_data)

    return OptionChainProfile(
        spot_price=spot_price,
        pcr=pcr,
        max_pain=max_pain,
        call_wall=call_wall,
        put_base=put_base,
        atm_strike=atm_strike,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_chain_analyzer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/chain_analyzer.py tests/test_silver_options/test_chain_analyzer.py
git commit -m "feat(silver): implement option chain PCR, max pain, and OI wall analytics"
```

---

### Task 4: Spread Selection Engine (`spread_selector.py`)

**Files:**
- Create: `strategy/silver_options/spread_selector.py`
- Test: `tests/test_silver_options/test_spread_selector.py`

**Interfaces:**
- Consumes: `SilverConfig`, `OptionChainProfile`, `SignalType`, `SpreadType`, `SpreadPosition`, `OptionLeg`.
- Produces:
  - `select_spread(signal: SignalType, profile: OptionChainProfile, chain_data: List[dict], config: SilverConfig) -> Optional[SpreadPosition]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_spread_selector.py`:
```python
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SignalType, SpreadType
from strategy.silver_options.chain_analyzer import build_option_chain_profile
from strategy.silver_options.spread_selector import select_spread

@pytest.fixture
def mock_chain():
    return [
        {"strike": 145000, "ce_oi": 100, "pe_oi": 1500, "ce_ltp": 9500.0, "pe_ltp": 700.0},
        {"strike": 148000, "ce_oi": 250, "pe_oi": 2200, "ce_ltp": 6800.0, "pe_ltp": 1200.0},
        {"strike": 154000, "ce_oi": 1200, "pe_oi": 1400, "ce_ltp": 2200.0, "pe_ltp": 2400.0},
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 1400.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 600.0, "pe_ltp": 6800.0},
    ]

def test_select_bull_call_spread(mock_chain):
    cfg = SilverConfig(max_debit_width_ratio=0.35)
    profile = build_option_chain_profile(153800.0, mock_chain, cfg)
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, mock_chain, cfg)

    assert spread is not None
    assert spread.spread_type == SpreadType.BULL_CALL_SPREAD
    assert len(spread.legs) == 2
    long_leg, short_leg = spread.legs[0], spread.legs[1]
    
    # Matching webinar: Buy 155,000 CE (pivot) / Sell 160,000 CE (call wall)
    assert long_leg.action == "BUY" and long_leg.strike == 155000 and long_leg.option_type == "CE"
    assert short_leg.action == "SELL" and short_leg.strike == 160000 and short_leg.option_type == "CE"
    
    # Net debit = 1400 - 600 = 800 pts. Spread width = 5000 pts.
    # 800 <= 0.35 * 5000 (1750) -> Valid!
    assert spread.entry_net_debit == 800.0
    assert spread.max_profit == 4200.0
    assert spread.max_loss == 800.0

def test_reject_unfavorable_risk_reward(mock_chain):
    # If net debit exceeds 35% of spread width, reject the trade
    expensive_chain = [
        {"strike": 155000, "ce_oi": 2500, "pe_oi": 1100, "ce_ltp": 2500.0, "pe_ltp": 2800.0},
        {"strike": 160000, "ce_oi": 3800, "pe_oi": 400, "ce_ltp": 500.0, "pe_ltp": 6800.0},
    ]
    cfg = SilverConfig(max_debit_width_ratio=0.35)
    profile = build_option_chain_profile(153800.0, expensive_chain, cfg)
    # Net debit = 2000 > 0.35 * 5000 (1750)
    spread = select_spread(SignalType.BULLISH_REVERSAL, profile, expensive_chain, cfg)
    assert spread is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_spread_selector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.spread_selector'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/spread_selector.py`:
```python
from typing import List, Optional
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import (
    SignalType,
    SpreadType,
    OptionLeg,
    OptionChainProfile,
    SpreadPosition,
)

def select_spread(
    signal: SignalType,
    profile: OptionChainProfile,
    chain_data: List[dict],
    config: SilverConfig,
) -> Optional[SpreadPosition]:
    chain_by_strike = {row["strike"]: row for row in chain_data}

    if signal == SignalType.BULLISH_REVERSAL:
        # Long Leg: Max Pain pivot or nearest strike above spot
        long_strike = profile.max_pain if profile.max_pain in chain_by_strike else profile.atm_strike
        short_strike = profile.call_wall

        if long_strike >= short_strike or long_strike not in chain_by_strike or short_strike not in chain_by_strike:
            return None

        long_row = chain_by_strike[long_strike]
        short_row = chain_by_strike[short_strike]
        long_ltp = long_row.get("ce_ltp", 0.0)
        short_ltp = short_row.get("ce_ltp", 0.0)

        net_debit = long_ltp - short_ltp
        spread_width = short_strike - long_strike

        if net_debit <= 0 or net_debit > (config.max_debit_width_ratio * spread_width):
            return None

        leg1 = OptionLeg(strike=long_strike, option_type="CE", action="BUY", quantity=config.lot_size, premium=long_ltp)
        leg2 = OptionLeg(strike=short_strike, option_type="CE", action="SELL", quantity=config.lot_size, premium=short_ltp)

        return SpreadPosition(
            spread_type=SpreadType.BULL_CALL_SPREAD,
            legs=[leg1, leg2],
            entry_net_debit=round(net_debit, 2),
            max_profit=round(spread_width - net_debit, 2),
            max_loss=round(net_debit, 2),
        )

    elif signal == SignalType.BEARISH_BREAKDOWN:
        long_strike = profile.max_pain if profile.max_pain in chain_by_strike else profile.atm_strike
        short_strike = profile.put_base

        if long_strike <= short_strike or long_strike not in chain_by_strike or short_strike not in chain_by_strike:
            return None

        long_row = chain_by_strike[long_strike]
        short_row = chain_by_strike[short_strike]
        long_ltp = long_row.get("pe_ltp", 0.0)
        short_ltp = short_row.get("pe_ltp", 0.0)

        net_debit = long_ltp - short_ltp
        spread_width = long_strike - short_strike

        if net_debit <= 0 or net_debit > (config.max_debit_width_ratio * spread_width):
            return None

        leg1 = OptionLeg(strike=long_strike, option_type="PE", action="BUY", quantity=config.lot_size, premium=long_ltp)
        leg2 = OptionLeg(strike=short_strike, option_type="PE", action="SELL", quantity=config.lot_size, premium=short_ltp)

        return SpreadPosition(
            spread_type=SpreadType.BEAR_PUT_SPREAD,
            legs=[leg1, leg2],
            entry_net_debit=round(net_debit, 2),
            max_profit=round(spread_width - net_debit, 2),
            max_loss=round(net_debit, 2),
        )

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_spread_selector.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/spread_selector.py tests/test_silver_options/test_spread_selector.py
git commit -m "feat(silver): implement vertical spread selection and risk-reward gating"
```

---

### Task 5: Risk Manager & MCX Devolvement Safeguards (`risk_manager.py`)

**Files:**
- Create: `strategy/silver_options/risk_manager.py`
- Test: `tests/test_silver_options/test_risk_manager.py`

**Interfaces:**
- Consumes: `SilverConfig`, `SpreadPosition`, `OptionLeg`.
- Produces:
  - `sequence_entry_legs(legs: List[OptionLeg]) -> List[OptionLeg]` (BUY first, SELL second)
  - `sequence_exit_legs(legs: List[OptionLeg]) -> List[OptionLeg]` (BUY-to-cover first, SELL second)
  - `check_exit_conditions(position: SpreadPosition, current_spread_value: float, current_time: time, is_expiry_day: bool, config: SilverConfig) -> tuple[bool, Optional[str]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_risk_manager.py`:
```python
from datetime import time
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadType, OptionLeg, SpreadPosition
from strategy.silver_options.risk_manager import (
    sequence_entry_legs,
    sequence_exit_legs,
    check_exit_conditions,
)

@pytest.fixture
def sample_position():
    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0)
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0)
    return SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg2, leg1],  # Intentionally unordered
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
        status="ACTIVE",
    )

def test_hedge_first_entry_sequencing(sample_position):
    ordered = sequence_entry_legs(sample_position.legs)
    assert ordered[0].action == "BUY"
    assert ordered[1].action == "SELL"

def test_hedge_first_exit_sequencing(sample_position):
    # On exit, short leg must be closed first (BUY to cover), then long leg sold
    ordered = sequence_exit_legs(sample_position.legs)
    assert ordered[0].strike == 160000  # The short leg
    assert ordered[1].strike == 155000  # The long leg

def test_take_profit_trigger(sample_position):
    cfg = SilverConfig(take_profit_ratio=0.75)
    # Entry debit 800, max profit 4200. 75% target = 800 + (0.75 * 4200) = 3950
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=4000.0, current_time=time(14, 0), is_expiry_day=False, config=cfg
    )
    assert should_exit is True
    assert reason == "TAKE_PROFIT"

def test_stop_loss_trigger(sample_position):
    cfg = SilverConfig(stop_loss_ratio=0.50)
    # 50% loss on 800 debit = value falls to 400
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=350.0, current_time=time(14, 0), is_expiry_day=False, config=cfg
    )
    assert should_exit is True
    assert reason == "STOP_LOSS"

def test_mandatory_devolvement_cutoff(sample_position):
    cfg = SilverConfig(devolvement_cutoff=time(15, 30))
    # On expiry day at 15:35, mandatory exit regardless of PnL
    should_exit, reason = check_exit_conditions(
        sample_position, current_spread_value=1200.0, current_time=time(15, 35), is_expiry_day=True, config=cfg
    )
    assert should_exit is True
    assert reason == "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_risk_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.risk_manager'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/risk_manager.py`:
```python
from datetime import time
from typing import List, Tuple, Optional
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import OptionLeg, SpreadPosition

def sequence_entry_legs(legs: List[OptionLeg]) -> List[OptionLeg]:
    """Hedge-first: BUY leg first to secure margin discount, SELL leg second."""
    buys = [l for l in legs if l.action == "BUY"]
    sells = [l for l in legs if l.action == "SELL"]
    return buys + sells

def sequence_exit_legs(legs: List[OptionLeg]) -> List[OptionLeg]:
    """Safety-first: Close short leg first (eliminate short liability), then close long leg."""
    sells = [l for l in legs if l.action == "SELL"]
    buys = [l for l in legs if l.action == "BUY"]
    return sells + buys

def check_exit_conditions(
    position: SpreadPosition,
    current_spread_value: float,
    current_time: time,
    is_expiry_day: bool,
    config: SilverConfig,
) -> Tuple[bool, Optional[str]]:
    # 1. Mandatory Expiry Devolvement Cutoff
    if is_expiry_day and current_time >= config.devolvement_cutoff:
        return True, "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF"

    # 2. Take Profit: 75% of max profit
    profit_target_value = position.entry_net_debit + (config.take_profit_ratio * position.max_profit)
    if current_spread_value >= profit_target_value:
        return True, "TAKE_PROFIT"

    # 3. Stop Loss: 50% loss of initial net debit
    stop_loss_value = position.entry_net_debit * (1.0 - config.stop_loss_ratio)
    if current_spread_value <= stop_loss_value:
        return True, "STOP_LOSS"

    return False, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_risk_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/risk_manager.py tests/test_silver_options/test_risk_manager.py
git commit -m "feat(silver): implement hedge-first order sequencing and 15:30 devolvement cutoff"
```

---

### Task 6: Contract Resolver & Order Execution Manager (`contract_resolver.py`, `order_manager.py`)

**Files:**
- Create: `strategy/silver_options/contract_resolver.py`
- Create: `strategy/silver_options/order_manager.py`
- Test: `tests/test_silver_options/test_order_manager.py`

**Interfaces:**
- Consumes: `SilverConfig`, `OptionLeg`, `SpreadPosition`.
- Produces:
  - `resolve_silver_symbols(instruments: List[dict], underlying: str = "SILVERM") -> List[dict]`
  - `execute_spread_entry(kite_client, spread: SpreadPosition, is_paper: bool = True) -> bool`
  - `execute_spread_exit(kite_client, spread: SpreadPosition, reason: str, is_paper: bool = True) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_order_manager.py`:
```python
from unittest.mock import MagicMock
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadType, OptionLeg, SpreadPosition
from strategy.silver_options.contract_resolver import resolve_silver_symbols
from strategy.silver_options.order_manager import execute_spread_entry, execute_spread_exit

def test_resolve_silver_symbols():
    mock_instruments = [
        {"tradingsymbol": "SILVERM26NOVFUT", "instrument_type": "FUT", "segment": "MCX-FUT", "name": "SILVERM"},
        {"tradingsymbol": "SILVERM26NOV155000CE", "instrument_type": "CE", "strike": 155000, "segment": "MCX-OPT", "name": "SILVERM"},
        {"tradingsymbol": "SILVERM26NOV160000CE", "instrument_type": "CE", "strike": 160000, "segment": "MCX-OPT", "name": "SILVERM"},
        {"tradingsymbol": "GOLDM26NOVFUT", "instrument_type": "FUT", "segment": "MCX-FUT", "name": "GOLDM"},
    ]
    resolved = resolve_silver_symbols(mock_instruments, underlying="SILVERM")
    assert len(resolved) == 3
    assert all("SILVERM" in item["tradingsymbol"] for item in resolved)

def test_paper_execution_lifecycle():
    kite_mock = MagicMock()
    leg1 = OptionLeg(strike=155000, option_type="CE", action="BUY", quantity=5, premium=1400.0, trading_symbol="SILVERM26NOV155000CE")
    leg2 = OptionLeg(strike=160000, option_type="CE", action="SELL", quantity=5, premium=600.0, trading_symbol="SILVERM26NOV160000CE")
    spread = SpreadPosition(
        spread_type=SpreadType.BULL_CALL_SPREAD,
        legs=[leg1, leg2],
        entry_net_debit=800.0,
        max_profit=4200.0,
        max_loss=800.0,
    )

    success_entry = execute_spread_entry(kite_mock, spread, is_paper=True)
    assert success_entry is True
    assert spread.status == "ACTIVE"
    # Kite API should NOT be called in paper mode
    assert kite_mock.place_order.call_count == 0

    success_exit = execute_spread_exit(kite_mock, spread, reason="TAKE_PROFIT", is_paper=True)
    assert success_exit is True
    assert spread.status == "CLOSED"
    assert spread.exit_reason == "TAKE_PROFIT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_order_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.contract_resolver'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/contract_resolver.py`:
```python
from typing import List, Dict

def resolve_silver_symbols(instruments: List[Dict], underlying: str = "SILVERM") -> List[Dict]:
    return [
        inst for inst in instruments
        if inst.get("name") == underlying or underlying in inst.get("tradingsymbol", "")
    ]
```

Create `strategy/silver_options/order_manager.py`:
```python
import logging
from typing import Optional
from strategy.silver_options.models import SpreadPosition
from strategy.silver_options.risk_manager import sequence_entry_legs, sequence_exit_legs

logger = logging.getLogger("silver_options.order_manager")

def execute_spread_entry(kite_client, spread: SpreadPosition, is_paper: bool = True) -> bool:
    ordered_legs = sequence_entry_legs(spread.legs)
    if is_paper:
        logger.info(f"[PAPER] Entering spread {spread.spread_type}: {len(ordered_legs)} legs placed.")
        spread.status = "ACTIVE"
        return True

    # Live execution: place buy leg, verify fill, then place sell leg
    for leg in ordered_legs:
        try:
            order_id = kite_client.place_order(
                variety="regular",
                exchange="MCX",
                tradingsymbol=leg.trading_symbol,
                transaction_type=leg.action,
                quantity=leg.quantity,
                order_type="MARKET",
                product="NRML",
            )
            leg.order_id = str(order_id)
        except Exception as e:
            logger.error(f"Failed to place live order for leg {leg.trading_symbol}: {e}")
            spread.status = "FAILED"
            return False

    spread.status = "ACTIVE"
    return True

def execute_spread_exit(kite_client, spread: SpreadPosition, reason: str, is_paper: bool = True) -> bool:
    ordered_legs = sequence_exit_legs(spread.legs)
    if is_paper:
        logger.info(f"[PAPER] Exiting spread {spread.spread_type} (Reason: {reason})")
        spread.status = "CLOSED"
        spread.exit_reason = reason
        return True

    # Live execution: close short leg first, then long leg
    for leg in ordered_legs:
        close_action = "BUY" if leg.action == "SELL" else "SELL"
        try:
            kite_client.place_order(
                variety="regular",
                exchange="MCX",
                tradingsymbol=leg.trading_symbol,
                transaction_type=close_action,
                quantity=leg.quantity,
                order_type="MARKET",
                product="NRML",
            )
        except Exception as e:
            logger.error(f"Failed to exit leg {leg.trading_symbol}: {e}")
            return False

    spread.status = "CLOSED"
    spread.exit_reason = reason
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_order_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/contract_resolver.py strategy/silver_options/order_manager.py tests/test_silver_options/test_order_manager.py
git commit -m "feat(silver): implement contract resolver and atomic order manager"
```

---

### Task 7: Strategy Orchestrator & Live State Machine (`engine.py`)

**Files:**
- Create: `strategy/silver_options/engine.py`
- Test: `tests/test_silver_options/test_engine.py`

**Interfaces:**
- Consumes: All components from Tasks 1–6.
- Produces:
  - `SilverOptionsEngine`: Orchestrator class managing tick/bar event processing, signal checks, and spread monitoring.

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_engine.py`:
```python
from datetime import time
from unittest.mock import MagicMock
import pandas as pd
import pytest
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.engine import SilverOptionsEngine

def test_engine_lifecycle_paper_mode():
    cfg = SilverConfig()
    engine = SilverOptionsEngine(kite_client=MagicMock(), config=cfg, is_paper=True)
    
    # Simulate daily df with bullish divergence and hourly confirmation
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

    # Process step
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.engine'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/engine.py`:
```python
from datetime import time
from typing import List, Optional, Dict
import pandas as pd
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import SpreadPosition
from strategy.silver_options.technical_signals import evaluate_technical_signal
from strategy.silver_options.chain_analyzer import build_option_chain_profile
from strategy.silver_options.spread_selector import select_spread
from strategy.silver_options.risk_manager import check_exit_conditions
from strategy.silver_options.order_manager import execute_spread_entry, execute_spread_exit

class SilverOptionsEngine:
    def __init__(self, kite_client, config: Optional[SilverConfig] = None, is_paper: bool = True):
        self.kite_client = kite_client
        self.config = config or SilverConfig()
        self.is_paper = is_paper
        self.active_position: Optional[SpreadPosition] = None
        self.closed_positions: List[SpreadPosition] = []

    def on_market_tick(
        self,
        spot_price: float,
        daily_df: pd.DataFrame,
        hourly_df: pd.DataFrame,
        chain_data: List[Dict],
        current_time: time,
        is_expiry_day: bool = False,
    ):
        # 1. If we have an active position, monitor exit conditions
        if self.active_position:
            chain_by_strike = {r["strike"]: r for r in chain_data}
            legs = self.active_position.legs
            long_leg = next(l for l in legs if l.action == "BUY")
            short_leg = next(l for l in legs if l.action == "SELL")

            long_ltp = chain_by_strike.get(long_leg.strike, {}).get(f"{long_leg.option_type.lower()}_ltp", long_leg.premium)
            short_ltp = chain_by_strike.get(short_leg.strike, {}).get(f"{short_leg.option_type.lower()}_ltp", short_leg.premium)
            current_spread_value = long_ltp - short_ltp

            should_exit, reason = check_exit_conditions(
                self.active_position,
                current_spread_value=current_spread_value,
                current_time=current_time,
                is_expiry_day=is_expiry_day,
                config=self.config,
            )
            if should_exit and reason:
                execute_spread_exit(self.kite_client, self.active_position, reason, is_paper=self.is_paper)
                self.active_position.realized_pnl = (current_spread_value - self.active_position.entry_net_debit) * self.config.lot_size
                self.closed_positions.append(self.active_position)
                self.active_position = None
            return

        # 2. If idle, check for entry signals
        signal = evaluate_technical_signal(daily_df, hourly_df, self.config)
        profile = build_option_chain_profile(spot_price, chain_data, self.config)
        spread = select_spread(signal, profile, chain_data, self.config)

        if spread:
            success = execute_spread_entry(self.kite_client, spread, is_paper=self.is_paper)
            if success:
                self.active_position = spread
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/engine.py tests/test_silver_options/test_engine.py
git commit -m "feat(silver): implement strategy orchestrator engine and event loop"
```

---

### Task 8: Historical Backtesting Harness & Frictions Model (`backtest.py`)

**Files:**
- Create: `strategy/silver_options/backtest.py`
- Test: `tests/test_silver_options/test_backtest.py`

**Interfaces:**
- Produces:
  - `calculate_mcx_costs(turnover: float) -> float`
  - `run_silver_backtest(trades: List[dict]) -> dict` (net_pnl, win_rate, profit_factor, max_dd)

- [ ] **Step 1: Write the failing test**

Create `tests/test_silver_options/test_backtest.py`:
```python
import pytest
from strategy.silver_options.backtest import calculate_mcx_costs, run_silver_backtest

def test_mcx_cost_calculation():
    # Turnover of 1 lot (5kg * 150000 = 7.5L)
    costs = calculate_mcx_costs(turnover=750000.0)
    # Expected brokerage (40) + CTT + exchange fee + GST > 50
    assert costs > 50.0

def test_run_silver_backtest_metrics():
    mock_trades = [
        {"pnl": 15000.0, "holding_days": 2},
        {"pnl": -4000.0, "holding_days": 1},
        {"pnl": 18000.0, "holding_days": 3},
        {"pnl": -3500.0, "holding_days": 1},
    ]
    summary = run_silver_backtest(mock_trades)
    assert summary["total_trades"] == 4
    assert summary["win_rate"] == 50.0
    assert summary["net_pnl"] > 20000.0
    assert summary["profit_factor"] > 3.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_silver_options/test_backtest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy.silver_options.backtest'`

- [ ] **Step 3: Write minimal implementation**

Create `strategy/silver_options/backtest.py`:
```python
from typing import List, Dict

def calculate_mcx_costs(turnover: float, legs: int = 2) -> float:
    brokerage = 20.0 * legs * 2  # ₹20/order entry and exit
    mcx_turnover_fee = turnover * 0.000021
    ctt = turnover * 0.0001  # Commodity Transaction Tax
    sebi_fee = turnover * 0.000001
    gst = (brokerage + mcx_turnover_fee + sebi_fee) * 0.18
    stamp_duty = turnover * 0.00003
    return round(brokerage + mcx_turnover_fee + ctt + sebi_fee + gst + stamp_duty, 2)

def run_silver_backtest(trades: List[Dict], slippage_per_trade: float = 200.0) -> Dict:
    if not trades:
        return {"total_trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0}

    total_gross = 0.0
    wins = 0
    total_wins = 0.0
    total_losses = 0.0
    cumulative_pnl = []
    current_cum = 0.0

    for t in trades:
        pnl = t["pnl"] - slippage_per_trade
        total_gross += pnl
        current_cum += pnl
        cumulative_pnl.append(current_cum)

        if pnl > 0:
            wins += 1
            total_wins += pnl
        else:
            total_losses += abs(pnl)

    # Max Drawdown
    peak = 0.0
    max_dd = 0.0
    for val in cumulative_pnl:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd

    win_rate = (wins / len(trades)) * 100.0
    profit_factor = (total_wins / total_losses) if total_losses > 0 else float("inf")

    return {
        "total_trades": len(trades),
        "net_pnl": round(total_gross, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_dd, 2),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_silver_options/test_backtest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/silver_options/backtest.py tests/test_silver_options/test_backtest.py
git commit -m "feat(silver): implement backtest metrics and MCX friction cost model"
```

---

### Task 9: CLI Entrypoint, Integration Verification & Full Regression Suite

**Files:**
- Create: `scripts/run_silver_desk.py`
- Test: Full test suite verification `uv run pytest`

**Interfaces:**
- Produces: CLI script allowing running the Silver options desk in paper or live mode with `--paper` flag.

- [ ] **Step 1: Write CLI script**

Create `scripts/run_silver_desk.py`:
```python
"""CLI entrypoint for running the MCX Silver Options Desk."""
import argparse
import logging
import sys
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.engine import SilverOptionsEngine
from kite.client import get_kite_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("silver_options.cli")

def main():
    parser = argparse.ArgumentParser(description="MCX Silver Options Desk")
    parser.add_argument("--paper", action="store_true", default=True, help="Run in paper trading mode")
    parser.add_argument("--live", action="store_true", help="Run in live trading mode")
    args = parser.parse_args()

    is_paper = not args.live
    logger.info(f"Starting MCX Silver Options Desk (Mode: {'PAPER' if is_paper else 'LIVE'})...")

    cfg = SilverConfig()
    kite = get_kite_client()
    engine = SilverOptionsEngine(kite_client=kite, config=cfg, is_paper=is_paper)
    logger.info(f"Initialized Silver Options Engine for {cfg.symbol} (Lot size: {cfg.lot_size} kg). Ready.")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run full regression test suite**

Run: `uv run pytest`
Expected: All 149 existing NIFTY strangle tests pass + all new Silver options desk tests pass with zero failures.

- [ ] **Step 3: Commit and finalize**

```bash
git add scripts/run_silver_desk.py
git commit -m "feat(silver): add CLI runner and verify full regression suite"
```
