# Technical Design Specification: NIFTY 10:00 AM Short Strangle Strategy & Realistic Backtesting Engine

**Date:** 2026-11-03  
**Status:** Approved  
**Target Workspace:** `d:\code\sharads_intraday_option_selling_desk`  

---

## 1. Executive Summary

This document specifies the architecture, math model, execution pipeline, and realistic backtesting engine for **Sharad's 10:00 AM Intraday Short Strangle Strategy** on NIFTY 50 options.

### Core Strategy Rules
- **Underlying Index:** NIFTY 50 (`NSE:NIFTY 50`, token `256265`).
- **Entry Time:** 10:00 AM IST on valid NSE trading days.
- **Contract Size:** 10 lots for Call and 10 lots for Put (quantity = $10 \times \text{lot\_size}$).
- **Expiry Selection:**
  - If today is an Expiry Day (0 DTE), trade **Next Week's** expiry.
  - On non-expiry days, trade the **Current Week's** nearest expiry.
- **Strike Selection:**
  - Scan option chain for the target expiry.
  - Select 1 CE strike with LTP strictly $\ge ₹30.0$ and closest to ₹30 from above.
  - Select 1 PE strike with LTP strictly $\ge ₹30.0$ and closest to ₹30 from above.
- **Risk Management & Exits:**
  - **Stop-Loss (SL):** $+50\%$ above entry price (`entry_price * 1.50`), tracked independently per leg.
  - **Profit Target:** $80\%$ decay of entry premium (`entry_price * 0.20`), tracked independently per leg.
  - **Time Exit:** 3:00 PM IST (15:00) mandatory square-off for any remaining active leg.
- **Dual Execution Modes:**
  - **PAPER (Default):** Realistic simulated fills with bid-ask slippage and cost accounting.
  - **LIVE:** Real order placement via Kite Connect (`place_order`) using `MIS` product type.
- **Realistic Backtesting:**
  - Continuous fractional DTE theta decay model (Black-76).
  - Realistic ₹0.50 slippage per share on entry and exit.
  - Exact NSE statutory charges: STT (0.1%), Exchange turnover (0.0495%), SEBI charges, Stamp duty, GST (18%), and Zerodha brokerage (₹20/order).

---

## 2. System Architecture & Module Boundaries

The project structure extends the modular foundation created for Kite Connect:

```
sharads_intraday_option_selling_desk/
│
├── kite/                          # Zerodha Kite Connect SDK integration
│   ├── __init__.py
│   ├── auth.py                    # Headless TOTP login & token lifecycle
│   ├── client.py                  # Authenticated session manager
│   └── positions.py               # Positions retrieval & formatting
│
├── strategy/                      # Strategy & Backtesting Subsystem
│   ├── __init__.py                # Strategy package exports
│   ├── options_model.py           # Black-76 pricing, Greeks & continuous fractional DTE
│   ├── option_chain.py            # Live Kite option chain & strike lookup
│   ├── costs.py                   # NSE transaction costs, taxes & slippage accounting
│   ├── order_manager.py           # Paper & Live order execution abstraction
│   ├── strangle_desk.py           # Live/Paper state machine & 5s monitoring loop
│   └── backtest.py                # Historical bar-by-bar backtest simulation engine
│
├── config.py                      # Global settings & environment loader
├── main.py                        # Positions viewer CLI
├── run_backtest.py                # Backtesting runner CLI with performance analytics
├── run_strategy.py                # Live/Paper trading desk entrypoint
├── pyproject.toml                 # uv dependency management
├── requirements.txt               # Pinned dependencies
├── .env.example / .env
├── data/                          # Scratch data, trade logs, cached bars & persistent state
│   ├── trades_today.json          # Crash-resilient active trade state
│   └── backtest_trade_log.csv     # Backtest execution ledger
└── tests/
    ├── test_options_model.py      # Tests for Black-76 pricing & fractional DTE
    ├── test_costs.py              # Tests for exact NSE cost calculations
    ├── test_option_chain.py       # Tests for 0 DTE expiry shift & strike selection
    ├── test_order_manager.py      # Tests for Paper vs Live order execution
    └── test_backtest.py           # Tests for historical simulation engine
```

---

## 3. Mathematical Models & Cost Specifications

