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
| • Dynamic 1.25x ATR Strikes     |      | • Nifty, Junior, Gold, Silver   |      | • MCX SILVERM (5kg) / SILVER    |
| • 09:15-10:00 Morning Filter    |      | • Monthly Fibonacci P & S1      |      | • 1.5% OTM Liquidity-Filtered   |
| • Single-Leg Exit + Cost Trail  |      | • Friday Weekly Close Trigger   |      | • [20:00, 23:10) Window & EOD   |
| • 80% Decay Target / 10k Loss   |      | • Monday 09:16 AM Rebalance     |      | • Single-Leg 40% SL + Cost Trail|
| • Freeze-Split + 14:45 EOD Exit |      | • CNC Delivery / Zero Leverage  |      | • Dynamic Lots*SL Cap & 0-DTE   |
| • Paper & Live Isolated State   |      | • Rs 10 Lakhs Portfolio Ballast |      | • 0 Overnight / Devolvement Risk|
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
- **Docker & Daemon Support**: Runs as a daemon scheduling Friday 15:20 IST preview scans and Monday 09:16 IST rebalances.
- CLI runner: `python scripts/run_weekly_etf_desk.py --scan` or `--rebalance` or `--loop`.

### 3. Desk 3: MCX SILVERM US Post-Open IV Crush Evening Strangle Desk
- **Instrument & Expiry Handling**: MCX `SILVERM` (`5 kg`/lot) or `SILVER` (`30 kg`/lot) options. Aligns underlying futures contract to active option expiry and skips 0-DTE option expiry days by default (`--no-skip-0dte` to override).
- **US Session Timing**: Enters in the `[20:00, 23:10)` IST window after initial US opening macro volatility subsides, capturing evening IV crush and theta decay until mandatory **23:10 IST** EOD square-off (never opens fresh legs at or after cutoff).
- **Liquidity-Filtered 1.5% OTM Strikes**: Selects OTM strikes (step 500 or 1000 above ₹1,20,000) gated by minimum OI (`>= 100`), non-empty bid/ask depth, and `<= 3%` bid-ask spread, scanning further OTM rather than stepping inward toward ATM.
- **Depth-Anchored Execution & Safe Escalation**: Anchors `LIMIT` orders to live `best_bid` / `best_ask` within `max_limit_slippage_pct` (1%). Urgent exits (`SL`, `MAX_PORTFOLIO_LOSS`, `EOD`, abort rollback) escalate unfilled balances to `MARKET`, while voluntary `TARGET_DECAY` exits remain limit-protected.
- **Single-Leg 40% SL, Pending Cost Trailing & Target Decay**: Tracks `exit_filled_lots` and weighted open/closed P&L (`effective_points`) accurately across partial fills, trails surviving legs via `cost_trail_pending` on stop-out or $\ge 30\%$ decay, and closes remaining legs at `70%` target decay (or supports dual-leg mode via `--no-single-leg-exit`).
- **Dynamic Risk Cap, Reconciliation & State Safety**: Portfolio max loss cap scales by lots (`sl_per_lot * lots`, e.g., ₹3,000/lot × 2 lots = ₹6,000), periodic 60s LIVE broker reconciliation detects manual Kite closes (`BROKER_RECONCILED`), pre-entry `19:45 IST` token validation avoids stale sessions, and `save_state()` uses atomic file replacement with isolated `silver_paper_trades_today.json` in `PAPER` mode.
- **Exact MCX Cost Modeling**: Computes per-leg brokerage, MCX exchange fee (`0.05%`), CTT (`0.0625%` sell), SEBI fee, stamp duty, and 18% GST via `calculate_silver_mcx_costs()`.
- **Zero Overnight & Devolvement Risk**: Mandatorily squares off all positions by 23:10 IST every evening, avoiding overnight COMEX gaps and MCX tender delivery risk.
- **CLI Runner**: `python scripts/run_silver_desk.py --live --lots 2 --loop`

### 4. Zero-Hit Off-Market Safety & Auth Core
- **Zero Kite API Hits Off-Market**: NSE desks verify `is_market_open()` (`09:15–15:30 IST` + `NSE_HOLIDAYS_2026` + fixed national closures) and MCX Silver verifies `is_mcx_trading_holiday(..., session="EVENING")` before hitting broker endpoints on holidays/weekends.
- **Automated Headless TOTP & Cross-Container Sync**: Daily login via `pyotp` RFC 6238 with proactive token validation (`08:00 AM` NSE / `19:45` MCX) that adopts already-fresh `.env` tokens from peer containers before forcing a new login.
- **Atomic Inter-Process Lock**: File-locking (`data/.kite_auth.lock`) and per-desk `RLock` retries ensure concurrent containers (`strangle-desk`, `etf-swing-desk`, `silver-desk`) never invalidate each other's active sessions.

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

### 4. Desk 3: MCX SILVERM Evening Strangle Desk
```bash
# Preview MCX SILVERM strikes, OI, and bid-ask spreads
uv run python scripts/run_silver_desk.py --dry-run

# Paper simulation (uses isolated data/silver_paper_trades_today.json)
uv run python scripts/run_silver_desk.py --paper --lots 2 --sl-per-lot 3000 --sl-pct 0.40 --entry-time 20:00 --exit-time 23:10 --loop

# Live Production daemon (2 lots, 6k max loss cap)
uv run python scripts/run_silver_desk.py --live --lots 2 --sl-per-lot 3000 --sl-pct 0.40 --strike-otm-pct 0.015 --entry-time 20:00 --exit-time 23:10 --loop

# Historical MCX SILVERM Backtests (with portfolio loss cap, 0-DTE roll & exact MCX taxes)
uv run python scripts/backtest_silver_strangle.py
uv run python scripts/backtest_silver_strangle_1y.py
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

The codebase features comprehensive automated unit and regression test coverage across all desks:

```bash
uv run pytest
```

### Coverage Highlights:
- **Authentication & Concurrency**: Headless TOTP generation, token validation, `.env` adoption, `interprocess_auth_lock`, and thread-safe `RLock` retries.
- **Market Hours & Holidays**: NSE/MCX exchange calendars, fixed cross-year national holidays, weekends, and off-market guards.
- **NIFTY Adaptive Strangle**: Shifted prior-day ATR strike selection, freeze-limit slicing & rollback, partial-fill exit escalation, `SINGLE_LEG_10K_EXIT` anti-cascade, and broker reconciliation.
- **MCX SILVERM Strangle**: OI & bid/ask depth liquidity gating, 0-DTE expiry skip, partial-exit `effective_points` P&L, limit-only `TARGET_DECAY`, cost trailing, and atomic state isolation.
- **ETF Swing Desk**: Monthly Fibonacci pivot calculations, weekly Friday close evaluation, Monday order rebalancing.

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
│   └── silver_options/            # MCX SILVERM Evening Strangle Desk
│       ├── chain.py
│       ├── config.py
│       ├── desk.py
│       ├── models.py
│       └── order_manager.py
├── scripts/                       # Runners, backtesters, and utilities
│   ├── run_silver_desk.py
│   ├── run_weekly_etf_desk.py
│   ├── backtest_nifty_3y.py
│   ├── backtest_silver_strangle.py
│   └── backtest_silver_strangle_1y.py
├── tests/                         # Pytest test suite
│   ├── test_silver_options/
│   ├── test_etf_swing.py
│   └── ...
└── data/                          # State JSON files, caches, and universes
```
