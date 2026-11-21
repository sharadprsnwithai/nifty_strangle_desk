import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, os.path.abspath("."))
from kite import get_kite_client

kite = get_kite_client()
inst = kite.instruments("NSE")
df_inst = pd.DataFrame(inst)

# 12 diverse liquid F&O stocks with existing lot sizes
stocks = [
    "TATASTEEL", "HINDALCO", "BHEL", "VEDL", "NTPC",
    "SAIL", "GLENMARK", "MCX", "FEDERALBNK", "POLYCAB",
    "BHARATFORG", "CUMMINSIND"
]

out_dir = Path("data/cache_15m_3y")
out_dir.mkdir(exist_ok=True)

end_dt = datetime.now()
start_dt = end_dt - timedelta(days=3 * 365 + 10)

print(f"Fetching 3Y 15m data for {len(stocks)} stocks from {start_dt.date()} to {end_dt.date()}...")

for s in stocks:
    out_path = out_dir / f"{s}_15m.parquet"
    if out_path.exists():
        print(f"Skipping {s}, already exists.")
        continue

    row = df_inst[df_inst["tradingsymbol"] == s]
    if row.empty:
        print(f"Token not found for {s}")
        continue
    token = int(row.iloc[0]["instrument_token"])

    cur_start = start_dt
    chunks = []
    while cur_start < end_dt:
        cur_end = min(cur_start + timedelta(days=95), end_dt)
        try:
            records = kite.historical_data(
                token,
                cur_start.strftime("%Y-%m-%d"),
                cur_end.strftime("%Y-%m-%d"),
                "15minute"
            )
            if records:
                chunks.extend(records)
        except Exception as e:
            print(f"Error {s} chunk {cur_start.date()}: {e}")
        cur_start = cur_end + timedelta(days=1)
        time.sleep(0.35)

    if chunks:
        df = pd.DataFrame(chunks)
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        df.to_parquet(out_path)
        print(f"Saved {s}: {len(df)} bars ({df['date'].min().date()} to {df['date'].max().date()})")
    else:
        print(f"No records for {s}")

print("Fetch complete!")
