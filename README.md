# Sharad's Intraday NIFTY Option Selling Desk

A modular algorithmic trading desk connected to Zerodha Kite Connect API using automated headless TOTP 2FA.

## Features
- **Headless TOTP 2FA**: Automated daily login via `pyotp` RFC 6238 without manual browser interaction.
- **Smart Token Caching**: Caches and validates daily `access_token` in `.env` to avoid unnecessary logins.
- **10:00 AM NIFTY Adaptive Short Strangle Strategy**: Dynamic ATR strike selection ($1.25\times\text{ATR}$ offset), 09:15–10:00 AM morning range trend filter (skips violent opening trend days $\ge 0.60\%$), 0 DTE $\to$ next week shift, combined portfolio stop-loss exit (₹10,000 max loss across both legs), single-leg exit with breakeven cost trailing on the winning leg, 80% decay target, mandatory 14:45 IST cutoff time exit, and dual Paper/Live execution.
- **Crash-Resilient State Machine**: Persists active legs and trade state to handle restarts gracefully.
- **Realistic Backtesting Engine**: Day-by-day continuous fractional-DTE option simulation accounting for exact NSE transaction taxes, STT, SEBI fees, GST, brokerage, and execution slippage.
- **Comprehensive Positions Viewer**: Displays both Net and Day positions with quantity, entry averages, LTP, MTM, and P&L metrics.
- **Dual Output**: Formatted terminal tables or raw structured `--json` output.

## Setup & Installation

1. Make sure Python `>= 3.10` and `uv` are available.
2. Open or navigate to this repository:
   ```bash
   cd d:\code\sharads_intraday_option_selling_desk
   ```
3. Create virtual environment and install dependencies:
   ```bash
   uv venv
   uv pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your Zerodha credentials:
   ```bash
   cp .env.example .env
   ```
   Required keys:
   - `KITE_API_KEY`: Zerodha developer app API key
   - `KITE_API_SECRET`: Zerodha developer app API secret
   - `KITE_USER_ID`: Zerodha trading account user ID
   - `KITE_PASSWORD`: Zerodha trading account password
   - `KITE_TOTP_KEY`: Base32 2FA secret key from Zerodha TOTP setup

## Usage

### 1. View Current Account Positions
```bash
# Tabular positions view
uv run python main.py

# Open positions only
uv run python main.py --open-only

# Intraday day positions view
uv run python main.py --day

# Structured JSON output
uv run python main.py --json

# Force fresh login (regenerate access token)
uv run python main.py --force-login
```

### 2. Run Historical Backtester
Simulates the 10:00 AM Short Strangle across historical 5-minute NIFTY candles with continuous fractional DTE option pricing, exact NSE statutory taxes & charges, and execution slippage:

```bash
# Standard 3-year adaptive backtest (1.25x ATR strikes + Morning Filter, default: Rs 10,000 portfolio max loss)
uv run python scripts/backtest_nifty_3y.py

# CLI backtest with custom options
uv run python run_backtest.py --days 180 --lots 10 --strategy-max-loss 10000 --strike-mode ATR --atr-multiplier 1.25 --morning-filter --morning-filter-pct 0.60

