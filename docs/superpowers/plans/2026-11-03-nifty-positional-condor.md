# NIFTY Monthly Positional Condor & 1:2:1 Directional Ratio Strategy Implementation Plan

A production-ready algorithmic trading desk implementing Uday Chandra's Positional Evergreen Strategy for Monthly NIFTY contracts on Zerodha Kite Connect. The system monitors Daily RSI(14) at 15:20 IST daily, dynamically switching between a 1:1 Balanced Iron Condor (sideways) and a 1:2:1 Broken-Wing Directional Ratio structure (trending).

---

## 1. Strategy Specifications

### A. Market Regime via Daily RSI (14)
- **Timeframe**: Daily bars (fetched via Kite API `historical_data(..., interval='day')`).
- **Evaluation Time**: Daily at **15:20 IST** on closing basis.
- **Regimes**:
  - **Sideways**: $40 \le \text{RSI} \le 60 \implies$ Deploy **1:1 Balanced Iron Condor**.
  - **Bearish**: $\text{RSI} < 40 \implies$ Deploy **1:2:1 Bearish Put Ratio**.
  - **Bullish**: $\text{RSI} > 60 \implies$ Deploy **1:2:1 Bullish Call Ratio**.

---

### B. Strike Selection Rules

#### 1. Sideways 1:1 Balanced Iron Condor ($40 \le \text{RSI} \le 60$)
- **Expiry**: Current active Monthly NIFTY expiry (last Thursday of the month).
- **Legs**: 4 legs $[PE_{\text{buy}}, PE_{\text{sell}}, CE_{\text{sell}}, CE_{\text{buy}}]$.
- **Objective Solver**:
  - Scans monthly strike quotes to minimize:
    $$\left| \frac{\text{Max Profit}}{\text{Max Loss}} - 1.0 \right|$$
  - Target: Max Profit $\approx 4.5\% - 5.5\%$ of allocated capital (~₹5,000 on ₹1.0L margin).
- **Break-Even Roll Rule**:
  - If NIFTY spot breaches lower break-even ($K_{PE\_sell} - \text{Credit}$) or upper break-even ($K_{CE\_sell} + \text{Credit}$) at 15:20 IST, but RSI is **still** between 40 and 60, close the existing condor and roll into a fresh centered 1:1 Iron Condor.

#### 2. Trending 1:2:1 Directional Ratio Structures
Exact user-specified strike coordinates:

- **Bearish ($\text{RSI} < 40$)**:
  - **Buy 1 lot**: $\text{ATM} - 50$ PE
  - **Sell 2 lots**: $\text{ATM} - 250$ PE
  - **Buy 1 lot**: $\text{ATM} - 300$ PE
- **Bullish ($\text{RSI} > 60$)**:
  - **Buy 1 lot**: $\text{ATM} + 50$ CE
  - **Sell 2 lots**: $\text{ATM} + 250$ CE
  - **Buy 1 lot**: $\text{ATM} + 300$ CE

*Payoff Dynamics*: Risk on adverse moves is capped very small (~1–2%), peak profit sits at the 200-point spread target (~4–5%), and severe black-swan gap crash risk is fully protected by the outer 50-pt wing.

---

### C. Risk & Capital Management
- **Capital Allocation**: ₹1,00,000 to ₹1,10,000 per lot.
- **Monthly Profit Target**: $+3.5\%$ to $+4.0\%$ net.
  - When hit, close all open legs, bank the gain, and pause until next monthly expiry.
- **Monthly Max Loss Stop**: $-4.0\%$ net.
  - If hit, immediately close all open legs and halt trading for the remainder of the month.
- **Monthly Rollover**:
  - On monthly expiry day (last Thursday) at 15:15 IST, close any open legs and deploy for the next month's contract.

---

## 2. Architecture & File Structure

```
d:\code\sharads_intraday_option_selling_desk\
├── strategy\
│   └── positional_condor\
│       ├── __init__.py
│       ├── indicator.py          # Daily RSI(14) computation from Kite daily bars
│       ├── strike_solver.py      # 1:1 Iron Condor solver & 1:2:1 Directional strike selector
│       ├── condor_desk.py        # PositionalDesk state machine, roll supervisor, MTM tracker
│       └── backtest.py           # Historical daily backtest simulation engine
├── run_positional_condor.py      # Standalone CLI runner (--paper, --live, --dry-run, --once)
├── tests\
│   └── test_positional_condor.py # Comprehensive unit and integration test suite
└── docs\superpowers\plans\
    └── 2026-11-03-nifty-positional-condor.md
```

---

## 3. Bite-Sized Implementation Tasks

### Task 1: Daily RSI(14) Indicator Engine
- **File**: `strategy/positional_condor/indicator.py`
- **Functionality**:
  - Fetch historical daily candles for `NSE:NIFTY 50` via `kite.historical_data`.
  - Calculate standard Wilder's RSI(14) on close prices.
  - Return current daily RSI value and classified regime (`SIDEWAYS`, `BEARISH`, `BULLISH`).
- **Tests**: Verify RSI calculation against known series; test regime boundaries at 39.9, 40.0, 50.0, 60.0, 60.1.

### Task 2: Strike Solver & 1:2:1 Ratio Generator
- **File**: `strategy/positional_condor/strike_solver.py`
- **Functionality**:
  - Identify monthly expiry date (last Thursday of current month, or next month if within 3 days of expiry).
  - Compute ATM strike (rounded to nearest 50).
  - Implement `get_directional_ratio_strikes(atm_strike, regime)`:
    - Bearish: Buy `ATM - 50` (1x), Sell `ATM - 250` (2x), Buy `ATM - 300` (1x).
    - Bullish: Buy `ATM + 50` (1x), Sell `ATM + 250` (2x), Buy `ATM + 300` (1x).
  - Implement `solve_1_to_1_iron_condor(option_chain, spot_price, target_capital=100000)`:
    - Evaluate strike quadruplets to find optimal 1:1 payoff ($\text{Max Profit} / \text{Max Loss} \approx 1.0$).
- **Tests**: Test strike coordinate calculations, ATM offsets, and 1:1 condor strike optimization.

### Task 3: State Machine & Positional Condor Desk
- **File**: `strategy/positional_condor/condor_desk.py`
- **Functionality**:
  - State persistence in `data/positional_condor_state.json`.
  - Daily 15:20 IST evaluation routine.
  - Regime transition execution: close old position and deploy new position.
  - Break-even breach detection & roll routine for Iron Condors.
  - Monthly profit target (+3.5% to 4.0%) and max loss stop (-4.0%) circuit breakers.
  - Telegram alert integration on state changes, rolls, and MTM updates.
- **Tests**: Mock order placement, state loading/recovery, break-even rolls, profit locking, and monthly risk halts.

### Task 4: CLI Runner & Historical Simulation Engine
- **Files**: `run_positional_condor.py`, `strategy/positional_condor/backtest.py`
- **Functionality**:
  - CLI runner with `--paper`, `--live`, `--dry-run`, `--once`, `--log-level`.
  - Multi-year daily backtest simulating 2024–2026 returns with exact transaction charges and slippage.
- **Tests**: Verify CLI flag parsing, dry-run output, and backtest data ingestion.

---

## 4. Verification & Validation Checklist
- [ ] 100% test pass rate across `tests/test_positional_condor.py`.
- [ ] Existing intraday strangle desk tests (101 tests) remain 100% green without regressions.
- [ ] Clean dry-run executed against live Kite instruments for monthly contracts.
- [ ] Detailed logs written to `logs/positional_condor.log` on host machine.
