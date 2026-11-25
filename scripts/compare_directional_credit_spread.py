import os
import sys
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath("."))
from strategy.options_model import compute_fractional_dte, black_76_price
from strategy.costs import calculate_trade_costs
from strategy.backtest import (
    calculate_daily_atr,
    get_weekly_expiry_for_date,
    run_backtest_simulation,
)


def calculate_long_option_costs(
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_per_share: float = 0.50,
) -> Dict[str, float]:
    """
    Computes exact NSE F&O costs for a LONG option leg (bought at entry, sold at exit):
    - Brokerage: Rs. 20 per order (Rs. 40 round trip)
    - STT: 0.1% on SELL turnover (exit side for long options)
    - Exchange turnover charges: 0.0495% on total turnover
    - SEBI charges: Rs. 10 / crore (0.0001%)
    - Stamp duty: 0.003% on BUY turnover (entry side for long options)
    - GST: 18% on (Brokerage + Exchange Fees + SEBI Fees)
    - Slippage: Applied symmetrically to entry and exit executions
    """
    qty = abs(quantity)
    if qty == 0:
        return {"gross_pnl": 0.0, "net_pnl": 0.0, "statutory_taxes": 0.0, "total_slippage": 0.0}

    gross_pnl = round((exit_price - entry_price) * qty, 2)
    brokerage = 40.0
    stt = round(0.001 * exit_price * qty, 2)
    turnover = (entry_price + exit_price) * qty
    exchange_fee = round(0.000495 * turnover, 2)
    sebi_fee = round(0.000001 * turnover, 2)
    stamp_duty = round(0.00003 * entry_price * qty, 2)
    gst = round(0.18 * (brokerage + exchange_fee + sebi_fee), 2)
    total_slippage = round(slippage_per_share * 2.0 * qty, 2)
    statutory_taxes = round(brokerage + stt + exchange_fee + sebi_fee + stamp_duty + gst, 2)
    net_pnl = round(gross_pnl - statutory_taxes - total_slippage, 2)

    return {
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "statutory_taxes": statutory_taxes,
        "total_slippage": total_slippage,
    }


