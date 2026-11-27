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
|      NIFTY Adaptive Strangle    |      |   Weekly Fibonacci ETF Swing    |      |   NIFTY 30m ORB Directional     |
|        (Option Selling)         |      |          (Cash Swing)           |      |     (Directional Selling)       |
+---------------------------------+      +---------------------------------+      +---------------------------------+
| • Dynamic 1.25x ATR Strikes     |      | • Nifty, Junior, Gold, Silver   |      | • 30m ORB (09:45) + TWAP + EMA  |
| • 09:15-10:00 Morning Filter    |      | • Monthly Fibonacci P & S1      |      | • 100 OTM Naked + 50% SL        |
| • Single-Leg Exit + Cost Trail  |      | • Monday 09:16 AM Rebalance     |      | • 5m ADX>=20 & ORB<=0.65%       |
| • 80% Decay Target / 10k Loss   |      | • Monday 09:16 AM Complete Run  |      | • 30% Cost Trail / 75% Decay    |
| • Freeze-Split + 14:45 EOD Exit |      | • CNC Delivery / Zero Leverage  |      | • Completed-Bar + 15:15 EOD     |
| • Paper & Live Isolated State   |      | • Rs 10 Lakhs Portfolio Ballast |      | • 5 Lots (Rs 10L) / 0 Overnight |
+---------------------------------+      +---------------------------------+      +---------------------------------+
     |                                            |                                            |
     +--------------------------------------------+--------------------------------------------+
                                                  |
                           +---------------------------------------------+
                           |         Shared Core Infrastructure          |
                           +---------------------------------------------+
                           | • Headless RFC 6238 TOTP 2FA Authentication |
                           | • Atomic Inter-Process Lock (.env Sync)     |
                           | • NSE & MCX Calendars + Cutoff Guards       |
                           | • Real-Time Rich HTML Telegram Notifier     |
                           | • Atomic Crash-Resilient State Persistence  |
                           | • Full Pytest Unit & Regression Suite       |
                           +---------------------------------------------+
