"""CLI entrypoint for running the MCX Silver Options Desk."""
import argparse
from datetime import date, datetime, timedelta
import logging
import os
from pathlib import Path
import sys
from typing import Optional, Tuple, List, Dict, Any

import pandas as pd

# Ensure repository root is in sys.path when invoked directly
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from strategy.silver_options.config import SilverConfig
from strategy.silver_options.contract_resolver import resolve_silver_symbols
from strategy.silver_options.engine import SilverOptionsEngine
from kite.client import get_kite_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("silver_options.cli")


def _parse_expiry(expiry_val: Any) -> Optional[date]:
    """Safely normalizes instrument expiry to a datetime.date."""
    if isinstance(expiry_val, datetime):
        return expiry_val.date()
    if isinstance(expiry_val, date):
        return expiry_val
    if isinstance(expiry_val, str):
        try:
            return datetime.strptime(expiry_val.split()[0], "%Y-%m-%d").date()
        except Exception:
            pass
    return None


def fetch_silver_market_data(
    kite: Any, config: Optional[SilverConfig] = None
) -> Tuple[float, pd.DataFrame, List[Dict]]:
    """
    Fetches active SILVERM futures instrument and historical 5-minute bars (>= 70 bars),
    and queries option chain quotes for strikes around the current spot price.
    """
    cfg = config or SilverConfig()

    try:
        instruments = kite.instruments(cfg.exchange)
    except Exception as e:
        logger.error("Failed to fetch instruments for %s from Kite: %s", cfg.exchange, e)
        instruments = []

    if not isinstance(instruments, list):
        instruments = []

    silver_instruments = resolve_silver_symbols(instruments, underlying=cfg.symbol)

    # 1. Resolve active futures contract
    fut_contracts = [
        inst for inst in silver_instruments
        if inst.get("instrument_type") == "FUT"
        or (isinstance(inst.get("tradingsymbol"), str) and inst["tradingsymbol"].endswith("FUT"))
    ]

    active_fut = None
    if fut_contracts:
        today = datetime.now().date()
        fut_with_exp = [(c, _parse_expiry(c.get("expiry"))) for c in fut_contracts]
        valid_fut = [item for item in fut_with_exp if item[1] is not None and item[1] >= today]
        if valid_fut:
            valid_fut.sort(key=lambda x: x[1])
            active_fut = valid_fut[0][0]
        else:
            active_fut = fut_contracts[0]

    # 2. Fetch 5-minute historical bars for active futures
    df_5m = pd.DataFrame()
    spot_price = 0.0

    if active_fut:
        token = active_fut.get("instrument_token")
        fut_sym = active_fut.get("tradingsymbol")

        if token is not None and hasattr(kite, "historical_data"):
            to_date = datetime.now()
            from_date = to_date - timedelta(days=5)
            try:
                candles = kite.historical_data(
                    instrument_token=token,
                    from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
                    to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
                    interval="5minute",
                )
                if isinstance(candles, pd.DataFrame):
                    df_5m = candles
                elif isinstance(candles, list):
                    df_5m = pd.DataFrame(candles)
            except Exception as e:
                logger.warning("Failed to fetch 5m candles for token %s: %s", token, e)

        for col in ["high", "low", "close"]:
            if col in df_5m.columns:
                df_5m[col] = pd.to_numeric(df_5m[col], errors="coerce")

        # 3. Resolve spot price
        if fut_sym and hasattr(kite, "quote"):
            try:
                q = kite.quote([f"{cfg.exchange}:{fut_sym}"])
                spot_price = float(q.get(f"{cfg.exchange}:{fut_sym}", {}).get("last_price") or 0.0)
            except Exception as e:
                logger.debug("Failed to fetch spot price quote for %s: %s", fut_sym, e)

        if spot_price <= 0.0 and not df_5m.empty and "close" in df_5m.columns:
            valid_closes = df_5m["close"].dropna()
            if not valid_closes.empty:
                spot_price = float(valid_closes.iloc[-1])

    if len(df_5m) < cfg.williams_r_period:
        logger.warning(
            "Fetched %d 5m bars; strategy recommends at least %d bars for Williams %%R.",
            len(df_5m),
            cfg.williams_r_period,
        )

    # 4. Fetch option chain quotes for strikes around spot
    opt_contracts = [
        inst for inst in silver_instruments
        if inst.get("instrument_type") in ("CE", "PE")
    ]

    # Expiry alignment: match active future's expiry or nearest upcoming
    if active_fut:
        fut_exp = _parse_expiry(active_fut.get("expiry"))
        if fut_exp is not None:
            opt_with_exp = [(c, _parse_expiry(c.get("expiry"))) for c in opt_contracts]
            matching = [c for c, exp in opt_with_exp if exp == fut_exp]
            if matching:
                opt_contracts = matching
            else:
                today = datetime.now().date()
                valid_opts = [item for item in opt_with_exp if item[1] is not None and item[1] >= today]
                if valid_opts:
                    min_exp = min(item[1] for item in valid_opts)
                    opt_contracts = [item[0] for item in valid_opts if item[1] == min_exp]

    # Filter strikes within +/- 10 strike steps of spot
    if spot_price > 0:
        nearby = []
        for c in opt_contracts:
            try:
                strike = float(c.get("strike", 0))
                if abs(strike - spot_price) <= 10 * cfg.strike_step:
                    nearby.append(c)
            except (ValueError, TypeError):
                continue
        if nearby:
            opt_contracts = nearby

    quotes: Dict[str, Any] = {}
    opt_symbols = [f"{cfg.exchange}:{c['tradingsymbol']}" for c in opt_contracts if c.get("tradingsymbol")]
    if opt_symbols and hasattr(kite, "quote"):
        for i in range(0, len(opt_symbols), 100):
            batch = opt_symbols[i:i + 100]
            try:
                quotes.update(kite.quote(batch))
            except Exception as e:
                logger.warning("Failed to fetch option quotes batch: %s", e)

    # Assemble structured chain_data
    chain_by_strike: Dict[float, Dict[str, Any]] = {}
    for c in opt_contracts:
        try:
            strike = float(c["strike"])
        except (KeyError, ValueError, TypeError):
            continue
        if strike not in chain_by_strike:
            chain_by_strike[strike] = {
                "strike": strike,
                "ce_oi": 0,
                "ce_ltp": 0.0,
                "ce_tradingsymbol": "",
                "pe_oi": 0,
                "pe_ltp": 0.0,
                "pe_tradingsymbol": "",
            }
        sym = c.get("tradingsymbol", "")
        q_data = quotes.get(f"{cfg.exchange}:{sym}", {})
        ltp = float(q_data.get("last_price") or c.get("last_price") or 0.0)
        oi = int(q_data.get("oi") or c.get("oi") or 0)
        itype = c.get("instrument_type")
        if itype == "CE":
            chain_by_strike[strike]["ce_oi"] = oi
            chain_by_strike[strike]["ce_ltp"] = ltp
            chain_by_strike[strike]["ce_tradingsymbol"] = sym
        elif itype == "PE":
            chain_by_strike[strike]["pe_oi"] = oi
            chain_by_strike[strike]["pe_ltp"] = ltp
            chain_by_strike[strike]["pe_tradingsymbol"] = sym

    chain_data = sorted(chain_by_strike.values(), key=lambda x: x["strike"])
    return spot_price, df_5m, chain_data


