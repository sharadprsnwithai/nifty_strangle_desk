"""Backtest for NIFTY 50 Intraday Directional ORB + TWAP/EMA Option Selling."""
import argparse
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from tabulate import tabulate

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from strategy.backtest import get_weekly_expiry_for_date
from strategy.costs import calculate_trade_costs
from strategy.directional_orb_desk import compute_adx14
from strategy.options_model import black_76_price, compute_fractional_dte

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest.directional_orb")


def _compute_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "gross_pnl": 0.0,
            "total_costs": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_net_per_trade": 0.0,
        }

    net_pnls = [t["net_pnl"] for t in trades]
    gross_pnls = [t["gross_pnl"] for t in trades]
    costs = [t["total_costs"] for t in trades]
    wins = sum(1 for p in net_pnls if p > 0)
    losses = sum(1 for p in net_pnls if p <= 0)

    gross_win = sum(p for p in net_pnls if p > 0)
    gross_loss = abs(sum(p for p in net_pnls if p < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.99 if gross_win > 0 else 0.0)

    cum = np.cumsum(net_pnls)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / len(trades)) * 100.0, 1),
        "gross_pnl": round(sum(gross_pnls), 2),
        "total_costs": round(sum(costs), 2),
        "net_pnl": round(sum(net_pnls), 2),
        "profit_factor": pf,
        "max_drawdown": round(max_dd, 2),
        "avg_net_per_trade": round(sum(net_pnls) / len(trades), 2),
    }


