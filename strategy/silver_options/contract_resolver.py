from typing import List, Dict

def resolve_silver_symbols(instruments: List[Dict], underlying: str = "SILVERM") -> List[Dict]:
    return [
        inst for inst in instruments
        if inst.get("name") == underlying or underlying in inst.get("tradingsymbol", "")
    ]
