import os
import re
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union
from kiteconnect import KiteConnect

from strategy.stock_futures.config import MOMENTUM_30_LOT_SIZES, STOCK_NAME_MAPPING

logger = logging.getLogger("strategy.stock_futures.rehydrator")

# Non-equity cash / collateral keywords to ignore
NON_EQUITY_KEYWORDS = {"TRIPARTY", "REPO", "TREPS", "CASH", "COLLATERAL"}

# Known company name aliases to NSE symbols
NAME_ALIASES: Dict[str, str] = {
    "laurus labs limited": "LAURUSLABS",
    "laurus labs": "LAURUSLABS",
    "multi commodity exchange of india limited": "MCX",
    "multi commodity exchange of india": "MCX",
    "multi commodity exchange": "MCX",
    "mcx": "MCX",
    "shriram finance limited": "SHRIRAMFIN",
    "shriram finance": "SHRIRAMFIN",
    "hindalco industries limited": "HINDALCO",
    "hindalco industries": "HINDALCO",
    "hindalco": "HINDALCO",
    "tata steel limited": "TATASTEEL",
    "tata steel": "TATASTEEL",
    "cummins india limited": "CUMMINSIND",
    "cummins india": "CUMMINSIND",
    "ntpc limited": "NTPC",
    "ntpc": "NTPC",
    "ge vernova t&d india limited": "GVT_D",
    "ge vernova t&d india": "GVT_D",
    "ge vernova": "GVT_D",
    "gvt&d": "GVT_D",
    "gvt_d": "GVT_D",
    "vedanta limited": "VEDL",
    "vedanta": "VEDL",
    "adani power limited": "ADANIPOWER",
    "adani power": "ADANIPOWER",
    "bse limited": "BSE",
    "bse": "BSE",
    "the federal bank limited": "FEDERALBNK",
    "federal bank limited": "FEDERALBNK",
    "federal bank": "FEDERALBNK",
    "bharat heavy electricals limited": "BHEL",
    "bharat heavy electricals": "BHEL",
    "bhel": "BHEL",
    "samvardhana motherson international limited": "MOTHERSON",
    "samvardhana motherson international": "MOTHERSON",
    "samvardhana motherson": "MOTHERSON",
    "motherson": "MOTHERSON",
    "bharat forge limited": "BHARATFORG",
    "bharat forge": "BHARATFORG",
    "cg power and industrial solutions limited": "CGPOWER",
    "cg power and industrial solutions": "CGPOWER",
    "cg power": "CGPOWER",
    "hitachi energy india limited": "POWERINDIA",
    "hitachi energy india": "POWERINDIA",
    "hitachi energy": "POWERINDIA",
    "powerindia": "POWERINDIA",
    "adani energy solutions limited": "ADANIENSOL",
    "adani energy solutions": "ADANIENSOL",
    "torrent pharmaceuticals limited": "TORNTPHARM",
    "torrent pharmaceuticals": "TORNTPHARM",
    "torrent pharma": "TORNTPHARM",
    "national aluminium company limited": "NATIONALUM",
    "national aluminium company": "NATIONALUM",
    "national aluminium": "NATIONALUM",
    "polycab india limited": "POLYCAB",
    "polycab india": "POLYCAB",
    "polycab": "POLYCAB",
    "vodafone idea limited": "IDEA",
    "vodafone idea": "IDEA",
    "idea": "IDEA",
    "solar industries india limited": "SOLARINDS",
    "solar industries india": "SOLARINDS",
    "solar industries": "SOLARINDS",
    "abb india limited": "ABB",
    "abb india": "ABB",
    "abb": "ABB",
    "adani green energy limited": "ADANIGREEN",
    "adani green energy": "ADANIGREEN",
    "adani green": "ADANIGREEN",
    "glenmark pharmaceuticals limited": "GLENMARK",
    "glenmark pharmaceuticals": "GLENMARK",
    "glenmark pharma": "GLENMARK",
    "glenmark": "GLENMARK",
    "kei industries limited": "KEI",
    "kei industries": "KEI",
    "kei": "KEI",
    "steel authority of india limited": "SAIL",
    "steel authority of india": "SAIL",
    "sail": "SAIL",
    "aditya birla capital limited": "ABCAPITAL",
    "aditya birla capital": "ABCAPITAL",
    "l&t finance limited": "LTF",
    "l&t finance holdings limited": "LTF",
    "l&t finance": "LTF",
    "lt finance": "LTF",
    "ltf": "LTF",
}


