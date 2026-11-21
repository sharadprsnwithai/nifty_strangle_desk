# Silver Options Desk: Intraday Williams %R + Triple EMA Strategy Implementation Plan

This plan replaces the previous multi-day RSI divergence logic in `strategy/silver_options/` with the empirically validated **Intraday 5-Minute Williams %R (70) + Triple EMA (5, 15, 50)** option selling desk.

## Scope & Architectural Decisions
- Timeframe: Strictly **5-minute intraday bars** for MCX Silver Mini (`SILVERM`, 5 kg lot size).
- Strike target: **0.30–0.40 Delta** (default 0.35 Delta, ~2%–2.5% OTM).
- Entry session: **09:15 to 18:00 IST** (the "Calm Theta" window with 1.71 Profit Factor and low drawdown).
- Exit rules: Super Order Stop Loss at **1.5x entry premium**, **Trend Reversal (5 EMA crossing 50 EMA)** with candle confirmation, and **Intraday EOD cutoff**.
- Clean retirement of daily RSI divergence and hourly 20 EMA code in `strategy/silver_options/`.
- Repository tests must remain 100% passing at all times (all NIFTY tests + Silver tests).

---

### Task 1: Update Configuration & Models

**Files to modify:**
- `strategy/silver_options/config.py`
- `strategy/silver_options/models.py`
- `tests/test_silver_options/test_config_and_models.py`

**Steps:**
1. Update `SilverConfig` in `strategy/silver_options/config.py`:
   - Remove legacy parameters: `daily_rsi_period`, `daily_rsi_oversold`, `daily_rsi_overbought`, `hourly_ema_period`, `max_debit_width_ratio`.
   - Add:
     - `williams_r_period: int = 70`
     - `ema_fast: int = 5`
     - `ema_mid: int = 15`
     - `ema_slow: int = 50`
     - `target_delta: float = 0.35`
     - `sl_multiplier: float = 1.5`
     - `session_start: time = time(9, 15)`
     - `session_end: time = time(18, 0)`
     - `eod_cutoff: time = time(23, 15)`
2. Update `models.py`:
   - `SignalType`: `BULLISH_MOMENTUM = "BULLISH_MOMENTUM"`, `BEARISH_MOMENTUM = "BEARISH_MOMENTUM"`, `NO_SIGNAL = "NO_SIGNAL"`.
   - `SpreadType`: `SHORT_PUT = "SHORT_PUT"`, `SHORT_CALL = "SHORT_CALL"`, `BULL_PUT_SPREAD = "BULL_PUT_SPREAD"`, `BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"`.
   - `SpreadPosition`: Ensure fields support option selling (`entry_net_credit: float = 0.0`, `target_delta: float = 0.35`, `sl_price: float = 0.0`).
3. Update `tests/test_silver_options/test_config_and_models.py` to test the new config defaults and models.
4. Run `uv run pytest tests/test_silver_options/test_config_and_models.py`.

---

### Task 2: Implement Williams %R + Triple EMA Signals & Trend Reversal

**Files to modify:**
- `strategy/silver_options/technical_signals.py`
- `tests/test_silver_options/test_technical_signals.py`

**Steps:**
1. Implement `compute_williams_r(df: pd.DataFrame, period: int = 70) -> pd.Series`:
   $$\%R = \frac{\text{HighestHigh}(70) - \text{Close}}{\text{HighestHigh}(70) - \text{LowestLow}(70)} \times -100$$
   Handles division by zero safely with fallback `-50.0`.
2. Implement `compute_ema(series: pd.Series, span: int) -> pd.Series`:
   Uses `ewm(span=span, adjust=False).mean()`.
3. Implement `evaluate_technical_signal(df_5m: pd.DataFrame, config: SilverConfig, current_time: Optional[time] = None) -> SignalType`:
   - Checks session window: `config.session_start <= current_time <= config.session_end` (if current_time provided).
   - Computes Williams %R(70), 5 EMA, 15 EMA, 50 EMA on `df_5m`.
   - Bullish Momentum: `wr > -20.0` and `ema5 > ema15 > ema50` and `curr_high > prev_high` $\rightarrow$ `SignalType.BULLISH_MOMENTUM`.
   - Bearish Momentum: `wr < -80.0` and `ema5 < ema15 < ema50` and `curr_low < prev_low` $\rightarrow$ `SignalType.BEARISH_MOMENTUM`.
   - Else $\rightarrow$ `SignalType.NO_SIGNAL`.
4. Implement `check_trend_reversal(df_5m: pd.DataFrame, opt_type: str, config: SilverConfig) -> bool`:
   - For Short Put (`PE`): 5 EMA crosses below 50 EMA and current Low < prev Low.
   - For Short Call (`CE`): 5 EMA crosses above 50 EMA and current High > prev High.
