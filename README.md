# Sharad's Intraday Option Selling Desk

A modular algorithmic trading desk connected to Zerodha Kite Connect API using automated headless TOTP 2FA.

## Features
- **Headless TOTP 2FA**: Automated daily login via `pyotp` RFC 6238 without manual browser interaction.
- **Smart Token Caching**: Caches and validates daily `access_token` in `.env` to avoid unnecessary logins.
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

### View Current Positions (Tabular)
```bash
uv run python main.py
```

### View Only Open Positions
```bash
uv run python main.py --open-only
```

### View Intraday Day Positions
```bash
uv run python main.py --day
```

### Output Clean JSON (for automations / pipelines)
```bash
uv run python main.py --json
```

### Force Fresh Login (refresh access token)
```bash
uv run python main.py --force-login
```

## Running Tests
```bash
uv run pytest -v
```
