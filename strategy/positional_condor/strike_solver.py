import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("condor.solver")


def parse_expiry(exp_val: Any) -> date:
    """Parses date from string or date object."""
    if isinstance(exp_val, date) and not isinstance(exp_val, datetime):
        return exp_val
    if isinstance(exp_val, datetime):
        return exp_val.date()
    return datetime.strptime(str(exp_val)[:10], "%Y-%m-%d").date()


def get_monthly_expiry(
    instruments: List[Dict[str, Any]],
    current_date: Optional[date] = None,
    underlying: str = "NIFTY"
) -> date:
    """
    Finds the active monthly expiry for the given underlying.
    In NSE, monthly options expire on the last Thursday/expiry of the month.
    If current_date is on or within 2 days of that expiry, rolls to next month's expiry.
    """
    now_date = current_date or date.today()

    expiries = set()
    for inst in instruments:
        if inst.get("name") == underlying and inst.get("expiry"):
            try:
                exp_d = parse_expiry(inst["expiry"])
                if exp_d >= now_date:
                    expiries.add(exp_d)
            except Exception:
                continue

    if not expiries:
        raise ValueError(f"No upcoming expiries found for underlying {underlying}")

    # Group expiries by (year, month) and pick the maximum date in each month (the monthly expiry)
    by_month: Dict[Tuple[int, int], List[date]] = {}
    for d in expiries:
        key = (d.year, d.month)
        by_month.setdefault(key, []).append(d)

    monthly_expiries = sorted([max(dates) for dates in by_month.values()])

    for m_exp in monthly_expiries:
        days_remaining = (m_exp - now_date).days
        # If expiry is more than 2 days away, trade this month; else roll to next month
        if days_remaining > 2:
            logger.info("Selected Monthly Expiry: %s (%d days to expiry)", m_exp, days_remaining)
            return m_exp

    # Fallback to the latest available monthly expiry
    selected = monthly_expiries[-1]
    logger.info("Selected Fallback Monthly Expiry: %s", selected)
    return selected


def get_directional_ratio_strikes(
    spot_price: float,
    regime: str,
    expiry: date,
    instruments: List[Dict[str, Any]],
    lots: int = 1,
    underlying: str = "NIFTY"
) -> Dict[str, Any]:
    """
    Generates exact 1:2:1 Directional Ratio strikes based on user specification:
    - Bearish (RSI < 40):
        Buy 1 lot  @ ATM - 50 PE
        Sell 2 lots @ ATM - 250 PE
        Buy 1 lot  @ ATM - 300 PE
    - Bullish (RSI > 60):
        Buy 1 lot  @ ATM + 50 CE
        Sell 2 lots @ ATM + 250 CE
        Buy 1 lot  @ ATM + 300 CE
    """
    regime_norm = regime.upper()
    if regime_norm not in ("BEARISH", "BULLISH"):
        raise ValueError(f"Invalid regime for directional ratio: {regime}. Expected 'BEARISH' or 'BULLISH'.")

    # ATM rounded to nearest 50
    atm_strike = int(round(spot_price / 50.0) * 50)
    inst_type = "PE" if regime_norm == "BEARISH" else "CE"

    if regime_norm == "BEARISH":
        k_buy_1 = atm_strike - 50
        k_sell = atm_strike - 250
        k_buy_2 = atm_strike - 300
    else:
        k_buy_1 = atm_strike + 50
        k_sell = atm_strike + 250
        k_buy_2 = atm_strike + 300

    target_strikes = {k_buy_1, k_sell, k_buy_2}
    matched: Dict[int, Dict[str, Any]] = {}

    for inst in instruments:
        if (
            inst.get("name") == underlying
            and inst.get("instrument_type") == inst_type
            and parse_expiry(inst.get("expiry")) == expiry
        ):
            stk = int(round(float(inst.get("strike", 0))))
            if stk in target_strikes:
                matched[stk] = inst

    missing = target_strikes - set(matched.keys())
    if missing:
        raise ValueError(f"Missing instruments for strikes {missing} on expiry {expiry}")

    lot_size = int(matched[k_buy_1].get("lot_size", 65))

    return {
        "regime": regime_norm,
        "expiry": expiry,
        "atm_strike": atm_strike,
        "instrument_type": inst_type,
        "lot_size": lot_size,
        "lots": lots,
        "buy_leg_1": {
            "strike": k_buy_1,
            "symbol": matched[k_buy_1]["tradingsymbol"],
            "action": "BUY",
            "quantity": 1 * lots * lot_size,
            "lots": 1 * lots
        },
        "sell_body": {
            "strike": k_sell,
            "symbol": matched[k_sell]["tradingsymbol"],
            "action": "SELL",
            "quantity": 2 * lots * lot_size,
            "lots": 2 * lots
        },
        "buy_leg_2": {
            "strike": k_buy_2,
            "symbol": matched[k_buy_2]["tradingsymbol"],
            "action": "BUY",
            "quantity": 1 * lots * lot_size,
            "lots": 1 * lots
        }
    }


