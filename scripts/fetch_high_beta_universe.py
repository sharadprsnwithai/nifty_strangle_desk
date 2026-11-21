import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, os.path.abspath("."))
from kite import get_kite_client

kite = get_kite_client()
inst_nse = pd.DataFrame(kite.instruments("NSE"))
inst_nfo = pd.DataFrame(kite.instruments("NFO"))

new_targets = {
    "BANKNIFTY": ("NIFTY BANK", 260105, 30), # Bank Nifty lot size is 30 (or 15 depending on expiry, let's verify)
    "ANGELONE": ("ANGELONE", 82945, None),
    "CDSL": ("CDSL", 5420545, None),
    "JINDALSTEL": ("JINDALSTEL", 1723649, None),
    "DLF": ("DLF", 3771393, None),
    "GODREJPROP": ("GODREJPROP", 4576001, None),
    "OBEROIRLTY": ("OBEROIRLTY", 5181953, None),
    "DIXON": ("DIXON", 5552641, None),
    "TRENT": ("TRENT", 502785, None),
    "PERSISTENT": ("PERSISTENT", 4701441, None),
    "COFORGE": ("COFORGE", 2955009, None),
    "CHOLAFIN": ("CHOLAFIN", 175361, None),
    "INDIGO": ("INDIGO", 2865921, None),
    "ASHOKLEY": ("ASHOKLEY", 54273, None),
}

# Resolve lot sizes from NFO
print("Resolving lot sizes from NFO...")
lot_sizes = {}
for sym in new_targets:
    nfo_match = inst_nfo[(inst_nfo["name"] == sym) & (inst_nfo["instrument_type"] == "FUT")]
    if not nfo_match.empty:
        lot = int(nfo_match.iloc[0]["lot_size"])
        lot_sizes[sym] = lot
        print(f"{sym} lot size: {lot}")
    else:
        # Fallback for Bank Nifty or other
        if sym == "BANKNIFTY":
            nfo_bn = inst_nfo[inst_nfo["tradingsymbol"].str.startswith("BANKNIFTY") & (inst_nfo["instrument_type"] == "FUT")]
            if not nfo_bn.empty:
                lot = int(nfo_bn.iloc[0]["lot_size"])
                lot_sizes[sym] = lot
                print(f"{sym} lot size: {lot}")
            else:
                lot_sizes[sym] = 30
        else:
            print(f"Warning: Lot size not found for {sym}")

print("Lot sizes mapping:", lot_sizes)

out_dir = Path("data/cache_15m_1y")
out_dir.mkdir(exist_ok=True)

end_dt = datetime.now()
start_dt = end_dt - timedelta(days=365 + 10)

print(f"\nFetching 1Y 15m data for {len(new_targets)} symbols from {start_dt.date()} to {end_dt.date()}...")

for sym, (tradingsym, token, _) in new_targets.items():
    out_path = out_dir / f"{sym}_15m.parquet"
    if out_path.exists():
        print(f"Skipping {sym}, already exists in {out_path}.")
        continue

    print(f"Fetching {sym} (token {token})...")
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
            time.sleep(0.35)
        except Exception as e:
            print(f"Error fetching chunk for {sym}: {e}")
            time.sleep(1.0)
        cur_start = cur_end + timedelta(days=1)

    if chunks:
        df_chunk = pd.DataFrame(chunks)
        df_chunk = df_chunk.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        df_chunk.to_parquet(out_path, index=False)
        print(f"Saved {sym}: {len(df_chunk)} bars to {out_path}")
    else:
        print(f"No records fetched for {sym}")

print("All fetches complete.")
