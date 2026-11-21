import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from datetime import datetime, date
from kite import get_kite_client
from config import load_config

def main():
    cfg = load_config()
    kite = get_kite_client(config=cfg, validate=True, ignore_holidays=True)
    instruments = kite.instruments("NFO")

    test_stocks = [
        "RELIANCE", "SBIN", "INFY", "HDFCBANK", "ICICIBANK",
        "TATASTEEL", "VEDL", "BHEL", "MCX", "BHARATFORG",
        "POLYCAB", "NATIONALUM", "ADANIPOWER"
    ]
    today = date.today()

    # Find nearest monthly expiry
    expiries = sorted(list(set(i["expiry"] for i in instruments if i["expiry"] and i["expiry"] >= today and i["instrument_type"] in ("CE", "PE"))))
    target_expiry = expiries[0]
    print(f"\n================ MAX OI & 2% PROXIMITY SCREENER ================")
    print(f"Target Expiry: {target_expiry} | Date: {today} | Stocks Analyzed: {len(test_stocks)}")
    print(f"==================================================================")

    # 1. Spot quotes
    spot_symbols = [f"NSE:{s}" for s in test_stocks]
    spot_quotes = kite.quote(spot_symbols)

    results = []
    for s in test_stocks:
        spot_key = f"NSE:{s}"
        spot = spot_quotes.get(spot_key, {}).get("last_price", 0.0)
        if spot == 0.0:
            continue

        ce_contracts = [i for i in instruments if i["name"] == s and i["instrument_type"] == "CE" and i["expiry"] == target_expiry]
        pe_contracts = [i for i in instruments if i["name"] == s and i["instrument_type"] == "PE" and i["expiry"] == target_expiry]

        # Batch query quotes for all strikes
        all_contracts = ce_contracts + pe_contracts
        query_syms = [f"NFO:{i['tradingsymbol']}" for i in all_contracts]

        opt_quotes = {}
        for k in range(0, len(query_syms), 100):
            opt_quotes.update(kite.quote(query_syms[k:k+100]))

        # Max CE OI
        max_ce = None
        max_ce_oi = -1
        for i in ce_contracts:
            sym = f"NFO:{i['tradingsymbol']}"
            oi = opt_quotes.get(sym, {}).get("oi", 0)
            if oi > max_ce_oi:
                max_ce_oi = oi
                max_ce = i

        # Max PE OI
        max_pe = None
        max_pe_oi = -1
        for i in pe_contracts:
            sym = f"NFO:{i['tradingsymbol']}"
            oi = opt_quotes.get(sym, {}).get("oi", 0)
            if oi > max_pe_oi:
                max_pe_oi = oi
                max_pe = i

        ce_strike = max_ce["strike"] if max_ce else 0.0
        pe_strike = max_pe["strike"] if max_pe else 0.0

        ce_dist_pct = ((ce_strike - spot) / spot) * 100.0 if spot > 0 else 999.0
        pe_dist_pct = ((spot - pe_strike) / spot) * 100.0 if spot > 0 else 999.0

        is_near_call = abs(ce_dist_pct) <= 2.0
        is_near_put = abs(pe_dist_pct) <= 2.0

        results.append({
            "Stock": s,
            "Spot": spot,
            "Max Call Strike": ce_strike,
            "Call OI": max_ce_oi,
            "Call Dist %": round(ce_dist_pct, 2),
            "Near Call (<=2%)": "YES 🎯" if is_near_call else "No",
            "Max Put Strike": pe_strike,
            "Put OI": max_pe_oi,
            "Put Dist %": round(pe_dist_pct, 2),
            "Near Put (<=2%)": "YES 🎯" if is_near_put else "No",
        })

    df = pd.DataFrame(results)
    print(df.to_string(index=False))

    qualifying = df[(df["Near Call (<=2%)"] == "YES 🎯") | (df["Near Put (<=2%)"] == "YES 🎯")]
    print(f"\nStocks currently within 2% of Max OI Strike ({len(qualifying)} / {len(df)}):")
    if len(qualifying) > 0:
        print(qualifying[["Stock", "Spot", "Max Call Strike", "Call Dist %", "Near Call (<=2%)", "Max Put Strike", "Put Dist %", "Near Put (<=2%)"]].to_string(index=False))
    else:
        print("None in this sample set.")

if __name__ == "__main__":
    main()
