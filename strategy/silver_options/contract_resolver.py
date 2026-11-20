from typing import List, Dict

def resolve_silver_symbols(instruments: List[Dict], underlying: str = "SILVERM") -> List[Dict]:
    results = []
    for inst in instruments:
        symbol = inst.get("tradingsymbol") or ""
        name = inst.get("name") or ""
        if underlying == "SILVERM" and (symbol.startswith("SILVERMIC") or name == "SILVERMIC"):
            continue
        if name == underlying or underlying in symbol:
            results.append(inst)
    return results
