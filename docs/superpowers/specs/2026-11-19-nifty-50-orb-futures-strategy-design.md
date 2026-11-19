# NIFTY 50 Index ORB Futures Strategy — System Design Specification

- **Date**: 2026-11-19
- **Status**: Approved for Implementation
- **Author**: Antigravity & Sharad
- **Target Architecture**: Approach 1 (Dedicated `strategy/nifty_orb` module + `run_nifty_orb.py` + `docker-compose.yml`)

---

## 1. Executive Summary & Strategy Context

This specification defines the architecture, rules, execution flow, risk management, and containerization for a dedicated **NIFTY 50 Index Futures Opening Range Breakout (ORB) Trading Desk**.

Patterned directly after the battle-tested `strategy/silver_orb` architecture in this repository, this strategy adapts the multi-tranche execution discipline to the Indian equity derivatives market (`NFO` exchange):
- **Asset**: NIFTY 50 Index Monthly Futures (`NFO` contract, e.g. `NIFTY26NOVFUT`).
- **Opening Range**: 30-Minute ORB (09:15 to 09:45 IST).
- **Confirmation**: Closed 15-minute candle breakout outside the 30-min range with a 50 EMA trend filter and 1.2x volume confirmation.
- **Direction**: Bi-directional (Long on breakout above High, Short on breakdown below Low).
- **Execution & Sizing**: 2-lot tranche scaling (50% target lock + 50% risk-free runner to 15:15 IST).
- **Automation**: Docker Compose integration, state persistence, Telegram alerts, and Kite token resilience.

---

## 2. Core Trading Rules & Mathematical Model

### 2.1 Timeline & Market Windows
1. **09:15 – 09:45 IST (Reference Window)**:
   - Two consecutive 15-minute bars (09:15–09:30 and 09:30–09:45).
   - $\text{ref\_high} = \max(\text{High}_{09:15}, \text{High}_{09:30})$
   - $\text{ref\_low} = \min(\text{Low}_{09:15}, \text{Low}_{09:30})$
   - Broadcast reference levels to Telegram at 09:45 IST.
2. **09:45 – 13:30 IST (Breakout Evaluation Window)**:
   - Scan each newly closed 15-minute candle (at 10:00, 10:15, 10:30, ..., 13:30 IST).
   - Disallow new trade entries after 13:30 IST to avoid low-liquidity/late-session chop.
3. **15:15 IST (Session Cutoff Window)**:
   - Mandatory automated exit of any active runner positions before broker intraday auto-square-off (15:20–15:25 IST).

### 2.2 Breakout Signal Conditions
For a closed 15-minute bar $B$ with High $H_B$, Low $L_B$, Close $C_B$, Volume $V_B$, 50-period Exponential Moving Average $\text{EMA50}_B$, and 20-period Volume Simple Moving Average $\text{SMA20}(V)_B$:

- **Volume Confirmation**:
  $$V_B \ge 1.2 \times \text{SMA20}(V)_B$$

- **Long Breakout**:
  $$H_B > \text{ref\_high} \quad \text{AND} \quad C_B > \text{ref\_high} \quad \text{AND} \quad C_B > \text{EMA50}_B \quad \text{AND} \quad \text{Volume Confirmed}$$

- **Short Breakdown**:
  $$L_B < \text{ref\_low} \quad \text{AND} \quad C_B < \text{ref\_low} \quad \text{AND} \quad C_B < \text{EMA50}_B \quad \text{AND} \quad \text{Volume Confirmed}$$

- **Max Trades Per Day**: Exactly **1 trade** per session. Once a position is opened and subsequently closed (target, stop, or EOD), no further trades are initiated that day.

---

## 3. Position Sizing & Multi-Tranche Risk Management

### 3.1 Position Sizing
- **Total Lots**: Default 2 Lots (configurable via `--lots`).
- **Tranche Division**:
  - $\text{Lots}_{\text{Tranche1}} = \max(1, \lfloor \text{Lots}_{\text{total}} / 2 \rfloor)$
  - $\text{Lots}_{\text{Runner}} = \text{Lots}_{\text{total}} - \text{Lots}_{\text{Tranche1}}$
