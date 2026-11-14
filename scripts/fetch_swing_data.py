import urllib.request
import csv
import time
import os
import sys
from datetime import datetime
import pandas as pd

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kite import get_kite_client

def get_symbol_universe():
    symbols = set()
    for idx in ['nifty200', 'niftymidcap150']:
        url = f'https://archives.nseindia.com/content/indices/ind_{idx}list.csv'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req) as resp:
                reader = csv.DictReader([l.decode('utf-8') for l in resp.readlines()])
                symbols.update(r['Symbol'] for r in reader)
        except Exception as e:
            print(f'Error fetching {idx}: {e}')
    
    # Extra examples mentioned in video
    for s in ['RVNL', 'BDL', 'MUTHOOTFIN', 'BSE', 'MCX']:
        symbols.add(s)
    
    return sorted(list(symbols))

def main():
    kite = get_kite_client()
    symbols = get_symbol_universe()
    print(f"Target universe: {len(symbols)} symbols")

    insts = kite.instruments('NSE')
    token_map = {i['tradingsymbol']: i['instrument_token'] for i in insts if i['instrument_type'] == 'EQ' and i['segment'] == 'NSE'}

    all_dfs = []
    from_date = '2024-11-01'
    to_date = '2026-11-06'

    count = 0
    for sym in symbols:
        token = token_map.get(sym)
        if not token:
            continue
        try:
            records = kite.historical_data(token, from_date, to_date, 'day')
            if records and len(records) > 200:
                df = pd.DataFrame(records)
                df['symbol'] = sym
                all_dfs.append(df)
                count += 1
                if count % 25 == 0 or count == len(symbols):
                    print(f"[{count}/{len(symbols)}] Fetched {sym} ({len(df)} bars)")
            time.sleep(0.35)  # Kite rate limit safety: ~3 req/s
        except Exception as e:
            print(f"Failed {sym}: {e}")
            time.sleep(1.0)

    if all_dfs:
        full_df = pd.concat(all_dfs, ignore_index=True)
        os.makedirs('data', exist_ok=True)
        output_file = 'data/swing_daily_2y.parquet'
        full_df.to_parquet(output_file, index=False)
        print(f"\nSuccessfully saved {len(full_df)} daily records for {count} symbols to {output_file}!")

if __name__ == '__main__':
    main()
