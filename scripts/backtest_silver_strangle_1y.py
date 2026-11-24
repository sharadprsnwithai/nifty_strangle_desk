import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
import numpy as np

from config import load_config
from kite import get_kite_client
from scripts.backtest_silver_strangle import run_strangle_backtest

def main():
    cache_file = Path("data/silverm_5m_1year.parquet")
    if cache_file.exists():
        print("Loading cached 1-year SILVERM 5m data...")
        df = pd.read_parquet(cache_file)
    else:
        print("Fetching full 1-year 5m data for SILVERM26NOVFUT from Kite API...")
        cfg = load_config()
        k = get_kite_client(config=cfg, check_market_hours=False, ignore_holidays=True)
        token = 123668487 # SILVERM26NOVFUT
        end = datetime.now()
        chunks = []
        cur_end = end
        for i in range(8):
            cur_start = cur_end - timedelta(days=60)
            try:
                c = k.historical_data(instrument_token=token, from_date=cur_start, to_date=cur_end, interval='5minute')
                if c:
                    chunks = c + chunks
                    cur_end = c[0]['date'] - timedelta(minutes=5)
                else:
                    break
            except Exception as e:
                print('Error:', e)
                break

        df = pd.DataFrame(chunks)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_file)

    print(f"Dataset has {len(df)} 5m bars from {df['date'].min()} to {df['date'].max()}")
    results = run_strangle_backtest(df)
    results.to_csv("data/silver_strangle_1year_trades.csv", index=False)
    print(f"Total trading sessions executed: {len(results)}")
    
    # Analyze results
    results['month'] = pd.to_datetime(results['date']).dt.to_period('M')
    print("\nSample recent trades:")
    print(results.tail(10)[["date", "spot_entry", "ce_strike", "pe_strike", "ce_pts", "pe_pts", "total_pts", "ce_reason", "pe_reason"]])

    total_pts = results["total_pts"].sum()
    win_days = (results["total_pts"] > 0).sum()
    loss_days = (results["total_pts"] < 0).sum()
    win_rate = win_days / len(results) * 100

    avg_win = results[results["total_pts"] > 0]["total_pts"].mean()
    avg_loss = results[results["total_pts"] < 0]["total_pts"].mean()
    profit_factor = abs(results[results["total_pts"] > 0]["total_pts"].sum() / results[results["total_pts"] < 0]["total_pts"].sum()) if loss_days > 0 else 999.0

    cum_pts = results["total_pts"].cumsum()
    peak = cum_pts.cummax()
    dd = peak - cum_pts
    max_dd_pts = dd.max()

    print("\n--- 1-YEAR OVERALL PERFORMANCE SUMMARY ---")
    print(f"Date Range: {results['date'].min()} to {results['date'].max()}")
    print(f"Total Trading Sessions: {len(results)}")
    print(f"Win Sessions: {win_days} | Loss Sessions: {loss_days} | Win Rate: {win_rate:.1f}%")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Total Net Points Captured: {total_pts:,.2f} pts")
    print(f"Average Points per Day: {total_pts / len(results):.2f} pts")
    print(f"Avg Win: +{avg_win:.2f} pts | Avg Loss: {avg_loss:.2f} pts")
    print(f"Max Drawdown: {max_dd_pts:,.2f} pts")

    # Multiplier for SILVERM (Silver Mini: 1 lot = 5 kg, 2 lots = 10 kg -> 1 pt = Rs 10)
    silverm_gross = total_pts * 10.0
    silverm_costs = len(results) * 120.0
    silverm_net = silverm_gross - silverm_costs
    silverm_max_dd = max_dd_pts * 10.0

    # Capital assumptions
    margin_req = 120000.0 # ~Rs 1.2 Lakhs for 2 lots
    safe_capital = 200000.0 # Rs 2.0 Lakhs recommended capital
    conservative_cap = 250000.0 # Rs 2.5 Lakhs conservative capital

    roi_safe = (silverm_net / safe_capital) * 100.0
    roi_conservative = (silverm_net / conservative_cap) * 100.0
    roi_margin = (silverm_net / margin_req) * 100.0

    print("\n" + "=" * 65)
    print("      MCX SILVERM EVENING STRANGLE (2 LOTS = 10 KG) 1-YEAR ROI")
    print("=" * 65)
    print(f"Gross P&L (10 kg multiplier) : Rs {silverm_gross:,.2f}")
    print(f"Taxes & Charges (Rs 120/day) : -Rs {silverm_costs:,.2f}")
    print(f"NET REALIZED P&L             : Rs {silverm_net:,.2f}")
    print(f"Max Peak-to-Trough Drawdown  : Rs {silverm_max_dd:,.2f} ({(silverm_max_dd/safe_capital)*100:.1f}% of Safe Cap)")
    print("-" * 65)
    print(f"ROI on Peak Margin (Rs 1.20L): {roi_margin:.2f}%")
    print(f"ROI on Safe Capital (Rs 2.00L): {roi_safe:.2f}% (Annualized: {roi_safe:.2f}%)")
    print(f"ROI on Conservative (Rs 2.50L): {roi_conservative:.2f}%")
    print("=" * 65)

    # Monthly breakdown
    m_grp = results.groupby('month').agg(
        sessions=('total_pts', 'count'),
        wins=('total_pts', lambda x: (x > 0).sum()),
        losses=('total_pts', lambda x: (x <= 0).sum()),
        pts=('total_pts', 'sum'),
    ).reset_index()
    m_grp['win_rate'] = (m_grp['wins'] / m_grp['sessions']) * 100.0
    m_grp['gross_pnl'] = m_grp['pts'] * 10.0
    m_grp['costs'] = m_grp['sessions'] * 120.0
    m_grp['net_pnl'] = m_grp['gross_pnl'] - m_grp['costs']
    m_grp['roi_safe'] = (m_grp['net_pnl'] / safe_capital) * 100.0

    print("\n--- MONTHLY PERFORMANCE BREAKDOWN ---")
    print(f"{'Month':<8} | {'Sessions':<8} | {'W/L':<6} | {'Win %':<6} | {'Pts':<10} | {'Net P&L (Rs)':<14} | {'Mo ROI (2L)':<10}")
    print("-" * 75)
    for _, r in m_grp.iterrows():
        print(f"{str(r['month']):<8} | {r['sessions']:>8} | {r['wins']:>2}W/{r['losses']:<2}L | {r['win_rate']:>5.1f}% | {r['pts']:>10.2f} | Rs {r['net_pnl']:>11,.2f} | {r['roi_safe']:>9.2f}%")
    print("-" * 75)
    print(f"Winning Months: {(m_grp['net_pnl'] > 0).sum()} of {len(m_grp)} ({((m_grp['net_pnl'] > 0).sum() / len(m_grp))*100:.1f}%)")
    print("=" * 75)

if __name__ == "__main__":
    main()