### 3.1 Continuous Fractional DTE & Black-76 Pricing
Traditional models measure time-to-expiry in integer days ($T = \text{DTE} / 365$), creating artificial discrete jumps at midnight. In reality, intraday option sellers benefit from continuous decay between 10:00 AM and 3:00 PM.

Fractional trading DTE is calculated as:
$$T = \frac{\max(\text{Calendar Days to Expiry} - 1, 0) \times 375 + \text{Remaining Trading Minutes Today}}{375 \times 252}$$
Where:
- Standard trading day = 375 minutes (09:15 to 15:30).
- Remaining Trading Minutes = $\max(15:30 - t, 0)$.

Black-76 pricing equations:
$$d_1 = \frac{\ln(S / K) + 0.5 \sigma^2 T}{\sigma \sqrt{T}}, \quad d_2 = d_1 - \sigma \sqrt{T}$$
$$\text{Call Price} = e^{-r T} [S \cdot N(d_1) - K \cdot N(d_2)]$$
$$\text{Put Price} = e^{-r T} [K \cdot N(-d_2) - S \cdot N(-d_1)]$$
Parameters:
- $S$: Underlying NIFTY Index spot price.
- $K$: Strike price (in multiples of 50).
- $\sigma$: Annualized Implied Volatility (IV), default 0.13 (13%).
- $r$: Risk-free interest rate, default 0.065 (6.5% RBI repo rate).

### 3.2 Exact NSE F&O Transaction Costs & Slippage
For each option leg traded:
1. **Gross P&L:**
   $$\text{Gross P\&L} = (\text{Entry Price} - \text{Exit Price}) \times \text{Quantity}$$
2. **Execution Slippage:**
   - Entry sell price realized $= \text{Model Price} - \text{Slippage}$ (default ₹0.50).
   - Exit buy price realized $= \text{Model Price} + \text{Slippage}$ (default ₹0.50).
3. **Statutory Taxes & Brokerage (NSE F&O Options):**
   - **Brokerage:** ₹20 per executed order (₹40 round-trip per leg).
   - **STT (Securities Transaction Tax):** $0.1\%$ on SELL turnover only:
     $$\text{STT} = 0.001 \times \text{Entry Price} \times \text{Quantity}$$
   - **Exchange Turnover Charge:** $0.0495\%$ on total turnover (entry + exit):
     $$\text{Exchange Fee} = 0.000495 \times (\text{Entry Price} + \text{Exit Price}) \times \text{Quantity}$$
   - **SEBI Turnover Fee:** ₹10 per crore ($0.0001\%$):
     $$\text{SEBI Fee} = 0.000001 \times (\text{Entry Price} + \text{Exit Price}) \times \text{Quantity}$$
   - **Stamp Duty:** $0.003\%$ on BUY turnover only (exit side):
     $$\text{Stamp Duty} = 0.00003 \times \text{Exit Price} \times \text{Quantity}$$
   - **GST:** $18\%$ on non-tax charges:
     $$\text{GST} = 0.18 \times (\text{Brokerage} + \text{Exchange Fee} + \text{SEBI Fee})$$
   - **Net P&L:**
     $$\text{Net P\&L} = \text{Gross P\&L} - (\text{Brokerage} + \text{STT} + \text{Exchange Fee} + \text{SEBI Fee} + \text{Stamp Duty} + \text{GST})$$

---

## 4. Component Details & Workflows

### 4.1 Expiry & Strike Resolution (`strategy/option_chain.py`)
- Reads NFO instruments list via `kite.instruments("NFO")`.
- Filters for `name == "NIFTY"`.
- Determines sorted unique upcoming expiry dates: $[E_0, E_1, \dots]$.
- If $E_0 == \text{today}$ (0 DTE), sets `target_expiry = E_1` (Next Week). Otherwise, `target_expiry = E_0` (Current Week).
- Fetches live quotes for all strikes on `target_expiry` using `kite.ltp()`.
- Sorts CE strikes where $\text{LTP} \ge 30.0$ ascending by LTP; picks the first contract.
- Sorts PE strikes where $\text{LTP} \ge 30.0$ ascending by LTP; picks the first contract.
- Determines `lot_size` from instrument record, returning quantity $= 10 \times \text{lot\_size}$.