def log_tick_summary(
    engine: SilverOptionsEngine, prev_active: Any, prev_closed_count: int
) -> None:
    """Logs summary of signal evaluation, action taken, and position status."""
    if prev_active is None and engine.active_position is not None:
        legs_str = ", ".join(l.trading_symbol or f"{l.strike}{l.option_type}" for l in engine.active_position.legs)
        action = (
            f"ENTRY -> Opened {engine.active_position.spread_type} "
            f"[{legs_str}] "
            f"(Net Credit: {engine.active_position.entry_net_credit:.2f}, "
            f"SL: {engine.active_position.sl_price:.2f})"
        )
    elif prev_active is not None and engine.active_position is None:
        last_closed = engine.closed_positions[-1] if engine.closed_positions else None
        pnl = last_closed.realized_pnl if last_closed and last_closed.realized_pnl is not None else 0.0
        reason = last_closed.exit_reason if last_closed else "UNKNOWN"
        action = f"EXIT -> Closed position (Reason: {reason}, Realized PnL: Rs. {pnl:.2f})"
    elif engine.active_position is not None:
        action = (
            f"HOLD -> Monitoring active position {engine.active_position.spread_type} "
            f"(Current Option Price: {getattr(engine.active_position, 'current_value', 0.0):.2f})"
        )
    else:
        action = "IDLE -> No active position, no entry signal triggered"

    active_summary = (
        f"{engine.active_position.spread_type} (Net Credit: {engine.active_position.entry_net_credit:.2f})"
        if engine.active_position
        else "None"
    )
    total_realized_pnl = sum(p.realized_pnl for p in engine.closed_positions if p.realized_pnl is not None)

    logger.info("=== Market Tick Summary ===")
    logger.info("Action: %s", action)
    logger.info("Active Position: %s", active_summary)
    logger.info("Closed Positions: %d", len(engine.closed_positions))
    logger.info("Total Realized PnL: Rs. %.2f", total_realized_pnl)
    logger.info("===========================")


