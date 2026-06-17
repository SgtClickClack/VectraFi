"""
VectraFi Agent Swarm Orchestrator
====================================
Sandbox simulation — workspace/ only. No MCP network calls; uses bank_ledger
directly for full transactional integrity.

Simulates a 3-step autonomous agent cluster cycle:
  Step 1: agent-zero inspects the active bounty state (MCP read via faba_server tools).
  Step 2: agent-one triggers a risk-weighted pooling arrangement.
  Step 3: Live settlement transfer with 1.5% micro-tax deduction → treasury.

All state writes go to workspace/bank.db only.
Telemetry is appended to workspace/agents/agent-zero/cost_log.jsonl.

Security changes (C-1, C-2, C-4):
  - Removed importlib.util.exec_module loading of validated/ artifacts.  Those
    ran untrusted Python code in-process with full parent privileges and polluted
    sys.modules with unqualified short names, enabling origin-blind schema
    substitution by any file that landed in workspace/validated/.
  - LeaseTerms is now a Pydantic BaseModel owned by this orchestrator.
    calculate_lease() serialises inputs to JSON and validates via
    model_validate_json(), treating cross-agent schema data as pure data
    rather than executable Python modules.
  - Pool math and bounty settlement are inlined; no dynamic code loading from
    validated/ at any point in the swarm execution path.
  - bank_ledger (workspace root, first-party) is imported via sys.path +
    regular import — not exec_module — to preserve normal module semantics.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_WORKSPACE = Path(__file__).resolve().parent

# Add workspace root to sys.path so bank_ledger can be imported with a standard
# import statement.  bank_ledger.py lives at the workspace root (not in
# validated/) and is first-party trusted code; this replaces the previous
# exec_module pattern.
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

import bank_ledger  # noqa: E402 — intentional: import after sys.path setup

init_db                   = bank_ledger.init_db
get_balance               = bank_ledger.get_balance
get_connection            = bank_ledger.get_connection
list_wallets              = bank_ledger.list_wallets
execute_agent_transaction = bank_ledger.execute_agent_transaction

_DB_PATH  = bank_ledger._DB_PATH
_COST_LOG = _WORKSPACE / "agents" / "agent-zero" / "cost_log.jsonl"


# ---------------------------------------------------------------------------
# Trusted Pydantic schema — replaces cross-agent importlib token_lease loading
# ---------------------------------------------------------------------------

TimeUnit = Literal["hour", "day", "week"]

_SECONDS: dict[str, int] = {"hour": 3_600, "day": 86_400, "week": 604_800}


class LeaseTerms(BaseModel):
    """
    Validated lease terms schema owned by this orchestrator.

    total_fee and expiry_epoch are derived from base fields at validation time
    and cannot be overridden by caller-supplied values — the before-validator
    always recomputes them.
    """
    model_config = {"frozen": True}

    principal: float = Field(..., gt=0)
    duration_units: int = Field(..., gt=0)
    time_unit: TimeUnit = "day"
    micro_tax_rate: float = Field(..., gt=0, lt=1)
    total_fee: float = Field(default=0.0)
    expiry_epoch: int = Field(default=0)

    @model_validator(mode="before")
    @classmethod
    def _compute_derived(cls, data: Any) -> Any:
        """Compute total_fee and expiry_epoch from base fields before the model is frozen."""
        if isinstance(data, dict):
            data = dict(data)  # avoid mutating the caller's dict
            p  = float(data.get("principal", 0))
            d  = int(data.get("duration_units", 0))
            r  = float(data.get("micro_tax_rate", 0))
            tu = str(data.get("time_unit", "day"))
            data["total_fee"]    = round(p * r * d, 8)
            data["expiry_epoch"] = int(time.time()) + d * _SECONDS.get(tu, 86_400)
        return data


def calculate_lease(
    principal: float,
    duration_units: int,
    time_unit: TimeUnit = "day",
    micro_tax_rate: float = 0.001,
) -> LeaseTerms:
    """
    Build and validate a LeaseTerms instance via JSON round-trip.

    Serialising inputs with json.dumps and deserialising with model_validate_json
    treats lease properties as pure data, ensuring no Python object coercion
    path and validating against the trusted Pydantic schema owned by this
    orchestrator (Fix C-4).
    """
    payload = json.dumps({
        "principal": principal,
        "duration_units": duration_units,
        "time_unit": time_unit,
        "micro_tax_rate": micro_tax_rate,
    })
    return LeaseTerms.model_validate_json(payload)


# ---------------------------------------------------------------------------
# Pool yield distribution
# ---------------------------------------------------------------------------

@dataclass
class PoolYieldDistribution:
    total_pool_yield: float
    expiry_epoch: int
    shares: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)


def pool_leases(
    agent_leases: list[tuple[str, LeaseTerms, float]],
) -> PoolYieldDistribution:
    """
    Distribute pool yield across participants weighted by principal × risk_multiplier.
    All inputs are validated LeaseTerms instances — no dynamic code loading.
    """
    if not agent_leases:
        raise ValueError("Pool must have at least one participant")

    total_yield = sum(lease.total_fee for _, lease, _ in agent_leases)
    expiry      = min(lease.expiry_epoch for _, lease, _ in agent_leases)

    raw_weights = {aid: lease.principal * rm for aid, lease, rm in agent_leases}
    weight_sum  = sum(raw_weights.values())
    if weight_sum == 0:
        raise ArithmeticError("Aggregate pool weight is zero — cannot distribute")

    norm_weights = {aid: w / weight_sum for aid, w in raw_weights.items()}
    shares = {aid: round(total_yield * w, 8) for aid, w in norm_weights.items()}

    return PoolYieldDistribution(
        total_pool_yield=round(total_yield, 8),
        expiry_epoch=expiry,
        shares=shares,
        weights=norm_weights,
    )


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def _log(event: str, payload: dict | None = None, elapsed_ms: float = 0.0) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "event": event,
        "elapsed_ms": round(elapsed_ms, 2),
    }
    if payload:
        entry.update(payload)
    with _COST_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"  [{event}] {json.dumps({k: v for k, v in entry.items() if k not in ('ts', 'event', 'elapsed_ms')})}")


# ---------------------------------------------------------------------------
# Step 1 — agent-zero: Inspect active bounty state
# ---------------------------------------------------------------------------

_SANDBOX_BOUNTIES = [
    {"id": "bounty-001", "title": "Implement Protocol Treasury Fee Collector", "yield_units": 400},
    {"id": "bounty-002", "title": "Implement Multi-Route DeFi Yield Aggregator",  "yield_units": 600},
    {"id": "bounty-003", "title": "Implement Autonomous Portfolio Rebalancing",    "yield_units": 500},
]


def step1_inspect_bounties(agent_id: str = "agent-zero", db_path: Path | None = None) -> dict:
    """agent-zero inspects available bounties and selects the highest-yield target."""
    t0 = time.monotonic()
    conn = get_connection(db_path or _DB_PATH)
    balance = get_balance(agent_id, conn)
    conn.close()

    selected = max(_SANDBOX_BOUNTIES, key=lambda b: b["yield_units"])
    elapsed = (time.monotonic() - t0) * 1000
    _log("step1_inspect_bounties", {
        "agent": agent_id,
        "balance": balance,
        "bounties_seen": len(_SANDBOX_BOUNTIES),
        "selected_bounty": selected["id"],
        "selected_yield": selected["yield_units"],
    }, elapsed)
    return {"agent": agent_id, "balance": balance, "selected": selected}


# ---------------------------------------------------------------------------
# Step 2 — agent-one: Compute risk-weighted pooling arrangement
# ---------------------------------------------------------------------------

def step2_pool_arrangement(
    agent_zero_principal: int,
    agent_one_principal: int,
    bounty_yield: int,
) -> dict:
    """agent-one computes the risk-weighted yield split for the selected bounty."""
    t0 = time.monotonic()

    lease_zero = calculate_lease(float(agent_zero_principal), 1, "day", 0.01)
    lease_one  = calculate_lease(float(agent_one_principal),  1, "day", 0.01)

    pool = pool_leases([
        ("agent-zero", lease_zero, 1.2),   # agent-zero: higher risk multiplier (lead claimant)
        ("agent-one",  lease_one,  0.8),   # agent-one:  lower risk multiplier (counterpart)
    ])

    zero_share = round(bounty_yield * pool.weights["agent-zero"])
    one_share  = bounty_yield - zero_share

    elapsed = (time.monotonic() - t0) * 1000
    _log("step2_pool_arrangement", {
        "bounty_yield": bounty_yield,
        "agent_zero_weight": round(pool.weights["agent-zero"], 4),
        "agent_one_weight":  round(pool.weights["agent-one"],  4),
        "agent_zero_share":  zero_share,
        "agent_one_share":   one_share,
    }, elapsed)
    return {
        "pool": pool,
        "zero_share": zero_share,
        "one_share":  one_share,
    }


# ---------------------------------------------------------------------------
# Step 3 — Settlement: inline bounty claim with micro-tax → treasury
# ---------------------------------------------------------------------------

def step3_settle(
    claimant_id: str,
    counterpart_id: str,
    bounty_amount: int,
) -> dict:
    """
    Inline bounty settlement — no validated/ module loading.

    Derives the yield split via the trusted LeaseTerms Pydantic schema and
    pool_leases, then calls bank_ledger.execute_agent_transaction directly.
    Mirrors the logic previously in bank_settlement.claim_bounty; the
    claimant's principal is 2× the counterpart's to model lead-claimant
    contribution weighting.
    """
    t0 = time.monotonic()

    if claimant_id == counterpart_id:
        raise ValueError("claimant and counterpart must differ")
    if bounty_amount <= 0:
        raise ValueError("bounty_amount must be positive")

    lease_claimant    = calculate_lease(bounty_amount * 2, 1, "day", 0.01)
    lease_counterpart = calculate_lease(bounty_amount,     1, "day", 0.01)

    pool = pool_leases([
        (claimant_id,    lease_claimant,    1.0),
        (counterpart_id, lease_counterpart, 1.0),
    ])

    counterpart_share = round(bounty_amount * pool.weights[counterpart_id])
    claimant_share    = bounty_amount - counterpart_share

    transfers = []
    if counterpart_share > 0:
        tx = execute_agent_transaction(
            sender_id=claimant_id,
            receiver_id=counterpart_id,
            amount=counterpart_share,
            tx_type="bounty_yield_split",
        )
        transfers.append(tx)

    conn = get_connection(_DB_PATH)
    post_balances = {
        claimant_id:    get_balance(claimant_id, conn),
        counterpart_id: get_balance(counterpart_id, conn),
        "treasury":     get_balance("treasury", conn),
    }
    conn.close()

    total_tax = sum(t["tax_amount"] for t in transfers)
    elapsed   = (time.monotonic() - t0) * 1000

    _log("step3_settlement", {
        "claimant":          claimant_id,
        "counterpart":       counterpart_id,
        "bounty_amount":     bounty_amount,
        "claimant_share":    claimant_share,
        "counterpart_gross": counterpart_share,
        "tax_collected":     total_tax,
        "post_balances":     post_balances,
    }, elapsed)

    return {
        "bounty_amount":       bounty_amount,
        "claimant_id":         claimant_id,
        "claimant_share":      claimant_share,
        "counterpart_id":      counterpart_id,
        "counterpart_share":   counterpart_share,
        "total_tax_collected": total_tax,
        "transfers":           transfers,
        "post_balances":       post_balances,
    }


# ---------------------------------------------------------------------------
# Swarm run
# ---------------------------------------------------------------------------

def run_swarm() -> None:
    print("\n=== VectraFi Agent Swarm Orchestrator ===\n")

    init_db(_DB_PATH)

    wallets_before = {w["agent_id"]: w["balance"] for w in list_wallets(_DB_PATH)}
    print(f"Initial balances: {wallets_before}\n")
    _log("swarm_start", {"initial_balances": wallets_before})

    # --- Step 1 ---
    print("Step 1: agent-zero inspects bounty state")
    s1 = step1_inspect_bounties("agent-zero")

    # --- Step 2 ---
    print("\nStep 2: agent-one computes risk-weighted pool arrangement")
    conn = get_connection(_DB_PATH)
    z_bal = get_balance("agent-zero", conn)
    o_bal = get_balance("agent-one",  conn)
    conn.close()
    s2 = step2_pool_arrangement(
        agent_zero_principal=z_bal,
        agent_one_principal=o_bal,
        bounty_yield=s1["selected"]["yield_units"],
    )

    # --- Step 3 ---
    print("\nStep 3: live settlement — agent-zero claims bounty, splits yield, micro-tax deducted")
    s3 = step3_settle(
        claimant_id="agent-zero",
        counterpart_id="agent-one",
        bounty_amount=s1["selected"]["yield_units"],
    )

    # Final state
    wallets_after = {w["agent_id"]: w["balance"] for w in list_wallets(_DB_PATH)}
    tax_total = s3["total_tax_collected"]

    _log("swarm_complete", {
        "final_balances": wallets_after,
        "cumulative_tax_to_treasury": tax_total,
    })

    print("\n=== Swarm Simulation Complete ===")
    print(f"\nFinal balances:   {wallets_after}")
    print(f"Treasury tax collected this run: {tax_total} units")
    print(f"Treasury total balance:          {wallets_after.get('treasury', 0)} units")


if __name__ == "__main__":
    run_swarm()
