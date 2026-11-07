import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger("strategy.fo_ban")

NSE_FO_BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def parse_fo_ban_csv(content: str) -> Set[str]:
    """
    Parses the raw content of NSE's fo_secban.csv into a clean set of uppercase symbols.
    
    Expected format from NSE:
        Securities in Ban For Trade Date 08-NOV-2026:
        1,KAYNES
        2,IDEA
    """
    banned = set()
    if not content:
        return banned

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on comma or whitespace
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) >= 2 and parts[0].isdigit():
            # Standard "1,SYMBOL" row
            sym = parts[1].upper()
            # Clean any exchange prefix or punctuation
            sym = re.sub(r"[^A-Z0-9_\-&]", "", sym)
            if sym:
                banned.add(sym)
        elif len(parts) == 1 and not parts[0].isdigit():
            # Fallback if list is single-column without serial numbers
            upper_line = parts[0].upper()
            if not any(keyword in upper_line for keyword in ["SECURITIES", "TRADE DATE", "BAN", "DATE:"]):
                sym = re.sub(r"[^A-Z0-9_\-&]", "", upper_line)
                if sym:
                    banned.add(sym)

    return banned


def get_nse_fo_ban_list(
    check_date: Optional[date] = None,
    cache_dir: Optional[Path] = None,
    use_cache: bool = True,
    timeout_sec: int = 5
) -> Set[str]:
    """
    Retrieves the list of securities under NSE F&O ban (MWPL > 95%) for the given trade date.
    
    1. Checks local cache file `data/fo_secban_<YYYY-MM-DD>.txt`.
    2. If missing or cache disabled, fetches live from NSE CSV endpoint.
    3. Respects FO_BAN_OVERRIDE env var (comma-separated symbols) if provided.
    4. On network errors or timeouts, fails gracefully and returns cached data or empty set.
    """
    if check_date is None:
        check_date = date.today()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    date_str = check_date.strftime("%Y-%m-%d")

    # Resolve cache directory
    if cache_dir is None:
        cache_dir = Path("data")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"fo_secban_{date_str}.txt"

    # 1. Read from cache if exists
    if use_cache and cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_symbols = {line.strip().upper() for line in f if line.strip()}
            logger.info("Loaded %d F&O banned stocks from local cache (%s)", len(cached_symbols), cache_file.name)
            return _apply_env_overrides(cached_symbols)
        except Exception as e:
            logger.warning("Error reading ban cache file %s: %s", cache_file, e)

    # 2. Fetch live from NSE endpoint
    banned_symbols: Set[str] = set()
    try:
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
        resp = requests.get(NSE_FO_BAN_URL, headers=headers, timeout=timeout_sec)
        if resp.status_code == 200 and resp.text:
            banned_symbols = parse_fo_ban_csv(resp.text)
            logger.info("Fetched %d F&O banned securities from NSE for %s", len(banned_symbols), date_str)
            # Write to cache
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    for s in sorted(banned_symbols):
                        f.write(f"{s}\n")
            except Exception as e:
                logger.warning("Failed to save F&O ban cache: %s", e)
        else:
            logger.warning("NSE F&O ban URL returned status code %s. Proceeding with 0 ban restrictions.", resp.status_code)
    except Exception as e:
        logger.warning("Failed to fetch live NSE F&O ban list (%s). Proceeding gracefully.", e)
        # Fallback to cache if exists
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    banned_symbols = {line.strip().upper() for line in f if line.strip()}
            except Exception as fb_err:
                logger.warning("Could not read fallback F&O ban cache %s: %s", cache_file, fb_err)
                banned_symbols = set()

    return _apply_env_overrides(banned_symbols)


def _apply_env_overrides(banned_symbols: Set[str]) -> Set[str]:
    """Merges any manually specified symbols from FO_BAN_OVERRIDE env var."""
    override = os.getenv("FO_BAN_OVERRIDE", "").strip()
    if override:
        extras = {s.strip().upper() for s in override.split(",") if s.strip()}
        if extras:
            logger.info("Applying FO_BAN_OVERRIDE symbols: %s", extras)
            banned_symbols = banned_symbols.union(extras)
    return banned_symbols


def filter_banned_stocks(
    target_stocks: List[Dict[str, Any]],
    banned_symbols: Set[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Filters out any stocks in target_stocks whose symbol matches a banned symbol.
    
    Returns:
        (allowed_stocks, excluded_symbols)
    """
    if not banned_symbols:
        return target_stocks, []

    allowed = []
    excluded = []
    for item in target_stocks:
        raw_sym = item.get("symbol", "")
        # Clean prefix like "NSE:"
        clean_sym = raw_sym.split(":")[-1].strip().upper()
        if clean_sym in banned_symbols:
            excluded.append(raw_sym)
        else:
            allowed.append(item)

    return allowed, excluded
