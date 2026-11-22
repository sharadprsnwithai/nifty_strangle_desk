"""
Scanner module for Weekly Monthly-Fibonacci ETF Strategy.
Fetches weekly and monthly bars, calculates Fibonacci Pivots, and generates Entry/Exit signals.
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
import yfinance as yf
import logging
from strategy.etf_swing.config import ETFSwingConfig, ETFInfo

logger = logging.getLogger("etf_swing.scanner")

class ETFScanner:
    def __init__(self, config: ETFSwingConfig, kite_client=None):
        self.config = config
        self.kite = kite_client

    def fetch_etf_data(self, etf: ETFInfo, period: str = "6mo") -> Optional[pd.DataFrame]:
        """
        Fetches historical daily data. Defaults to Yahoo Finance for robustness.
        """
        try:
            df = yf.download(etf.yf_ticker, period=period, auto_adjust=True, progress=False)
            if df is None or len(df) == 0:
                logger.warning(f"No data returned for {etf.symbol}")
                return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        except Exception as e:
            logger.error(f"Error downloading data for {etf.symbol}: {e}")
            return None

    def calculate_levels(self, daily_df: pd.DataFrame) -> Tuple[float, float, float, float]:
        """
        Calculates Monthly Pivot (P), S1, Latest Close, and 12-week Momentum.
        """
        # Monthly OHLC
        monthly = daily_df.resample('ME').agg({
            'High': 'max',
            'Low': 'min',
            'Close': 'last'
        })
        
        if len(monthly) < 2:
            raise ValueError("Insufficient data to compute monthly pivots")
            
        # Use previous month's High, Low, Close
        prev_m = monthly.iloc[-2]
        H = prev_m['High']
        L = prev_m['Low']
        C = prev_m['Close']
        
        P = (H + L + C) / 3.0
        range_m = H - L
        S1 = P - 0.382 * range_m
        
        # Latest daily/weekly close
        latest_close = daily_df['Close'].iloc[-1]
        
        # 12-week Momentum (Rate of Change)
        weekly = daily_df.resample('W-FRI').agg({'Close': 'last'})
        if len(weekly) >= 13:
            mom12 = (weekly['Close'].pct_change(12).iloc[-1]) * 100.0
        else:
            mom12 = (weekly['Close'].pct_change(len(weekly) - 1).iloc[-1]) * 100.0
            
        return P, S1, latest_close, mom12

    def scan_universe(self, current_holdings: List[str]) -> Dict:
        """
        Scans the complete ETF universe.
        Returns:
            - exits: List of symbols currently held that closed below S1
            - holds: List of symbols currently held that remain valid
            - entries: List of potential buy candidates ranked by 12-week momentum
            - details: Full table of levels for logging / display
        """
        exits = []
        holds = []
        candidates = []
        details = []

        for symbol, info in self.config.universe.items():
            df = self.fetch_etf_data(info)
            if df is None:
                continue

            try:
                P, S1, ltp, mom12 = self.calculate_levels(df)
            except Exception as e:
                logger.warning(f"Could not calculate levels for {symbol}: {e}")
                continue

            dist_p = ((ltp / P) - 1.0) * 100.0
            dist_s1 = ((ltp / S1) - 1.0) * 100.0

            # Determine signal
            if symbol in current_holdings:
                if ltp < S1:
                    status = "EXIT (Below S1)"
                    exits.append(symbol)
                else:
                    status = "HOLD (Above S1)"
                    holds.append(symbol)
            else:
                if ltp > P:
                    status = "BUY CANDIDATE (Above Pivot)"
                    candidates.append({
                        "symbol": symbol,
                        "name": info.name,
                        "ltp": ltp,
                        "pivot": P,
                        "s1": S1,
                        "mom12": mom12,
                        "dist_p": dist_p
                    })
                elif ltp < S1:
                    status = "AVOID (Below S1)"
                else:
                    status = "PULLBACK (Between S1 and P)"

            details.append({
                "Symbol": symbol,
                "Name": info.name,
                "LTP": round(ltp, 2),
                "Monthly P": round(P, 2),
                "Monthly S1": round(S1, 2),
                "Dist P %": round(dist_p, 2),
                "12W Mom %": round(mom12, 1),
                "Status": status
            })

        # Rank candidates by 12-week momentum descending
        candidates.sort(key=lambda x: x["mom12"], reverse=True)

        return {
            "exits": exits,
            "holds": holds,
            "candidates": candidates,
            "details": pd.DataFrame(details)
        }
