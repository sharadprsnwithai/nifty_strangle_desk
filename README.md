# Sharad's Intraday Option Selling Desk

A modular algorithmic trading desk connected to Zerodha Kite Connect API using automated headless TOTP 2FA.

## Features
- **Headless TOTP 2FA**: Automated daily login via `pyotp` RFC 6238 without manual browser interaction.
- **Smart Token Caching**: Caches and validates daily `access_token` in `.env` to avoid unnecessary logins.
- **10:00 AM NIFTY Short Strangle Strategy**: Automated strike discovery ($\ge ₹30.0$), 0 DTE $\to$ next week shift, independent $+50\%$ SL, $80\%$ decay target, 3:00 PM time exit, and dual Paper/Live execution.
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
# Standard 180-day backtest with 10 lots
uv run python run_backtest.py --days 180 --lots 10

# Custom slippage and output CSV file
uv run python run_backtest.py --days 90 --slippage 0.75 --lots 5 --output data/custom_log.csv
```

> **Data Caching**: Historical bars can be loaded from `HISTORICAL_CACHE_PATH` (if set), `data/nifty_5m_cache.parquet`, or fetched via Kite Connect in 60-day slices.

### 3. Run 10:00 AM Strangle Strategy Desk
Executes or simulates the 10:00 AM Short Strangle desk:
- **0 DTE Rule**: When today is Tuesday (weekly expiry), automatically selects next week's expiry.
- **Contract Specifications**: NIFTY lot size is 65 (10 lots = 650 shares).
- **Strike Selection**: Lowest OTM strikes with LTP $\ge ₹30.0$.
- **Risk Management**: Independent $+50\%$ Stop Loss per leg, independent $80\%$ decay profit target per leg, hard ₹10,000 per-leg max loss exit, and mandatory 3:00 PM square-off.
- **Order Execution Verification & Timeout**: Monitors entry order status for up to 5 minutes (`--order-timeout 300`). If unfilled, pending orders are automatically cancelled. If partially filled, pending portions are cancelled and filled shares are immediately squared off (`buy_to_cover`) to prevent unhedged or unmatched exposure.
- **Market Protection**: Uses marketable `LIMIT` orders with customizable price buffer (default: 5% off LTP) rounded to NSE ₹0.05 ticks to prevent Zerodha RMS freak-trade order rejections.
- **NSE Holiday Protection**: Automatically checks weekend and official NSE trading holiday calendars and skips non-trading sessions.
- **Telegram Alerts**: Real-time notifications on entry fills, SL/Max-Loss triggers, decay targets, and 15:00 summary.
- **State Recovery & Host Event Logging**: Crash-resilient tracking persisted to `data/trades_today.json`, and rotating detailed event logs persisted directly to `logs/strangle_desk.log` on the host machine.

```bash
# Dry-run strike lookup and strategy plan (without placing orders)
uv run python run_strategy.py --paper --dry-run

# Run Paper Trading simulation (polling loop, 5% limit order buffer, debug logging)
uv run python run_strategy.py --paper --lots 10 --order-type LIMIT --limit-buffer-pct 0.05 --log-level DEBUG

# Single evaluation pass (for tests or one-shot automation)
uv run python run_strategy.py --paper --once

# Execute Live Trading via Kite Connect with marketable limit orders
uv run python run_strategy.py --live --lots 1 --order-type LIMIT --max-loss 1000
```

### 4. Docker & Cloud VPS Deployment

The desk is fully containerized with Indian Standard Time (`Asia/Kolkata`) pre-configured, state volume mounts, and host event log streaming.

#### Using Docker Compose (Recommended)

1. **Configure Execution Mode in `docker-compose.yml`**:
   Choose your command in `docker-compose.yml`:
   ```yaml
   # 1-Lot Live Incubation (Recommended for week 1):
   command: ["--live", "--lots", "1", "--order-type", "LIMIT", "--limit-buffer-pct", "0.05", "--max-loss", "1000"]

   # 10-Lot Full Production Execution:
   # command: ["--live", "--lots", "10", "--order-type", "LIMIT", "--limit-buffer-pct", "0.05", "--max-loss", "10000"]
   ```

2. **Start the Container**:
   ```bash
   docker compose up -d --build
   ```

3. **Stream Real-Time Strategy Logs**:
   ```bash
   # From container stdout
   docker compose logs -f

   # Or inspect persisted log file on host machine directly
   tail -f logs/strangle_desk.log
   ```

4. **Stop or Restart**:
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
    --live --lots 1 --order-type LIMIT --limit-buffer-pct 0.05 --max-loss 1000
  ```

## Running Tests
```bash
uv run pytest -v
```
