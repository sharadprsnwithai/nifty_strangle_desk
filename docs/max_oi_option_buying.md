# Saurabh Put-Call Parity Max OI Option Buying Desk

## 1. Executive Summary & Strategy Theory

The **Max OI Unwinding Option Buying Strategy** is an algorithmic option buying system developed from institutional market maker mechanics and the **Put-Call Parity** principle:

$$S + P = C$$

Where:
- $S$ = Underlying Stock Spot Price
- $P$ = Put Option Price
- $C$ = Call Option Price

### Why Do Retail Option Buyers Lose Money?
Data across exchanges shows that nearly 80% of options expire worthless. Option writers profit from continuous time decay (theta). Buyers only profit when:
1. An **explosive, directional move** occurs in the underlying.
2. The move happens **rapidly without delay** (beating theta).

### When Does an Explosive Move Occur?
Institutional option writers sell large volumes of OTM Call and Put options, creating natural resistance (Max Call OI strike) and support (Max Put OI strike). 

Writers panic when their "safe" Max OI strikes are breached **in the second half of the monthly expiry (after the 15th)**:
- Early in the expiry, writers have 20–25 days to adjust positions.
- **After the 15th**, time has run out, gamma sensitivity spikes, and short positions quickly enter deep losses.
- Covering short calls requires **buying back the calls**, causing call prices ($C$) to skyrocket.
- Arbitrageurs enforce $S + P = C$: as call prices spike, arbitrageurs buy the underlying equity, creating a self-reinforcing short-squeeze rally.

---

## 2. Core Strategy Rules

| Criterion | Rule | Reasoning |
| :--- | :--- | :--- |
| **Expiry Timing** | **After the 15th of the month** | Panic covering only causes explosive moves when gamma spikes and expiry is near. |
| **Strike Proximity** | **Within $\le 2.0\%$ of Max OI** | Stock must actively test or breach the institutional strike level. |
| **Panic Unwinding Filter** | **$\ge 20\%$ OI reduction** | Day-over-day decrease of $\ge 20\%$ confirms institutional writers are actively liquidating. |
| **Option Selection** | **ATM Option ($S + P = C$)** | Buy ATM Call (bullish breach) or ATM Put (bearish breakdown) with highest liquidity and delta. |
| **Stop Loss** | **50% Hard Stop Loss** | Closes position if option premium drops to $\le 50\%$ of entry price. |
| **Profit Target** | **2x (100% Gain)** | Books profit when option premium doubles. |
| **Time Exit** | **2 Trading Days Max** | Mandatory exit by 15:00 IST on Day 2 to avoid theta decay if the breakout stalls. |
| **Capital Allocation** | **Max 2% Risk Cap** | Sized to risk at most 2% of total trading portfolio per setup. |

---

## 3. Workflow & Architecture

```
09:15                                        09:30                                           15:00
  |--------------------------------------------|-----------------------------------------------|
  [ Market Open & Zero-Hit Guard ]       [ Daily Morning Scan ]                    [ 2-Day Time Exit ]
  No Kite API calls outside market hours  Screens ~200 F&O stocks                   Auto-closes trades
                                          Detects 2% Proximity & >20% Unwinding      at Day 2 EOD
                                          Enters Paper Trade & Alerts Telegram
```

### Modules
- **`strategy/max_oi/config.py`**: Configuration parameters (proximity 2%, unwinding 20%, SL 50%, 2-day holding, 1 lot, max 3 trades).
- **`strategy/max_oi/models.py`**: Data classes (`MaxOICandidate`, `OptionBuySignal`, `OptionPosition`).
- **`strategy/max_oi/scanner.py`**: Resolves active monthly expiry, queries batch spot and option Open Interest, compares with prior day snapshot (`data/oi_snapshots/`).
- **`strategy/max_oi/desk.py`**: Daily 09:30 IST execution daemon, paper trade order manager, active position monitor, and Telegram notifier.
- **`scripts/run_max_oi_scanner.py`**: CLI entrypoint with single-pass and daemon loop modes.

---

## 4. Automated Paper Trading Mode

The desk runs in **`PAPER` mode by default**, allowing live forward simulation without broker capital risk:
- **Automatic Execution**: Simulates market entry for 1 lot at the live ATM LTP when a signal triggers.
- **State Persistence**: Active and closed trades are recorded in `data/max_oi_trades.json`.
- **Live Exit Engine**: Continuously tracks live quotes during market hours:
  - Auto-exits on **50% SL hit**.
  - Auto-exits on **2x Target hit**.
  - Auto-exits on **Day-2 15:00 IST Time Stop**.
- **Accurate PnL Tracking**: Deducts simulated brokerage, STT, and exchange transaction fees.

---

## 5. Usage Commands

```bash
# 1. Run Single Scan Pass & Output Results (Console Table + Telegram)
uv run python scripts/run_max_oi_scanner.py --now

# 2. Run Daemon Scheduler (Runs daily 09:30 IST during market hours)
uv run python scripts/run_max_oi_scanner.py --loop --poll 60

# 3. Run in Alert-Only Mode (No Paper Orders)
uv run python scripts/run_max_oi_scanner.py --alert-only

# 4. Off-Market / Weekend Diagnostic Mode (Bypasses holiday check)
uv run python scripts/run_max_oi_scanner.py --now --ignore-holidays --stocks RELIANCE,TATASTEEL
```