5. Update `tests/test_silver_options/test_technical_signals.py` to comprehensively test Williams %R, Triple EMA alignment, breakout confirmation, session filter, and trend reversals.
6. Run `uv run pytest tests/test_silver_options/test_technical_signals.py`.

---

### Task 3: Implement Option Strike Selection for Target Delta (0.35 Delta)

**Files to modify:**
- `strategy/silver_options/spread_selector.py`
- `tests/test_silver_options/test_spread_selector.py`

**Steps:**
1. Update `select_spread` in `strategy/silver_options/spread_selector.py`:
   - Given `signal: SignalType` (`BULLISH_MOMENTUM` $\implies$ sell Put, `BEARISH_MOMENTUM` $\implies$ sell Call):
   - Determine option type: `"PE"` for Bullish, `"CE"` for Bearish.
   - Search option chain for strike with Delta closest to `config.target_delta` (0.35) or calculate via Black-76 if Greek not populated.
   - Construct `SpreadPosition` with a single short leg (`action="SELL"`), or optionally with a far OTM hedge wing (`action="BUY"`) if configured.
   - Compute `entry_net_credit` = premium received $\times$ lot_size.
   - Set `max_profit = entry_net_credit` and `status = "PENDING"`.
2. Update `tests/test_silver_options/test_spread_selector.py` to test 0.35 Delta strike selection for both Call and Put options.
3. Run `uv run pytest tests/test_silver_options/test_spread_selector.py`.

---

### Task 4: Implement Option Selling Risk Management (1.5x SL, Trend Reversal, EOD)

**Files to modify:**
- `strategy/silver_options/risk_manager.py`
- `tests/test_silver_options/test_risk_manager.py`

**Steps:**
1. Update `check_exit_conditions` in `strategy/silver_options/risk_manager.py`:
   - Accept `(position, current_opt_price, df_5m, current_time, is_expiry_day, config)`.
   - Exit check 1 (Expiry Devolvement): If `is_expiry_day and current_time >= config.devolvement_cutoff` $\rightarrow$ `(True, "MANDATORY_EXPIRY_DEVOLVEMENT_CUTOFF")`.
   - Exit check 2 (Stop Loss): If `current_opt_price >= position.legs[0].premium * config.sl_multiplier` $\rightarrow$ `(True, "STOP_LOSS")`.
   - Exit check 3 (Session / EOD Cutoff): If `current_time >= config.eod_cutoff` (or `config.session_end` if intraday session exit) $\rightarrow$ `(True, "INTRADAY_EOD")`.
   - Exit check 4 (Trend Reversal): If `check_trend_reversal(df_5m, position.legs[0].option_type, config)` $\rightarrow$ `(True, "TREND_REVERSAL")`.
2. Update `tests/test_silver_options/test_risk_manager.py` to test all exit triggers.
3. Run `uv run pytest tests/test_silver_options/test_risk_manager.py`.

---

### Task 5: Update Engine to Coordinate 5-Minute Intraday Loop

**Files to modify:**
- `strategy/silver_options/engine.py`
- `tests/test_silver_options/test_engine.py`

**Steps:**
1. Update `SilverOptionsEngine.on_market_tick`:
   - Change signature to: `(spot_price: float, df_5m: pd.DataFrame, chain_data: List[Dict], current_time: time, is_expiry_day: bool = False)`.
   - If `self.active_position`:
     - Lookup current LTP for short option from chain.
     - Evaluate `check_exit_conditions(...)`.
     - If exit triggered, call `execute_spread_exit(...)`, update `realized_pnl = (entry_premium - exit_premium) * lot_size`, archive to `closed_positions`, and reset `active_position = None`.
   - If idle:
     - Call `evaluate_technical_signal(df_5m, self.config, current_time)`.
     - If signal is not `NO_SIGNAL`:
       - Profile chain, select strike via `select_spread(...)`.
       - If spread selected, execute entry via `execute_spread_entry(...)`.
2. Update `tests/test_silver_options/test_engine.py` to mock 5m candles, signal generation, order execution, SL trigger, and EOD square-off.
3. Run `uv run pytest tests/test_silver_options/test_engine.py`.

---

### Task 6: Update CLI & Final End-to-End Verification

**Files to modify:**
- `strategy/silver_cli.py`
- `tests/test_silver_options/test_silver_cli.py`
- `tests/test_silver_options/test_order_manager.py`

**Steps:**
1. Update `strategy/silver_cli.py`:
   - Update `monitor` / `run` command to fetch 5m candles (`interval="5minute"`), pass to `engine.on_market_tick`.
   - Support paper execution and live dry-run mode.
2. Update `tests/test_silver_options/test_silver_cli.py` and `tests/test_silver_options/test_order_manager.py` for full compatibility.
3. Run full test suite: `uv run pytest` (verify 210+ tests pass).