```

---

## Features

### 1. Desk 1: 10:00 AM NIFTY Adaptive Short Strangle Desk
- **Dynamic ATR & OTM Strike Selection**: Uses prior-day 14-day Daily ATR to position OTM strikes at $\max(1.25\times\text{ATR}, 150)$ points (nearest 50 step), preferring non-zero LTP strikes and falling back cleanly to `PRICE` mode (`>= ₹30` OTM strikes) when needed. Automatically rolls 0-DTE expiries to next week.
- **Morning Range Trend Filter (09:15–09:59 IST)**: Skips sessions where pre-10:00 AM range is $\ge 0.60\%$ of open, with `TokenException` propagation so auth failures are never masked.
- **Single-Leg Exit & Pending Cost Trailing**: When one leg hits +50% SL or `SINGLE_LEG_10K_EXIT` closes the losing leg on a ₹10,000 portfolio breach, only the losing leg is exited (without same-tick cascading on the surviving leg). The surviving leg trails to entry cost immediately if profitable or arms `cost_trail_pending` to lock breakeven once it drops below entry.
- **Execution Hardening**: Concurrent `ThreadPoolExecutor` CE/PE entry protected by `threading.RLock`, automatic NIFTY freeze-limit (`1755` qty) order slicing with partial-slice rollback protection, partial-fill exit tracking (`exit_filled_qty`) with `MARKET` escalation, atomic `.tmp` state persistence with `PAPER` (`paper_trades_today.json`) vs `LIVE` (`trades_today.json`) isolation, and periodic 60s LIVE broker reconciliation.
- **Circuit Breakers & Cutoff**: Portfolio max loss cap (`--max-loss`, default ₹10,000), 80% decay target, and configurable intraday cutoff exit (`--exit-time`, default `14:45 IST`) with market-hours and post-cutoff entry guards.

### 2. Desk 2: Weekly Monthly-Fibonacci ETF Swing Trading Desk
- **Low-Frequency Positional Momentum**: Tailored for capital preservation with ~10–12 trades per year and ~3.5 months average holding period.
- **Monthly Fibonacci Pivots**: Uses prior month's High, Low, and Close to calculate $P = (H+L+C)/3$ and $S1 = P - 0.382\times(H-L)$.
- **Strict Weekly Closing Rule**:
  - **Entry**: Weekly Friday close $> P$. Executed on Monday 09:16 AM open.
  - **Exit / Stop Loss**: Weekly Friday close $< S1$. Executed on Monday 09:16 AM open. Eliminates mid-week intraday noise shakeouts.
- **Configured Capital**: ₹10,00,000 divided across 3 concurrent ETF slots (₹3.33 Lakhs per ETF).
- **Execution & Safeguards**: CNC Delivery equity product (zero leverage, zero margin calls) with marketable limit orders (0.5% cap) to protect against bid-ask slippage.
- **Docker & Daemon Support**: Runs as a daemon scheduling the complete weekly scan and CNC rebalance every Monday at 09:16 IST.
- CLI runner: `python scripts/run_weekly_etf_desk.py --scan` or `--rebalance` or `--loop`.

### 3. Desk 3: NIFTY 30m ORB Directional Option Selling Desk
- **Directional Breakout + Trend Filters**: Evaluates completed 5-minute NIFTY 50 bars after the `09:15–09:44` 30-minute ORB (`ORB High`/`ORB Low`) aligned with intraday `TWAP`, `EMA(9/21)`, `5m ADX(14) >= 20`, and `ORB Width <= 0.65%`.
- **100 OTM Naked Execution**: Sells `ATM - 100 PE` on bullish breakouts or `ATM + 100 CE` on bearish breakdowns (auto-rolling 0-DTE to next week), sized at 5 lots (`₹10L` capital).
- **Risk Management & Verification**: `50%` SL with `30%` decay cost-trailing, `75%` target decay, `15:15 IST` EOD exit, `OrderManager` fill verification with `MARKET` escalation, 60s `LIVE` broker reconciliation, and isolated `PAPER`/`LIVE` state files.

### 4. Zero-Hit Off-Market Safety & Auth Core
- **Zero Kite API Hits Off-Market**: NSE desks verify `is_market_open()` (`09:15–15:30 IST` + `NSE_HOLIDAYS_2026` + fixed national closures) before hitting broker endpoints on holidays/weekends.
- **Automated Headless TOTP & Cross-Container Sync**: Daily login via `pyotp` RFC 6238 with proactive token validation (`08:00 AM` NSE) that adopts already-fresh `.env` tokens from peer containers before forcing a new login.
- **Atomic Inter-Process Lock**: File-locking (`data/.kite_auth.lock`) and per-desk `RLock` retries ensure concurrent containers (`strangle-desk`, `etf-swing-desk`, `directional-orb-desk`) never invalidate each other's active sessions.

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

# Live Production Trading via Kite Connect (customizable cutoff)
uv run python run_strategy.py --live --lots 12 --order-type LIMIT --limit-buffer-pct 0.05 --max-loss 12000 --single-leg-exit --trail-cost --morning-filter --strike-mode ATR --atr-multiplier 1.25 --exit-time 14:45

# Historical NIFTY Backtest CLI & 3-Year Adaptive Backtest
uv run python run_backtest.py --days 180 --strike-mode ATR --atr-multiplier 1.25 --morning-filter --exit-cutoff 14:45
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

### 4. Desk 3: NIFTY 30m ORB Directional Option Selling Desk
```bash
# Paper simulation (5 Lots for Rs 10L, 30m ORB 09:45, 100 OTM Naked, 50% SL, ADX>=20, ORB<=0.65%)
uv run python scripts/run_directional_orb_desk.py --paper --lots 5

# Live Production Execution
uv run python scripts/run_directional_orb_desk.py --live --lots 5 --order-type LIMIT --limit-buffer-pct 0.05

