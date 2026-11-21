import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from kiteconnect import KiteConnect

from strategy.holidays import is_market_open
from strategy.max_oi.config import MaxOIConfig
from strategy.max_oi.models import MaxOICandidate, OptionBuySignal

logger = logging.getLogger("strategy.max_oi.scanner")


class MaxOIScanner:
    """
    Scans NSE F&O universe for Max Call/Put OI proximity and >20% panic unwinding breakouts.
    Based on Saurabh's Put-Call Parity Option Buying Strategy.
    """

    def __init__(self, kite: Optional[KiteConnect] = None, config: Optional[MaxOIConfig] = None):
        self.kite = kite
        self.config = config or MaxOIConfig()
        self._instruments_cache: Optional[List[Dict[str, Any]]] = None

    def fetch_instruments(self) -> List[Dict[str, Any]]:
        """Fetches and caches NFO instruments list."""
        if self._instruments_cache is not None:
            return self._instruments_cache
        if not self.kite:
            return []
        try:
            self._instruments_cache = self.kite.instruments("NFO")
            return self._instruments_cache
        except Exception as e:
            logger.error("Failed to fetch NFO instruments from Kite: %s", e)
            return []

    def resolve_target_monthly_expiry(self, instruments: List[Dict[str, Any]], check_date: Optional[date] = None) -> Optional[date]:
        """Resolves the current active monthly expiry date for stock options."""
        today = check_date or date.today()
        # Monthly expiries in India are the last Thursday of each month
        all_expiries = {
            i.get("expiry") for i in instruments
            if i.get("expiry") and i.get("instrument_type") in ("CE", "PE")
        }
        valid_expiries = sorted([e for e in all_expiries if e and e >= today])
        return valid_expiries[0] if valid_expiries else None

    def load_previous_oi_snapshot(self, current_date: Optional[date] = None) -> Dict[str, int]:
        """Loads the most recent prior trading day's OI snapshot from disk."""
        target_d = current_date or date.today()
        snapshot_files = sorted(self.config.snapshots_dir.glob("oi_*.json"))
        if not snapshot_files:
            return {}

        # Find the most recent snapshot dated strictly before target_d
        prev_file = None
        for sf in reversed(snapshot_files):
            # Extract YYYY-MM-DD from oi_YYYY-MM-DD.json
            name_date_str = sf.stem.replace("oi_", "")
            try:
                sf_date = datetime.strptime(name_date_str, "%Y-%m-%d").date()
                if sf_date < target_d:
                    prev_file = sf
                    break
            except ValueError:
                continue

        if prev_file and prev_file.exists():
            try:
                with open(prev_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info("Loaded previous OI snapshot from %s (%d strikes)", prev_file.name, len(data))
                    return data
            except Exception as e:
                logger.warning("Failed to read snapshot file %s: %s", prev_file, e)

        return {}

    def save_oi_snapshot(self, snapshot: Dict[str, int], snap_date: Optional[date] = None) -> Path:
        """Persists today's OI snapshot for day-over-day unwinding detection."""
        d_str = (snap_date or date.today()).strftime("%Y-%m-%d")
        snap_path = self.config.snapshots_dir / f"oi_{d_str}.json"
        try:
            with open(snap_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)
            logger.info("Saved OI snapshot for %s (%d strikes) to %s", d_str, len(snapshot), snap_path)
        except Exception as e:
            logger.error("Failed to save OI snapshot: %s", e)
        return snap_path

    def scan(
        self,
        stocks: Optional[List[str]] = None,
        check_date: Optional[date] = None,
        ignore_date_filter: bool = False,
        ignore_holidays: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes full scanning pass across F&O stocks:
        1. Checks market hours (must be OPEN unless ignore_holidays is True).
        2. Verifies timing (post-15th of the month unless ignore_date_filter is True).
        3. Discovers Max Call OI and Max Put OI strikes.
        4. Identifies stocks within proximity_pct (default: 2%) of Max OI.
        5. Calculates OI change vs previous day snapshot.
        6. Triggers buy signal if strike is breached with >20% unwinding.
        """
        today = check_date or date.today()
        now_time = datetime.now().strftime("%H:%M")

        # Guard 1: Zero-hit off-market guard - never hit Kite API when market is closed
        market_open, market_reason = is_market_open(ignore_holidays=ignore_holidays)
        if not market_open:
            logger.info("[SCANNER] Market is closed (%s). Skipping scan to prevent off-market API calls.", market_reason)
            return {
                "status": "MARKET_CLOSED",
                "reason": market_reason,
                "date": str(today),
                "candidates": [],
                "signals": []
            }

        # Guard 2: In production, strategy runs in the 2nd half of the monthly expiry (after 15th)
        if self.config.post_15_only and not ignore_date_filter:
            if today.day < 15:
                logger.info("[SCANNER] Today is day %d of the month (< 15th). Scanner idle until second half of expiry.", today.day)
                return {
                    "status": "IDLE_PRE_15TH",
                    "date": str(today),
                    "day": today.day,
                    "candidates": [],
                    "signals": []
                }

        instruments = self.fetch_instruments()
        if not instruments or not self.kite:
            logger.warning("Kite client or instruments not available.")
            return {"status": "ERROR_NO_DATA", "candidates": [], "signals": []}

        target_expiry = self.resolve_target_monthly_expiry(instruments, check_date=today)
        if not target_expiry:
            logger.warning("Could not resolve target monthly expiry for date %s", today)
            return {"status": "ERROR_NO_EXPIRY", "candidates": [], "signals": []}

        # Determine target underlying stocks (~224 F&O stocks or custom list)
        if stocks is None:
            # All equity F&O underlying names (excluding indices like NIFTY, BANKNIFTY)
            index_names = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
            all_names = sorted(list(set(
                i.get("name") for i in instruments
                if i.get("name") and i.get("name") not in index_names and i.get("instrument_type") in ("CE", "PE")
            )))
            stocks = all_names

        logger.info(
            "Starting Max OI Scan on %d F&O stocks | Expiry: %s | Proximity: %.1f%% | Min Unwinding: %.0f%%",
            len(stocks), target_expiry, self.config.proximity_pct * 100, self.config.unwinding_pct * 100
        )

        # 1. Fetch live underlying spot prices in batches
        spot_symbols = [f"NSE:{s}" for s in stocks]
        spot_quotes: Dict[str, Any] = {}
        for idx in range(0, len(spot_symbols), 100):
            try:
                batch = spot_symbols[idx:idx + 100]
                quotes = self.kite.quote(batch)
                spot_quotes.update(quotes)
            except Exception as e:
                logger.warning("Error fetching spot quotes batch: %s", e)

        # 2. Collect all option contracts for target expiry
        stock_contracts_map: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        all_option_symbols: List[str] = []

        for inst in instruments:
            name = inst.get("name")
            if name in stocks and inst.get("expiry") == target_expiry:
                itype = inst.get("instrument_type")
                if itype in ("CE", "PE"):
                    if name not in stock_contracts_map:
                        stock_contracts_map[name] = {"CE": [], "PE": []}
                    stock_contracts_map[name][itype].append(inst)
                    all_option_symbols.append(f"NFO:{inst.get('tradingsymbol')}")

        # 3. Batch query quotes for all options to extract live Open Interest
        opt_quotes: Dict[str, Any] = {}
        for idx in range(0, len(all_option_symbols), 100):
            try:
                batch = all_option_symbols[idx:idx + 100]
                quotes = self.kite.quote(batch)
                opt_quotes.update(quotes)
            except Exception as e:
                logger.warning("Error fetching option quotes batch: %s", e)

        # 4. Load previous day OI snapshot to detect unwinding
        prev_oi_snapshot = self.load_previous_oi_snapshot(current_date=today)
        current_oi_snapshot: Dict[str, int] = {}

        candidates: List[MaxOICandidate] = []
        signals: List[OptionBuySignal] = []

        # 5. Process each stock
        for sym in stocks:
            spot_key = f"NSE:{sym}"
            spot_data = spot_quotes.get(spot_key, {})
            spot = spot_data.get("last_price", 0.0)
            if not spot or spot <= 0:
                continue

            contracts = stock_contracts_map.get(sym, {})
            ce_list = contracts.get("CE", [])
            pe_list = contracts.get("PE", [])
            if not ce_list or not pe_list:
                continue

            # Find Max Call OI strike
            max_ce_inst = None
            max_ce_oi = -1
            for inst in ce_list:
                tsym = inst.get("tradingsymbol")
                q = opt_quotes.get(f"NFO:{tsym}", {})
                oi = int(q.get("oi") or 0)
                current_oi_snapshot[tsym] = oi
                if oi > max_ce_oi:
                    max_ce_oi = oi
                    max_ce_inst = inst

            # Find Max Put OI strike
            max_pe_inst = None
            max_pe_oi = -1
            for inst in pe_list:
                tsym = inst.get("tradingsymbol")
                q = opt_quotes.get(f"NFO:{tsym}", {})
                oi = int(q.get("oi") or 0)
                current_oi_snapshot[tsym] = oi
                if oi > max_pe_oi:
                    max_pe_oi = oi
                    max_pe_inst = inst

            if not max_ce_inst or not max_pe_inst:
                continue

            ce_strike = float(max_ce_inst.get("strike", 0.0))
            pe_strike = float(max_pe_inst.get("strike", 0.0))

            ce_dist = (ce_strike - spot) / spot
            pe_dist = (spot - pe_strike) / spot

            is_near_ce = abs(ce_dist) <= self.config.proximity_pct
            is_near_pe = abs(pe_dist) <= self.config.proximity_pct

            # Calculate day-over-day or intraday OI unwinding
            prev_ce_oi = prev_oi_snapshot.get(max_ce_inst.get("tradingsymbol"))
            ce_oi_change_pct = None
            if prev_ce_oi and prev_ce_oi > 0:
                ce_oi_change_pct = round(((max_ce_oi - prev_ce_oi) / prev_ce_oi) * 100.0, 2)

            prev_pe_oi = prev_oi_snapshot.get(max_pe_inst.get("tradingsymbol"))
            pe_oi_change_pct = None
            if prev_pe_oi and prev_pe_oi > 0:
                pe_oi_change_pct = round(((max_pe_oi - prev_pe_oi) / prev_pe_oi) * 100.0, 2)

            cand = MaxOICandidate(
                symbol=sym,
                spot=spot,
                expiry=target_expiry,
                max_ce_strike=ce_strike,
                max_ce_oi=max_ce_oi,
                prev_ce_oi=prev_ce_oi,
                ce_oi_change_pct=ce_oi_change_pct,
                ce_dist_pct=round(ce_dist * 100, 2),
                is_near_ce=is_near_ce,
                max_pe_strike=pe_strike,
                max_pe_oi=max_pe_oi,
                prev_pe_oi=prev_pe_oi,
                pe_oi_change_pct=pe_oi_change_pct,
                pe_dist_pct=round(pe_dist * 100, 2),
                is_near_pe=is_near_pe,
            )

            if is_near_ce or is_near_pe:
                candidates.append(cand)

            # Check Signal Condition 1: Call Panic Unwinding Breakout
            # Max Call OI strike tested, OI unwound by >= 20% (change <= -20%), and price breaks above strike
            if ce_oi_change_pct is not None and ce_oi_change_pct <= -(self.config.unwinding_pct * 100):
                if spot >= ce_strike:
                    # Find ATM Call option
                    atm_ce = min(ce_list, key=lambda i: abs(float(i.get("strike", 0.0)) - spot))
                    atm_sym = atm_ce.get("tradingsymbol")
                    atm_q = opt_quotes.get(f"NFO:{atm_sym}", {})
                    atm_ltp = float(atm_q.get("last_price") or 0.0)
                    lot_size = int(atm_ce.get("lot_size") or 1)

                    if atm_ltp > 0:
                        # 2 trading days holding period cutoff
                        max_exit = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
                        signals.append(OptionBuySignal(
                            symbol=sym,
                            side="CE",
                            spot_price=spot,
                            breached_strike=ce_strike,
                            oi_unwinding_pct=ce_oi_change_pct,
                            atm_strike=float(atm_ce.get("strike")),
                            atm_tradingsymbol=atm_sym,
                            atm_token=int(atm_ce.get("instrument_token")),
                            atm_ltp=atm_ltp,
                            lot_size=lot_size,
                            sl_price=round(atm_ltp * (1.0 - self.config.sl_pct), 2),
                            target_price=round(atm_ltp * 2.0, 2),
                            trigger_time=now_time,
                            expiry_date=str(target_expiry),
                            max_exit_date=max_exit,
                        ))

            # Check Signal Condition 2: Put Panic Unwinding Breakdown
            # Max Put OI strike tested, OI unwound by >= 20%, and price breaks below strike
            if pe_oi_change_pct is not None and pe_oi_change_pct <= -(self.config.unwinding_pct * 100):
                if spot <= pe_strike:
                    # Find ATM Put option
                    atm_pe = min(pe_list, key=lambda i: abs(float(i.get("strike", 0.0)) - spot))
                    atm_sym = atm_pe.get("tradingsymbol")
                    atm_q = opt_quotes.get(f"NFO:{atm_sym}", {})
                    atm_ltp = float(atm_q.get("last_price") or 0.0)
                    lot_size = int(atm_pe.get("lot_size") or 1)

                    if atm_ltp > 0:
                        max_exit = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
                        signals.append(OptionBuySignal(
                            symbol=sym,
                            side="PE",
                            spot_price=spot,
                            breached_strike=pe_strike,
                            oi_unwinding_pct=pe_oi_change_pct,
                            atm_strike=float(atm_pe.get("strike")),
                            atm_tradingsymbol=atm_sym,
                            atm_token=int(atm_pe.get("instrument_token")),
                            atm_ltp=atm_ltp,
                            lot_size=lot_size,
                            sl_price=round(atm_ltp * (1.0 - self.config.sl_pct), 2),
                            target_price=round(atm_ltp * 2.0, 2),
                            trigger_time=now_time,
                            expiry_date=str(target_expiry),
                            max_exit_date=max_exit,
                        ))

        # 6. Save current snapshot for subsequent scans
        if current_oi_snapshot:
            self.save_oi_snapshot(current_oi_snapshot, snap_date=today)

        logger.info(
            "Scan completed: %d stocks within 2%% range of Max OI | %d Actionable Breakout Signals generated.",
            len(candidates), len(signals)
        )

        return {
            "status": "COMPLETED",
            "date": str(today),
            "expiry": str(target_expiry),
            "total_stocks_scanned": len(stocks),
            "candidates_count": len(candidates),
            "candidates": candidates,
            "signals_count": len(signals),
            "signals": signals,
        }
