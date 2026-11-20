# MCX Silver Options Desk — Architecture & Design Specification

**Date**: 2026-11-20  
**Branch**: `feature/silver-trading`  
**Status**: APPROVED DESIGN SPECIFICATION  

---

## 1. Executive Summary & Market Thesis

This specification defines the architecture, quantitative models, spread selection logic, risk controls, and testing framework for a dedicated **MCX Silver Options Desk** (`strategy/silver_options/`) on the `feature/silver-trading` branch.

### Background & Expert Webinar Synthesis
Following the *Commodities by Dhan* expert analysis (hosted by Piyush with analyst Varun), MCX Silver experienced an extreme macro correction, dropping from $120 down to $41–$44 and breaching the historical $48 resistance-turned-support level.
- **Daily Technicals**: Daily RSI collapsed into deep oversold territory (~19), where price made lower lows while RSI formed higher lows, creating a classic **Bullish RSI Divergence**.
- **Hourly Structure**: Hourly RSI hovered around ~36, establishing a downtrend channel that requires a breakout above the 20 EMA or prior swing high before confirming a reversal.
- **MCX Silver Mini (`SILVERM`) Option Chain Structure**:
  - **Spot / ATM**: ~₹1,53,800 – ₹1,54,000.
  - **Put-Call Ratio (PCR)**: 0.77 (favorable oversold baseline).
  - **Max Pain Strike / Pivot**: ₹1,55,000. Under ₹1,55,000, market is sell-on-rise; above ₹1,55,000 triggers short covering.
  - **Call Resistance Wall**: ₹1,60,000 (highest Call Open Interest).
  - **Put Support Base**: ₹1,48,000 (day's low) and ₹1,45,000.
  - **Expected Expiry Range**: ₹1,48,000 to ₹1,60,000 (12,000-point corridor).
- **Core Strategy**: A defined-risk **Vertical Bull Call Spread** (`BUY 155,000 CE / SELL 160,000 CE` at ~800 pts net debit) capturing short-covering momentum to ₹160,000 with strictly capped downside risk (~₹4,000 per 5 kg lot).

---

## 2. Architectural Boundaries & Isolation

The Silver desk is 100% isolated from the existing audited NIFTY short strangle desk:
- `strategy/strangle_desk/`: Retains exclusive focus on intraday NIFTY 50 options (09:15–15:30 IST, cash-settled). All 149 existing tests remain untouched and must pass.
- `strategy/silver_options/`: Owns all MCX commodity logic, contracts, multi-day holding periods (09:00–23:30 IST), and physical devolvement risk controls.
- Shared infrastructure: Utilizes shared root utilities for Kite connectivity (`kite/`), notifications (`notifications/telegram.py`), and database persistence.

```
strategy/silver_options/
├── __init__.py
├── config.py                 # MCX Silver Mini rules, trading hours (09:00-23:30), strike step (1000)
├── models.py                 # Domain dataclasses: SilverQuote, OptionChainProfile, SpreadPosition, etc.
├── contract_resolver.py      # Resolves active MCX SILVERM futures and option contracts (expiry cycles)
├── chain_analyzer.py         # Option Chain analytics: PCR, Max Pain, Call Wall, Put Base
├── technical_signals.py      # Multi-timeframe indicator: Daily RSI divergence + Hourly pivot breakout
├── spread_selector.py        # Selects optimal vertical spreads / condors based on risk-reward & OI walls
├── risk_manager.py           # Margin validation, hedge-first order sequencing, stop-loss, devolvement cutoff
├── order_manager.py          # Kite basket & two-leg atomic order execution with slippage control
├── backtest.py               # Historical backtesting harness with MCX transaction costs & slippage
└── engine.py                 # Orchestrator & state machine managing live polling and position lifecycle
```

---

## 3. Contract Specifications & MCX Parameters

| Parameter | Value | Description |
| :--- | :--- | :--- |
| **Exchange** | `MCX` | Multi Commodity Exchange of India |
| **Trading Symbol** | `SILVERM` | Silver Mini Option Contract |
| **Underlying Asset** | `SILVERM` Futures | MCX Silver Mini Futures contract of matching expiry |
| **Lot Size** | $5\text{ kg}$ | 1 lot represents 5 kilograms of silver |
| **Strike Interval** | $1,000\text{ points}$ | Standard strikes spaced every ₹1,000 (e.g., 154000, 155000, 156000) |
| **Tick Size** | $₹1.00$ | Minimum price fluctuation |
| **Trading Hours** | 09:00 to 23:30 IST | Extended evening trading (US session overlap) |
| **Settlement Type** | Physical Devolvement | ITM/CTM options devolve into futures if held past expiry cutoff |
| **Devolvement Cutoff** | 15:30 IST on Expiry Day | Hard automated desk cutoff to prevent futures assignment |

---

## 4. Quantitative Signal & Option Chain Analytics

### 4.1 Multi-Timeframe Signal Engine (`technical_signals.py`)
- **Daily Timeframe**:
  - Indicator: 14-period RSI computed on daily close prices.
  - Oversold Threshold: $\text{RSI} \le 25$.
  - Overbought Threshold: $\text{RSI} \ge 75$.
  - **Bullish Divergence**:
    Within a trailing window of $N=5$ to $20$ daily bars:
    $$\text{Price}_{\text{current}} < \text{Price}_{\text{prior low}} \quad \text{AND} \quad \text{RSI}_{\text{current}} > \text{RSI}_{\text{prior low}}$$
  - **Bearish Divergence**:
    $$\text{Price}_{\text{current}} > \text{Price}_{\text{prior high}} \quad \text{AND} \quad \text{RSI}_{\text{current}} < \text{RSI}_{\text{prior high}}$$

- **Hourly Timeframe (Confirmation Trigger)**:
  - Divergence alone only arms the system. Entry requires momentum confirmation:
  - **Bullish Trigger**: 1-hour bar closes above the 20-period EMA or breaks the previous 4-hour swing high.
  - **Bearish Trigger**: 1-hour bar closes below the 20-period EMA or breaks the previous 4-hour swing low.

### 4.2 Option Chain Analytics (`chain_analyzer.py`)
Given active strike data from Kite:
1. **Put-Call Ratio (PCR)**:
   $$\text{PCR} = \frac{\sum \text{Put Open Interest}}{\sum \text{Call Open Interest}}$$
   - $\text{PCR} < 0.80$: Oversold sentiment (short-covering potential).
   - $\text{PCR} > 1.20$: Overbought sentiment (long-unwinding potential).
2. **Max Pain Strike ($K^*$)**:
   $$\text{Pain}(K) = \sum_{i} \text{OI}_{\text{CE}, i} \cdot \max(0, K - S_i) + \sum_{j} \text{OI}_{\text{PE}, j} \cdot \max(0, S_j - K)$$
   $$K^* = \arg\min_K \text{Pain}(K)$$
3. **Call Resistance Wall**:
   $$K_{\text{call\_wall}} = \arg\max_K \text{OI}_{\text{CE}}(K)$$
4. **Put Support Base**:
   $$K_{\text{put\_base}} = \arg\max_K \text{OI}_{\text{PE}}(K)$$
5. **Market State Classification**:
   - `BULLISH_REVERSAL`: Bullish divergence confirmed + Hourly EMA cross + Spot $< K_{\text{call\_wall}}$.
   - `BEARISH_BREAKDOWN`: Bearish divergence confirmed + Hourly EMA breakdown + Spot $> K_{\text{put\_base}}$.
   - `RANGE_BOUND`: No active divergence, $0.85 \le \text{PCR} \le 1.15$, spot within Put Base and Call Wall.

---

## 5. Spread Selection & State Machine

### 5.1 Strike Selection (`spread_selector.py`)
1. **Bull Call Spread** (`BULLISH_REVERSAL`):
   - **Leg 1 (Long Call)**: ATM strike or nearest pivot above spot ($K_1$, e.g., ₹155,000 CE).
   - **Leg 2 (Short Call)**: Call Wall strike ($K_2 = K_{\text{call\_wall}}$, e.g., ₹160,000 CE).
   - **Gating Filter**:
     $$\text{Net Debit} = \text{Premium}(K_1) - \text{Premium}(K_2)$$
     $$\text{Net Debit} \le 0.35 \times (K_2 - K_1)$$
     Requires minimum 1:2.86 risk-to-reward potential before opening.
2. **Bear Put Spread** (`BEARISH_BREAKDOWN`):
   - **Leg 1 (Long Put)**: ATM strike or nearest breakdown pivot ($K_2$, e.g., ₹154,000 PE).
   - **Leg 2 (Short Put)**: Put Support Base ($K_1 = K_{\text{put\_base}}$, e.g., ₹148,000 PE).
   - Same $\text{Net Debit} \le 0.35 \times \text{Spread Width}$ gating filter.
3. **Iron Condor** (`RANGE_BOUND`, $> 3$ days to expiry):
   - Short legs at $K_{\text{call\_wall}}$ and $K_{\text{put\_base}}$.
   - Long protective wings bought 2,000 points OTM.

### 5.2 Position State Machine
```
[IDLE] ──(Signal Triggered)──> [PENDING_ENTRY]
       ──(Hedge-First Fill)──> [ACTIVE_MONITORING]
       ──(Take Profit 75% | Stop Loss 50% | Expiry Cutoff 15:30)──> [CLOSED]
```

---

## 6. Risk Management & MCX Safeguards (`risk_manager.py`)

### 6.1 Hedge-First Order Sequencing
To eliminate margin spikes and unhedged short liability:
- **On Entry**:
  1. Submit **BUY (Long Leg)** first.
  2. Wait for fill confirmation.
  3. Submit **SELL (Short Leg)** with hedge margin discount active.
- **On Exit**:
  1. Submit **BUY to Cover (Short Leg)** first.
  2. Wait for fill confirmation.
  3. Submit **SELL (Long Leg)** to close.
- **Guarantee**: Account never holds an unhedged naked short option at any time.

### 6.2 Mandatory Expiry Devolvement Cutoff
- **Trigger**: Every trading day, check if current date is the contract expiry date.
- **Action at 15:30 IST**:
  - Cancel any pending entry orders.
  - Forcefully close all open Silver spread legs at marketable limit prices.
  - Zero contracts held into the MCX 17:00 IST tender window. Completely eliminates physical futures assignment risk.

### 6.3 Profit Targets & Stop-Loss Rules
- **Debit Spreads (Bull Call / Bear Put)**:
  - **Take-Profit**: 75% of maximum potential profit (e.g., if max profit is ₹21,000, trigger exit at ₹15,750 gain).
  - **Stop-Loss**: 50% loss of initial net debit paid (e.g., if debit was ₹4,000, exit if spread value decays to ₹2,000).
  - **Breakeven Trailing**: Move stop-loss to entry debit once position reaches +50% of debit.
- **Execution Safeguards**: Limit orders placed with a marketable buffer (`Ask + 2 ticks` for buys, `Bid - 2 ticks` for sells) to prevent bad fills on wide MCX spreads.

---

## 7. Testing Strategy & Quality Gates

### 7.1 Unit Tests (`tests/test_silver_options/`)
- `test_config.py`: Verifies `SILVERM` parameters (lot size 5, step 1000, market timings, devolvement cutoff).
- `test_technical_signals.py`: Tests RSI calculation, Bullish Divergence detection (matching webinar scenario), Bearish Divergence, and hourly 20 EMA crossover triggers.
- `test_chain_analyzer.py`: Tests PCR, Max Pain calculation with edge cases, Call Wall, and Put Base detection.
- `test_spread_selector.py`: Tests Bull Call spread generation (155k/160k), R:R gating filters, Bear Put spread, and Iron Condor generation.
- `test_risk_manager.py`: Tests hedge-first order sequencing, stop loss, take profit, and mandatory 15:30 expiry auto-square off.
- `test_order_manager.py`: Tests atomic leg execution, timeout handling, and partial fill fallbacks.

### 7.2 Zero-Regression Requirement
Running the full test suite must pass all 149 existing NIFTY tests alongside all new Silver tests:
```bash
uv run pytest
```

---

## 8. Summary of Approval
This specification has been reviewed and approved across all 5 sections. Proceed to creating the implementation plan using `writing-plans`.
