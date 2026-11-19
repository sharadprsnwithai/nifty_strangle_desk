import sys
import os
from pathlib import Path
from datetime import datetime, time as dt_time
import pandas as pd
import numpy as np
from tabulate import tabulate

# Add project root to sys.path
sys.path.insert(0, os.path.abspath("."))
from strategy.costs import calculate_trade_costs


def run_nifty_orb_backtest(
    parquet_path: str = "data/nifty_5m_3y.parquet",
    lots: int = 2,
    lot_size: int = 65,
    initial_sl_pts: float = 50.0,
    target1_pts: float = 75.0,
    use_trend_filter: bool = True,
    slippage_pts: float = 0.5,
):
    print(f"Loading 5-minute historical data from {parquet_path}...")
    df_5m = pd.read_parquet(parquet_path)
    df_5m["date"] = pd.to_datetime(df_5m["date"])
    if df_5m["date"].dt.tz is not None:
        df_5m["date"] = df_5m["date"].dt.tz_convert("Asia/Kolkata")
    df_5m["time"] = df_5m["date"].dt.time
    df_5m["day"] = df_5m["date"].dt.date
    df_5m = df_5m.sort_values("date").reset_index(drop=True)

    print(f"Loaded {len(df_5m)} 5m bars from {df_5m['date'].min()} to {df_5m['date'].max()}")

    # Resample to 15-minute bars for signal generation
    # Align to market open 09:15
    df_indexed = df_5m.set_index("date")
    df_15m = df_indexed.resample("15min", offset="15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()

    df_15m["time"] = df_15m["date"].dt.time
    df_15m["day"] = df_15m["date"].dt.date

    # Calculate 50 EMA on 15m close
    df_15m["ema50"] = df_15m["close"].ewm(span=50, adjust=False).mean()

    # Pre-index 15m and 5m by day
    days_15m = {d: group for d, group in df_15m.groupby("day")}
    days_5m = {d: group for d, group in df_5m.groupby("day")}

    unique_days = sorted(list(days_15m.keys()))
    print(f"Total trading days available: {len(unique_days)}")

    trades = []
    lot1_lots = max(1, lots // 2)
    lot2_lots = lots - lot1_lots

    lot1_qty = lot1_lots * lot_size
    lot2_qty = lot2_lots * lot_size
    total_qty = lots * lot_size

    for d in unique_days:
        day_15m = days_15m[d]
        if d not in days_5m:
            continue
        day_5m = days_5m[d]

        # 1. Evaluate 09:15 - 09:45 30-min reference range (from 09:15 and 09:30 bars)
        ref_bars = day_15m[(day_15m["time"] >= dt_time(9, 15)) & (day_15m["time"] <= dt_time(9, 30))]
        if len(ref_bars) < 2:
            continue

        ref_high = float(ref_bars["high"].max())
        ref_low = float(ref_bars["low"].min())

        # 2. Breakout Evaluation Window: 09:45 to 13:30 (15m bars closing after 09:45 up to 13:30)
        # 15m bars starting at 09:45 close at 10:00, starting 13:15 close at 13:30
        scan_bars = day_15m[(day_15m["time"] >= dt_time(9, 45)) & (day_15m["time"] <= dt_time(13, 15))]

        active_trade = None

        for idx, bar in scan_bars.iterrows():
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            b_close = float(bar["close"])
            ema50_val = float(bar["ema50"])
            bar_start = bar["time"]
            bar_close_time = (bar["date"] + pd.Timedelta(minutes=15)).time()

            long_ok = (b_high > ref_high) and (b_close > ref_high)
            if use_trend_filter:
                long_ok = long_ok and (b_close > ema50_val)

            short_ok = (b_low < ref_low) and (b_close < ref_low)
            if use_trend_filter:
                short_ok = short_ok and (b_close < ema50_val)

            if long_ok:
                active_trade = {
                    "day": d,
                    "direction": "LONG",
                    "entry_time": bar_close_time,
                    "entry_date": bar["date"] + pd.Timedelta(minutes=15),
                    "entry_price": b_close + slippage_pts,
                    "ref_high": ref_high,
                    "ref_low": ref_low,
                    "sl_price": (b_close + slippage_pts) - initial_sl_pts,
                    "target1_price": (b_close + slippage_pts) + target1_pts,
                }
                break
            elif short_ok:
                active_trade = {
                    "day": d,
                    "direction": "SHORT",
                    "entry_time": bar_close_time,
                    "entry_date": bar["date"] + pd.Timedelta(minutes=15),
                    "entry_price": b_close - slippage_pts,
                    "ref_high": ref_high,
                    "ref_low": ref_low,
                    "sl_price": (b_close - slippage_pts) + initial_sl_pts,
                    "target1_price": (b_close - slippage_pts) - target1_pts,
                }
                break

        if active_trade is None:
            continue

        # 3. Simulate Intra-day Execution via 5-minute bars from entry time onwards
        direction = active_trade["direction"]
        entry_p = active_trade["entry_price"]
        sl_p = active_trade["sl_price"]
        t1_p = active_trade["target1_price"]
        entry_dt = active_trade["entry_date"]

        subseq_5m = day_5m[day_5m["date"] >= entry_dt].sort_values("date")

        t1_hit = False
        lot1_exit_p = None
        lot1_exit_time = None
        lot1_exit_reason = None

        lot2_exit_p = None
        lot2_exit_time = None
        lot2_exit_reason = None

        for _, bar5 in subseq_5m.iterrows():
            b_high5 = float(bar5["high"])
            b_low5 = float(bar5["low"])
            b_close5 = float(bar5["close"])
            b_time5 = bar5["time"]

            # Check 15:15 session cutoff
            is_eod_cutoff = b_time5 >= dt_time(15, 15)

            if not t1_hit:
                # Still holding both lots
                # Check Initial SL
                if direction == "LONG":
                    if b_low5 <= sl_p:
                        # Full SL hit
                        exit_price = sl_p - slippage_pts
                        lot1_exit_p = exit_price
                        lot2_exit_p = exit_price
                        lot1_exit_time = b_time5
                        lot2_exit_time = b_time5
                        lot1_exit_reason = "INITIAL_SL"
                        lot2_exit_reason = "INITIAL_SL"
                        break
                    elif b_high5 >= t1_p:
                        # Target 1 hit!
                        t1_hit = True
                        lot1_exit_p = t1_p - slippage_pts
                        lot1_exit_time = b_time5
                        lot1_exit_reason = "TARGET1_HIT"
                        # Trail Runner SL to Cost (Entry price)
                        sl_p = entry_p
                else:  # SHORT
                    if b_high5 >= sl_p:
                        exit_price = sl_p + slippage_pts
                        lot1_exit_p = exit_price
                        lot2_exit_p = exit_price
                        lot1_exit_time = b_time5
                        lot2_exit_time = b_time5
                        lot1_exit_reason = "INITIAL_SL"
                        lot2_exit_reason = "INITIAL_SL"
                        break
                    elif b_low5 <= t1_p:
                        t1_hit = True
                        lot1_exit_p = t1_p + slippage_pts
                        lot1_exit_time = b_time5
                        lot1_exit_reason = "TARGET1_HIT"
                        sl_p = entry_p

            if t1_hit and lot2_exit_p is None:
                # Lot 1 already exited, managing Lot 2 Runner with SL at Cost
                if direction == "LONG":
                    if b_low5 <= sl_p:
                        # Runner hit Cost / Breakeven
                        lot2_exit_p = sl_p - slippage_pts
                        lot2_exit_time = b_time5
                        lot2_exit_reason = "COST_REVERSAL"
                        break
                    elif is_eod_cutoff:
                        lot2_exit_p = b_close5 - slippage_pts
                        lot2_exit_time = b_time5
                        lot2_exit_reason = "EOD_1515_CUTOFF"
                        break
                else:  # SHORT
                    if b_high5 >= sl_p:
                        lot2_exit_p = sl_p + slippage_pts
                        lot2_exit_time = b_time5
                        lot2_exit_reason = "COST_REVERSAL"
                        break
                    elif is_eod_cutoff:
                        lot2_exit_p = b_close5 + slippage_pts
                        lot2_exit_time = b_time5
                        lot2_exit_reason = "EOD_1515_CUTOFF"
                        break

        # If EOD reached without SL or Target
        if lot1_exit_p is None:
            last_p = subseq_5m.iloc[-1]["close"]
            lot1_exit_p = last_p
            lot2_exit_p = last_p
            lot1_exit_time = subseq_5m.iloc[-1]["time"]
            lot2_exit_time = subseq_5m.iloc[-1]["time"]
            lot1_exit_reason = "EOD_1515_CUTOFF"
            lot2_exit_reason = "EOD_1515_CUTOFF"

        # Calculate PnL
        if direction == "LONG":
            lot1_pts = lot1_exit_p - entry_p
            lot2_pts = lot2_exit_p - entry_p
        else:
            lot1_pts = entry_p - lot1_exit_p
            lot2_pts = entry_p - lot2_exit_p

        lot1_pnl_rs = lot1_pts * lot1_qty
        lot2_pnl_rs = lot2_pts * lot2_qty
        gross_pnl_rs = lot1_pnl_rs + lot2_pnl_rs

        # Futures transaction charges & STT
        # Turnover = (entry_p * total_qty) + (lot1_exit_p * lot1_qty) + (lot2_exit_p * lot2_qty)
        # Approximate Zerodha F&O cost ~ 0.02% of turnover + brokerage
        turnover = (entry_p * total_qty) + (lot1_exit_p * lot1_qty) + (lot2_exit_p * lot2_qty)
        charges_rs = 60.0 + (turnover * 0.00015)  # Rs 20 per order * 3 legs + STT & exchange fees
        net_pnl_rs = gross_pnl_rs - charges_rs

        trade_record = {
            "day": d,
            "direction": direction,
            "entry_time": str(active_trade["entry_time"]),
            "entry_price": round(entry_p, 1),
            "lot1_exit_price": round(lot1_exit_p, 1),
            "lot1_exit_time": str(lot1_exit_time),
            "lot1_reason": lot1_exit_reason,
            "lot1_pts": round(lot1_pts, 1),
            "lot2_exit_price": round(lot2_exit_p, 1),
            "lot2_exit_time": str(lot2_exit_time),
            "lot2_reason": lot2_exit_reason,
            "lot2_pts": round(lot2_pts, 1),
            "avg_pts": round((lot1_pts + lot2_pts) / 2.0, 1),
            "gross_pnl_rs": round(gross_pnl_rs, 2),
            "charges_rs": round(charges_rs, 2),
            "net_pnl_rs": round(net_pnl_rs, 2),
            "target1_hit": t1_hit,
        }
        trades.append(trade_record)

    tdf = pd.DataFrame(trades)
    return tdf


if __name__ == "__main__":
    tdf = run_nifty_orb_backtest()
    tdf.to_csv("data/nifty_orb_3y_trades.csv", index=False)

    print("\n" + "=" * 90)
    print("        NIFTY 50 30-MIN ORB FUTURES STRATEGY: 3-YEAR BACKTEST SUMMARY")
    print("=" * 90)

    total_trades = len(tdf)
    win_trades = (tdf["net_pnl_rs"] > 0).sum()
    loss_trades = (tdf["net_pnl_rs"] < 0).sum()
    win_rate = (win_trades / total_trades) * 100 if total_trades > 0 else 0.0

    gross_pnl = tdf["gross_pnl_rs"].sum()
    total_charges = tdf["charges_rs"].sum()
    net_pnl = tdf["net_pnl_rs"].sum()

    avg_win = tdf[tdf["net_pnl_rs"] > 0]["net_pnl_rs"].mean() if win_trades > 0 else 0.0
    avg_loss = abs(tdf[tdf["net_pnl_rs"] < 0]["net_pnl_rs"].mean()) if loss_trades > 0 else 0.0
    profit_factor = (tdf[tdf["net_pnl_rs"] > 0]["net_pnl_rs"].sum() / abs(tdf[tdf["net_pnl_rs"] < 0]["net_pnl_rs"].sum())) if loss_trades > 0 else np.nan

    # Drawdown calculation
    tdf["cum_net_pnl"] = tdf["net_pnl_rs"].cumsum()
    tdf["cum_peak"] = tdf["cum_net_pnl"].cummax()
    tdf["drawdown_rs"] = tdf["cum_peak"] - tdf["cum_net_pnl"]
    max_dd = tdf["drawdown_rs"].max()

    long_trades = tdf[tdf["direction"] == "LONG"]
    short_trades = tdf[tdf["direction"] == "SHORT"]

    t1_hit_count = tdf["target1_hit"].sum()

    print(f"Total Trading Days Processed : {total_trades}")
    print(f"Total Trades Taken           : {total_trades}")
    print(f"Target 1 Hit Rate            : {t1_hit_count} / {total_trades} ({t1_hit_count/total_trades*100:.1f}%)")
    print(f"Winning Trades               : {win_trades} ({win_rate:.1f}%)")
    print(f"Losing Trades                : {loss_trades} ({loss_trades/total_trades*100:.1f}%)")
    print(f"Profit Factor                : {profit_factor:.2f}")
    print(f"Average Win                  : Rs {avg_win:,.2f}")
    print(f"Average Loss                 : Rs {avg_loss:,.2f}")
    print(f"Win/Loss Ratio               : {avg_win/avg_loss:.2f}" if avg_loss > 0 else "N/A")
    print("-" * 90)
    print(f"Gross P&L                    : Rs {gross_pnl:+,.2f}")
    print(f"Total Brokerage & Taxes      : Rs {total_charges:,.2f}")
    print(f"Net Realized P&L             : Rs {net_pnl:+,.2f}")
    print(f"Maximum Drawdown             : Rs {max_dd:,.2f}")
    print("-" * 90)
    print(f"Long Trades                  : {len(long_trades)} | Net PnL: Rs {long_trades['net_pnl_rs'].sum():+,.2f} | Win Rate: {(long_trades['net_pnl_rs']>0).sum()/len(long_trades)*100:.1f}%")
    print(f"Short Trades                 : {len(short_trades)} | Net PnL: Rs {short_trades['net_pnl_rs'].sum():+,.2f} | Win Rate: {(short_trades['net_pnl_rs']>0).sum()/len(short_trades)*100:.1f}%")

    # Monthly breakdown
    tdf["month_year"] = pd.to_datetime(tdf["day"]).dt.to_period("M").astype(str)
    monthly = []
    for m, grp in tdf.groupby("month_year"):
        m_tot = len(grp)
        m_win = (grp["net_pnl_rs"] > 0).sum()
        m_loss = (grp["net_pnl_rs"] < 0).sum()
        m_net = grp["net_pnl_rs"].sum()
        m_pts = grp["avg_pts"].sum()
        monthly.append({
            "Month": m,
            "Trades": m_tot,
            "Win Rate (%)": f"{(m_win/m_tot)*100:.0f}%",
            "Points": f"{m_pts:+.1f}",
            "Net P&L (Rs)": f"Rs {m_net:+,.2f}",
        })
    mdf = pd.DataFrame(monthly)
    print("\n" + "=" * 90)
    print("                           MONTHLY PERFORMANCE BREAKDOWN")
    print("=" * 90)
    print(tabulate(mdf, headers="keys", tablefmt="pipe", showindex=False))

    # Yearly breakdown
    tdf["year"] = pd.to_datetime(tdf["day"]).dt.year
    yearly = []
    for yr, grp in tdf.groupby("year"):
        y_tot = len(grp)
        y_win = (grp["net_pnl_rs"] > 0).sum()
        y_net = grp["net_pnl_rs"].sum()
        y_charges = grp["charges_rs"].sum()
        y_cum = grp["net_pnl_rs"].cumsum()
        y_dd = (y_cum.cummax() - y_cum).max()
        yearly.append({
            "Year": yr,
            "Trades": y_tot,
            "Win Rate (%)": f"{(y_win/y_tot)*100:.1f}%",
            "Net P&L (Rs)": f"Rs {y_net:+,.2f}",
            "Max DD (Rs)": f"Rs {y_dd:,.2f}",
        })
    ydf = pd.DataFrame(yearly)
    print("\n" + "=" * 90)
    print("                            YEAR-WISE PERFORMANCE")
    print("=" * 90)
    print(tabulate(ydf, headers="keys", tablefmt="pipe", showindex=False))
