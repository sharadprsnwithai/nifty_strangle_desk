import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("strategy.techno_funda.fundamentals")


def fetch_and_evaluate_fundamentals(
    symbol: str,
    cache_path: Optional[Path] = None,
    min_ttm_growth: float = 0.30,
    min_quarterly_growth: float = 0.30,
    max_debt_equity: float = 1.0,
    min_margin: float = 0.08,
) -> Dict[str, Any]:
    """
    Evaluates corporate operational earnings (EBITDA), margins, and solvency (D/E)
    using yfinance with persistent local disk caching.
    """
    clean_sym = symbol.replace("NSE:", "").strip().upper()
    cache_file = cache_path or Path("data/techno_funda_fundamentals.json")

    # 1. Load Cache
    cache_data = {}
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except Exception:
            cache_data = {}

    # Check valid cache (less than 7 days old)
    now_ts = time.time()
    if clean_sym in cache_data:
        entry = cache_data[clean_sym]
        cached_at = entry.get("cached_at", 0)
        if now_ts - cached_at < 7 * 86400:
            return entry

    # 2. Fetch via yfinance
    result = {
        "symbol": clean_sym,
        "company_name": clean_sym,
        "sector": "Equity",
        "ttm_ebitda_growth_pct": None,
        "quarterly_ebitda_growth_pct": None,
        "debt_to_equity": None,
        "operating_margin_pct": None,
        "passed_fundamentals": False,
        "fundamental_reason": "No data",
        "cached_at": now_ts,
    }

    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{clean_sym}.NS")
        info = ticker.info or {}

        result["company_name"] = info.get("longName") or info.get("shortName") or clean_sym
        result["sector"] = info.get("sector") or "Diversified"

        # Operating Margins
        op_margin = info.get("operatingMargins")
        if op_margin is not None:
            result["operating_margin_pct"] = round(float(op_margin) * 100.0, 2)

        # Debt to Equity
        de = info.get("debtToEquity")
        if de is not None:
            # yfinance often returns D/E as percentage (e.g. 120 = 1.20)
            norm_de = float(de) / 100.0 if float(de) > 10.0 else float(de)
            result["debt_to_equity"] = round(norm_de, 2)

        # EBITDA and Financials
        qf = ticker.quarterly_financials
        if qf is not None and not qf.empty:
            ebitda_row = None
            for row_name in ["EBITDA", "Normalized EBITDA", "Operating Income"]:
                if row_name in qf.index:
                    ebitda_row = qf.loc[row_name].dropna()
                    break

            if ebitda_row is not None and len(ebitda_row) >= 2:
                # Latest Quarter YoY growth (compare with same quarter 1 year ago if 4+ quarters exist)
                latest_q = float(ebitda_row.iloc[0])
                if len(ebitda_row) >= 5 and float(ebitda_row.iloc[4]) != 0:
                    yoy_q = float(ebitda_row.iloc[4])
                    q_growth = (latest_q - yoy_q) / abs(yoy_q)
                else:
                    prev_q = float(ebitda_row.iloc[1])
                    q_growth = (latest_q - prev_q) / abs(prev_q) if prev_q != 0 else 0.0

                result["quarterly_ebitda_growth_pct"] = round(q_growth * 100.0, 2)

                # TTM YoY EBITDA Growth
                if len(ebitda_row) >= 4:
                    ttm = float(ebitda_row.iloc[:4].sum())
                    if len(ebitda_row) >= 8 and float(ebitda_row.iloc[4:8].sum()) != 0:
                        prior_ttm = float(ebitda_row.iloc[4:8].sum())
                        ttm_growth = (ttm - prior_ttm) / abs(prior_ttm)
                    else:
                        prior_ttm = float(ebitda_row.iloc[1:5].sum()) if len(ebitda_row) >= 5 else ttm * 0.8
                        ttm_growth = (ttm - prior_ttm) / abs(prior_ttm) if prior_ttm != 0 else 0.0

                    result["ttm_ebitda_growth_pct"] = round(ttm_growth * 100.0, 2)

        # 3. Evaluate Criteria
        reasons = []
        passes_ttm = (result["ttm_ebitda_growth_pct"] is not None and result["ttm_ebitda_growth_pct"] >= min_ttm_growth * 100)
        passes_q = (result["quarterly_ebitda_growth_pct"] is not None and result["quarterly_ebitda_growth_pct"] >= min_quarterly_growth * 100)
        passes_de = (result["debt_to_equity"] is None or result["debt_to_equity"] <= max_debt_equity)
        passes_margin = (result["operating_margin_pct"] is None or result["operating_margin_pct"] >= min_margin * 100)

        if passes_ttm or passes_q:
            reasons.append("High EBITDA Growth (>30%)")
        if passes_de:
            reasons.append("Clean Balance Sheet (D/E < 1.0)")
        if passes_margin:
            reasons.append("Solid Margin (>=8%)")

        # Must have positive operational growth and low debt
        if (passes_ttm or passes_q) and passes_de:
            result["passed_fundamentals"] = True
            result["fundamental_reason"] = " & ".join(reasons)
        else:
            result["passed_fundamentals"] = False
            result["fundamental_reason"] = f"Failed criteria (TTM: {result['ttm_ebitda_growth_pct']}%, D/E: {result['debt_to_equity']})"

    except Exception as e:
        logger.warning("Error fetching fundamentals for %s: %s", clean_sym, e)
        result["fundamental_reason"] = f"Fetch error: {e}"

    # 4. Save to Cache
    try:
        cache_data[clean_sym] = result
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save fundamentals cache: %s", e)

    return result
