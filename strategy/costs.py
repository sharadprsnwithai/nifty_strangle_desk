from typing import Dict

def calculate_trade_costs(
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_per_share: float = 0.50
) -> Dict[str, float]:
    """
    Computes exact NSE F&O Option transaction costs, taxes, slippage, and net P&L:
    - Brokerage: Rs. 20 per order (Rs. 40 round trip)
    - STT: 0.1% on SELL turnover (entry side for short options)
    - Exchange turnover charges: 0.0495% on total turnover
    - SEBI charges: Rs. 10 / crore (0.0001%)
    - Stamp duty: 0.003% on BUY turnover (exit side for short options)
    - GST: 18% on (Brokerage + Exchange Fees + SEBI Fees)
    - Slippage: Applied symmetrically to entry and exit executions
    
    Parameters:
        entry_price: Short option execution price
        exit_price: Buy-to-cover execution price
        quantity: Traded quantity (absolute number of shares/units)
        slippage_per_share: Friction per share applied to each leg (default 0.50)
        
    Returns:
        Dict containing gross_pnl, net_pnl, brokerage, stt, exchange_turnover_fee,
        sebi_fee, stamp_duty, gst, statutory_taxes, total_slippage, total_costs.
    """
    if entry_price < 0 or exit_price < 0:
        raise ValueError(
            f"Prices must be non-negative. Got entry_price={entry_price}, exit_price={exit_price}"
        )
    if slippage_per_share < 0:
        raise ValueError(
            f"slippage_per_share must be non-negative. Got slippage_per_share={slippage_per_share}"
        )

    qty = abs(quantity)
    if qty == 0:
        return {
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_turnover_fee": 0.0,
            "sebi_fee": 0.0,
            "stamp_duty": 0.0,
            "gst": 0.0,
            "statutory_taxes": 0.0,
            "total_slippage": 0.0,
            "total_costs": 0.0,
        }

    # Gross P&L based on executed order prices
    gross_pnl = round((entry_price - exit_price) * qty, 2)

    # 1. Brokerage: Rs 20 per executed order leg (round trip = Rs 40)
    brokerage = 40.0

    # 2. STT: 0.1% on SELL turnover (mandatory for option sellers on entry)
    stt = round(0.001 * entry_price * qty, 2)

    # 3. Exchange Turnover Charge: 0.0495% on both legs
    turnover = (entry_price + exit_price) * qty
    exchange_fee = round(0.000495 * turnover, 2)

    # 4. SEBI turnover fee: Rs 10 per crore (0.0001%)
    sebi_fee = round(0.000001 * turnover, 2)

    # 5. Stamp duty: 0.003% on BUY turnover (exit side)
    stamp_duty = round(0.00003 * exit_price * qty, 2)

    # 6. GST: 18% on (Brokerage + Exchange + SEBI)
    gst = round(0.18 * (brokerage + exchange_fee + sebi_fee), 2)

    # 7. Total Slippage (friction per share on both entry and exit)
    total_slippage = round(slippage_per_share * 2.0 * qty, 2)

    statutory_taxes = round(brokerage + stt + exchange_fee + sebi_fee + stamp_duty + gst, 2)
    total_costs = round(statutory_taxes + total_slippage, 2)
    net_pnl = round(gross_pnl - total_costs, 2)

    return {
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "brokerage": brokerage,
        "stt": stt,
        "exchange_turnover_fee": exchange_fee,
        "sebi_fee": sebi_fee,
        "stamp_duty": stamp_duty,
        "gst": gst,
        "statutory_taxes": statutory_taxes,
        "total_slippage": total_slippage,
        "total_costs": total_costs,
    }
