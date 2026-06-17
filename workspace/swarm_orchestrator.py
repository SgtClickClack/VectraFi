"""
VectraFi Agent Swarm Orchestrator
====================================
Sandbox simulation — workspace/ only. No MCP network calls; uses bank_ledger
and bank_settlement directly for full transactional integrity.

Simulates a 3-step autonomous agent cluster cycle:
  Step 1: agent-zero inspects the active bounty state (MCP read via faba_server tools).
  Step 2: agent-one triggers a risk-weighted pooling arrangement.
  Step 3: Live settlement transfer with 1.5% micro-tax deduction → treasury.

All state writes go to workspace/bank.db only.
Telemetry is appended to workspace/agents/agent-zero/cost_log.jsonl.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_WORKSPACE = Path(__file__).resolve().parent


def _load(name: str, rel: str):
    if name in sys.modules:
        return sys.modules[name]
    path = _WORKSPACE / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                  # type: ignore[union-attr]
    return mod


_ledger     = _load("bank_ledger",      "bank_ledger.py")
_settlement = _load("bank_settlement",  "validated/bank_settlement.py")
_pooler     = _load("liquidity_pooler", "validated/liquidity_pooler.py")

init_db                   = _ledger.init_db
get_balance               = _ledger.get_balance
get_connection            = _ledger.get_connection
list_wallets              = _ledger.list_wallets
execute_agent_transaction = _ledger.execute_agent_transaction
claim_bounty              = _settlement.claim_bounty
pool_leases               = _pooler.pool_leases
calculate_lease           = _pooler.calculate_lease

_DB_PATH   = _ledger._DB_PATH
_COST_LOG  = _WORKSPACE / "agents" / "agent-zero" / "cost_log.jsonl"

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
# Step 3 — Live settlement: micro-tax deduction → treasury
# ---------------------------------------------------------------------------

def step3_settle(
    claimant_id: str,
    counterpart_id: str,
    bounty_amount: int,
) -> dict:
    """Execute the bounty settlement with 1.5% micro-tax routed to treasury."""
    t0 = time.monotonic()
    result = claim_bounty(claimant_id, bounty_amount, counterpart_id)
    elapsed = (time.monotonic() - t0) * 1000

    _log("step3_settlement", {
        "claimant":            claimant_id,
        "counterpart":         counterpart_id,
        "bounty_amount":       result["bounty_amount"],
        "claimant_share":      result["claimant_share"],
        "counterpart_share":   result["counterpart_share"],
        "tax_collected":       result["total_tax_collected"],
        "post_balances":       result["post_balances"],
    }, elapsed)
    return result


# ---------------------------------------------------------------------------
# Swarm run
# ---------------------------------------------------------------------------

def run_swarm() -> None:
    print("\n=== VectraFi Agent Swarm Orchestrator ===\n")

    # Ensure DB is initialised with seed wallets
    init_db(_DB_PATH)

    # Snapshot balances before simulation
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
