# Sharad's Quantitative Trading Desk

A production-grade algorithmic trading platform connected to the Zerodha Kite Connect API with automated headless TOTP 2FA authentication, multi-strategy execution desks, off-market zero-hit safety guards, Telegram alert dispatching, and Docker containerization.

---

## Architecture Overview

```
                                      +---------------------------------------------+
                                      |        Zerodha Kite Connect API Gateway     |
                                      +---------------------------------------------+
                                                             ^
                                                             | (Zero-Hit Off-Market Guard)
                     +---------------------------------------+---------------------------------------+
                     |                                       |                                       |
    +---------------------------------+     +---------------------------------+     +---------------------------------+
    |             DESK 1              |     |             DESK 2              |     |             DESK 3              |
    |      NIFTY Adaptive Strangle    |     |   Nifty 200 Momentum 30 Futures |     |   Weekly Fibonacci ETF Swing    |
    |        (Option Selling)         |     |       (Futures Breakout)        |     |          (Cash Swing)           |
    +---------------------------------+     +---------------------------------+     +---------------------------------+
    | • Dynamic 1.25x ATR Strikes     |     | • Top 30 Momentum Stock Futures |     | • Nifty, Junior, Gold, Silver   |
    | • 09:15-10:00 Morning Filter    |     | • 15m Opening Range (C1) [L1,H1]|     | • Monthly Fibonacci P & S1      |
    | • Single-Leg Exit + Cost Trail  |     | • C2-C5 Base Vol Contraction    |     | • Friday Weekly Close Trigger   |
    | • 80% Decay Target / 10k Loss   |     | • 10:30 IST Breakout Scan       |     | • Monday 09:16 AM Rebalance     |
    | • Mandatory 14:45 EOD Exit      |     | • 2.25% Target (2.25:1 R:R)     |     | • CNC Delivery / Zero Leverage  |
    | • Paper & Live Execution Modes  |     | • 1.0% Hard Stop / 15:00 Exit   |     | • Rs 10 Lakhs Portfolio Ballast |
    +---------------------------------+     +---------------------------------+     +---------------------------------+
                     |                                       |                                       |
                     +---------------------------------------+---------------------------------------+
                                                             |
                                      +---------------------------------------------+
                                      |         Shared Core Infrastructure          |
                                      +---------------------------------------------+
                                      | • Headless RFC 6238 TOTP 2FA Authentication |
                                      | • Atomic Inter-Process Lock (Token Safety)  |
                                      | • NSE Trading Holiday & Market Hours Guards |
                                      | • Real-Time Rich HTML Telegram Notifier     |
                                      | • State Persistence (Crash-Resilient JSON)  |
                                      | • Full Pytest Test Suite (183/183 Passing)  |
                                      +---------------------------------------------+
```

---

## Features

### 1. Desk 1: 10:00 AM NIFTY Adaptive Short Strangle Desk
- **Dynamic ATR Strike Selection**: Uses 14-day Daily ATR to position OTM strikes at $1.25\times\text{ATR}$ distance (rounded to nearest 50 points), automatically widening in high volatility and tightening in calm regimes.
- **Morning Range Trend Filter (09:15–10:00 IST)**: Skips days where the opening 45-minute range $\ge 0.60\%$ of spot, preventing shorting into violent trending market opens.
- **Single-Leg Exit with Cost Trailing**: When a losing leg hits +50% SL or ₹10,000 loss, only the losing leg is squared off. The winning leg remains open and its stop-loss is immediately moved to breakeven (`cost`), allowing theta decay to cushion drawdowns.
- **Circuit Breakers**: Combined portfolio ₹10,000 risk cap, 80% premium decay lock-in, and mandatory 14:45 IST cutoff exit.

