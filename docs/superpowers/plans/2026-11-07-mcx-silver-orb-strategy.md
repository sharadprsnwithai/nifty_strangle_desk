# Implementation Plan: MCX Silver Mini 18:00 US Session Breakout Strategy

**Date:** 2026-11-07  
**Branch:** `feature/swing-trading`  
**Purpose:** Implement an automated, self-contained intraday swing desk for MCX Silver Mini (`SILVERM`) running the 18:00 IST US Session Opening Range Breakout with 2-lot tranche scaling, Telegram P&L alerts, and Docker Compose integration.

---

## 1. System Architecture & File Structure

```
d:\code\sharads_intraday_option_selling_desk\
│
├── strategy/silver_orb/
│   ├── __init__.py                # Module exports
│   ├── config.py                  # SilverORBConfig dataclass (defaults: 2 lots, 500 SL, 800 Tgt1, PAPER)
│   ├── contract_resolver.py       # Resolves near-month active SILVERM futures contract & expiry rollover
│   ├── order_manager.py           # Paper / Live execution manager for MCX SILVERM
│   └── desk.py                    # Main SilverORBDesk engine (17:45 range, 18:00 trigger, 2-lot tranche, state)
│
├── run_silver_orb.py              # CLI entry point for local execution & container runner
├── docker-compose.yml             # Adds new 'silver-orb-desk' service (disabled from live by default)
└── tests/test_silver_orb.py       # Comprehensive unit tests for state, triggers, tranche exits, and notifications
```

---

## 2. Component Specifications

### 2.1 `strategy/silver_orb/config.py`
* Dataclass `SilverORBConfig`:
  * `lots`: `int = 2`
  * `lot_size_kg`: `float = 5.0` (Silver Mini is 5 kg per lot $\rightarrow$ ₹5/pt per lot, ₹10/pt for 2 lots)
  * `initial_sl_pts`: `float = 500.0`
  * `target1_pts`: `float = 800.0`
  * `ref_candle_time`: `time(17, 45)`
  * `trade_window_start`: `time(18, 0)`
  * `trade_window_end`: `time(19, 30)`
  * `session_cutoff_time`: `time(22, 45)`
  * `vol_multiplier`: `float = 1.2`
  * `mode`: `"PAPER"` (supports `"LIVE"`)
  * `product`: `"MIS"`
  * `exchange`: `"MCX"`
  * `state_file`: `data/silver_trades_today.json`

### 2.2 `strategy/silver_orb/contract_resolver.py`
* Method `resolve_active_silverm_contract(kite)`:
  * Queries `kite.instruments('MCX')`.
  * Filters for `instrument_type == 'FUT'` and `tradingsymbol.startswith('SILVERM')`.
  * Sorts by `expiry`.
  * Filters out contracts expiring in $\le 3$ trading days (to avoid physical tender periods).
  * Returns nearest active contract `(tradingsymbol, instrument_token, expiry)`.

### 2.3 `strategy/silver_orb/order_manager.py`
* Paper & Live order execution for MCX:
  * `buy(symbol, quantity, price)`
  * `sell(symbol, quantity, price)`
  * In PAPER mode: Simulates immediate fills at current market quote with zero broker friction.
  * In LIVE mode: Submits regular LIMIT/MARKET orders via Kite Connect.

### 2.4 `strategy/silver_orb/desk.py`
* Daily lifecycle state:
  * Persistent JSON state at `data/silver_trades_today.json` with fallback resilience.
  * Day evaluation:
    1. Checks NSE/MCX trading holidays.
    2. At 18:00 IST: Extracts High & Low of the 17:45 15m candle.
    3. Calculates 50 EMA and 20 SMA Volume.
    4. Evaluates Long/Short breakout condition with 50 EMA trend filter.
    5. Executes 2 lots entry:
       * Sends Telegram Entry Alert.
    6. Position Monitoring Loop (polling every 5 seconds):
       * **Lot 1 Management:** If price reaches $+800$ pts, exits 1 lot, records $+₹4,000$ realized gain, moves Lot 2 SL to Breakeven (entry price), and sends Telegram Target 1 Alert with Total Day P&L.
       * **Stop Loss Management:** If price hits SL before Target 1, exits both lots, records $-₹5,000$ loss, and sends Telegram SL Alert with Total Day P&L.
       * **Runner Management:** If Lot 2 is active, trails at Breakeven. Exits at 22:45 IST cutoff (or stops out at Breakeven), sends Telegram Runner Exit Alert with Total Day P&L.

---

## 3. Step-by-Step Implementation Tasks

### Task 1: Create `strategy/silver_orb/config.py` and `contract_resolver.py`
* Implement dataclass and near-month contract auto-resolution with unit tests.

### Task 2: Create `strategy/silver_orb/order_manager.py`
* Implement paper trading execution manager with position tracking.

### Task 3: Create `strategy/silver_orb/desk.py`
* Implement the core engine, state persistence, 2-lot tranche logic, and Telegram notifications.

### Task 4: Create `run_silver_orb.py` CLI Runner
* CLI arguments: `--paper`, `--live`, `--lots`, `--state-file`, `--dry-run`, `--once`.

### Task 5: Configure `docker-compose.yml`
* Add `silver-orb-desk` service container matching project standard.

### Task 6: Unit Test Suite `tests/test_silver_orb.py`
* Test state recovery, contract resolver, breakout triggers, 2-lot scaling, and Telegram message content.
