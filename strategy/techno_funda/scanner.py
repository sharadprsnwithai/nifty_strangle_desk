import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any
import pandas as pd
from kiteconnect import KiteConnect

from strategy.holidays import is_market_open
from strategy.techno_funda.config import TechnoFundaConfig
from strategy.techno_funda.models import TechnoFundaCandidate
from strategy.techno_funda.technicals import evaluate_mtf_rsi
from strategy.techno_funda.fundamentals import fetch_and_evaluate_fundamentals

logger = logging.getLogger("strategy.techno_funda.scanner")


class TechnoFundaScanner:
    """
    2-Stage Multi-Timeframe Techno-Funda Breakout Scanner.
    Stage 1: Multi-timeframe technical screening (Monthly > 60, Weekly > 60, Daily crosses 65).
    Stage 2: Fundamental validation (TTM EBITDA Growth > 30%, Quarterly > 30%, D/E < 1.0, Margin >= 8%).
    """

    def __init__(self, config: Optional[TechnoFundaConfig] = None, kite: Optional[KiteConnect] = None):
        self.config = config or TechnoFundaConfig()
        self.kite = kite
        self._universe_cache: Optional[Dict[str, pd.DataFrame]] = None

    def load_universe_data(self) -> Dict[str, pd.DataFrame]:
        """Loads historical daily candles from cache or parquet database."""
        if self._universe_cache is not None:
            return self._universe_cache

        data_path = self.config.daily_data_path
        if not data_path.exists():
            logger.warning("Daily data parquet file not found at %s", data_path)
            return {}

        try:
            df = pd.read_parquet(data_path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            universe: Dict[str, pd.DataFrame] = {}
            for sym, group in df.groupby("symbol"):
                group = group.sort_values("date").set_index("date")
                universe[str(sym).upper()] = group
            self._universe_cache = universe
            logger.info("Loaded %d symbols from daily historical parquet.", len(universe))
            return universe
        except Exception as e:
            logger.error("Failed to load daily universe data: %s", e)
            return {}

    def scan(
        self,
        stocks: Optional[List[str]] = None,
        tech_only: bool = False,
        ignore_holidays: bool = False,
        custom_data: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Dict[str, Any]:
        """
        Executes the 2-Stage MTF Techno-Funda Breakout Scan:
        1. Checks zero-hit market hours guard.
        2. Stage 1: Technical MTF RSI Screen (Monthly > 60, Weekly > 60, Daily crosses 65).
        3. Stage 2: Fundamental EBITDA & D/E Validation.
        """
        # Guard: Zero-hit off-market guard
        market_open, market_reason = is_market_open(ignore_holidays=ignore_holidays)
        if not market_open and not ignore_holidays:
            logger.info("[SCANNER] Market is closed (%s). Skipping scan to prevent off-market API calls.", market_reason)
            return {
                "status": "MARKET_CLOSED",
                "reason": market_reason,
                "candidates": [],
                "technical_hits": [],
            }

        universe = custom_data if custom_data is not None else self.load_universe_data()
        if not universe:
            logger.warning("No candle data available for scanning.")
            return {"status": "ERROR_NO_DATA", "candidates": [], "technical_hits": []}

        target_symbols = [s.upper() for s in stocks] if stocks else list(universe.keys())
        today_str = date.today().strftime("%Y-%m-%d")

        logger.info(
            "Starting MTF Techno-Funda Scan on %d stocks (Monthly > %.0f, Weekly > %.0f, Daily Cross > %.0f)...",
            len(target_symbols), self.config.monthly_rsi_threshold, self.config.weekly_rsi_threshold, self.config.daily_rsi_trigger
        )

        # ----------------------------------------------------------------------
        # STAGE 1: Technical Multi-Timeframe RSI Screen
        # ----------------------------------------------------------------------
        technical_matches: List[Tuple[str, Dict[str, Any]]] = []
        for sym in target_symbols:
            df = universe.get(sym)
            if df is None or len(df) < 50:
                continue

            tech_res = evaluate_mtf_rsi(df, config=self.config)
            if tech_res:
                technical_matches.append((sym, tech_res))

        logger.info("Stage 1 Technical Filter: %d / %d stocks passed MTF RSI criteria.", len(technical_matches), len(target_symbols))

        # ----------------------------------------------------------------------
        # STAGE 2: Fundamental Operational High-Growth Validation
        # ----------------------------------------------------------------------
        candidates: List[TechnoFundaCandidate] = []
        for sym, tech in technical_matches:
            funda = {}
            if not tech_only:
                funda = fetch_and_evaluate_fundamentals(
                    symbol=sym,
                    cache_path=self.config.fundamentals_cache_file,
                    min_ttm_growth=self.config.min_ttm_ebitda_growth,
                    min_quarterly_growth=self.config.min_quarterly_ebitda_growth,
                    max_debt_equity=self.config.max_debt_to_equity,
                    min_margin=self.config.min_operating_margin,
                )

            passed_funda = funda.get("passed_fundamentals", True if tech_only else False)

            cand = TechnoFundaCandidate(
                symbol=sym,
                company_name=funda.get("company_name", sym),
                sector=funda.get("sector", "Equity"),
                ltp=tech["ltp"],
                daily_rsi=tech["daily_rsi"],
                prev_daily_rsi=tech["prev_daily_rsi"],
                weekly_rsi=tech["weekly_rsi"],
                monthly_rsi=tech["monthly_rsi"],
                rsi_cross_confirmed=tech["rsi_cross_confirmed"],
                volume_ratio=tech["volume_ratio"],
                chart_pattern=tech["chart_pattern"],
                ttm_ebitda_growth_pct=funda.get("ttm_ebitda_growth_pct"),
                quarterly_ebitda_growth_pct=funda.get("quarterly_ebitda_growth_pct"),
                debt_to_equity=funda.get("debt_to_equity"),
                operating_margin_pct=funda.get("operating_margin_pct"),
                passed_fundamentals=passed_funda,
                fundamental_reason=funda.get("fundamental_reason", "Technicals passed"),
                swing_sl=tech["swing_sl"],
                target_1=tech["target_1"],
                target_2=tech["target_2"],
                scan_date=today_str,
            )

            # In production, prioritize stocks that pass both technicals and fundamentals
            if tech_only or passed_funda:
                candidates.append(cand)

        logger.info("Stage 2 Fundamental Validation: %d actionable Techno-Funda Breakout candidates identified.", len(candidates))

        return {
            "status": "COMPLETED",
            "date": today_str,
            "total_scanned": len(target_symbols),
            "technical_hits_count": len(technical_matches),
            "candidates_count": len(candidates),
            "candidates": candidates,
        }