def run_directional_orb_backtest(
    bars_df: pd.DataFrame,
    orb_end_time: time = time(9, 30),
    entry_cutoff_time: time = time(13, 30),
    exit_cutoff_time: time = time(15, 15),
    otm_offset: int = 0,
    hedge_width: int = 0,
    leg_sl_pct: float = 0.35,
    trail_to_cost_decay_pct: float = 0.30,
    target_decay_pct: float = 0.75,
    use_ema_exit: bool = True,
    min_adx: float = 0.0,
    max_orb_width_pct: float = 0.0,
    lots: int = 10,
    lot_size: int = 65,
    slippage: float = 0.50,
    iv: float = 0.13,
    r: float = 0.065,
    expiry_weekday: int = 1,
) -> Dict[str, Any]:
    """Simulate intraday ORB + TWAP + EMA(9/21) directional option selling."""
    df = bars_df.copy()
    ts_col = "date" if "date" in df.columns else ("timestamps" if "timestamps" in df.columns else df.columns[0])
    df["dt"] = pd.to_datetime(df[ts_col])
    df["trade_date"] = df["dt"].dt.date
    df = df.sort_values("dt").reset_index(drop=True)

    # Continuous EMAs and 5m ADX(14) across the 5m series
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    if "vix" not in df.columns:
        df["vix"] = iv * 100.0
    df["adx14"] = compute_adx14(df)

    qty = lots * lot_size
    trades: List[Dict[str, Any]] = []

    for t_date, day_df in df.groupby("trade_date", sort=True):
        day_df = day_df.sort_values("dt").copy()
        day_df["twap"] = day_df["tp"].expanding().mean()

        orb_bars = day_df[day_df["dt"].dt.time < orb_end_time]
        if orb_bars.empty:
            continue
        orb_high = float(orb_bars["high"].max())
        orb_low = float(orb_bars["low"].min())
        orb_open = float(orb_bars.iloc[0]["open"])
        if max_orb_width_pct > 0 and orb_open > 0:
            if ((orb_high - orb_low) / orb_open) * 100.0 > max_orb_width_pct:
                continue

        target_expiry, is_zero_dte = get_weekly_expiry_for_date(t_date, expiry_weekday=expiry_weekday)

        in_trade = False
        direction: Optional[str] = None  # "BULL" (sell PE) or "BEAR" (sell CE)
        opt_type = ""
        short_strike = 0.0
        hedge_strike = 0.0
        entry_dt = None
        entry_spot = 0.0
        short_entry_px = 0.0
        hedge_entry_px = 0.0
        trailed_to_cost = False
        exit_reason = ""
        exit_dt = None
        short_exit_px = 0.0
        hedge_exit_px = 0.0

        post_orb = day_df[day_df["dt"].dt.time >= orb_end_time]
        for _, row in post_orb.iterrows():
            bar_dt = row["dt"].to_pydatetime()
            bar_t = bar_dt.time()
            close = float(row["close"])
            twap = float(row["twap"])
            ema9 = float(row["ema9"])
            ema21 = float(row["ema21"])
            bar_iv = max(float(row["vix"]) / 100.0, 0.05)

            if not in_trade:
                if bar_t > entry_cutoff_time or bar_t >= exit_cutoff_time:
                    break
                if min_adx > 0 and float(row["adx14"]) < min_adx:
                    continue
                if close > orb_high and close > twap and ema9 > ema21:
                    direction = "BULL"
                    opt_type = "PE"
                elif close < orb_low and close < twap and ema9 < ema21:
                    direction = "BEAR"
                    opt_type = "CE"
                else:
                    continue

                atm = round(close / 50.0) * 50.0
                if opt_type == "PE":
                    short_strike = atm - otm_offset
                    hedge_strike = short_strike - hedge_width if hedge_width > 0 else 0.0
                else:
                    short_strike = atm + otm_offset
                    hedge_strike = short_strike + hedge_width if hedge_width > 0 else 0.0

                dte = compute_fractional_dte(target_expiry, bar_dt)
                short_entry_px = black_76_price(close, short_strike, dte, bar_iv, r, opt_type)
                hedge_entry_px = (
                    black_76_price(close, hedge_strike, dte, bar_iv, r, opt_type)
                    if hedge_width > 0
                    else 0.0
                )
                entry_dt = bar_dt
                entry_spot = close
                entry_iv = bar_iv
                in_trade = True
                continue

            # Manage open trade
            dte = compute_fractional_dte(target_expiry, bar_dt)
            cur_short_px = black_76_price(close, short_strike, dte, bar_iv, r, opt_type)
            cur_hedge_px = (
                black_76_price(close, hedge_strike, dte, bar_iv, r, opt_type)
                if hedge_width > 0
                else 0.0
            )

            # Trail SL to cost once short leg decays by trail_to_cost_decay_pct
            if not trailed_to_cost and cur_short_px <= short_entry_px * (1.0 - trail_to_cost_decay_pct):
                trailed_to_cost = True

            sl_trigger_px = short_entry_px if trailed_to_cost else short_entry_px * (1.0 + leg_sl_pct)
            target_px = short_entry_px * (1.0 - target_decay_pct)

            if bar_t >= exit_cutoff_time:
                exit_reason = "EOD_CUTOFF"
            elif cur_short_px >= sl_trigger_px:
                exit_reason = "COST_TRAIL_SL" if trailed_to_cost else "LEG_SL"
            elif cur_short_px <= target_px:
                exit_reason = "TARGET_DECAY"
            elif use_ema_exit and ((direction == "BULL" and close < ema21) or (direction == "BEAR" and close > ema21)):
                exit_reason = "EMA21_REVERSAL"

            if exit_reason:
                exit_dt = bar_dt
                short_exit_px = cur_short_px
                hedge_exit_px = cur_hedge_px
                break

        if in_trade and not exit_reason:
            last_row = day_df.iloc[-1]
            exit_dt = last_row["dt"].to_pydatetime()
            dte = compute_fractional_dte(target_expiry, exit_dt)
            last_iv = max(float(last_row["vix"]) / 100.0, 0.05)
            short_exit_px = black_76_price(float(last_row["close"]), short_strike, dte, last_iv, r, opt_type)
            hedge_exit_px = (
                black_76_price(float(last_row["close"]), hedge_strike, dte, last_iv, r, opt_type)
                if hedge_width > 0
                else 0.0
            )
            exit_reason = "SESSION_END"

        if in_trade:
            short_costs = calculate_trade_costs(short_entry_px, short_exit_px, qty, slippage_per_share=slippage)
            if hedge_width > 0:
                # Long hedge leg: bought at entry, sold at exit (note: STT applies on sell side in real life;
                # calculate_trade_costs models short-leg STT, which is a close approximation for spread cost)
                hedge_costs = calculate_trade_costs(hedge_exit_px, hedge_entry_px, qty, slippage_per_share=slippage)
                gross_pnl = round(short_costs["gross_pnl"] + hedge_costs["gross_pnl"], 2)
                total_costs = round(short_costs["total_costs"] + hedge_costs["total_costs"], 2)
                net_pnl = round(gross_pnl - total_costs, 2)
            else:
                gross_pnl = short_costs["gross_pnl"]
                total_costs = short_costs["total_costs"]
                net_pnl = short_costs["net_pnl"]

            trades.append({
                "date": str(t_date),
                "direction": direction,
                "opt_type": opt_type,
                "short_strike": short_strike,
                "hedge_strike": hedge_strike,
                "entry_time": entry_dt.strftime("%H:%M"),
                "exit_time": exit_dt.strftime("%H:%M"),
                "entry_spot": round(entry_spot, 2),
                "entry_vix": round(entry_iv * 100.0, 2),
                "short_entry": short_entry_px,
                "short_exit": short_exit_px,
                "exit_reason": exit_reason,
                "gross_pnl": gross_pnl,
                "total_costs": total_costs,
                "net_pnl": net_pnl,
            })

    return {"summary": _compute_metrics(trades), "trades": trades}


