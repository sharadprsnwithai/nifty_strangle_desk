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
     +--------------------------------------------+--------------------------------------------+
     |                                            |                                            |
+---------------------------------+      +---------------------------------+      +---------------------------------+
|             DESK 1              |      |             DESK 2              |      |             DESK 3              |
|      NIFTY Adaptive Strangle    |      |   Weekly Fibonacci ETF Swing    |      |   MCX SILVERM Evening Strangle  |
|        (Option Selling)         |      |          (Cash Swing)           |      |        (Commodity Strangle)     |
+---------------------------------+      +---------------------------------+      +---------------------------------+
| • Dynamic 1.25x ATR Strikes     |      | • Nifty, Junior, Gold, Silver   |      | • MCX SILVERM (5kg Mini Lots)   |
| • 09:15-10:00 Morning Filter    |      | • Monthly Fibonacci P & S1      |      | • 1.5% OTM Post-US Open Strike  |
| • Single-Leg Exit + Cost Trail  |      | • Friday Weekly Close Trigger   |      | • 20:00 Entry / 23:15 EOD Exit  |
| • 80% Decay Target / 10k Loss   |      | • Monday 09:16 AM Rebalance     |      | • Single-Leg 40% SL + Cost Trail|
| • Mandatory 14:45 EOD Exit      |      | • CNC Delivery / Zero Leverage  |      | • SL Dynamic from Number of Lots|
| • Paper & Live Execution Modes  |      | • Rs 10 Lakhs Portfolio Ballast |      | • 0 Overnight / Devolvement Risk|
+---------------------------------+      +---------------------------------+      +---------------------------------+
     |                                            |                                            |
     +--------------------------------------------+--------------------------------------------+
                                                  |
                           +---------------------------------------------+
                           |         Shared Core Infrastructure          |
                           +---------------------------------------------+
                           | • Headless RFC 6238 TOTP 2FA Authentication |
                           | • Atomic Inter-Process Lock (Token Safety)  |
                           | • NSE & MCX Trading Holiday / Hours Guards  |
                           | • Real-Time Rich HTML Telegram Notifier     |
                           | • State Persistence (Crash-Resilient JSON)  |
                           | • Full Pytest Test Suite (194/194 Passing)  |
                           +---------------------------------------------+
```

---

## Features

### 1. Desk 1: 10:00 AM NIFTY Adaptive Short Strangle Desk
- **Dynamic ATR Strike Selection**: Uses 14-day Daily ATR to position OTM strikes at $1.25\times\text{ATR}$ distance (rounded to nearest 50 points), automatically widening in high volatility and tightening in calm regimes.
- **Morning Range Trend Filter (09:15–10:00 IST)**: Skips days where the opening 45-minute range $\ge 0.60\%$ of spot, preventing shorting into violent trending market opens.
- **Single-Leg Exit with Cost Trailing**: When a losing leg hits +50% SL or ₹10,000 loss, only the losing leg is squared off. The winning leg remains open and its stop-loss is immediately moved to breakeven (`cost`), allowing theta decay to cushion drawdowns.
- **Circuit Breakers**: Combined portfolio ₹10,000 risk cap, 80% premium decay lock-in, and mandatory 14:45 IST cutoff exit.

### 2. Desk 2: Weekly Monthly-Fibonacci ETF Swing Trading Desk
- **Low-Frequency Positional Momentum**: Tailored for capital preservation with ~10–12 trades per year and ~3.5 months average holding period.
- **Monthly Fibonacci Pivots**: Uses prior month's High, Low, and Close to calculate $P = (H+L+C)/3$ and $S1 = P - 0.382\times(H-L)$.
- **Strict Weekly Closing Rule**:
  - **Entry**: Weekly Friday close $> P$. Executed on Monday 09:16 AM open.
  - **Exit / Stop Loss**: Weekly Friday close $< S1$. Executed on Monday 09:16 AM open. Eliminates mid-week intraday noise shakeouts.
- **Configured Capital**: ₹10,00,000 divided across 3 concurrent ETF slots (₹3.33 Lakhs per ETF).
- **Execution & Safeguards**: CNC Delivery equity product (zero leverage, zero margin calls) with marketable limit orders (0.5% cap) to protect against bid-ask slippage.
- **Docker & Daemon Support**: Runs as a daemon scheduling Friday 15:20 IST preview scans and Monday 09:16 IST rebalances.
- CLI runner: `python scripts/run_weekly_etf_desk.py --scan` or `--rebalance` or `--loop`.

### 3. Desk 3: MCX SILVERM US Post-Open IV Crush Evening Strangle Desk
- **Instrument**: MCX SILVERM (Silver Mini, 5 kg lot size) options.
- **US Session Timing**: Enters at **20:00 IST** after the initial US opening macro volatility subsides, capturing the steep evening Implied Volatility (IV) crush and Theta decay until **23:15 IST**.
- **1.5% OTM Strike Selection**: Dynamically resolves active monthly futures spot and sells 1.5% OTM Call and Put strikes (rounded to nearest 500/1000 step).
- **Single-Leg 40% SL & Cost Trailing**: Single-leg 40% stop loss. When one leg stops out or decays by $\ge 30\%$, the surviving leg's SL is automatically trailed to its entry price (cost).
- **Dynamic Risk Cap**: Total portfolio maximum loss is calculated directly from the number of lots (`sl_per_lot * lots`, e.g., ₹3,000/lot × 2 lots = ₹6,000 risk cap).
- **Zero Overnight & Devolvement Risk**: Mandatorily closes all positions at 23:15 IST every evening, completely avoiding overnight COMEX price gaps and the MCX physical delivery tender period.
- **CLI Runner**: `python scripts/run_silver_desk.py --live --lots 2 --loop`

### 5. Zero-Hit Off-Market Safety & Auth Core
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

### 3. Desk 2: Weekly Monthly-Fibonacci ETF Swing Trading Desk
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
- `etf-swing-desk`: Weekly Monthly-Fibonacci ETF Swing Desk (Live Rs 10 Lakhs).
- `silver-desk`: MCX SILVERM Evening Strangle Desk (Live 2 Lots).

```bash
# Start all desks in the background
docker compose up -d

# Build or update images
docker compose build

# Stream logs for a specific desk
docker compose logs -f strangle-desk
docker compose logs -f etf-swing-desk
docker compose logs -f silver-desk

# Stop all desks
docker compose down
```

---

## Testing & Quality Assurance

The codebase features comprehensive unit test coverage with **194 automated tests**:

```bash
uv run pytest
```

### Coverage Highlights:
- **Authentication**: Headless TOTP generation, token validation, inter-process lock acquisition.
- **Market Hours**: NSE exchange calendar, weekends, pre-market, and holiday bypass guards.
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
│   ├── etf_swing/                 # Weekly ETF Swing Engine & Scanner
│   │   ├── config.py
│   │   ├── desk.py
│   │   ├── order_manager.py
│   │   └── scanner.py
│   └── silver_options/            # MCX SILVERM Evening Strangle Desk
├── scripts/                       # Runners, backtesters, and utilities
│   ├── run_silver_desk.py
│   ├── run_weekly_etf_desk.py
│   └── backtest_nifty_3y.py
├── tests/                         # Pytest test suite
│   ├── test_silver_options/
│   ├── test_etf_swing.py
│   └── ...
└── data/                          # State JSON files, caches, and universes
```