- Contract Lot Size (units per lot) is queried dynamically from KiteConnect NFO master (handles standard 25, 50, or 75 units per lot).

### 3.2 Order Types & Slippage Protection
- **Order Type**: Marketable `LIMIT` order with a 5.0-point protection buffer (configurable via `--limit-buffer-pts`).
  - Long limit price: $\text{LTP} + 5.0$
  - Short limit price: $\text{LTP} - 5.0$
- Product: `MIS` (Intraday) by default, selectable `NRML` via `--product`.

### 3.3 Tranche Lifecycle
1. **Entry**:
   - Both tranches fill at average entry price $P_{\text{entry}}$.
   - Initial Stop Loss is placed:
     - Long: $P_{\text{sl}} = P_{\text{entry}} - 50.0$
     - Short: $P_{\text{sl}} = P_{\text{entry}} + 50.0$
   - Target 1 level is computed:
     - Long: $P_{\text{target1}} = P_{\text{entry}} + 75.0$
     - Short: $P_{\text{target1}} = P_{\text{entry}} - 75.0$

2. **Scenario A: Initial Stop Loss Hit Before Target 1**:
   - If LTP breaches $P_{\text{sl}}$, both Tranche 1 and Runner exit immediately.
   - Trade P&L: $-\text{Lots}_{\text{total}} \times \text{lot\_size} \times 50.0$ pts.
   - Status updated to `COMPLETED`; alert dispatched; day concludes.

3. **Scenario B: Target 1 Hit**:
   - When LTP reaches or crosses $P_{\text{target1}}$:
     - Tranche 1 is immediately squared off, booking $+75.0$ points profit.
     - **Runner SL Trailed to Cost**: $P_{\text{runner\_sl}} = P_{\text{entry}}$.
     - The entire trade is now 100% risk-free.
     - Status updated to `TARGET1_HIT`.

4. **Scenario C: Runner Management**:
   - **Cost Hit**: If price reverses to $P_{\text{entry}}$, the runner exits at break-even ($0$ P&L). Net trade retains the $+75.0$ pts from Tranche 1.
   - **Trend Continuation**: The runner stays open, capturing the intraday trend until **15:15 IST**.
   - **15:15 IST Cutoff**: Desk automatically squares off the runner at current market price, computes total day P&L, and concludes the session.

---

## 4. Component Architecture & Directory Structure

```
sharads_intraday_option_selling_desk/
├── strategy/
│   └── nifty_orb/
│       ├── __init__.py               # Exports NiftyORBConfig, NiftyORBDesk, NiftyOrderManager
│       ├── config.py                 # Dataclass configuration
│       ├── contract_resolver.py      # Dynamic monthly NIFTY FUT contract identification
│       ├── desk.py                   # State machine, signal loop, and tranche supervisor
│       └── order_manager.py          # Paper & Live order execution abstraction
├── run_nifty_orb.py                  # Standalone CLI entrypoint
├── docker-compose.yml                # nifty-orb-desk service configuration
├── data/
│   └── nifty_orb_trades_today.json   # Resilient intraday state persistence
├── logs/
│   └── nifty_orb_desk.log            # Dedicated rolling log file
└── tests/
    └── test_nifty_orb.py             # Comprehensive unit & integration test suite
```

### 4.1 `strategy/nifty_orb/config.py`
`NiftyORBConfig` holds all parameters with clean defaults:
- `lots: int = 2`
- `initial_sl_pts: float = 50.0`
- `target1_pts: float = 75.0`
- `ref_start_time: time = time(9, 15)`
- `ref_end_time: time = time(9, 45)`
- `trade_window_end: time = time(13, 30)`
- `session_cutoff_time: time = time(15, 15)`
- `vol_multiplier: float = 1.2`
- `use_trend_filter: bool = True`
- `mode: str = "PAPER"` (or `"LIVE"`)
- `product: str = "MIS"`
- `exchange: str = "NFO"`
- `order_type: str = "LIMIT"`
- `limit_buffer_pts: float = 5.0`
- `poll_interval_seconds: int = 5`
- `state_file: str = "data/nifty_orb_trades_today.json"`

