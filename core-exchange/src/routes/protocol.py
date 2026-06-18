"""
Protocol parameter discovery endpoint.

GET /api/v1/protocol/params
    Returns static and runtime protocol parameters so external LLM-driven
    agents can programmatically map execution paths, fee structures, and
    liquidity floors without inspecting source code.
"""

from __future__ import annotations

from fastapi import APIRouter

from config import PLATFORM_TREASURY_ADDRESS, PROTOCOL_DOMAIN
from schemas import ProtocolParamsResponse
from services.web3_provider import is_live_mode

router = APIRouter(prefix="/api/v1/protocol", tags=["protocol"])

# Mirror the constants from settlement.py and arbitrage.py so external
# agents always receive the authoritative values in force at runtime.
_TAX_RATE_FRACTION:   float = 0.001          # 0.1%
_MIN_TRANSFER_USDC:   float = 0.0001
_SAFETY_FLOOR_PCT:    float = 0.005          # 0.5% default slippage floor
_RELAY_HOPS:          int   = 3
_CANDIDATE_CAP:       int   = 10
_GAS_COST_PER_HOP:    float = 0.05          # USDC per hop


@router.get("/params", response_model=ProtocolParamsResponse)
def protocol_params() -> ProtocolParamsResponse:
    """
    Returns all protocol parameters required for external agent route planning.

    Includes:
    - Current 0.1% platform transaction tax rate.
    - Minimum transfer floor (dust-splitting prevention).
    - Default safety floor for rebalance trigger evaluation.
    - Relay topology constants (hops, candidate cap, gas cost).
    - Execution mode (sandbox vs. live_rpc).
    - Platform treasury address receiving accumulated fees.
    """
    return ProtocolParamsResponse(
        tax_rate_pct=_TAX_RATE_FRACTION * 100,
        tax_rate_fraction=_TAX_RATE_FRACTION,
        min_transfer_usdc=_MIN_TRANSFER_USDC,
        safety_floor_pct=_SAFETY_FLOOR_PCT,
        relay_hops=_RELAY_HOPS,
        candidate_cap=_CANDIDATE_CAP,
        gas_cost_per_hop_usdc=_GAS_COST_PER_HOP,
        execution_mode="live_rpc" if is_live_mode() else "sandbox",
        platform_treasury_address=PLATFORM_TREASURY_ADDRESS,
        protocol_domain=PROTOCOL_DOMAIN,
    )