### 4.2 State Machine & Monitoring Desk (`strategy/strangle_desk.py`)
- Lifecycle states:
  1. `WAITING_FOR_10AM`: Checks clock until 10:00:00 AM IST.
  2. `ENTRY_AT_10AM`:
     - Discovers CE & PE strikes $\ge ₹30$.
     - Executes short sales (Paper or Live).
     - Calculates independent SL (`entry * 1.50`) and Target (`entry * 0.20`).
     - Serializes state to `data/trades_today.json`.
  3. `MONITORING`:
     - Polls `kite.ltp()` every 5 seconds.
     - For CE leg:
       - If $\text{LTP} \ge \text{SL}$: Triggers Buy Market Exit with reason `STOP_LOSS`.
       - Else if $\text{LTP} \le \text{Target}$: Triggers Buy Market Exit with reason `DECAY_TARGET`.
     - For PE leg:
       - Evaluated independently using identical logic.
     - Updates `data/trades_today.json`.
  4. `SQUARE_OFF_3PM`:
     - At 15:00:00 IST, closes any remaining active leg with reason `TIME_EXIT`.
  5. `COMPLETED`:
     - Generates final session report and resets daily active trade file.
- **Crash Recovery:** If initialized after 10:00 AM, inspects `data/trades_today.json`. If active positions exist for today, resumes monitoring without re-entering.

### 4.3 Historical Backtest Simulation Engine (`strategy/backtest.py`)
- Iterates historical 5-minute candles of NIFTY spot over the backtest period.
- For each trading day:
  - Identifies 10:00 AM candle.
  - Determines weekly expiry according to the 0 DTE $\to$ next week rule.
  - Calculates strike prices (in 50-pt steps) and theoretical premiums using Black-76 with fractional DTE.
  - Selects CE and PE strikes with price $\ge 30.0$ closest to 30.
  - Applies ₹0.50 entry slippage.
  - Traverses intraday 5-min candles between 10:05 and 15:00:
    - Calculates high, low, and close option valuations on each bar.
    - If candle high breaches $+50\%$ SL $\to$ exits at SL + ₹0.50 slippage.
    - If candle low breaches $80\%$ decay target $\to$ exits at Target - ₹0.50 slippage.
    - Remaining open legs exit at 15:00 close bar + ₹0.50 slippage.
  - Calculates gross P&L, exact NSE transaction costs, and net P&L.
- Outputs:
  - Formatted terminal analytics (Total Net P&L, Win Rate %, Profit Factor, Max Drawdown ₹ / %, Average Daily Return, Exit Breakdown).
  - Monthly breakdown table.
  - Row-by-row trade log exported to `data/backtest_trade_log.csv`.

---

## 5. Dependencies & Prerequisites

- `kiteconnect` (`>=5.0.0`): Zerodha trading client.
- `pyotp` (`>=2.9.0`): RFC 6238 TOTP engine.
- `requests` (`>=2.31.0`): HTTP sessions.
- `scipy` (`>=1.11.0`): Cumulative normal distribution for Black-76 equations.
- `numpy` (`>=1.24.0`): Numeric operations.
- `pandas` (`>=2.0.0`): Bar data processing and trade log CSV export.
- `pyarrow` (`>=14.0.0`): Parquet file caching for historical data.
- `tabulate` (`>=0.9.0`): Terminal reporting.
- `pytest` (`>=8.0.0`): Unit and regression tests.

---

## 6. Testing & Quality Assurance Plan

1. **Unit Tests (`tests/`)**:
   - `test_options_model.py`: Verifies Black-76 call and put prices match theoretical benchmarks; verifies intraday fractional DTE decreases monotonically across trading hours.
   - `test_costs.py`: Verifies exact penny-accurate calculation of Brokerage, STT, Exchange fees, SEBI, Stamp duty, and GST on known sample trades.
   - `test_option_chain.py`: Verifies 0 DTE expiry shift logic and closest-to-30 strike selection with mock instruments and quotes.
   - `test_order_manager.py`: Tests simulated order fills, slippage application, and trade state persistence.
   - `test_backtest.py`: Tests historical candle playback, per-leg SL triggering, 80% decay target triggering, and 3:00 PM time exit on synthetic multi-day market data.
2. **Verification Gate**:
   - 100% test pass rate across all new and existing test suites.
   - Complete `run_backtest.py` run on actual historical NIFTY data verifying authentic P&L numbers.
