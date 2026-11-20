from typing import Dict, List


def calculate_mcx_costs(turnover: float, legs: int = 2) -> float:
    """Calculate MCX friction costs including brokerage, turnover fee, CTT, SEBI, GST, and stamp duty."""
    brokerage = 20.0 * legs * 2  # ₹20/order entry and exit
    mcx_turnover_fee = turnover * 0.000021
    ctt = turnover * 0.0001  # Commodity Transaction Tax
    sebi_fee = turnover * 0.000001
    gst = (brokerage + mcx_turnover_fee + sebi_fee) * 0.18
    stamp_duty = turnover * 0.00003
    return round(brokerage + mcx_turnover_fee + ctt + sebi_fee + gst + stamp_duty, 2)


def run_silver_backtest(trades: List[Dict], slippage_per_trade: float = 200.0) -> Dict:
    """Compute summary backtesting metrics from a list of executed trades."""
    if not trades:
        return {
            "total_trades": 0,
            "net_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
        }

    total_net_pnl = 0.0
    wins = 0
    total_wins = 0.0
    total_losses = 0.0
    cumulative_pnl = []
    current_cum = 0.0

    for t in trades:
        pnl = t["pnl"] - slippage_per_trade
        total_net_pnl += pnl
        current_cum += pnl
        cumulative_pnl.append(current_cum)

        if pnl > 0:
            wins += 1
            total_wins += pnl
        else:
            total_losses += abs(pnl)

    # Max Drawdown
    peak = 0.0
    max_dd = 0.0
    for val in cumulative_pnl:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd

    win_rate = (wins / len(trades)) * 100.0
    profit_factor = (total_wins / total_losses) if total_losses > 0 else float("inf")

    return {
        "total_trades": len(trades),
        "net_pnl": round(total_net_pnl, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_dd, 2),
    }
