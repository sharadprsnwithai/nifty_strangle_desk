# Sharad's Intraday Option Selling Desk

A modular algorithmic trading desk connected to Zerodha Kite Connect API using automated headless TOTP 2FA.

## Features
- **Headless TOTP 2FA**: Automated daily login via `pyotp` RFC 6238 without manual browser interaction.
- **Smart Token Caching**: Caches and validates daily `access_token` in `.env` to avoid unnecessary logins.
- **10:00 AM NIFTY Short Strangle Strategy**: Automated strike discovery ($\ge ₹30.0$), 0 DTE $\to$ next week shift, 5-minute portfolio stop-loss exit (₹10,000 max strategy loss across all legs), independent $+50\%$ SL, $80\%$ decay target, 3:00 PM time exit, and dual Paper/Live execution.
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
# Standard 180-day backtest with 10 lots (default: Rs 10,000 portfolio max loss)
uv run python run_backtest.py --days 180 --lots 10 --strategy-max-loss 10000

# Custom slippage, portfolio stop-loss, and output CSV file
uv run python run_backtest.py --days 90 --slippage 0.75 --lots 5 --strategy-max-loss 5000 --output data/custom_log.csv
```

> **Data Caching**: Historical bars can be loaded from `HISTORICAL_CACHE_PATH` (if set), `data/nifty_5m_cache.parquet`, or fetched via Kite Connect in 60-day slices.

### 3. Run 10:00 AM Strangle Strategy Desk
Executes or simulates the 10:00 AM Short Strangle desk:
- **0 DTE Rule**: When today is Tuesday (weekly expiry), automatically selects next week's expiry.
- **Contract Specifications**: NIFTY lot size is 65 (10 lots = 650 shares).
- **Strike Selection**: Lowest OTM strikes with LTP $\ge ₹30.0$.
- **Risk Management**: 
  - **Portfolio Max Loss Exit**: ₹10,000 maximum daily loss across all open legs evaluated at every 5-minute check (closes all legs immediately on breach with reason `STRATEGY_MAX_LOSS_10K`, eliminating whipsaw losses and cutting max drawdown by 45.4%).
  - **Stop Loss & Target**: Independent $+50\%$ Stop Loss per leg (`entry * 1.50`) and independent $80\%$ decay profit target per leg (`entry * 0.20`).
  - **Time Exit**: Mandatory 3:00 PM (15:00 IST) market square-off for any remaining positions.
- **Automated 08:00 AM IST Daily TOTP Login Renewal**: Proactively renews the Zerodha Kite access token via headless TOTP authentication every morning at 08:00 AM IST on trading days. Persists the token to `.env` and notifies Telegram, ensuring all subsequent daily activities (10:00 AM strike scan, order entries, live polling) use a freshly minted, validated credential.
- **Order Execution Verification & Timeout**: Monitors entry order status for up to 5 minutes (`--order-timeout 300`). If unfilled, pending orders are automatically cancelled. If partially filled, pending portions are cancelled and filled shares are immediately squared off (`buy_to_cover`) to prevent unhedged or unmatched exposure.
- **Market Protection**: Uses marketable `LIMIT` orders with customizable price buffer (default: 5% off LTP) rounded to NSE ₹0.05 ticks to prevent Zerodha RMS freak-trade order rejections.
- **NSE Holiday Protection**: Automatically checks weekend and official NSE trading holiday calendars and skips non-trading sessions.
- **Telegram Alerts**: Real-time notifications on entry fills, SL/Max-Loss triggers, decay targets, and 15:00 summary.
- **State Recovery & Host Event Logging**: Crash-resilient tracking persisted to `data/trades_today.json`, and rotating detailed event logs persisted directly to `logs/strangle_desk.log` on the host machine.

```bash
# Execute on-demand morning TOTP login renewal immediately (and notify Telegram)
uv run python run_strategy.py --morning-login

# Dry-run strike lookup and strategy plan (without placing orders)
uv run python run_strategy.py --paper --dry-run

# Run Paper Trading simulation (polling loop, 5% limit order buffer, debug logging)
uv run python run_strategy.py --paper --lots 10 --order-type LIMIT --limit-buffer-pct 0.05 --log-level DEBUG

# Single evaluation pass (for tests or one-shot automation)
uv run python run_strategy.py --paper --once