def fetch_kite_nifty_5m(days: int = 45, include_vix: bool = False) -> pd.DataFrame:
    """Fetch fresh NIFTY 50 (and optionally INDIA VIX) 5-minute bars from Kite Connect API."""
    from kite import get_kite_client

    kite = get_kite_client()
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    records = kite.historical_data(256265, start_dt, end_dt, "5minute")
    df = pd.DataFrame(records)
    if include_vix and not df.empty:
        vix_records = kite.historical_data(264969, start_dt, end_dt, "5minute")
        vix_df = pd.DataFrame(vix_records)
        if not vix_df.empty:
            df["date"] = pd.to_datetime(df["date"])
            vix_df["date"] = pd.to_datetime(vix_df["date"])
            df = df.sort_values("date")
            vix_df = vix_df[["date", "close"]].rename(columns={"close": "vix"}).sort_values("date")
            df = pd.merge_asof(df, vix_df, on="date", direction="backward")
            df["vix"] = df["vix"].bfill().fillna(13.0)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest NIFTY 50 Directional ORB Option Selling")
    parser.add_argument("--cache", type=str, default="data/nifty_5m_cache.parquet")
    parser.add_argument("--fetch-kite", action="store_true", help="Fetch fresh 5m data from Kite Connect")
    parser.add_argument("--use-vix", action="store_true", help="Fetch and use actual 5m INDIA VIX from Kite")
    parser.add_argument("--days", type=int, default=0, help="Filter results to last N calendar days (0 = all)")
    parser.add_argument("--single", "--single-15m-atm", dest="single", action="store_true", help="Run single 15m ORB variant and print trade log")
    parser.add_argument("--compare-spreads", action="store_true", help="Compare 100 OTM Naked vs 200/300/400-pt Credit Spreads")
    parser.add_argument("--otm-offset", type=int, default=0, help="OTM offset in points for --single (default: 0)")
    parser.add_argument("--hedge-width", type=int, default=0, help="Hedge wing width in points for --single (default: 0)")
    parser.add_argument("--orb-end", type=str, default="09:30", help="ORB end time HH:MM for --single (default: 09:30)")
    parser.add_argument("--leg-sl", type=float, default=0.35, help="Leg SL fraction for --single (default: 0.35)")
    parser.add_argument("--min-adx", type=float, default=20.0, help="Min 5m ADX(14) for --single (0 = disabled, default: 20.0)")
    parser.add_argument("--max-orb-width-pct", type=float, default=0.65, help="Max ORB width %% for --single (0 = disabled, default: 0.65)")
    parser.add_argument("--lots", type=int, default=5)
    parser.add_argument("--slippage", type=float, default=0.50)
    args = parser.parse_args()

    if args.fetch_kite:
        fetch_days = max(args.days + 15, 45) if args.days > 0 else 180
        df = fetch_kite_nifty_5m(days=fetch_days, include_vix=args.use_vix)
    else:
        df = pd.read_parquet(ROOT_DIR / args.cache)

    ts_col = "date" if "date" in df.columns else "timestamps"
    dt = pd.to_datetime(df[ts_col])
    print(f"Loaded {len(df)} bars | {dt.dt.date.nunique()} trading days ({dt.min().date()} to {dt.max().date()})")

    if args.compare_spreads:
        widths = [0, 200, 300, 400]
        comp_rows = []
        best_spread_trades = None
        for w in widths:
            res = run_directional_orb_backtest(
                df,
                lots=args.lots,
                slippage=args.slippage,
                orb_end_time=time(9, 30),
                otm_offset=100,
                hedge_width=w,
                leg_sl_pct=0.40,
                use_ema_exit=False,
            )
            trades = res["trades"]
            if args.days > 0 and trades:
                cutoff_date = (dt.max() - timedelta(days=args.days)).date()
                trades = [t for t in trades if datetime.strptime(t["date"], "%Y-%m-%d").date() >= cutoff_date]
            s = _compute_metrics(trades)
            worst = min((t["net_pnl"] for t in trades), default=0.0)
            name = "100 OTM Naked" if w == 0 else f"100/{100 + w} OTM Spread ({w}w)"
            comp_rows.append([
                name,
                s["trades"],
                f"{s['win_rate_pct']:.1f}%",
                f"Rs. {s['gross_pnl']:+,.0f}",
                f"Rs. {s['total_costs']:,.0f}",
                f"Rs. {s['net_pnl']:+,.0f}",
                s["profit_factor"],
                f"Rs. {s['max_drawdown']:,.0f}",
                f"Rs. {worst:+,.0f}",
            ])
            if w == 200:
                best_spread_trades = trades
        print(f"\n15m ORB | 100 OTM | SL 40% — Naked vs Credit Spreads (Actual VIX, Last {args.days or 'All'} Days)")
        print(tabulate(
            comp_rows,
            headers=["Structure", "Trades", "Win%", "Gross P&L", "Costs+Slip", "Net P&L", "PF", "Max DD", "Worst Trade"],
            tablefmt="github",
        ))
        if best_spread_trades:
            print("\nTrade-by-Trade Comparison (100/300 OTM Spread, 200w):")
            t_rows = [
                [
                    t["date"],
                    t["direction"],
                    f"{int(t['short_strike'])}/{int(t['hedge_strike'])}{t['opt_type']}",
                    t["exit_reason"],
                    f"Rs. {t['net_pnl']:+,.0f}",
                ]
                for t in best_spread_trades
            ]
            print(tabulate(t_rows, headers=["Date", "Dir", "Spread", "Reason", "Net P&L"], tablefmt="github"))
        return

    if args.single:
        oh, om = map(int, args.orb_end.split(":"))
        res = run_directional_orb_backtest(
            df,
            lots=args.lots,
            slippage=args.slippage,
            orb_end_time=time(oh, om),
            otm_offset=args.otm_offset,
            hedge_width=args.hedge_width,
            leg_sl_pct=args.leg_sl,
            use_ema_exit=False,
            min_adx=args.min_adx,
            max_orb_width_pct=args.max_orb_width_pct,
        )
        trades = res["trades"]
        if args.days > 0 and trades:
            cutoff_date = (dt.max() - timedelta(days=args.days)).date()
            trades = [t for t in trades if datetime.strptime(t["date"], "%Y-%m-%d").date() >= cutoff_date]

        s = _compute_metrics(trades)
        orb_mins = (oh * 60 + om) - (9 * 60 + 15)
        struct = f"{args.otm_offset} OTM Naked" if args.hedge_width == 0 else f"{args.otm_offset}/{args.otm_offset + args.hedge_width} Spread"
        label = f"{orb_mins}m ORB ({args.orb_end}) | {struct} | SL {int(args.leg_sl * 100)}%"
        vix_note = "Actual 5m INDIA VIX" if args.use_vix else "Constant 13% IV"
        print(f"\n{label} ({vix_note}) — Last {args.days or 'All'} Days")
        print(tabulate(
            [[
                s["trades"],
                f"{s['wins']}W / {s['losses']}L",
                f"{s['win_rate_pct']:.1f}%",
                f"Rs. {s['gross_pnl']:+,.2f}",
                f"Rs. {s['total_costs']:,.2f}",
                f"Rs. {s['net_pnl']:+,.2f}",
                s["profit_factor"],
                f"Rs. {s['max_drawdown']:,.2f}",
                f"Rs. {s['avg_net_per_trade']:+,.2f}",
            ]],
            headers=["Trades", "W/L", "Win%", "Gross P&L", "Costs+Slip", "Net P&L", "PF", "Max DD", "Avg/Trade"],
            tablefmt="github",
        ))
        if trades:
            print("\nTrade-by-Trade Log:")
            t_rows = [
                [
                    t["date"],
                    t["direction"],
                    f"{int(t['short_strike'])}{t['opt_type']}",
                    t["entry_time"],
                    t["exit_time"],
                    t["entry_spot"],
                    t["entry_vix"],
                    t["short_entry"],
                    t["short_exit"],
                    t["exit_reason"],
                    f"Rs. {t['net_pnl']:+,.0f}",
                ]
                for t in trades
            ]
            print(tabulate(
                t_rows,
                headers=["Date", "Dir", "Strike", "In", "Out", "Spot", "VIX%", "Entry", "Exit", "Reason", "Net P&L"],
                tablefmt="github",
            ))
        return

    configs = [
        ("15m ORB | ATM Naked | SL 35% + EMA21", dict(orb_end_time=time(9, 30), otm_offset=0, hedge_width=0, leg_sl_pct=0.35, use_ema_exit=True)),
        ("15m ORB | ATM Naked | SL 35% (No EMA Exit)", dict(orb_end_time=time(9, 30), otm_offset=0, hedge_width=0, leg_sl_pct=0.35, use_ema_exit=False)),
        ("15m ORB | 100 OTM Naked | SL 40%", dict(orb_end_time=time(9, 30), otm_offset=100, hedge_width=0, leg_sl_pct=0.40, use_ema_exit=False)),
        ("15m ORB | ATM/400 Credit Spread | SL 35%", dict(orb_end_time=time(9, 30), otm_offset=0, hedge_width=400, leg_sl_pct=0.35, use_ema_exit=False)),
        ("30m ORB (09:45) | 100 OTM Naked | SL 50% | ADX>=20 & ORB<=0.65%", dict(orb_end_time=time(9, 45), otm_offset=100, hedge_width=0, leg_sl_pct=0.50, use_ema_exit=False, min_adx=20.0, max_orb_width_pct=0.65)),
        ("45m ORB (10:00) | ATM Naked | SL 35%", dict(orb_end_time=time(10, 0), otm_offset=0, hedge_width=0, leg_sl_pct=0.35, use_ema_exit=False)),
        ("45m ORB (10:00) | 100 OTM Naked | SL 40%", dict(orb_end_time=time(10, 0), otm_offset=100, hedge_width=0, leg_sl_pct=0.40, use_ema_exit=False)),
        ("45m ORB (10:00) | ATM/400 Spread | SL 35%", dict(orb_end_time=time(10, 0), otm_offset=0, hedge_width=400, leg_sl_pct=0.35, use_ema_exit=False)),
    ]

    rows = []
    best_trades = None
    best_name = ""
    best_net = -1e18

    for name, kw in configs:
        res = run_directional_orb_backtest(df, lots=args.lots, slippage=args.slippage, **kw)
        s = res["summary"]
        rows.append([
            name,
            s["trades"],
            f"{s['win_rate_pct']:.1f}%",
            f"Rs. {s['gross_pnl']:+,.0f}",
            f"Rs. {s['total_costs']:,.0f}",
            f"Rs. {s['net_pnl']:+,.0f}",
            s["profit_factor"],
            f"Rs. {s['max_drawdown']:,.0f}",
            f"Rs. {s['avg_net_per_trade']:+,.0f}",
        ])
        if s["net_pnl"] > best_net:
            best_net = s["net_pnl"]
            best_trades = res["trades"]
            best_name = name

    print("\n" + tabulate(
        rows,
        headers=["Variant", "Trades", "Win%", "Gross P&L", "Costs+Slip", "Net P&L", "PF", "Max DD", "Avg/Trade"],
        tablefmt="github",
    ))

    if best_trades:
        tdf = pd.DataFrame(best_trades)
        out_csv = ROOT_DIR / "data" / "backtest_directional_orb.csv"
        tdf.to_csv(out_csv, index=False)
        print(f"\nBest Variant Exit Breakdown ({best_name}):")
        print(tdf.groupby("exit_reason")["net_pnl"].agg(["count", "sum", "mean"]).to_string())
        print(f"\nSaved trade log to {out_csv}")


if __name__ == "__main__":
    main()