### 2. Desk 2: Nifty 200 Momentum 30 Stock Futures Desk
- **Dynamic Universe Rehydration**: Automatically queries and syncs the top 30 constituents of the NSE Nifty 200 Momentum 30 Index directly from NSE India daily between 08:00 and 09:10 IST.
- **Opening Range Base Formation**: Evaluates C1 (09:15–09:30 IST) range $[L_1, H_1]$ and confirms consolidation through C2–C5 (09:30–10:15 IST).
- **Volume Breakout Engine**: Initiates Long ($> H_1$) or Short ($< L_1$) entries strictly at or after 10:30 IST when candle volume $> 1.5\times$ 20-MA.
- **Optimized 2.25:1 R:R**: Rigorously backtested sweet-spot with **Fixed +2.25% Profit Target**, **1.0% Hard Stop Loss**, and mandatory 15:00 IST squareoff (+₹37.59 Lakhs on 2 lots, 1.67 profit factor, 13/13 green months).
- **Hard Concurrency Limit (Max 2 Positions)**: Enforces `max_concurrent_positions = 2` to prevent margin exhaustion and protect capital allocation (~₹16L–₹18L margin ceiling).
- **Freak-Trade Protection**: Places marketable `LIMIT` orders with 5% buffer rounded to NSE ₹0.05 ticks to prevent Zerodha RMS rejections.
- Detailed specs: [docs/stock_futures_desk.md](file:///d:/code/sharads_intraday_option_selling_desk/docs/stock_futures_desk.md).

### 3. Desk 3: Weekly Monthly-Fibonacci ETF Swing Trading Desk
- **Low-Frequency Positional Momentum**: Tailored for capital preservation with ~10–12 trades per year and ~3.5 months average holding period.
- **Monthly Fibonacci Pivots**: Uses prior month's High, Low, and Close to calculate $P = (H+L+C)/3$ and $S1 = P - 0.382\times(H-L)$.
- **Strict Weekly Closing Rule**:
  - **Entry**: Weekly Friday close $> P$. Executed on Monday 09:16 AM open.
  - **Exit / Stop Loss**: Weekly Friday close $< S1$. Executed on Monday 09:16 AM open. Eliminates mid-week intraday noise shakeouts.
- **Configured Capital**: ₹10,00,000 divided across 3 concurrent ETF slots (₹3.33 Lakhs per ETF).
- **Execution & Safeguards**: CNC Delivery equity product (zero leverage, zero margin calls) with marketable limit orders (0.5% cap) to protect against bid-ask slippage.
- **Docker & Daemon Support**: Runs as a daemon scheduling Friday 15:20 IST preview scans and Monday 09:16 IST rebalances.
- CLI runner: `python scripts/run_weekly_etf_desk.py --scan` or `--rebalance` or `--loop`.

### 4. Zero-Hit Off-Market Safety & Auth Core
- **Zero Kite API Hits Off-Market**: All desks verify `is_market_open()` before hitting any Kite API endpoints. On weekends, holidays, and outside 09:15–15:30 IST, all background polling loops sleep automatically without touching broker infrastructure.
- **Automated Headless TOTP**: Daily login via `pyotp` RFC 6238 without manual browser interaction. Proactive 08:00 AM IST token renewal.
- **Atomic Inter-Process Lock**: Safe file-locking ensures concurrent desks do not conflict over TOTP logins or token files.

---

## Setup & Installation

### Prerequisites
- Python $\ge 3.10$
- [uv](https://github.com/astral-sh/uv) package manager
- Zerodha Kite Connect developer account

```bash
# 1. Clone the repository
git clone https://github.com/sharadprsnwithai/nifty_strangle_desk.git
cd nifty_strangle_desk

# 2. Set up virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
```

### Required `.env` Variables:
```ini
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_USER_ID=your_user_id
KITE_PASSWORD=your_password
KITE_TOTP_KEY=your_base32_totp_secret

# Telegram Notifications
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

---

## CLI Usage Guide

### 1. Account Positions Viewer
```bash
# Display formatted positions table
uv run python main.py

# Filter for open positions only
uv run python main.py --open-only

# View intraday day positions
uv run python main.py --day

# Force fresh Kite login and token renewal
uv run python main.py --force-login
```

---

### 2. Desk 1: NIFTY Adaptive Short Strangle Desk
```bash
# Run Paper Trading simulation (10 Lots, ATR 1.25x strikes, Morning Filter)
uv run python run_strategy.py --paper --lots 10 --strike-mode ATR --atr-multiplier 1.25 --morning-filter

# Single evaluation pass
uv run python run_strategy.py --paper --once

# Live Production Trading via Kite Connect
uv run python run_strategy.py --live --lots 12 --order-type LIMIT --limit-buffer-pct 0.05 --max-loss 12000 --single-leg-exit --trail-cost --morning-filter --strike-mode ATR --atr-multiplier 1.25

# Historical 3-Year Adaptive Backtest
uv run python scripts/backtest_nifty_3y.py
```

---

### 3. Desk 2: Stock Futures Breakout Desk
```bash
# Run Paper Trading daemon (2 Lots, Max 2 Concurrent Positions, 2.25% Target, 1.0% SL)
uv run python scripts/run_stock_futures_desk.py --paper --lots 2 --max-concurrent 2

# Execute single scan pass
uv run python scripts/run_stock_futures_desk.py --once

# Run Live Production Desk
uv run python scripts/run_stock_futures_desk.py --live --lots 2 --max-concurrent 2 --order-type LIMIT --target-pct 0.0225 --sl-pct 0.010

# Dynamic Universe Rehydration from NSE India
uv run python scripts/rehydrate_momentum_universe.py
```

---

### 4. Desk 3: Weekly Monthly-Fibonacci ETF Swing Trading Desk
```bash
# Execute weekend preview scan (prints Fibonacci pivots & qualification status)
uv run python scripts/run_weekly_etf_desk.py --scan

# Run Live Production daemon (Mon-Fri continuous scheduler)
uv run python scripts/run_weekly_etf_desk.py --live --capital 1000000 --slots 3 --loop

# Execute immediate rebalance
uv run python scripts/run_weekly_etf_desk.py --live --rebalance
```

---

## Docker & Container Deployment

The platform is fully containerized using Docker and Docker Compose with `Asia/Kolkata` timezone pre-configured.

### Multi-Desk `docker-compose.yml` Services:
- `strangle-desk`: NIFTY Adaptive Short Strangle Desk (Live 10 Lots).
- `stock-futures-desk`: Nifty 200 Momentum 30 Stock Futures Desk (Live 2 Lots).
- `etf-swing-desk`: Weekly Monthly-Fibonacci ETF Swing Desk (Live Rs 10 Lakhs).

```bash
# Start all desks in the background
docker compose up -d

# Build or update images
docker compose build

# Stream logs for a specific desk
docker compose logs -f stock-futures-desk
docker compose logs -f strangle-desk
docker compose logs -f etf-swing-desk

# Stop all desks
docker compose down
```

---

## Testing & Quality Assurance

The codebase features comprehensive unit test coverage with **179 automated tests**:

```bash
uv run pytest
```

### Coverage Highlights:
- **Authentication**: Headless TOTP generation, token validation, inter-process lock acquisition.
- **Market Hours**: NSE exchange calendar, weekends, pre-market, and holiday bypass guards.
- **Stock Futures**: C1–C5 consolidation base rules, volume ratio breakouts, order manager execution, and target/SL calculations.
- **ETF Swing Desk**: Monthly Fibonacci pivot calculations, weekly Friday close evaluation, Monday order rebalancing.
- **Adaptive Strangle**: Daily ATR strike calculation, single-leg stop outs, cost trailing, and 14:45 cutoff rules.

---

## Project Structure

```
├── config.py                      # Core Kite configuration loader
├── main.py                        # Positions viewer entrypoint
├── run_strategy.py                # Strangle Desk CLI entrypoint
├── docker-compose.yml             # Multi-service container definitions
├── Dockerfile                     # Optimized Python 3.12 + uv container
├── kite/                          # Zerodha Kite Connect client & auth
│   ├── auth.py                    # Automated headless TOTP login
│   └── client.py                  # Token caching & atomic process lock
├── strategy/                      # Trading strategies & shared modules
│   ├── holidays.py                # NSE calendar & zero-hit market hours guard
│   ├── logger.py                  # Rotating file loggers
│   ├── stock_futures/             # Desk 2: Stock Futures Engine & Models
│   │   ├── config.py
│   │   ├── desk.py
│   │   ├── engine.py
│   │   ├── models.py
│   │   ├── order_manager.py
│   │   └── rehydrator.py
│   └── etf_swing/                 # Desk 3: Weekly ETF Swing Engine & Scanner
│       ├── config.py
│       ├── desk.py
│       ├── order_manager.py
│       └── scanner.py
├── scripts/                       # Runners, backtesters, and utilities
│   ├── run_stock_futures_desk.py
│   ├── run_weekly_etf_desk.py
│   ├── rehydrate_momentum_universe.py
│   └── backtest_nifty_3y.py
├── docs/                          # Detailed strategy documentation
│   └── stock_futures_desk.md
├── tests/                         # Pytest test suite (179 tests)
│   ├── test_stock_futures/
│   ├── test_etf_swing.py
│   └── ...
└── data/                          # State JSON files, caches, and universes
```