def display_status(engine: SilverOptionsEngine) -> None:
    """Displays current active position, today's closed positions, and realized PnL."""
    cfg = engine.config
    logger.info("=== MCX Silver Options Desk Status ===")
    logger.info(
        "Contract: %s | Lots: %d (%d kg) | SL/Lot: Rs. %.2f | Daily Max Loss: Rs. %.2f",
        cfg.symbol,
        cfg.lots,
        cfg.total_quantity,
        cfg.sl_per_lot,
        cfg.daily_max_loss,
    )
    if engine.active_position:
        legs_str = ", ".join(l.trading_symbol or f"{l.strike}{l.option_type}" for l in engine.active_position.legs)
        logger.info(
            "Active Position: %s [%s] | Net Credit: %.2f | SL Price: %.2f (Max Trade Loss: Rs. %.2f) | Status: %s",
            engine.active_position.spread_type,
            legs_str,
            engine.active_position.entry_net_credit,
            engine.active_position.sl_price,
            cfg.max_loss_per_trade,
            engine.active_position.status,
        )
    else:
        logger.info("Active Position: None (Flat)")

    logger.info("Closed Positions Today: %d", len(engine.closed_positions))
    for i, p in enumerate(engine.closed_positions, 1):
        legs_str = ", ".join(l.trading_symbol or f"{l.strike}{l.option_type}" for l in p.legs)
        pnl = p.realized_pnl if p.realized_pnl is not None else 0.0
        logger.info(
            " - #%d: Type=%s [%s], Realized PnL=Rs. %.2f, Reason=%s",
            i,
            p.spread_type,
            legs_str,
            pnl,
            p.exit_reason,
        )

    total_realized_pnl = sum(p.realized_pnl for p in engine.closed_positions if p.realized_pnl is not None)
    logger.info("Today Realized PnL: Rs. %.2f", total_realized_pnl)
    logger.info("Trading Halted Today: %s", "YES" if engine.trading_halted_for_day else "NO")
    logger.info("=======================================")


def execute_single_tick(
    engine: SilverOptionsEngine,
    kite: Any,
    config: Optional[SilverConfig] = None,
    current_time: Optional[Any] = None,
) -> None:
    """Fetches market data and processes a single market tick through the engine."""
    cfg = config or engine.config
    spot_price, df_5m, chain_data = fetch_silver_market_data(kite, cfg)
    if current_time is None:
        current_time = datetime.now().time()
    prev_active = engine.active_position
    prev_closed_count = len(engine.closed_positions)
    engine.on_market_tick(
        spot_price=spot_price,
        df_5m=df_5m,
        chain_data=chain_data,
        current_time=current_time,
    )
    log_tick_summary(engine, prev_active, prev_closed_count)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCX Silver Options Desk")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--paper", action="store_true", help="Run in paper trading mode (default)")
    mode_group.add_argument("--live", action="store_true", help="Run in live trading mode")
    parser.add_argument(
        "--lots",
        type=int,
        default=int(os.environ.get("SILVER_LOTS", "2")),
        help="Number of lots to trade (default: 2)",
    )
    parser.add_argument(
        "--sl-per-lot",
        type=float,
        default=float(os.environ.get("SILVER_SL_PER_LOT", "10000.0")),
        help="Per-lot stop loss cap in Rs (default: 10000.0)",
    )
    parser.add_argument(
        "--daily-max-loss",
        type=float,
        default=float(os.environ.get("SILVER_DAILY_MAX_LOSS", "15000.0")),
        help="Daily max loss cap per lot in Rs (default: 15000.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run single market tick with latest 5m candles from Kite or mock, then exit",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display current active position, today's closed positions, and realized PnL",
    )
    return parser


def main(args=None) -> SilverOptionsEngine:
    parser = build_parser()
    parsed_args = parser.parse_args(args)

    is_paper = not parsed_args.live
    logger.info(f"Starting MCX Silver Options Desk (Mode: {'PAPER' if is_paper else 'LIVE'})...")

    cfg = SilverConfig(
        lots=parsed_args.lots,
        sl_per_lot=parsed_args.sl_per_lot,
        daily_max_loss_per_lot=parsed_args.daily_max_loss,
    )
    kite = None
    if is_paper:
        try:
            kite = get_kite_client()
        except Exception as e:
            logger.info("No live Kite credentials found; proceeding in simulated paper mode. (%s)", e)
            kite = None
    else:
        kite = get_kite_client()

    engine = SilverOptionsEngine(kite_client=kite, config=cfg, is_paper=is_paper)
    logger.info(
        f"Initialized Silver Options Engine for {cfg.symbol} "
        f"(Lots: {cfg.lots}, Total Qty: {cfg.total_quantity} kg, SL/Lot: Rs. {cfg.sl_per_lot:,.0f}, "
        f"Daily Max Loss: Rs. {cfg.daily_max_loss:,.0f}). Ready."
    )

    if parsed_args.once:
        if kite is not None:
            logger.info("Executing single market tick (--once)...")
            execute_single_tick(engine, kite, cfg)
        else:
            logger.warning("Kite client not available; skipping live market tick in --once mode.")

    if parsed_args.status:
        display_status(engine)

    return engine


if __name__ == "__main__":
    main()