def normalize_text(text: str) -> str:
    """Normalizes whitespace and lowercase for consistent string lookup."""
    return re.sub(r"\s+", " ", text.strip().lower())


def resolve_symbol(name_or_symbol: str) -> Optional[str]:
    """Resolves a raw stock line or company name to a valid NSE equity ticker."""
    raw = name_or_symbol.strip()
    if not raw:
        return None

    # Check for non-equity assets (cash / repo)
    upper_raw = raw.upper()
    if any(k in upper_raw for k in NON_EQUITY_KEYWORDS):
        logger.debug("Filtered out non-equity asset: %s", raw)
        return None

    # Direct uppercase symbol match
    if upper_raw in MOMENTUM_30_LOT_SIZES:
        return upper_raw

    # Check normalized alias dictionary
    norm = normalize_text(raw)
    if norm in NAME_ALIASES:
        return NAME_ALIASES[norm]

    # Clean punctuation and check again
    clean_norm = re.sub(r"[^\w\s]", "", norm)
    if clean_norm in NAME_ALIASES:
        return NAME_ALIASES[clean_norm]

    # Try mapping reverse lookup from STOCK_NAME_MAPPING
    for sym, full_name in STOCK_NAME_MAPPING.items():
        if normalize_text(full_name) == norm:
            return sym

    # If already a compact ticker (<=12 chars alphanumeric)
    cleaned_sym = re.sub(r"[^\w]", "", upper_raw)
    if cleaned_sym in MOMENTUM_30_LOT_SIZES:
        return cleaned_sym

    logger.warning("Could not automatically resolve ticker symbol for: '%s'", raw)
    return None


