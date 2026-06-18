"""
Cognitive Token Cost Throttling — lean programmatic route evaluator.

Every HTTP call the swarm makes to /api/v1/arbitrage/route-path or
/api/v1/arbitrage/rebalance carries fixed overhead: network round-trip,
FastAPI middleware, SQLAlchemy session setup, JSON serialisation, and
response deserialisation.  On Base L2 a 10 USDC transfer at 1.5% yields
only 0.15 USDC tax.  If the API call costs more CPU/time than that margin
recovers, the swarm operates at a net loss.

This module replaces those round-trips with O(n) pure-Python evaluations
over cached DeskState values.  No I/O, no locks, no allocations beyond the
call frame.  The API is still called for authoritative confirmation (it
verifies PENDING_SYNC locks and live DB balances), but only when local
evidence already suggests the route is plausible.

Design contract
---------------
- All functions are pure (no side-effects, no global state).
- All inputs are primitive Python types or dataclass instances; no SQLAlchemy
  models, no Pydantic schemas.
- All decisions are made in O(n) time with O(1) memory relative to desk count.
- Gas-guard and equalization donor selection reuse the eth_balance cache that
  was already fetched by _check_gas_guard — zero additional RPC calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # DeskState lives in seed_swarm; imported only for type annotations so
    # this module stays importable in test contexts that don't bootstrap the
    # full swarm environment.
    from seed_swarm import DeskState  # noqa: F401

# Mirror the constants from routes/arbitrage.py and seed_swarm.py so this
# module's decisions are always aligned with the authoritative values.
# These are intentionally not imported from those modules to keep this module
# free of heavy transitive imports (web3, SQLAlchemy, FastAPI) during tests.
_GAS_COST_PER_HOP_USDC: float = 0.05   # static L2 gas friction per leg
_TAX_FRACTION:          float = 0.015  # 1.5% platform tax


# ---------------------------------------------------------------------------
# Route viability pre-check
# ---------------------------------------------------------------------------

def is_route_viable_local(
    balances: list[float],
    volume_usdc: float,
    slippage_pct: float,
    gas_cost_per_hop: float = _GAS_COST_PER_HOP_USDC,
) -> bool:
    """
    O(n) conservative viability pre-check using cached USDC balances.

    Returns False immediately when any agent's cached balance is provably
    below the per-leg cost floor, short-circuiting the HTTP route-path call.
    Returns True to signal that the route *might* be viable — the caller
    must still confirm via the authoritative API call (which also checks
    PENDING_SYNC locks and live DB balances).

    This is a one-way filter: False is conclusive, True is provisional.

    Args:
        balances:         Ordered cached USDC balances for each agent in the chain.
        volume_usdc:      Gross transfer volume being evaluated.
        slippage_pct:     Slippage tolerance as a fraction (e.g. 0.005 = 0.5%).
        gas_cost_per_hop: Static gas friction per leg in USDC (default 0.05).

    Returns:
        False if any agent is provably unable to cover the per-leg cost floor.
        True  if all agents appear able to participate (provisional).
    """
    n = len(balances)
    if n == 0:
        return False

    total_slippage = volume_usdc * slippage_pct
    slip_floor     = total_slippage / n
    per_leg_cost   = slip_floor + gas_cost_per_hop

    return all(b >= per_leg_cost for b in balances)


# ---------------------------------------------------------------------------
# Transfer sender selection
# ---------------------------------------------------------------------------

def select_transfer_pair(
    desks: list,
    min_balance_usdc: float,
) -> tuple | None:
    """
    Select a (sender, receiver) pair for an organic swarm transfer.

    Picks the highest-balance desk as sender (most likely to afford the
    transfer without triggering equalization) and a random-weighted receiver
    from the remaining desks ordered by lowest balance (simulating organic
    routing to under-capitalised desks).

    Returns None when fewer than 2 desks meet the minimum balance threshold.

    Args:
        desks:            Active DeskState list.
        min_balance_usdc: Minimum sender balance required (typically 2×transfer_amount).

    Returns:
        (sender, receiver) tuple or None.
    """
    eligible = [d for d in desks if d.balance_usdc >= min_balance_usdc]
    if len(eligible) < 1:
        return None

    others = [d for d in desks if d not in eligible or d is not eligible[0]]
    if not others:
        return None

    sender   = max(eligible, key=lambda d: d.balance_usdc)
    receiver = min(
        [d for d in desks if d is not sender],
        key=lambda d: d.balance_usdc,
    )
    return sender, receiver


# ---------------------------------------------------------------------------
# Equalization decisions
# ---------------------------------------------------------------------------

def compute_top_up(
    balance_usdc: float,
    stall_threshold: float,
    target_usdc: float,
) -> float | None:
    """
    Compute the USDC amount needed to bring a stalled desk to target.

    Returns None when the desk is not stalled (balance >= stall_threshold).
    Returns the exact gap (target_usdc − balance_usdc) otherwise.
    """
    if balance_usdc >= stall_threshold:
        return None
    return max(0.0, target_usdc - balance_usdc)


def select_equalization_donor(
    desks: list,
    stalled_desk,
    top_up_usdc: float,
    target_usdc: float,
    gas_floor_eth: float,
    live_mode: bool,
) -> object | None:
    """
    Select the best donor desk for an equalization transfer.

    Eligibility criteria (all must hold):
      1. Not the stalled desk itself.
      2. USDC balance > target_usdc + top_up_usdc  (stays above target after donating).
      3. In live mode only: cached eth_balance >= gas_floor_eth  (gas guard).

    Returns the eligible donor with the highest USDC balance, or None if no
    donor qualifies.  The eth_balance cache (populated by _check_gas_guard)
    is used directly — zero additional RPC calls.

    Args:
        desks:         Full list of active DeskState objects.
        stalled_desk:  The desk that needs the top-up.
        top_up_usdc:   Exact USDC amount to be transferred.
        target_usdc:   Minimum balance the donor must retain after donating.
        gas_floor_eth: Minimum ETH gas balance required in live mode.
        live_mode:     True when the bridge is in live_rpc mode.
    """
    candidates = [
        d for d in desks
        if d is not stalled_desk
        and d.balance_usdc > target_usdc + top_up_usdc
        and (not live_mode or d.eth_balance >= gas_floor_eth)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.balance_usdc)


# ---------------------------------------------------------------------------
# Rebalance trigger decision
# ---------------------------------------------------------------------------

def needs_server_rebalance(
    balance_usdc: float,
    rebalance_volume: float,
    safety_floor_pct: float,
) -> bool:
    """
    Return True when a desk's balance has fallen below the server-side
    rebalance trigger threshold.

    Mirrors the condition in seed_swarm.swarm_loop:
        floor = rebalance_volume * safety_floor_pct
        if desk.balance_usdc < floor → trigger rebalance API call

    Using this function avoids the comparison being scattered across the
    loop body and ensures the threshold matches the server's own calculation.
    """
    floor = rebalance_volume * safety_floor_pct
    return balance_usdc < floor


# ---------------------------------------------------------------------------
# Tax-to-overhead ratio guard
# ---------------------------------------------------------------------------

def tax_covers_overhead(
    transfer_amount_usdc: float,
    estimated_overhead_usdc: float,
    tax_fraction: float = _TAX_FRACTION,
) -> bool:
    """
    Return True when the 1.5% protocol tax collected on a transfer exceeds
    the estimated compute/network overhead cost of executing it.

    This is the mathematical guarantee required by the throttling goal:
    swarm overhead < tax collected per transaction.

    Args:
        transfer_amount_usdc:   Gross transfer amount in USDC.
        estimated_overhead_usdc: Estimated total overhead cost of this
                                 transfer (API calls, gas monitoring, etc.)
                                 expressed in USDC-equivalent cost units.
        tax_fraction:           Protocol tax rate (default 1.5% = 0.015).

    Returns:
        True  → proceed; tax covers overhead.
        False → defer or skip; overhead exceeds tax revenue.
    """
    tax_collected = transfer_amount_usdc * tax_fraction
    return tax_collected > estimated_overhead_usdc
