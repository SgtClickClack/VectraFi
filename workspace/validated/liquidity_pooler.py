"""
Multi-Agent Liquidity Pooler
=============================
Sandbox extension — workspace/ only. Self-contained, no protocol layer imports.

Dependencies:
  workspace/validated/token_lease.py  (Agent Zero's LeaseTerms schema, read-only)

Aggregates multiple LeaseTerms into a unified liquidity pool and distributes
pooled yield to participants based on a dynamic risk/reward ratio.

Risk/reward model
-----------------
Each participant's share of the pool yield is proportional to their
principal weighted by a risk multiplier:

    weighted_i = principal_i * risk_multiplier_i
    share_i    = weighted_i / sum(weighted_j for all j)
    yield_i    = total_pool_yield * share_i

The total pool yield is the sum of all constituent lease fees, capped so
that no distribution round-trip exceeds 100% of the pool.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Cross-agent import: load LeaseTerms from workspace/validated/
# ---------------------------------------------------------------------------

_VALIDATED_DIR = Path(__file__).resolve().parent.parent / "validated"
_LEASE_MODULE_PATH = _VALIDATED_DIR / "token_lease.py"

_spec = importlib.util.spec_from_file_location("token_lease", _LEASE_MODULE_PATH)
_token_lease_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["token_lease"] = _token_lease_mod   # required: dataclass resolves __module__ via sys.modules
_spec.loader.exec_module(_token_lease_mod)  # type: ignore[union-attr]

LeaseTerms = _token_lease_mod.LeaseTerms
calculate_lease = _token_lease_mod.calculate_lease


# ---------------------------------------------------------------------------
# Pool participant
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PoolParticipant:
    agent_id: str
    lease: LeaseTerms
    risk_multiplier: float = 1.0  # > 1.0 = higher risk appetite, larger share claim

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id must be non-empty")
        if self.risk_multiplier <= 0:
            raise ValueError("risk_multiplier must be positive")


# ---------------------------------------------------------------------------
# Pool yield distribution
# ---------------------------------------------------------------------------

@dataclass
class PoolYieldDistribution:
    total_pool_yield: float
    expiry_epoch: int                           # min of constituent lease expiries
    shares: dict[str, float] = field(default_factory=dict)   # agent_id -> yield amount
    weights: dict[str, float] = field(default_factory=dict)  # agent_id -> weight fraction

    def validate(self) -> None:
        total = sum(self.shares.values())
        if total > self.total_pool_yield * 1.000_001:  # floating-point tolerance
            raise ArithmeticError(
                f"Distributed yield {total:.8f} exceeds pool total {self.total_pool_yield:.8f}"
            )


def build_pool(participants: Sequence[PoolParticipant]) -> PoolYieldDistribution:
    """
    Construct a yield distribution from a list of pool participants.

    Invariants enforced:
    - total distributed yield == sum of constituent lease fees
    - per-agent share proportional to principal * risk_multiplier
    - pool expiry = min(constituent lease expiry epochs)
    - no agent receives > 100% of pool yield
    """
    if not participants:
        raise ValueError("Pool must have at least one participant")

    total_yield = sum(p.lease.total_fee for p in participants)
    expiry = min(p.lease.expiry_epoch for p in participants)

    raw_weights = {
        p.agent_id: p.lease.principal * p.risk_multiplier
        for p in participants
    }
    weight_sum = sum(raw_weights.values())
    if weight_sum == 0:
        raise ArithmeticError("Aggregate pool weight is zero — cannot distribute")

    norm_weights = {aid: w / weight_sum for aid, w in raw_weights.items()}
    shares = {aid: round(total_yield * w, 8) for aid, w in norm_weights.items()}

    dist = PoolYieldDistribution(
        total_pool_yield=round(total_yield, 8),
        expiry_epoch=expiry,
        shares=shares,
        weights=norm_weights,
    )
    dist.validate()
    return dist


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------

def pool_leases(
    agent_leases: list[tuple[str, LeaseTerms, float]],
) -> PoolYieldDistribution:
    """
    High-level entry point.

    Args:
        agent_leases: list of (agent_id, LeaseTerms, risk_multiplier) tuples.

    Returns:
        PoolYieldDistribution with per-agent yield shares.
    """
    participants = [
        PoolParticipant(agent_id=aid, lease=lease, risk_multiplier=rm)
        for aid, lease, rm in agent_leases
    ]
    return build_pool(participants)