class UniverseRehydrator:
    """Manages semi-annual rehydration of the Nifty 200 Momentum 30 trading universe."""

    DEFAULT_UNIVERSE_FILE = Path("data/momentum_30_universe.json")

    def __init__(self, kite: Optional[KiteConnect] = None, universe_file: Optional[Path] = None):
        self.kite = kite
        self.universe_file = universe_file or self.DEFAULT_UNIVERSE_FILE

    def fetch_live_fno_lot_sizes(self, symbols: List[str]) -> Dict[str, int]:
        """
        Queries Kite Connect NFO instrument master to resolve exact, live lot sizes.
        Falls back to known default lot sizes if Kite is unavailable.
        """
        lot_sizes: Dict[str, int] = {}
        if self.kite:
            try:
                logger.info("Fetching real-time F&O lot sizes from Kite Connect...")
                instruments = self.kite.instruments("NFO")
                now = datetime.now()
                for inst in instruments:
                    name = inst.get("name")
                    if name in symbols and inst.get("instrument_type") == "FUT":
                        exp = inst.get("expiry")
                        if exp and exp >= now.date():
                            if name not in lot_sizes:
                                lot_sizes[name] = int(inst.get("lot_size", 0))
                logger.info("Successfully fetched live lot sizes for %d symbols from Kite.", len(lot_sizes))
            except Exception as e:
                logger.warning("Failed to query Kite instruments for lot sizes: %s. Using cached lot sizes.", e)

        # Fallback to default lot sizes for any missing
        for sym in symbols:
            if sym not in lot_sizes:
                lot_sizes[sym] = MOMENTUM_30_LOT_SIZES.get(sym, 1)

        return lot_sizes

    def parse_constituents(self, source: Union[str, Path, List[str]]) -> List[str]:
        """Parses raw text, CSV file, or list of strings into resolved ticker symbols."""
        lines: List[str] = []
        if isinstance(source, Path) or (isinstance(source, str) and os.path.isfile(source)):
            with open(source, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        elif isinstance(source, str):
            lines = source.splitlines()
        elif isinstance(source, list):
            lines = source

        resolved = []
        for line in lines:
            line_str = line.strip()
            # If CSV line, extract first or second column
            if "," in line_str:
                parts = [p.strip().strip('"') for p in line_str.split(",")]
                # Skip CSV header rows
                if any(h in parts[0].lower() for h in ["company", "symbol", "name", "series"]):
                    continue
                # Try symbol column first, then company name column
                cand = None
                for part in parts[:2]:
                    cand = resolve_symbol(part)
                    if cand:
                        break
                if cand and cand not in resolved:
                    resolved.append(cand)
            else:
                sym = resolve_symbol(line_str)
                if sym and sym not in resolved:
                    resolved.append(sym)

        return resolved

    def rehydrate(
        self,
        source: Union[str, Path, List[str]],
        save: bool = True
    ) -> Dict[str, Any]:
        """
        Rehydrates the universe from source:
        1. Resolves all ticker symbols.
        2. Filters out cash/collateral items.
        3. Queries Kite Connect for official F&O lot sizes.
        4. Detects added and removed constituents.
        5. Atomically saves state to data/momentum_30_universe.json.
        """
        new_symbols = self.parse_constituents(source)
        if not new_symbols:
            raise ValueError("No valid equity constituents resolved from source.")

        # Load existing universe to detect changes
        old_symbols: List[str] = []
        if self.universe_file.exists():
            try:
                with open(self.universe_file, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    old_symbols = old_data.get("symbols", [])
            except Exception:
                old_symbols = list(MOMENTUM_30_LOT_SIZES.keys())
        else:
            old_symbols = list(MOMENTUM_30_LOT_SIZES.keys())

        added = sorted(list(set(new_symbols) - set(old_symbols)))
        removed = sorted(list(set(old_symbols) - set(new_symbols)))

        lot_sizes = self.fetch_live_fno_lot_sizes(new_symbols)
        names = {sym: STOCK_NAME_MAPPING.get(sym, sym) for sym in new_symbols}

        universe_payload = {
            "updated_at": datetime.now().isoformat(),
            "constituents_count": len(new_symbols),
            "symbols": new_symbols,
            "lot_sizes": lot_sizes,
            "names": names,
            "changes": {
                "added": added,
                "removed": removed,
                "is_changed": bool(added or removed)
            }
        }

        if save:
            self.universe_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.universe_file, "w", encoding="utf-8") as f:
                json.dump(universe_payload, f, indent=2)
            logger.info("Saved rehydrated universe (%d stocks) to %s", len(new_symbols), self.universe_file)

        return universe_payload

    def auto_rehydrate(self, save: bool = True) -> Dict[str, Any]:
        """
        Automated scheduled rehydration routine:
        1. Checks for drop files (data/momentum_30_constituents.txt, .csv, or ind_nifty200Momentum30list.csv).
        2. If found, rehydrates from the file; otherwise verifies live Kite lot sizes on active universe.
        3. Detects additions, removals, or lot size updates.
        4. Saves updated universe to universe_file.
        """
        drop_files = [
            Path("data/momentum_30_constituents.txt"),
            Path("data/momentum_30_constituents.csv"),
            Path("data/ind_nifty200Momentum30list.csv"),
        ]
        source: Union[Path, List[str]] = list(MOMENTUM_30_LOT_SIZES.keys())
        for df in drop_files:
            if df.exists() and df.stat().st_size > 0:
                logger.info("Found auto-rehydration drop file: %s", df)
                source = df
                break
        else:
            if self.universe_file.exists():
                try:
                    with open(self.universe_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("symbols"):
                            source = data["symbols"]
                except Exception:
                    pass

        return self.rehydrate(source=source, save=save)

    def load_active_universe(self) -> Tuple[List[str], Dict[str, int]]:
        """Loads the active cached universe, or falls back to built-in default config."""
        if self.universe_file.exists():
            try:
                with open(self.universe_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    symbols = data.get("symbols", [])
                    lot_sizes = data.get("lot_sizes", {})
                    if symbols and lot_sizes:
                        return symbols, lot_sizes
            except Exception as e:
                logger.warning("Failed to load universe from %s: %s. Using default config.", self.universe_file, e)

        return list(MOMENTUM_30_LOT_SIZES.keys()), dict(MOMENTUM_30_LOT_SIZES)
