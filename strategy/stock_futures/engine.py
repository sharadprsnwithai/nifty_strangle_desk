import logging
from typing import Optional, Tuple
import pandas as pd
from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.models import TradeSignal, SignalType, StockPosition

logger = logging.getLogger("strategy.stock_futures.engine")


class StockFuturesEngine:
    """Core logic engine for Nifty 200 Momentum 30 15m Consolidation & Breakout Strategy."""

    def __init__(self, config: Optional[StockFuturesConfig] = None):
        self.config = config or StockFuturesConfig()

    def evaluate_signal(self, symbol: str, df_day: pd.DataFrame) -> Optional[TradeSignal]:
        """
        Evaluates 15-minute intraday bars for a symbol on the current trading day:
        1. Identifies C1 candle (09:15-09:30) range [L1, H1].
        2. Validates consolidation: C2, C3, C4 (09:30, 09:45, 10:00) must close inside [L1, H1].
        3. Scans trigger bars (10:15 onwards up to entry_cutoff_time, default 13:00):
           - Breakout above H1 (LONG) or below L1 (SHORT).
           - Institutional Volume Surge: Volume >= vol_threshold * 20-period Volume MA.
        """
        if df_day is None or len(df_day) < 4:
            return None

        # Ensure datetime sorting and time format
        df = df_day.copy()
        if 'time' not in df.columns:
            if 'datetime' in df.columns:
                df['time'] = pd.to_datetime(df['datetime']).dt.strftime('%H:%M')
            elif 'date' in df.columns:
                df['time'] = pd.to_datetime(df['date']).dt.strftime('%H:%M')
            else:
                return None

        df = df.sort_values('time').reset_index(drop=True)

        # 1. Opening Candle C1
        c1 = df[df['time'] == self.config.c1_candle_time]
        if len(c1) == 0:
            return None
        c1_row = c1.iloc[0]
        h1 = float(c1_row['high'])
        l1 = float(c1_row['low'])

        # 2. Consolidation candles (09:30, 09:45, 10:00)
        c_cons = df[df['time'].isin(self.config.consolidation_candles)]
        if len(c_cons) < len(self.config.consolidation_candles):
            # Not all consolidation candles have formed yet
            return None

        for _, row in c_cons.iterrows():
            close_p = float(row['close'])
            if close_p > h1 or close_p < l1:
                # Closed outside C1 range: consolidation pattern invalidated
                return None

        # Calculate 20-period volume moving average if not already present
        if 'vol_ma20' not in df.columns:
            df['vol_ma20'] = df['volume'].rolling(20, min_periods=5).mean()

        # 3. Scan trigger bars from 10:15 up to cutoff
        trigger_bars = df[df['time'] >= self.config.entry_start_time].reset_index(drop=True)
        for _, bar in trigger_bars.iterrows():
            bar_time = str(bar['time'])
            if bar_time > self.config.entry_cutoff_time:
                break

            high = float(bar['high'])
            low = float(bar['low'])
            open_p = float(bar['open'])
            volume = float(bar['volume'])
            vol_ma = float(bar['vol_ma20']) if not pd.isna(bar['vol_ma20']) else 1.0

            long_trigger = high > h1
            short_trigger = low < l1

            # Ambiguous candle breaking both extremes is disqualified
            if long_trigger and short_trigger:
                continue

            vol_ratio = volume / max(vol_ma, 1.0)
            if vol_ratio < self.config.vol_threshold:
                continue

            if long_trigger:
                entry = max(open_p, h1)
                sl = entry * (1.0 - self.config.sl_pct)
                target = entry * (1.0 + self.config.target_pct)
                return TradeSignal(
                    symbol=symbol,
                    side=SignalType.LONG,
                    trigger_time=bar_time,
                    entry_price=round(entry, 2),
                    sl_price=round(sl, 2),
                    target_price=round(target, 2),
                    volume_ratio=round(vol_ratio, 2),
                    c1_high=h1,
                    c1_low=l1,
                )
            elif short_trigger:
                entry = min(open_p, l1)
                sl = entry * (1.0 + self.config.sl_pct)
                target = entry * (1.0 - self.config.target_pct)
                return TradeSignal(
                    symbol=symbol,
                    side=SignalType.SHORT,
                    trigger_time=bar_time,
                    entry_price=round(entry, 2),
                    sl_price=round(sl, 2),
                    target_price=round(target, 2),
                    volume_ratio=round(vol_ratio, 2),
                    c1_high=h1,
                    c1_low=l1,
                )

        return None

    def check_exit(
        self,
        position: StockPosition,
        current_price: float,
        current_time: str,
        bar_high: Optional[float] = None,
        bar_low: Optional[float] = None,
    ) -> Optional[Tuple[float, str]]:
        """
        Evaluates open position against SL, Target, and EOD square-off rules.
        Returns: (exit_price, exit_reason) or None.
        """
        if position.status != "OPEN":
            return None

        h = bar_high if bar_high is not None else current_price
        l = bar_low if bar_low is not None else current_price

        # Check EOD first if past square-off time
        if current_time >= self.config.squareoff_time:
            return round(current_price, 2), "15:00_EOD"

        if position.side == "LONG":
            if l <= position.sl_price:
                return position.sl_price, "SL_HIT"
            if h >= position.target_price:
                return position.target_price, "TARGET_HIT"
        elif position.side == "SHORT":
            if h >= position.sl_price:
                return position.sl_price, "SL_HIT"
            if l <= position.target_price:
                return position.target_price, "TARGET_HIT"

        return None
