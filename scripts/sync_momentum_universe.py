#!/usr/bin/env python3
"""
Sync / Verify Nifty 200 Momentum 30 Index Constituents from NSE Indices.
The Nifty 200 Momentum 30 Index is officially rebalanced semi-annually in June and December.
"""

import os
import sys
sys.path.insert(0, os.path.abspath("."))

import urllib.request
import io
import pandas as pd
from strategy.stock_futures.config import MOMENTUM_30_LOT_SIZES

NSE_INDICES_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty200Momentum30list.csv"

def fetch_latest_constituents():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(NSE_INDICES_CSV_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8')
            df = pd.read_csv(io.StringIO(content))
            return df
    except Exception as e:
        print(f"Could not fetch directly from NSE Indices: {e}")
        return None

def check_universe_status():
    print("=" * 80)
    print("  NIFTY 200 MOMENTUM 30 INDEX: REBALANCING SCHEDULE & UNIVERSE VERIFICATION")
    print("=" * 80)
    print("Official Methodology:")
    print("• Rebalancing Frequency : Semi-Annually (Twice a year)")
    print("• Rebalance Months      : JUNE and DECEMBER (Effective after monthly F&O expiry)")
    print("• Cut-Off Dates         : Last trading day of May and November")
    print("• Index Universe        : Top 30 stocks with highest Normalized Momentum Score from Nifty 200")
    print("-" * 80)

    print(f"\nCurrent Desk Configuration ({len(MOMENTUM_30_LOT_SIZES)} active F&O equities):")
    for idx, (sym, lot) in enumerate(sorted(MOMENTUM_30_LOT_SIZES.items()), 1):
        print(f"  {idx:>2}. {sym:<12} (Lot: {lot})")

    df_online = fetch_latest_constituents()
    if df_online is not None and not df_online.empty:
        print("\nSuccessfully fetched latest constituents from NSE Indices:")
        online_symbols = set(df_online['Symbol'].dropna().str.strip()) if 'Symbol' in df_online.columns else set()
        desk_symbols = set(MOMENTUM_30_LOT_SIZES.keys())
        diff_new = online_symbols - desk_symbols
        diff_removed = desk_symbols - online_symbols
        if not diff_new and not diff_removed:
            print(" Desk universe is 100% IN SYNC with NSE Indices.")
        else:
            if diff_new:
                print(f" New additions to index: {diff_new}")
            if diff_removed:
                print(f" Exits from index: {diff_removed}")
    else:
        print("\nNote: Automated fetch from niftyindices.com blocked by CDN or offline; manual verify via CSV download.")

if __name__ == "__main__":
    check_universe_status()
