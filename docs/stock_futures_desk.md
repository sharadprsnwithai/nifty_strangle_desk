# Nifty 200 Momentum 30 Stock Futures Desk

## 1. Executive Summary & Strategy Concept

The **Nifty 200 Momentum 30 Stock Futures Desk** is an automated intraday futures breakout trading system designed for high-beta momentum equities in the Indian market.

The system trades the top 30 highest-momentum constituents of the **Nifty 200 Momentum 30 Index** (`NIFTY200MOMENTUM30`), identifying institutional volume breakouts following morning consolidation.

---

## 2. Core Strategy Mechanics

```
09:15                      09:30                      10:15       10:30                13:00         15:00
  |--------------------------|--------------------------|-----------|--------------------|-------------|
  [ C1 Opening Candle 15m ]   [ C2–C5 Consolidation Base ]  Buffer   [ Breakout Scanning ] Entry Cutoff  Squareoff
   Establishes [L1, H1] range   Contained inside C1 range    Wait     Long > H1 (Vol>1.5x)  No new trades Mandatory
                                                                      Short < L1 (Vol>1.5x)
```

### A. Formation Window (09:15 – 10:15 IST)
1. **C1 Opening Candle (09:15 – 09:30 IST)**:
   - Establishes the initial reference range: High ($H_1$) and Low ($L_1$).
2. **C2–C5 Base Consolidation (09:30 – 10:15 IST)**:
   - Verifies volatility contraction inside the opening range:
     $$\text{High}(C_2..C_5) \le H_1 \quad \text{and} \quad \text{Low}(C_2..C_5) \ge L_1$$
3. **Consolidation Quality Filter**:
   - Ensures candles C2–C5 form a tight base without false whipsaws.

### B. Breakout Execution Window (10:30 – 13:00 IST)
- Scanning begins strictly at **10:30 IST** (after the complete morning base has settled).
- **Long Breakout**:
  - Close $> H_1$
  - Candle Volume $> 1.5\times$ 20-period Volume Moving Average
- **Short Breakdown**:
  - Close $< L_1$
  - Candle Volume $> 1.5\times$ 20-period Volume Moving Average
- **Entry Cutoff**: No new entries permitted after **13:00 IST**.

### C. Mathematical Exit Engine (2.25:1 R:R)
- **Target**: **Fixed +2.25%** from entry price.
  - Long Target: $\text{Entry} \times 1.0225$
  - Short Target: $\text{Entry} \times (1 - 0.0225)$
- **Stop Loss**: **Fixed 1.0% Hard Stop** from entry price.
  - Long Stop: $\text{Entry} \times 0.990$
  - Short Stop: $\text{Entry} \times 1.010$
- **Mandatory EOD Squareoff**: **15:00 IST** for any remaining intraday positions.
- **Why No Trailing SL?**: Rigorous backtests proved that trailing stops to cost at 1.0% gain trigger a "breakeven trap," prematurely shaking out trades during normal retests that later hit the 2.25% target.

---

## 3. Backtest Performance Metrics (30 Momentum Stocks, 2 Lots)

| Metric | Fixed 1.5% Target (Baseline) | Fixed 2.0% Target | Fixed 2.25% Target (Current Production) |
| :--- | :--- | :--- | :--- |
| **Gross PnL** | +₹36.03 Lakhs | +₹39.67 Lakhs | **+₹41.54 Lakhs** |
| **Total Charges** | ₹3.67 Lakhs | ₹3.95 Lakhs | **₹3.95 Lakhs** |
| **Net PnL** | +₹32.36 Lakhs | +₹35.72 Lakhs | **+₹37.59 Lakhs (+₹5.23L vs 1.5%)** |
| **Profit Factor** | 1.56 | 1.63 | **1.67** |
| **Win Rate** | 56.4% | 51.3% | **48.7%** |
| **Average Win / Loss Ratio** | 1.50 : 1 | 2.00 : 1 | **2.25 : 1** |
| **Max Drawdown** | ₹3.94 Lakhs | ₹3.56 Lakhs | **₹3.11 Lakhs (-21.1% lower DD)** |
| **Return / Max DD** | 8.21x | 10.03x | **12.08x** |
| **Profitable Months** | 13 / 13 (100%) | 13 / 13 (100%) | **13 / 13 (100%)** |

---

## 4. Architecture & Implementation

- **`strategy/stock_futures/config.py`**: Configuration parameters (lots, targets, SL, volume ratio, timing).
- **`strategy/stock_futures/models.py`**: Trade state classes (`StockFuturesPosition`, `StockFuturesSignal`, `Candle`).
- **`strategy/stock_futures/engine.py`**: Signal generator implementing the 15m ORB/VCP logic.
- **`strategy/stock_futures/order_manager.py`**: Order executor with marketable `LIMIT` orders and 5% freak-trade buffer.
- **`strategy/stock_futures/desk.py`**: Daemon loop coordinator with automated rehydration and position monitoring.
- **`strategy/stock_futures/rehydrator.py`**: Automatic index rehydration from NSE India.
- **`scripts/run_stock_futures_desk.py`**: CLI entrypoint with `--live`, `--paper`, `--lots`, `--target-pct 0.0225`.

---

## 5. Usage Commands

```bash
# Paper Trading Daemon (Default: 2 Lots, 2.25% Target, 1.0% SL)
uv run python scripts/run_stock_futures_desk.py --paper --lots 2

# Single Monitoring Pass
uv run python scripts/run_stock_futures_desk.py --once

# Live Production Trading via Kite Connect
uv run python scripts/run_stock_futures_desk.py --live --lots 2 --order-type LIMIT --limit-buffer-pct 0.05 --target-pct 0.0225 --sl-pct 0.010

# Dynamic Universe Rehydration from NSE
uv run python scripts/rehydrate_momentum_universe.py
```