def solve_1_to_1_iron_condor(
    spot_price: float,
    expiry: date,
    instruments: List[Dict[str, Any]],
    quotes: Dict[str, float],
    lots: int = 1,
    target_profit_ratio: float = 1.0,
    underlying: str = "NIFTY"
) -> Dict[str, Any]:
    """
    Mathematical solver finding 4 strikes [PE_buy, PE_sell, CE_sell, CE_buy]
    that minimize |(Max Profit / Max Loss) - target_profit_ratio|.
    """
    pe_contracts: Dict[int, Dict[str, Any]] = {}
    ce_contracts: Dict[int, Dict[str, Any]] = {}

    for inst in instruments:
        if inst.get("name") == underlying and parse_expiry(inst.get("expiry")) == expiry:
            stk = int(round(float(inst.get("strike", 0))))
            sym = inst.get("tradingsymbol", "")
            itype = inst.get("instrument_type")
            quote_key = f"NFO:{sym}" if ":" not in sym else sym
            ltp = quotes.get(quote_key, quotes.get(sym, 0.0))
            if ltp > 0:
                inst_data = dict(inst, ltp=ltp)
                if itype == "PE":
                    pe_contracts[stk] = inst_data
                elif itype == "CE":
                    ce_contracts[stk] = inst_data

    pe_strikes = sorted(pe_contracts.keys())
    ce_strikes = sorted(ce_contracts.keys())

    best_setup = None
    best_diff = float("inf")

    # Evaluate combinations of Put spread & Call spread
    for k_pe_sell in pe_strikes:
        if k_pe_sell >= spot_price:
            continue
        for k_pe_buy in pe_strikes:
            if k_pe_buy >= k_pe_sell:
                continue
            width_pe = k_pe_sell - k_pe_buy
            if width_pe < 100 or width_pe > 600:
                continue

            credit_pe = pe_contracts[k_pe_sell]["ltp"] - pe_contracts[k_pe_buy]["ltp"]
            if credit_pe <= 0:
                continue

            for k_ce_sell in ce_strikes:
                if k_ce_sell <= spot_price:
                    continue
                for k_ce_buy in ce_strikes:
                    if k_ce_buy <= k_ce_sell:
                        continue
                    width_ce = k_ce_buy - k_ce_sell
                    if width_ce < 100 or width_ce > 600:
                        continue

                    credit_ce = ce_contracts[k_ce_sell]["ltp"] - ce_contracts[k_ce_buy]["ltp"]
                    if credit_ce <= 0:
                        continue

                    net_credit = round(credit_pe + credit_ce, 2)
                    max_loss_pe = width_pe - net_credit
                    max_loss_ce = width_ce - net_credit
                    max_loss = max(max_loss_pe, max_loss_ce)

                    if max_loss <= 0:
                        continue

                    ratio = net_credit / max_loss
                    diff = abs(ratio - target_profit_ratio)

                    if diff < best_diff:
                        best_diff = diff
                        lot_size = int(pe_contracts[k_pe_sell].get("lot_size", 65))
                        best_setup = {
                            "regime": "SIDEWAYS",
                            "expiry": expiry,
                            "lot_size": lot_size,
                            "lots": lots,
                            "pe_buy": pe_contracts[k_pe_buy],
                            "pe_sell": pe_contracts[k_pe_sell],
                            "ce_sell": ce_contracts[k_ce_sell],
                            "ce_buy": ce_contracts[k_ce_buy],
                            "net_credit": net_credit,
                            "max_loss": round(max_loss, 2),
                            "ratio": round(ratio, 2),
                            "lower_breakeven": round(k_pe_sell - net_credit, 2),
                            "upper_breakeven": round(k_ce_sell + net_credit, 2),
                            "total_quantity": lots * lot_size
                        }

    if not best_setup:
        raise ValueError("Could not solve for a viable 1:1 Iron Condor with current chain quotes.")

    logger.info(
        "1:1 Iron Condor Solved | Net Credit: Rs %.2f | Max Loss: Rs %.2f | Ratio: %.2f | BE: [%.1f, %.1f]",
        best_setup["net_credit"], best_setup["max_loss"], best_setup["ratio"],
        best_setup["lower_breakeven"], best_setup["upper_breakeven"]
    )
    return best_setup
