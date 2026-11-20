import sys, os
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, os.path.abspath("."))
from kite import get_kite_client

kite = get_kite_client()
token = 256265  # NIFTY 50

end_dt = datetime.now()
start_dt = end_dt - timedelta(days=3 * 365 + 10)

print(f"Fetching NIFTY 5m data from {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}...")

chunks = []
cur_start = start_dt
chunk_days = 60

while cur_start < end_dt:
    cur_end = min(cur_start + timedelta(days=chunk_days), end_dt)
    print(f"Fetching chunk: {cur_start.strftime('%Y-%m-%d')} to {cur_end.strftime('%Y-%m-%d')}...")
    try:
        records = kite.historical_data(token, cur_start.strftime("%Y-%m-%d"), cur_end.strftime("%Y-%m-%d"), "5minute")
        if records:
            chunks.extend(records)
            print(f"  Got {len(records)} bars")
        else:
            print("  0 bars returned")
    except Exception as e:
        print(f"  Error fetching chunk: {e}")
    cur_start = cur_end + timedelta(days=1)

if chunks:
    df = pd.DataFrame(chunks)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    out_path = Path("data/nifty_5m_3y.parquet")
    df.to_parquet(out_path)
    print(f"\nSuccessfully saved {len(df)} 5m bars to {out_path}")
    print(f"Date range: {df['date'].min()} to {df['date'].max()}")
else:
    print("No data collected!")