# Backtest (124-day cache & 20-day actual Kite VIX)
uv run python scripts/backtest_directional_orb.py --single --orb-end 09:45 --otm-offset 100 --leg-sl 0.50 --lots 5
uv run python scripts/backtest_directional_orb.py --fetch-kite --use-vix --days 20 --single --orb-end 09:45 --otm-offset 100 --leg-sl 0.50 --lots 5
```

---

## Docker & Container Deployment

The platform is fully containerized using Docker and Docker Compose with `Asia/Kolkata` timezone pre-configured.

### Multi-Desk `docker-compose.yml` Services:
- `strangle-desk`: NIFTY Adaptive Short Strangle Desk (Paper 12 Lots).
- `etf-swing-desk`: Weekly Monthly-Fibonacci ETF Swing Desk (Live Rs 10 Lakhs).
- `directional-orb-desk`: NIFTY 30m ORB Directional Option Selling Desk (Paper 5 Lots, 100 OTM Naked, 50% SL, ADX>=20, ORB<=0.65%).

```bash
# Start all desks in the background
docker compose up -d

# Build or update images
docker compose build

# Stream logs for a specific desk
docker compose logs -f strangle-desk
docker compose logs -f etf-swing-desk
docker compose logs -f directional-orb-desk

# Stop all desks
docker compose down
```

---

## Testing & Quality Assurance

The codebase features comprehensive automated unit and regression test coverage across all desks:

```bash
uv run pytest
```

### Coverage Highlights:
- **Authentication & Concurrency**: Headless TOTP generation, token validation, `.env` adoption, `interprocess_auth_lock`, and thread-safe `RLock` retries.
- **Market Hours & Holidays**: NSE/MCX exchange calendars, fixed cross-year national holidays, weekends, and off-market guards.
- **NIFTY Adaptive Strangle**: Shifted prior-day ATR strike selection, freeze-limit slicing & rollback, partial-fill exit escalation, `SINGLE_LEG_10K_EXIT` anti-cascade, and broker reconciliation.
- **NIFTY 30m ORB Directional**: Completed-5m-bar guard, `ADX(14) >= 20` & `ORB <= 0.65%` filters, fill verification, and 60s `LIVE` broker reconciliation.
- **ETF Swing Desk**: Monthly Fibonacci pivot calculations, Monday `09:16` rebalance with token refresh.

---

## Project Structure

```
├── config.py                      # Core Kite configuration loader
├── main.py                        # Positions viewer entrypoint
├── run_strategy.py                # NIFTY Strangle Desk CLI entrypoint
├── run_backtest.py                # NIFTY Strangle Backtest CLI entrypoint
├── docker-compose.yml             # Multi-service container definitions
├── Dockerfile                     # Optimized Python 3.12 + uv container
├── kite/                          # Zerodha Kite Connect client & auth
│   ├── auth.py                    # Automated headless TOTP login
│   └── client.py                  # Token caching & atomic process lock
├── strategy/                      # Trading strategies & shared modules
│   ├── backtest.py                # NIFTY 5-minute Strangle simulator
│   ├── costs.py                   # NSE F&O statutory taxes & slippage
│   ├── directional_orb_desk.py    # NIFTY 30m ORB Directional Option Desk
│   ├── holidays.py                # NSE & MCX calendars & market hours guard
│   ├── logger.py                  # Rotating file loggers
│   ├── option_chain.py            # NIFTY ATR/PRICE OTM strike discovery
│   ├── options_model.py           # Black-76 & fractional DTE pricing
│   ├── order_manager.py           # NSE execution & freeze-limit slicing
│   ├── strangle_desk.py           # NIFTY lifecycle, trailing & reconcile
│   ├── etf_swing/                 # Weekly ETF Swing Engine & Scanner
│   │   ├── config.py
│   │   ├── desk.py
│   │   ├── order_manager.py
│   │   └── scanner.py
├── scripts/                       # Runners, backtesters, and utilities
│   ├── backtest_directional_orb.py
│   ├── run_directional_orb_desk.py
│   ├── run_weekly_etf_desk.py
│   └── backtest_nifty_3y.py
├── tests/                         # Pytest test suite
│   ├── test_directional_orb_desk.py
│   ├── test_etf_swing.py
│   └── ...
└── data/                          # State JSON files, caches, and universes
```
