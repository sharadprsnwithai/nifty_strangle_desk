# Adaptive Strangle Desk Enhancements Implementation Plan

This plan introduces two major enhancements to the production NIFTY 10:00 AM Short Strangle Desk (`strategy/strangle_desk.py`, `strategy/option_chain.py`, `run_strategy.py`):
1. **Morning Range Trend Filter**: At 10:00 AM, inspect the 09:15–10:00 AM range. If range / open >= 0.60%, skip entry to avoid catastrophic trend days.
2. **Dynamic ATR Strike Selection**: Select OTM strikes at $1.25 \times \text{Daily ATR}$ distance (min 150 pts) instead of static ₹30 premium, protecting against low-IV gamma traps with graceful fallback to ₹30 price discovery.

---

## File Structure

- **Modified Files**:
  - `strategy/option_chain.py`:
    - Add `fetch_daily_atr(kite, instrument_token, period=14) -> float`.
    - Update `find_strangle_strikes()` to support `strike_mode="ATR"` with `atr_multiplier=1.25` and fallback to `strike_mode="PRICE"`.
  - `strategy/strangle_desk.py`:
    - Add `check_morning_trend_filter(kite, underlying, threshold_pct=0.60) -> Tuple[bool, Dict[str, Any]]`.
    - Update `run_session()` to evaluate morning trend filter before entry, record `SKIPPED_TREND_DAY` state, and support `strike_mode` / `atr_multiplier`.
  - `run_strategy.py`:
    - Add CLI arguments: `--morning-filter` (default: True), `--no-morning-filter`, `--morning-filter-pct` (default: 0.60), `--strike-mode` (default: "ATR"), `--atr-multiplier` (default: 1.25).
  - `docker-compose.yml` & `.env.example`:
    - Update `strangle-desk` command flags / env vars.
  - `tests/test_option_chain.py`:
    - Tests for `fetch_daily_atr`, ATR-based strike resolution, and fallback behavior.
  - `tests/test_strategy_cli.py`:
    - Tests for morning filter trigger, CLI argument passing, and skipped state persistence.

---

## Proposed Tasks

### Task 1: Dynamic ATR Strike Selection in `strategy/option_chain.py`
- Add `fetch_daily_atr(kite, underlying_symbol="NIFTY 50", period=14)` fetching daily OHLC via `kite.historical_data` and calculating True Range.
- Update `find_strangle_strikes` to accept `strike_mode: str = "ATR"`, `atr_multiplier: float = 1.25`, resolve spot price, compute target OTM strikes, and query quotes.
- If ATR fails or token expires, log warning and gracefully fall back to `target_price=30.0`.
- Add unit tests in `tests/test_option_chain.py`.

### Task 2: Morning Range Trend Filter in `strategy/strangle_desk.py`
- Add `check_morning_trend_filter(kite, underlying="NIFTY", threshold_pct=0.60) -> Tuple[bool, Dict[str, Any]]`.
  - Queries 5m historical bars or intraday quotes between 09:15 and 10:00 AM.
  - Computes `morning_range = high - low` and `morning_range_pct = (morning_range / open_price) * 100.0`.
  - Returns `(True, details)` if triggered (exceeded threshold), else `(False, details)`.
- In `run_session()`, evaluate morning filter before strike discovery.
  - If triggered, notify Telegram, persist `status="SKIPPED_TREND_DAY"` in state JSON, and return cleanly without placing orders.
- Pass `strike_mode` and `atr_multiplier` into `find_strangle_strikes`.

### Task 3: CLI Flags & Entrypoint Integration in `run_strategy.py`
- Add CLI arguments:
  - `--morning-filter` / `--no-morning-filter` (default True via boolean flag)
  - `--morning-filter-pct` (default: 0.60)
  - `--strike-mode` (choices: `["ATR", "PRICE"]`, default: "ATR")
  - `--atr-multiplier` (default: 1.25)
- Wire parameters through `run_session` and `run_scheduler`.
- Update `docker-compose.yml` and `.env.example`.

### Task 4: Comprehensive Test Suite & Verification
- Unit test Morning Range Filter logic in `tests/test_strategy_cli.py`.
- Unit test ATR Strike Selection and Fallback in `tests/test_option_chain.py`.
- Run full pytest test suite across entire project.
- Verify end-to-end dry-run with `--dry-run`.
