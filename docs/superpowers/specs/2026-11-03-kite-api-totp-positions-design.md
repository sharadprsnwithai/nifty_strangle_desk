# Technical Design Specification: Kite API Connection with TOTP & Positions Fetcher

**Date:** 2026-11-03  
**Status:** Approved  
**Target Workspace:** `d:\code\sharads_intraday_option_selling_desk`  

---

## 1. Executive Summary

This project establishes the core connection and portfolio visibility layer for **Sharad's Intraday Option Selling Desk**. It connects to Zerodha's Kite Connect API using fully automated headless Two-Factor Authentication (TOTP via `pyotp`), manages and caches the daily `access_token` in `.env`, and fetches and formats current trading positions (both Net and Day) with calculated P&L and MTM summaries.

---

## 2. System Architecture & Project Structure

The project uses `uv` for Python project and virtual environment management, targeting Python >= 3.10. All Zerodha-specific logic is isolated in a modular `kite/` package to allow seamless reuse across future intraday option selling strategy engines.

```
sharads_intraday_option_selling_desk/
│
├── kite/                          # Zerodha Kite Connect subpackage
│   ├── __init__.py                # Package exports
│   ├── auth.py                    # Headless TOTP login & OAuth session generation
│   ├── client.py                  # Authenticated KiteConnect wrapper & token validation
│   └── positions.py               # Position retrieval, aggregation & presentation
│
├── config.py                      # Environment configuration & credential validation
├── main.py                        # CLI entrypoint supporting tabular and JSON output
├── pyproject.toml                 # uv project configuration and dependencies
├── requirements.txt               # Pinned dependencies for portability
├── .env.example                   # Template for credentials
├── .env                           # Local environment secrets and cached token (gitignored)
├── .gitignore                     # Git ignore rules for venv, secrets, logs, cache
│
└── tests/                         # Unit tests with mocked Kite API responses
    ├── __init__.py
    ├── test_auth.py               # Tests for TOTP code generation & token persistence
    └── test_positions.py          # Tests for position aggregation & output formatting
```

---

## 3. Component Details & Data Flow

### 3.1 Configuration Management (`config.py`)
- Reads from `.env` using `python-dotenv`.
- Required variables:
  - `KITE_API_KEY`: Zerodha Kite Connect app API key.
  - `KITE_API_SECRET`: Zerodha Kite Connect app API secret.
  - `KITE_USER_ID`: Zerodha trading account user ID.
  - `KITE_PASSWORD`: Zerodha trading account password.
  - `KITE_TOTP_KEY`: Base32 TOTP secret key for Two-Factor Authentication.
- Optional / Managed variables:
  - `KITE_ACCESS_TOKEN`: Today's active access token (updated automatically upon successful login).
- Validates credential availability before attempting authentication and raises actionable error messages if any are missing.

### 3.2 Headless TOTP Authentication (`kite/auth.py`)
- **Login Flow**:
  1. **User/Pass Login**: `POST` to `https://kite.zerodha.com/api/login` with `user_id` and `password`. Receives `request_id`.
  2. **TOTP Generation**: Computes time-based 6-digit OTP from `KITE_TOTP_KEY` via `pyotp.TOTP(key).now()`.
  3. **2FA Verification**: `POST` to `https://kite.zerodha.com/api/twofa` with `user_id`, `request_id`, `twofa_value=totp_code`, and `twofa_type="totp"`.
  4. **OAuth Redirect Traversal**: Initiates `GET` to `https://kite.zerodha.com/connect/login?api_key={api_key}&v=3`. Intercepts redirect location headers (handling single-hop or `/connect/finish` hops) and extracts the `request_token`.
  5. **Session Exchange**: Calls `KiteConnect.generate_session(request_token, api_secret)` to obtain the daily `access_token`.
  6. **Token Persistence**: Updates `KITE_ACCESS_TOKEN` in `.env` and `os.environ`.
- **Exception & Error Handling**:
  - Handles bad credentials, expired TOTP, and network timeouts with descriptive logger errors.

### 3.3 Kite Session & Client Manager (`kite/client.py`)
- Wraps `kiteconnect.KiteConnect`.
- Implements `get_kite_client(force_login: bool = False) -> KiteConnect`:
  - If `force_login` is `False` and `KITE_ACCESS_TOKEN` exists:
    - Sets token on client and calls `kite.profile()`.
    - If valid, returns client immediately (Fast Path).
    - If `TokenException` or invalid, falls back to `auto_login_with_totp()`.
  - If `force_login` is `True` or token is absent, triggers `auto_login_with_totp()` directly.

### 3.4 Position Processing & Display (`kite/positions.py`)
- Calls `kite.positions()` returning a payload with keys:
  - `net`: Cumulative/overnight and open positions.
  - `day`: Intraday trades executed today.
- **Data Aggregation**:
  - Computes per-position:
    - `symbol`: Trading symbol (e.g. `NIFTY26NOV24500CE`).
    - `product`: `MIS` / `NRML` / `CNC`.
    - `quantity`: Net quantity (positive = long, negative = short, zero = squared-off).
    - `buy_price`, `sell_price`, `last_price` (LTP).
    - `pnl`: Position net profit & loss.
    - `m2m`: Mark-to-market.
  - Aggregates portfolio summary:
    - Total Realised P&L.
    - Total Unrealised P&L.
    - Total Net P&L (Realised + Unrealised).
    - Open positions count vs Closed positions count.
- **Presentation**:
  - **Tabular View**: Formatted table using `tabulate` with clear column alignments and a clean summary footer.
  - **JSON View**: Structured dictionary serializable to JSON when `--json` flag is provided.

### 3.5 CLI Entry Point (`main.py`)
- Command line flags:
  - `--json`: Outputs raw structured JSON to stdout.
  - `--force-login`: Forces a fresh headless TOTP login, discarding any cached access token.
  - `--open-only`: Filters display to show only active/open positions (`quantity != 0`).
  - `--day`: Shows only today's intraday positions.
  - `--net`: Shows only cumulative net positions.

---

## 4. Dependencies

- `kiteconnect` (`>=5.0.0`): Official Zerodha Kite Connect Python client.
- `pyotp` (`>=2.9.0`): RFC 6238 TOTP library for automated 2FA code generation.
- `requests` (`>=2.31.0`): HTTP requests for web session and OAuth redirects.
- `python-dotenv` (`>=1.0.0`): Environment variable management.
- `tabulate` (`>=0.9.0`): Tabular formatting for terminal display.
- `pytest` (`>=8.0.0`): Test runner for unit tests.

---

## 5. Testing & Verification Plan

1. **Unit Testing (`pytest`)**:
   - `test_auth.py`:
     - Test TOTP code generation (verifies valid 6-digit numeric string).
     - Test `.env` file updating function without overwriting other environment keys.
   - `test_positions.py`:
     - Test position parsing and aggregate P&L calculation with mock Kite position payloads (mixed long, short, squared-off).
     - Test JSON serialization.
     - Test empty positions response (zero positions scenario).
2. **End-to-End Verification**:
   - Run `pytest` to ensure all tests pass.
   - Run CLI command `uv run python main.py --help` to confirm options.
   - Validate `.env.example` contains clear setup instructions for the user.