### 4.2 `strategy/nifty_orb/contract_resolver.py`
- Fetches all `NFO` instruments from `kite.instruments("NFO")`.
- Filters records where `name == "NIFTY"`, `instrument_type == "FUT"`, and expiry date is on or after today.
- Sorts by expiry ascending and selects the earliest valid contract (current near-month).
- Extracts `tradingsymbol` (e.g. `NIFTY26NOVFUT`), `instrument_token`, `expiry`, and `lot_size`.
- Includes offline fallback mock for testing/paper initialization without network dependencies.

### 4.3 `strategy/nifty_orb/order_manager.py`
- Inherits the clean pattern of `SilverOrderManager`:
  - `place_order(...)`: Handles live KiteConnect order placement or paper simulation.
  - Implements marketable limit price calculations with slippage buffer.
  - Generates unique paper order IDs and tracks order status (`COMPLETE`, `REJECTED`).

### 4.4 `strategy/nifty_orb/desk.py`
- Implements:
  - `initialize_day()`: Resumes or starts fresh daily state in `data/nifty_orb_trades_today.json`.
  - `_ensure_kite_session()`: Auto-refreshes Kite access tokens on token error.
  - `fetch_recent_candles()`: Pulls 15m historical candles for technical calculations.
  - `evaluate_reference_range()`: Computes High/Low across the 09:15 and 09:30 bars.
  - `evaluate_breakout_signal()`: Checks closed 15m candle against breakout rules, 50 EMA, and 1.2x volume SMA.
  - `run_monitoring_cycle()`: Monitors live tick/candle updates, executes Target 1 square-off, adjusts Runner SL to Cost, and triggers 15:15 EOD square-off.
  - Telegram alert dispatches.

---

## 5. Telegram Notification Suite

All messages use rich HTML formatting and display cumulative Total Day P&L:
1. 🔔 **Range Established (09:45 IST)**:
   - Nifty Contract, Reference High, Reference Low, Range size in pts, and 50 EMA level.
2. 🚀 **Breakout Entry**:
   - Direction (`LONG` / `SHORT`), Entry Price, Lots, Initial Stop Loss price (-50 pts), and Target 1 price (+75 pts).
3. 🎯 **Target 1 Hit**:
   - Tranche 1 Lots exited, Booked points & ₹ P&L.
   - **Clear Banner**: "Runner Stop Loss Trailed to Cost! Trade is now 100% Risk-Free."
4. 🛑 **Stop Loss Hit**:
   - Exit price, Loss points & ₹ P&L, Cumulative Day Realized P&L.
5. 🏁 **Runner Exit (15:15 IST or Cost)**:
   - Exit reason (`EOD Session Cutoff 15:15` or `Cost/Breakeven Reversal`), Runner exit price, Runner P&L, Total Trade P&L, and Total Day Realized P&L.

---

## 6. Docker Compose Service Specification

Add the `nifty-orb-desk` service to `docker-compose.yml`:

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

---

## 7. Verification & Testing Strategy

A dedicated test suite `tests/test_nifty_orb.py` will validate:
1. **Contract Resolver**:
   - Correctly selects nearest NIFTY FUT contract from sample instrument lists.
   - Extracts accurate dynamic lot size.
2. **Breakout Signal Detection**:
   - Long breakout triggers when close > high, close > 50 EMA, and volume > 1.2x SMA.
   - Rejects breakout if close < 50 EMA or volume is sub-threshold.
   - Short breakdown triggers when close < low, close < 50 EMA, and volume > 1.2x SMA.
3. **Execution State Machine**:
   - Tranche 1 Target Hit transitions state to `TARGET1_HIT` and sets Runner SL to entry price.
   - Initial SL hit before Target 1 exits both tranches.
   - 15:15 IST cutoff correctly forces exit on open runner.
4. **Resilience & State Storage**:
   - Atomic disk write and state recovery across simulated process restarts.