def simulate_credit_spread_day(
    day_df: pd.DataFrame,
    t_date: Any,
    daily_atr_val: float,
    mode: str,
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
    max_loss: float = 10000.0,
    exit_cutoff_time: time = time(14, 45),
    # Variant parameters
    chop_band: Optional[Tuple[float, float]] = (0.35, 0.65),
    short_otm_mode: str = "FIXED",  # "FIXED" or "ATR"
    short_otm_pts: float = 0.0,
    short_atr_mult: float = 0.5,
    wing_width_pts: float = 200.0,
    target_decay_pct: Optional[float] = 0.80,
    require_orb_breakout: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Simulates a Directional Credit Spread on a Morning-Trend-Filter triggered day.
    - Bullish -> Bull Put Spread (Sell PE, Buy lower PE)
    - Bearish -> Bear Call Spread (Sell CE, Buy higher CE)
    """
    morning_bars = day_df[
        (day_df["dt"].dt.hour == 9) | ((day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute == 0))
    ]
    if morning_bars.empty:
        return None

    m_open = float(morning_bars.iloc[0]["open"])
    m_high = float(morning_bars["high"].max())
    m_low = float(morning_bars["low"].min())
    m_range = m_high - m_low
    if m_range <= 0:
        return None

    entry_bars = day_df[(day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute <= 5)]
    if entry_bars.empty:
        entry_bars = day_df[(day_df["dt"].dt.hour == 9) & (day_df["dt"].dt.minute >= 55)]
    if entry_bars.empty:
        return None

    entry_bar = entry_bars.iloc[0]
    entry_time = entry_bar["dt"]
    spot_10am = float(entry_bar["open"])
    range_pos = (spot_10am - m_low) / m_range

    direction = None
    if require_orb_breakout:
        # Wait after 10:00 AM for spot to break morning high or low (before 13:30)
        post_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= time(13, 30))]
        for _, bar in post_bars.iterrows():
            if float(bar["close"]) > m_high:
                direction = "BULL"
                entry_time = bar["dt"]
                spot_10am = float(bar["close"])
                break
            elif float(bar["close"]) < m_low:
                direction = "BEAR"
                entry_time = bar["dt"]
                spot_10am = float(bar["close"])
                break
        if direction is None:
            return None
    else:
        if chop_band is not None:
            low_th, high_th = chop_band
            if range_pos >= high_th:
                direction = "BULL"
            elif range_pos <= low_th:
                direction = "BEAR"
            else:
                return None  # Choppy / two-way whipsaw morning -> still skip
        else:
            direction = "BULL" if range_pos >= 0.50 else "BEAR"

    opt_type = "PE" if direction == "BULL" else "CE"
    target_expiry, is_0dte = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)
    entry_dte = compute_fractional_dte(target_expiry, entry_time)

    atm_strike = round(spot_10am / 50.0) * 50.0
    if short_otm_mode == "ATR":
        otm_dist = round((daily_atr_val * short_atr_mult) / 50.0) * 50.0
    else:
        otm_dist = short_otm_pts

    if opt_type == "PE":
        short_strike = atm_strike - otm_dist
        long_strike = short_strike - wing_width_pts
    else:
        short_strike = atm_strike + otm_dist
        long_strike = short_strike + wing_width_pts

    short_entry = black_76_price(spot_10am, short_strike, entry_dte, iv, r, opt_type)
    long_entry = black_76_price(spot_10am, long_strike, entry_dte, iv, r, opt_type)
    net_credit = round(short_entry - long_entry, 2)

    # Guard against near-zero credit spreads
    if net_credit <= 2.0:
        return None

    total_qty = lots * lot_size
    intraday_bars = day_df[(day_df["dt"] > entry_time) & (day_df["dt"].dt.time <= exit_cutoff_time)]

    short_exit = short_entry
    long_exit = long_entry
    exit_reason = "TIME_EXIT"
    exit_time = entry_time

    target_spread_val = round(net_credit * (1.0 - target_decay_pct), 2) if target_decay_pct is not None else -1.0

    for _, bar in intraday_bars.iterrows():
        curr_dt = bar["dt"]
        curr_dte = compute_fractional_dte(target_expiry, curr_dt)
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        # Worst-case intra-bar spot for the spread
        worst_spot = bar_low if opt_type == "PE" else bar_high
        best_spot = bar_high if opt_type == "PE" else bar_low

        short_worst = black_76_price(worst_spot, short_strike, curr_dte, iv, r, opt_type)
        long_worst = black_76_price(worst_spot, long_strike, curr_dte, iv, r, opt_type)
        spread_worst = short_worst - long_worst
        worst_loss = (spread_worst - net_credit) * total_qty

        if max_loss is not None and worst_loss >= max_loss:
            # Cap exit at close of bar or exact max_loss equivalent
            short_close = black_76_price(bar_close, short_strike, curr_dte, iv, r, opt_type)
            long_close = black_76_price(bar_close, long_strike, curr_dte, iv, r, opt_type)
            # Use worse of close vs exact SL threshold to be conservative
            close_loss = ((short_close - long_close) - net_credit) * total_qty
            if close_loss >= max_loss:
                short_exit, long_exit = short_close, long_close
            else:
                # Hit intra-bar at max_loss
                short_exit, long_exit = short_worst, long_worst
            exit_reason = "MAX_LOSS_EXIT"
            exit_time = curr_dt
            break

        # Profit target check (only if max loss didn't trigger first)
        if target_decay_pct is not None:
            short_best = black_76_price(best_spot, short_strike, curr_dte, iv, r, opt_type)
            long_best = black_76_price(best_spot, long_strike, curr_dte, iv, r, opt_type)
            spread_best = short_best - long_best
            if spread_best <= target_spread_val:
                short_exit = short_best
                long_exit = long_best
                exit_reason = "TARGET_DECAY"
                exit_time = curr_dt
                break
    else:
        if not intraday_bars.empty:
            last_bar = intraday_bars.iloc[-1]
            curr_dte = compute_fractional_dte(target_expiry, last_bar["dt"])
            last_spot = float(last_bar["close"])
            short_exit = black_76_price(last_spot, short_strike, curr_dte, iv, r, opt_type)
            long_exit = black_76_price(last_spot, long_strike, curr_dte, iv, r, opt_type)
            exit_time = last_bar["dt"]

    short_cost = calculate_trade_costs(short_entry, short_exit, total_qty, slippage_per_share=slippage)
    long_cost = calculate_long_option_costs(long_entry, long_exit, total_qty, slippage_per_share=slippage)

    gross_pnl = round(short_cost["gross_pnl"] + long_cost["gross_pnl"], 2)
    taxes = round(short_cost["statutory_taxes"] + long_cost["statutory_taxes"], 2)
    slip = round(short_cost["total_slippage"] + long_cost["total_slippage"], 2)
    net_pnl = round(short_cost["net_pnl"] + long_cost["net_pnl"], 2)

    return {
        "date": str(t_date),
        "trade_type": f"CREDIT_SPREAD_{direction}",
        "direction": direction,
        "opt_type": opt_type,
        "range_pos": round(range_pos, 2),
        "spot_entry": spot_10am,
        "short_strike": short_strike,
        "long_strike": long_strike,
        "net_credit": net_credit,
        "exit_reason": exit_reason,
        "gross_pnl": gross_pnl,
        "taxes": taxes,
        "slippage": slip,
        "net_pnl": net_pnl,
    }


def summarize_trades(trades_df: pd.DataFrame) -> Dict[str, Any]:
    if trades_df.empty:
        return {
            "days": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_pnl": 0.0, "net_pnl": 0.0, "taxes": 0.0, "slippage": 0.0,
            "pf": 0.0, "max_dd": 0.0, "avg_daily": 0.0, "worst_month": "N/A",
        }

    df = trades_df.sort_values("date").reset_index(drop=True)
    days = len(df)
    wins = int((df["net_pnl"] > 0).sum())
    losses = int((df["net_pnl"] < 0).sum())
    win_rate = round((wins / days) * 100.0, 1)

    gross_pnl = round(float(df["gross_pnl"].sum()), 2)
    net_pnl = round(float(df["net_pnl"].sum()), 2)
    taxes = round(float(df["taxes"].sum()), 2)
    slip = round(float(df["slippage"].sum()), 2)

    gw = float(df[df["gross_pnl"] > 0]["gross_pnl"].sum())
    gl = abs(float(df[df["gross_pnl"] < 0]["gross_pnl"].sum()))
    pf = round(gw / gl, 2) if gl > 0 else 999.0

    cum = pd.concat([pd.Series([0.0]), df["net_pnl"]]).cumsum()
    max_dd = round(float((cum - cum.cummax()).min()), 2)

    df["month"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    m_net = df.groupby("month")["net_pnl"].sum()
    worst_m_name = m_net.idxmin()
    worst_m_val = m_net.min()

    return {
        "days": days,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "taxes": taxes,
        "slippage": slip,
        "pf": pf,
        "max_dd": max_dd,
        "avg_daily": round(net_pnl / days, 2),
        "worst_month": f"{worst_m_name} ({worst_m_val:+,.0f})",
    }


def run_comparison_for_period(bars_df: pd.DataFrame, period_label: str) -> None:
    df = bars_df.copy()
    df["dt"] = pd.to_datetime(df["date"])
    df["trade_date"] = df["dt"].dt.date
    daily_atr = calculate_daily_atr(df, period=14)

    # 1. Baseline: Strangle with Morning Filter (skip >= 0.60%)
    base_res = run_backtest_simulation(
        df,
        slippage=0.50,
        lots=10,
        lot_size=65,
        iv=0.13,
        r=0.065,
        strategy_max_loss=10000.0,
        use_leg_sl=True,
        trail_opposite_to_cost=True,
        morning_filter=True,
        morning_filter_threshold_pct=0.60,
        use_atr_strikes=True,
        atr_multiplier=1.25,
    )
    base_df = pd.DataFrame(base_res["trades"])
    base_dates = set(base_df["date"].tolist()) if not base_df.empty else set()

    # 2. Strangle without Morning Filter (for reference)
    nf_res = run_backtest_simulation(
        df,
        slippage=0.50,
        lots=10,
        lot_size=65,
        iv=0.13,
        r=0.065,
        strategy_max_loss=10000.0,
        use_leg_sl=True,
        trail_opposite_to_cost=True,
        morning_filter=False,
        morning_filter_threshold_pct=0.60,
        use_atr_strikes=True,
        atr_multiplier=1.25,
    )
    nf_df = pd.DataFrame(nf_res["trades"])

    # Identify skipped trend days (0.60%+ morning range)
    all_dates = sorted(df["trade_date"].unique())
    skipped_day_dfs: List[Tuple[Any, pd.DataFrame, float]] = []
    for t_date in all_dates:
        if str(t_date) in base_dates:
            continue
        day_df = df[df["trade_date"] == t_date].sort_values("dt")
        morning_bars = day_df[
            (day_df["dt"].dt.hour == 9) | ((day_df["dt"].dt.hour == 10) & (day_df["dt"].dt.minute == 0))
        ]
        if morning_bars.empty:
            continue
        m_open = float(morning_bars.iloc[0]["open"])
        m_high = float(morning_bars["high"].max())
        m_low = float(morning_bars["low"].min())
        m_range_pct = ((m_high - m_low) / m_open) * 100.0 if m_open > 0 else 0.0
        if m_range_pct >= 0.60:
            atr_val = float(daily_atr.get(t_date, 150.0))
            if pd.isna(atr_val) or atr_val <= 0:
                atr_val = 150.0
            skipped_day_dfs.append((t_date, day_df, atr_val))

    variants = [
        ("A. Credit Spread (All Skipped, ATM, 200w, 80% Tgt)", dict(
            chop_band=None, short_otm_mode="FIXED", short_otm_pts=0.0, wing_width_pts=200.0, target_decay_pct=0.80, require_orb_breakout=False
        )),
        ("B. Credit Spread (Chop-Filt 35/65, ATM, 200w, 80% Tgt)", dict(
            chop_band=(0.35, 0.65), short_otm_mode="FIXED", short_otm_pts=0.0, wing_width_pts=200.0, target_decay_pct=0.80, require_orb_breakout=False
        )),
        ("C. Credit Spread (Chop-Filt 35/65, 100-OTM, 200w, 80% Tgt)", dict(
            chop_band=(0.35, 0.65), short_otm_mode="FIXED", short_otm_pts=100.0, wing_width_pts=200.0, target_decay_pct=0.80, require_orb_breakout=False
        )),
        ("D. Credit Spread (Chop-Filt 35/65, 0.5xATR OTM, 250w, 80% Tgt)", dict(
            chop_band=(0.35, 0.65), short_otm_mode="ATR", short_atr_mult=0.5, wing_width_pts=250.0, target_decay_pct=0.80, require_orb_breakout=False
        )),
        ("E. Credit Spread (Chop-Filt 35/65, ATM, 200w, Hold 14:45)", dict(
            chop_band=(0.35, 0.65), short_otm_mode="FIXED", short_otm_pts=0.0, wing_width_pts=200.0, target_decay_pct=None, require_orb_breakout=False
        )),
        ("F. Credit Spread (ORB Breakout, 100-OTM, 200w, 80% Tgt)", dict(
            chop_band=None, short_otm_mode="FIXED", short_otm_pts=100.0, wing_width_pts=200.0, target_decay_pct=0.80, require_orb_breakout=True
        )),
    ]

    standalone_rows = []
    combined_rows = []

    base_sum = summarize_trades(base_df)
    nf_sum = summarize_trades(nf_df)

    # Strangle-only performance on those exact skipped days (for reference)
    nf_skipped_df = nf_df[nf_df["date"].isin([str(d[0]) for d in skipped_day_dfs])]
    nf_skip_sum = summarize_trades(nf_skipped_df)

    standalone_rows.append([
        "Ref: Unfiltered Strangle on Skipped Days",
        nf_skip_sum["days"], f"{nf_skip_sum['win_rate']}%",
        f"Rs {nf_skip_sum['net_pnl']:+,.0f}", nf_skip_sum["pf"],
        f"Rs {nf_skip_sum['max_dd']:+,.0f}", f"Rs {nf_skip_sum['avg_daily']:+,.0f}"
    ])

    combined_rows.append([
        "BASELINE (Strangle + Morning Filter Skip)",
        base_sum["days"], f"{base_sum['win_rate']}%",
        f"Rs {base_sum['net_pnl']:+,.0f}", base_sum["pf"],
        f"Rs {base_sum['max_dd']:+,.0f}", f"Rs {base_sum['avg_daily']:+,.0f}",
        base_sum["worst_month"], "—"
    ])
    combined_rows.append([
        "Ref: No Morning Filter (Strangle Every Day)",
        nf_sum["days"], f"{nf_sum['win_rate']}%",
        f"Rs {nf_sum['net_pnl']:+,.0f}", nf_sum["pf"],
        f"Rs {nf_sum['max_dd']:+,.0f}", f"Rs {nf_sum['avg_daily']:+,.0f}",
        nf_sum["worst_month"], f"Rs {nf_sum['net_pnl'] - base_sum['net_pnl']:+,.0f}"
    ])

    for vname, params in variants:
        cs_trades = []
        for t_date, day_df, atr_val in skipped_day_dfs:
            res = simulate_credit_spread_day(day_df, t_date, atr_val, mode=vname, **params)
            if res is not None:
                cs_trades.append(res)

        cs_df = pd.DataFrame(cs_trades)
        cs_sum = summarize_trades(cs_df)
        standalone_rows.append([
            vname, cs_sum["days"], f"{cs_sum['win_rate']}%",
            f"Rs {cs_sum['net_pnl']:+,.0f}", cs_sum["pf"],
            f"Rs {cs_sum['max_dd']:+,.0f}", f"Rs {cs_sum['avg_daily']:+,.0f}"
        ])

        comb_df = pd.concat([
            base_df[["date", "gross_pnl", "taxes", "slippage", "net_pnl"]],
            cs_df[["date", "gross_pnl", "taxes", "slippage", "net_pnl"]] if not cs_df.empty else pd.DataFrame()
        ], ignore_index=True)
        comb_sum = summarize_trades(comb_df)
        delta_vs_base = comb_sum["net_pnl"] - base_sum["net_pnl"]
        combined_rows.append([
            f"Baseline + {vname}",
            comb_sum["days"], f"{comb_sum['win_rate']}%",
            f"Rs {comb_sum['net_pnl']:+,.0f}", comb_sum["pf"],
            f"Rs {comb_sum['max_dd']:+,.0f}", f"Rs {comb_sum['avg_daily']:+,.0f}",
            comb_sum["worst_month"], f"Rs {delta_vs_base:+,.0f}"
        ])

    print("\n" + "=" * 120)
    print(f"  {period_label} | Total Triggered (>=0.60% Morning Range) Days: {len(skipped_day_dfs)}")
    print("=" * 120)
    print("\n[1] STANDALONE PERFORMANCE ON TRIGGERED TREND DAYS ONLY:")
    print(tabulate(
        standalone_rows,
        headers=["Strategy on Triggered Days", "Trades", "Win%", "Net PnL", "PF", "Max DD", "Avg/Trade"],
        tablefmt="pipe",
    ))

    print("\n[2] COMBINED PORTFOLIO PERFORMANCE (BASELINE STRANGLE + FALLBACK) VS BASELINE:")
    print(tabulate(
        combined_rows,
        headers=["Portfolio Variant", "Days", "Win%", "Net PnL", "PF", "Max DD", "Avg/Day", "Worst Month", "Delta vs Base"],
        tablefmt="pipe",
    ))


def main():
    df3 = pd.read_parquet("data/nifty_5m_3y.parquet")
    df3["dt"] = pd.to_datetime(df3["date"])
    cutoff_1y = df3["dt"].max() - timedelta(days=365)
    df1 = df3[df3["dt"] >= cutoff_1y].copy()

    print(f"1-Year Window: {df1['dt'].min().date()} to {df1['dt'].max().date()}")
    print(f"3-Year Window: {df3['dt'].min().date()} to {df3['dt'].max().date()}")

    run_comparison_for_period(df1, "LAST 1 YEAR")
    run_comparison_for_period(df3, "LAST 3 YEARS")


if __name__ == "__main__":
    main()
