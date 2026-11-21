# Milind Upasani's MTF Techno-Funda Breakout Desk

## 1. Executive Summary & Strategy Theory

The **MTF Techno-Funda Breakout Strategy** is an algorithmic positional and swing screening system based on Milind Upasani's methodology (featured on PhonePe's *Trading Edge*). 

The strategy's foundation is that **"The market is not a game of emotions; it is a game of probabilities."** To achieve high win probabilities, the system systematically intersects **Multi-Timeframe Dow Theory Momentum** with **High-Growth Operational Fundamentals**.

---

## 2. Quantitative Screening Criteria

```
                            [ MULTI-TIMEFRAME TECHNO-FUNDA SCANNER ]
                                              │
                   ┌──────────────────────────┴──────────────────────────┐
                   ▼                                                     ▼
       [ TECHNICAL ANALYSIS (MTF RSI) ]                       [ FUNDAMENTAL ANALYSIS ]
                   │                                                     │
  • Monthly RSI(14) > 60 (Primary Trend)               • TTM YoY EBITDA Growth > 30%
  • Weekly RSI(14)  > 60 (Secondary Trend)             • Quarterly YoY EBITDA Growth > 30%
  • Daily RSI(14) crosses above 65 (Trigger)           • Debt-to-Equity Ratio < 1.0 (Low/No Debt)
  • Chart Pattern: VCP / Box Breakout / Retest         • Operating Margin >= 8% - 9%
                   │                                                     │
                   └──────────────────────────┬──────────────────────────┘
                                              │
                                              ▼
                                [ HIGH-PROBABILITY SWING SETUPS ]
                               (Triggered daily at 15:15 IST)
```

### A. Technical Criteria (Multi-Timeframe Momentum)
1. **Primary Trend (Monthly Timeframe)**:
   - **Monthly RSI(14) > 60.0**: Confirms the stock is in a multi-month institutional bull regime.
2. **Secondary Trend (Weekly Timeframe)**:
   - **Weekly RSI(14) > 60.0**: Confirms medium-term trend strength and bullish swing momentum.
3. **Trigger / Minor Trend (Daily Timeframe)**:
   - **Daily RSI(14) crosses above 65.0** (or in sweet spot 65.0–75.0):
     - *Why 65 instead of 70?*: 70 is often over-extended. 65 catches the breakout **right as momentum accelerates** or upon a **successful retest** of a prior breakout resistance (e.g., Aurobindo Pharma, Tips Music).
4. **Base Pattern Recognition**:
   - Volatility Contraction Pattern (VCP)
   - Box Consolidation Breakout with Volume Surge $> 1.25\times$ 20-MA
   - Breakout Retest

### B. Fundamental Criteria (Operational High Growth)
Focuses exclusively on **EBITDA (Operational Earnings)** rather than EPS to avoid distortion from one-time asset sales or tax benefits:
1. **TTM YoY EBITDA Growth > 30%**: Trailing 12-month operational earnings up $> 30\%$ vs prior TTM.
2. **Quarterly YoY/QoQ EBITDA Growth > 30%**: Operating earnings growing $> 30\%$ quarter-on-quarter or quarter-over-prior-year quarter.
3. **Debt-to-Equity Ratio < 1.0**: Solvency safety filter; ensures the company has a clean, low-debt balance sheet.
4. **Operating Margin $\ge 8\%-9\%$**: High margin business resilience and pricing power.

---

## 3. Architecture & 2-Stage Pipeline

- **Stage 1 (Technical MTF Screen)**:
  - Screens the universe of ~250 F&O / high-cap equities using 2 years of daily data.
  - Computes Wilder's RSI on Daily, Weekly (`W-FRI`), and Monthly (`ME`) series.
  - Filters down to stocks passing `Monthly > 60`, `Weekly > 60`, and `Daily Cross > 65`.
- **Stage 2 (Fundamental Validation)**:
  - Fetches quarterly financials and balance sheet metrics via `yfinance` with a 7-day disk cache.
  - Verifies operational EBITDA growth $> 30\%$ and $\text{D/E} < 1.0$.
- **Off-Market Zero-Hit Guard**:
  - Checks `is_market_open()`. On weekends and outside market hours, background polling cleanly pauses with zero network calls to Kite.

---

## 4. Scheduling & Telegram Alert Schema

The desk daemon loop runs continuously during market hours and triggers strictly at **15:15 IST** (just before market close) to identify daily closing breakouts ready for next-day entry:

```text
🎯 TECHNO-FUNDA BREAKOUT ALERT

Stock: Aurobindo Pharma Ltd (AUROPHARMA)
Sector: Healthcare | LTP: Rs 1,800.00
Pattern: VCP_CONTRACTION_BREAKOUT (Vol: 1.4x)

📊 Multi-Timeframe Momentum (Dow Theory)
• Monthly RSI: 81.7 (Primary Bullish > 60)
• Weekly RSI: 67.3 (Secondary Bullish > 60)
• Daily RSI: 69.5 (Trigger: 69.5 (In Zone))

💎 High-Growth Operational Fundamentals
• TTM YoY EBITDA Growth: +34.2% (> 30%)
• Quarterly EBITDA Growth: +31.8% (> 30%)
• Debt-to-Equity: 0.18 (Clean Balance Sheet < 1.0)
• Operating Margin: 20.4% (Margin >= 8%)

🛡️ Reference Swing Stop: Rs 1,539.96 (Base Support)
🎯 Target 1 (1.5R): Rs 2,190.00
🎯 Target 2 (2.5R): Rs 2,450.00
Scan Time: 15:15 IST | Date: 2026-11-21
```

---

## 5. Usage Commands

```bash
# 1. Execute immediate scan pass & output results
uv run python scripts/run_techno_funda_scanner.py --now

# 2. Run continuous daemon scheduler (Triggers daily at 15:15 IST)
uv run python scripts/run_techno_funda_scanner.py --loop --poll 30

# 3. Technical-only mode (bypasses fundamental validation)
uv run python scripts/run_techno_funda_scanner.py --now --tech-only

# 4. Off-market / weekend diagnostic scan (bypasses holiday check)
uv run python scripts/run_techno_funda_scanner.py --now --ignore-holidays --stocks TIPSMUSIC,MCX,AUROPHARMA
```