# Custom slippage, portfolio stop-loss, and output CSV file
uv run python run_backtest.py --days 90 --slippage 0.75 --lots 5 --strategy-max-loss 5000 --output data/custom_log.csv
```

> **Data Caching**: Historical bars can be loaded from `HISTORICAL_CACHE_PATH` (if set), `data/nifty_5m_cache.parquet`, or fetched via Kite Connect in 60-day slices.

### 3. Run 10:00 AM Adaptive Strangle Strategy Desk
Executes or simulates the 10:00 AM Short Strangle desk:
- **0 DTE Rule**: When today is expiry day, automatically selects next week's expiry.
- **Dynamic ATR Strike Selection**: Automatically calculates 14-day Daily ATR and places OTM Call and Put strikes at $1.25\times\text{Daily ATR}$ distance from spot (rounded to nearest 50 points). Dynamically expands strike distance in high-volatility regimes and tightens in low-volatility regimes. (Falls back to target price $\ge ₹30.0$ if `--strike-mode PRICE` is specified).
- **Morning Range Trend Filter (09:15–10:00 IST)**: Evaluates the opening 45-minute high-low range. If range $\ge 0.60\%$ of spot, the desk skips entry for the day (`SKIPPED_TREND_DAY`), avoiding entry on violent directional opening thrust days. Can be disabled via `--no-morning-filter`.
- **Risk Management (Single-Leg Exit with Cost Trailing)**: 
  - **Single-Leg Stop-Loss Exit (+50% SL / ₹10,000 Leg Stop)**: When a leg hits its +50% SL (`entry * 1.50`) or breaches ₹10,000 loss, **only that losing leg is squared off** (`STOP_LOSS`). The winning leg remains **OPEN** to continue decaying towards its 80% decay target or cutoff!
  - **Breakeven Trailing on Winning Leg**: Upon a stop-out of the losing leg, the winning leg's stop-loss is immediately moved to its entry price (`cost`), eliminating whipsaw reversal risk while letting theta decay cushion the drawdown.
  - **Portfolio ₹10,000 Circuit Breaker**: If combined portfolio loss ever reaches ₹10,000 while both legs are open, the desk closes **only the losing leg** (`SINGLE_LEG_10K_EXIT`), ensuring the winning leg is never sacrificed prematurely.
  - **80% Premium Decay Target**: When a leg reaches its 80% decay target (`entry * 0.20`), it is locked in (`DECAY_TARGET`).
  - **14:45 IST Time Exit**: Mandatory 14:45 IST square-off for any remaining positions, avoiding 3:00 PM expiry gamma spikes and flash gap moves.
- **Automated 08:00 AM IST Daily TOTP Login Renewal**: Proactively renews the Zerodha Kite access token via headless TOTP authentication every morning at 08:00 AM IST on trading days. Persists the token to `.env` and notifies Telegram.
- **Order Execution Verification & Timeout**: Monitors entry order status for up to 5 minutes (`--order-timeout 300`). If unfilled, pending orders are automatically cancelled. If partially filled, pending portions are cancelled and filled shares are immediately squared off (`buy_to_cover`) to prevent unhedged or unmatched exposure.
- **Market Protection**: Uses marketable `LIMIT` orders with customizable price buffer (default: 5% off LTP) rounded to NSE ₹0.05 ticks to prevent Zerodha RMS freak-trade order rejections.
- **NSE Holiday Protection**: Automatically checks weekend and official NSE trading holiday calendars and skips non-trading sessions.
- **Telegram Alerts**: Real-time notifications on entry fills, morning trend skips, combined portfolio stop-loss triggers, decay targets, and 14:45 cutoff summary.
- **State Recovery & Host Event Logging**: Crash-resilient tracking persisted to `data/trades_today.json`, and rotating detailed event logs persisted directly to `logs/strangle_desk.log` on the host machine.

```bash
# Execute on-demand morning TOTP login renewal immediately (and notify Telegram)
uv run python run_strategy.py --morning-login

# Dry-run strike lookup and strategy plan (with ATR strikes and morning filter)
uv run python run_strategy.py --paper --dry-run

# Run Paper Trading simulation (10 lots, ATR 1.25x strikes, 0.60% morning filter, 5% limit order buffer)
uv run python run_strategy.py --paper --lots 10 --order-type LIMIT --limit-buffer-pct 0.05 --morning-filter --morning-filter-pct 0.60 --strike-mode ATR --atr-multiplier 1.25 --log-level DEBUG

# Single evaluation pass (for tests or one-shot automation)
uv run python run_strategy.py --paper --once

# Execute Live Trading via Kite Connect with marketable limit orders & portfolio stop loss
uv run python run_strategy.py --live --lots 10 --order-type LIMIT --strategy-max-loss 10000 --morning-filter --morning-filter-pct 0.60 --strike-mode ATR --atr-multiplier 1.25
```

### 4. Docker & Cloud VPS Deployment

The desk is fully containerized with Indian Standard Time (`Asia/Kolkata`) pre-configured, state volume mounts, and host event log streaming.

#### Using Docker Compose (Recommended)

1. **Start the Intraday Strangle Desk**:
   ```bash
   docker compose up -d --build
   ```

2. **Stream Real-Time Strategy Logs**:
   ```bash
   # From container stdout
   docker compose logs -f strangle-desk

   # Or inspect persisted log file on host machine directly
   tail -f logs/strangle_desk.log
   ```

3. **Stop or Restart**:
   ```bash
   docker compose restart
   docker compose down
   ```

#### Using Standalone Docker Run

- **Dry Run Verification**:
  ```bash
  docker run --rm --env-file .env -v "${PWD}/data:/app/data" -v "${PWD}/logs:/app/logs" nifty-strangle-desk:test --paper --dry-run
  ```

- **Live Production Trading**:
  ```bash
  docker run -d --name nifty_desk --restart unless-stopped \
    --env-file .env \
    -v "${PWD}/data:/app/data" \
    -v "${PWD}/logs:/app/logs" \
    nifty-strangle-desk:test \
    --live --lots 10 --order-type LIMIT --limit-buffer-pct 0.05 --max-loss 10000 --single-leg-exit --trail-cost --morning-filter --morning-filter-pct 0.60 --strike-mode ATR --atr-multiplier 1.25
  ```

## Running Tests
```bash
uv run pytest -v
```
