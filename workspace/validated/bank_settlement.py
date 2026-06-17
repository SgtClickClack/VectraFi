"""
Multi-Agent Bank Settlement Tool
==================================
Sandbox extension — workspace/ only. Self-contained, no protocol layer imports.

Dependencies:
  workspace/bank_ledger.py                 (ledger engine + SQLite)
  workspace/validated/liquidity_pooler.py  (yield-split logic, read-only)
  workspace/validated/token_lease.py       (LeaseTerms schema, via pooler)

Workflow:
  1. claimant calls claim_bounty(claimant_id, bounty_amount, counterpart_id).
  2. The pooler computes a principal-weighted yield split between the two agents.
  3. The counterpart's share is transferred claimant → counterpart via the
     ledger engine (1.5% micro-tax deducted; treasury receives the tax).
  4. The claimant retains their own share — no transfer needed, it stays in
     their wallet.  Only the outgoing peer transfer is subject to tax.

Economic model
--------------
  claimant_share   = bounty_amount × weight_claimant
  counterpart_share = bounty_amount - claimant_share
  tax on transfer  = counterpart_share × 1.5%
  treasury_receive = tax
  counterpart_net  = counterpart_share - tax
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Load workspace modules via importlib (cross-agent read-only pattern)
# ---------------------------------------------------------------------------

_WORKSPACE = Path(__file__).resolve().parent.parent


def _load(name: str, rel_path: str):
    if name in sys.modules:
        return sys.modules[name]
    path = _WORKSPACE / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                  # type: ignore[union-attr]
    return mod


_ledger = _load("bank_ledger",      "bank_ledger.py")
_pooler = _load("liquidity_pooler", "validated/liquidity_pooler.py")

execute_agent_transaction = _ledger.execute_agent_transaction
InsufficientFunds         = _ledger.InsufficientFunds
get_balance               = _ledger.get_balance
get_connection            = _ledger.get_connection
get_wallet                = _ledger.get_wallet
list_wallets              = _ledger.list_wallets
init_db                   = _ledger.init_db
pool_leases               = _pooler.pool_leases
calculate_lease           = _pooler.calculate_lease


# ---------------------------------------------------------------------------
# Settlement engine
# ---------------------------------------------------------------------------

def claim_bounty(
    claimant_id: str,
    bounty_amount: int,
    counterpart_id: str,
    db_path: Path | None = None,
) -> dict:
    """
    Process a bounty claim with yield-split and micro-tax settlement.

    The claimant has already earned bounty_amount (it sits in their wallet).
    This function transfers the counterpart's yield share to the counterpart,
    with 1.5% micro-tax routed to treasury on that outgoing transfer.

    Args:
        claimant_id:    Agent holding the gross bounty balance.
        bounty_amount:  Gross bounty in integer ledger units.
        counterpart_id: Peer agent receiving the split portion.
        db_path:        Optional DB path override (for isolated testing).

    Returns:
        Settlement summary with split amounts, transfer record, and balances.
    """
    if claimant_id == counterpart_id:
        raise ValueError("claimant and counterpart must differ")
    if bounty_amount <= 0:
        raise ValueError("bounty_amount must be positive")

    kwargs: dict = {"db_path": db_path} if db_path else {}

    # Use pooler to derive the yield-split weights
    # Principals proxy the relative contribution; claimant holds 2/3 of pool.
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
            **kwargs,
        )
        transfers.append(tx)

    # Read post-settlement balances
    db = db_path or _ledger._DB_PATH
    conn = get_connection(db)
    balances = {
        claimant_id:    get_balance(claimant_id, conn),
        counterpart_id: get_balance(counterpart_id, conn),
        "treasury":     get_balance("treasury", conn),
    }
    conn.close()

    total_tax = sum(t["tax_amount"] for t in transfers)

    return {
        "bounty_amount":       bounty_amount,
        "claimant_id":         claimant_id,
        "claimant_share":      claimant_share,
        "counterpart_id":      counterpart_id,
        "counterpart_share":   counterpart_share,
        "total_tax_collected": total_tax,
        "transfers":           transfers,
        "post_balances":       balances,
    }
