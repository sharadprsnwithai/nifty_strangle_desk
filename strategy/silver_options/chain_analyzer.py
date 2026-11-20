from typing import List, Tuple
from strategy.silver_options.config import SilverConfig
from strategy.silver_options.models import OptionChainProfile


def compute_pcr(chain_data: List[dict]) -> float:
    """Compute the Put-Call Ratio (PCR) from open interest across all strikes."""
    total_ce_oi = sum(row.get("ce_oi", 0) for row in chain_data)
    total_pe_oi = sum(row.get("pe_oi", 0) for row in chain_data)
    if total_ce_oi == 0:
        return 1.0
    return round(total_pe_oi / total_ce_oi, 3)


def find_oi_walls(chain_data: List[dict]) -> Tuple[int, int]:
    """Identify call resistance wall (highest CE OI) and put support base (highest PE OI)."""
    if not chain_data:
        return 0, 0
    call_wall = max(chain_data, key=lambda r: r.get("ce_oi", 0))["strike"]
    put_base = max(chain_data, key=lambda r: r.get("pe_oi", 0))["strike"]
    return call_wall, put_base


def compute_max_pain(chain_data: List[dict]) -> int:
    """Compute the Max Pain strike where option sellers incur the least collective loss."""
    if not chain_data:
        return 0
    strikes = [row["strike"] for row in chain_data]
    min_pain = float("inf")
    max_pain_strike = strikes[0]

    for k in strikes:
        total_pain = 0.0
        for row in chain_data:
            s = row["strike"]
            ce_oi = row.get("ce_oi", 0)
            pe_oi = row.get("pe_oi", 0)
            if k > s:
                total_pain += ce_oi * (k - s)
            elif k < s:
                total_pain += pe_oi * (s - k)
        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = k

    return max_pain_strike


def build_option_chain_profile(
    spot_price: float, chain_data: List[dict], config: SilverConfig
) -> OptionChainProfile:
    """Build a comprehensive OptionChainProfile snapshot for the current spot price."""
    step = config.strike_step
    atm_strike = int(round(spot_price / step) * step)
    pcr = compute_pcr(chain_data)
    call_wall, put_base = find_oi_walls(chain_data)
    max_pain = compute_max_pain(chain_data)

    return OptionChainProfile(
        spot_price=spot_price,
        pcr=pcr,
        max_pain=max_pain,
        call_wall=call_wall,
        put_base=put_base,
        atm_strike=atm_strike,
    )
