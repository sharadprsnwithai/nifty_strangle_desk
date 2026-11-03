"""Positions fetching, summarization, and formatting engine for Kite Connect."""

from typing import Dict, List, Any
from tabulate import tabulate
from kiteconnect import KiteConnect

def fetch_positions(kite: KiteConnect) -> Dict[str, List[Dict[str, Any]]]:
    """Retrieves raw positions payload from Kite Connect."""
    return kite.positions()

def summarize_positions(raw_positions: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Parses raw positions and calculates portfolio metrics:
    total realised, total unrealised, total P&L, open count, closed count.
    """
    net_list = raw_positions.get("net", [])
    day_list = raw_positions.get("day", [])

    total_realised = 0.0
    total_unrealised = 0.0
    total_m2m = 0.0
    open_count = 0
    closed_count = 0

    parsed_net = []
    for pos in net_list:
        qty = pos.get("quantity", 0)
        pnl = float(pos.get("pnl", 0.0))
        m2m = float(pos.get("m2m", 0.0))
        realised = float(pos.get("realised", 0.0))
        unrealised = float(pos.get("unrealised", 0.0))

        total_realised += realised
        total_unrealised += unrealised
        total_m2m += m2m

        if qty != 0:
            open_count += 1
        else:
            closed_count += 1

        parsed_net.append({
            "symbol": pos.get("tradingsymbol", ""),
            "exchange": pos.get("exchange", ""),
            "product": pos.get("product", ""),
            "quantity": qty,
            "buy_price": float(pos.get("buy_price", 0.0)),
            "sell_price": float(pos.get("sell_price", 0.0)),
            "last_price": float(pos.get("last_price", 0.0)),
            "pnl": pnl,
            "m2m": m2m,
            "realised": realised,
            "unrealised": unrealised,
        })

    parsed_day = []
    for pos in day_list:
        parsed_day.append({
            "symbol": pos.get("tradingsymbol", ""),
            "exchange": pos.get("exchange", ""),
            "product": pos.get("product", ""),
            "quantity": pos.get("quantity", 0),
            "buy_price": float(pos.get("buy_price", 0.0)),
            "sell_price": float(pos.get("sell_price", 0.0)),
            "last_price": float(pos.get("last_price", 0.0)),
            "pnl": float(pos.get("pnl", 0.0)),
            "m2m": float(pos.get("m2m", 0.0)),
        })

    total_pnl = total_realised + total_unrealised

    return {
        "net_positions": parsed_net,
        "day_positions": parsed_day,
        "totals": {
            "total_realised": round(total_realised, 2),
            "total_unrealised": round(total_unrealised, 2),
            "total_m2m": round(total_m2m, 2),
            "total_pnl": round(total_pnl, 2),
            "open_positions_count": open_count,
            "closed_positions_count": closed_count,
        }
    }

def format_positions_table(summary: Dict[str, Any], view: str = "net", open_only: bool = False) -> str:
    """Formats position data and summary metrics as a clean terminal table."""
    positions = summary["net_positions"] if view == "net" else summary["day_positions"]

    if open_only:
        positions = [p for p in positions if p["quantity"] != 0]

    view_title = "NET POSITIONS (Cumulative)" if view == "net" else "DAY POSITIONS (Intraday)"
    lines = [f"\n=== {view_title} ==="]

    if not positions:
        lines.append("No positions found.")
    else:
        table_rows = []
        for p in positions:
            qty_str = f"{p['quantity']:+d}" if p['quantity'] != 0 else "0 (CLOSED)"
            pnl_val = p["pnl"]
            pnl_str = f"{pnl_val:+.2f}"
            
            table_rows.append([
                p["symbol"],
                p["exchange"],
                p["product"],
                qty_str,
                f"{p['buy_price']:.2f}",
                f"{p['sell_price']:.2f}",
                f"{p['last_price']:.2f}",
                pnl_str
            ])

        qty_col = "Day Qty" if view == "day" else "Net Qty"
        headers = ["Symbol", "Exch", "Product", qty_col, "Buy Avg", "Sell Avg", "LTP", "P&L (INR)"]
        lines.append(tabulate(table_rows, headers=headers, tablefmt="grid"))

    # Summary section
    totals = summary["totals"]
    summary_rows = [
        ["Total Realised P&L", f"{totals['total_realised']:+.2f}"],
        ["Total Unrealised P&L", f"{totals['total_unrealised']:+.2f}"],
        ["Total MTM (M2M)", f"{totals['total_m2m']:+.2f}"],
        ["Total Net P&L", f"{totals['total_pnl']:+.2f}"],
        ["Open / Closed Count", f"{totals['open_positions_count']} open / {totals['closed_positions_count']} closed"]
    ]
    lines.append("\n=== PORTFOLIO SUMMARY ===")
    lines.append(tabulate(summary_rows, tablefmt="simple"))

    return "\n".join(lines)