# Execute Live Trading via Kite Connect with marketable limit orders & portfolio stop loss
uv run python run_strategy.py --live --lots 1 --order-type LIMIT --strategy-max-loss 1000
```

### 4. Run NIFTY Positional Evergreen Condor Desk
A monthly positional option selling desk based on daily Wilder's RSI(14) regime classification:
- **Sideways ($40 \le \text{RSI} \le 60$)**: Deploys a **1:1 Risk-to-Reward Iron Condor** using monthly expiry strikes.
  - Automatically checks break-even boundaries daily at 15:20 IST. If spot breaches either boundary, rolls the breached spread inward to the new ATM level.
- **Bearish ($\text{RSI} < 40$)**: Deploys a **1:2:1 Put Ratio Spread**:
  - Buy $\text{ATM} - 50$ Put (1 lot)
  - Sell $\text{ATM} - 250$ Put (2 lots)
  - Buy $\text{ATM} - 300$ Put (1 lot)
- **Bullish ($\text{RSI} > 60$)**: Deploys a **1:2:1 Call Ratio Spread**:
  - Buy $\text{ATM} + 50$ Call (1 lot)
  - Sell $\text{ATM} + 250$ Call (2 lots)
  - Buy $\text{ATM} + 300$ Call (1 lot)
- **Monthly Risk Management & Circuit Breakers**:
  - **+4.0% Monthly Profit Target**: Automatically squares off all positions and locks realized profit until next monthly expiry.
  - **-3.0% Monthly Stop Loss**: Hard stop-loss cuts all exposure at -₹3,000 per ₹1,00,000 margin allocation, preserving capital through sharp 2,000+ pt market trends.
- **Continuous 15:20 IST Automated Daemon**: Runs continuously in the background, waking up at 15:20 IST every trading day, skipping NSE holidays, evaluating spot & RSI, managing rolls/exits, and persisting state to `data/positional_condor_state.json`.

```bash
# Run 1-year historical monthly backtest with 3% stop loss
uv run python run_positional_condor.py --backtest --monthly-stop-loss 0.03

# Single dry-run inspection of today's regime and strike plan
uv run python run_positional_condor.py --paper --dry-run

# Run automated daily daemon in Paper mode (runs continuously at 15:20 IST)
uv run python run_positional_condor.py --paper --lots 1 --schedule-time 15:20

# Run automated daily daemon in Live mode with 3% stop loss and 4% target
uv run python run_positional_condor.py --live --lots 1 --monthly-stop-loss 0.03 --monthly-target 0.04
```

### 5. Run 15-Minute ORB Intraday Desk (Indrajit Shantharaj Strategy)
An automated high-momentum breakout trading desk operating across **30 high-beta stocks**:
- **Initial Balance (09:15 - 10:15)**: Forms high/low range from first 4 candles.
- **20% Rejection Wick Filter**: Excludes candles where counter-wick exceeds 20% of range.
- **Previous Day High/Low (PDH/PDL) Filter**: Longs must clear PDH; Shorts must break PDL.
- **NIFTY Direction Filter**: Longs require NIFTY > Day Open; Shorts require NIFTY < Day Open.
- **Risk-Parity Position Sizing**: Fixed Rs 2,500 risk per trade (Quantity = int(2500 / RiskPerShare)).
- **Max 5 Trades / Day Cap**: Selects top 5 highest-conviction setups by cleanest wick %.
- **Daily Portfolio Stop-Loss Circuit Breaker**: Hard halt and emergency square-off at -Rs 10,000 daily portfolio loss.
- **Trade Lifecycle**:
  - 1:1 R Target: Trail Stop-Loss to Breakeven (eliminate all risk).
  - 1:2.5 R Target: Book 50% profit (+Rs 3,125 gain), trail remainder at Breakeven.
  - 15:15 IST: Mandatory intraday square-off.
- **Telegram Live Alerts**: Real-time notifications for setup discovery, entry orders, 1:1 BE trails, 1:2.5 profit booking, and daily session summaries.

```bash
# Single dry-run inspection of today's IB ranges, NIFTY open, and active positions
uv run python run_orb.py --dry-run

# Run single scan & monitor cycle
uv run python run_orb.py --once

# Run automated Paper trading daemon (default: Rs 2,500 risk, max 5 trades, Rs 10k daily SL)
uv run python run_orb.py --paper --risk 2500 --max-trades 5 --daily-loss 10000

# Run automated Live trading daemon via Kite Connect API
uv run python run_orb.py --live --risk 2500 --max-trades 5 --daily-loss 10000
```

### 6. Docker & Cloud VPS Deployment

The desk is fully containerized with Indian Standard Time (`Asia/Kolkata`) pre-configured, state volume mounts, and host event log streaming.

#### Using Docker Compose (Recommended)

1. **Configure Execution Mode in `docker-compose.yml`**:
   Choose your command for either the Intraday Strangle desk or the Positional Condor daemon:
   ```yaml
   # strangle-desk (Intraday 10:00 AM Short Strangle):
   command: ["--live", "--lots", "1", "--order-type", "LIMIT", "--limit-buffer-pct", "0.05", "--max-loss", "1000"]

   # positional-condor-desk (Positional Evergreen Condor 15:20 IST Daily Daemon):
   command: ["--live", "--lots", "1", "--monthly-stop-loss", "0.03", "--monthly-target", "0.04", "--schedule-time", "15:20"]
   ```

2. **Start the Containers**:
   ```bash
   # Start Positional Condor Desk daemon in background
   docker compose up positional-condor-desk -d --build

   # Or start Intraday Strangle Desk
   docker compose up strangle-desk -d --build

   # Or run both simultaneously
   docker compose up -d --build
   ```

3. **Stream Real-Time Strategy Logs**:
   ```bash
   # From container stdout
   docker compose logs -f positional-condor-desk

   # Or inspect persisted log file on host machine directly
   tail -f logs/positional_condor.log
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
