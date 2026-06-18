"""
Integration tests for GET /api/v1/onboard/journey

Covers the five-step onboarding progression:
  1. Unknown agent (no wallet) → citizen_status="unknown", 0/5 steps
  2. Wallet only (no transfers, no claims) → citizen_status="wallet_only", 1/5 steps
  3. Wallet + first transfer → citizen_status="active", 2/5 steps
  4. Wallet + corridor claim submitted (no transfer) → citizen_status="active", step 3 done
  5. Wallet + claim submitted + claim evaluated → step 4 done
  6. Wallet + all five steps complete → citizen_status="corridor_provisioner", 5/5 steps, 100%
  7. next_endpoint includes the actual negotiation_id when a queued corridor claim exists
  8. Missing agent_id query param → 422
"""

import time
import uuid

import pytest
from conftest import _TestSessionLocal, make_agent

from models import NegotiationClaim, SettlementTransaction

_JOURNEY_URL = "/api/v1/onboard/journey"
_NEGOTIATE_URL = "/api/v1/protocol/negotiate-intent"


def _seed_transfer(sender_id: str, receiver_id: str) -> None:
    """Insert a minimal SettlementTransaction row so sender_id has made a transfer."""
    gross = 10.0
    tax = round(gross * 0.001, 8)
    db = _TestSessionLocal()
    db.add(SettlementTransaction(
        tx_id=str(uuid.uuid4()),
        sender_id=sender_id,
        receiver_id=receiver_id,
        gross_amount_usdc=str(gross),
        tax_amount_usdc=str(tax),
        net_amount_usdc=str(gross - tax),
        tx_type="peer_transfer",
        created_at=int(time.time()),
    ))
    db.commit()
    db.close()


def _seed_claim(agent_id: str, intent_type: str, status: str) -> str:
    """Insert a NegotiationClaim row directly. Returns negotiation_id."""
    nid = str(uuid.uuid4())
    now = int(time.time())
    db = _TestSessionLocal()
    db.add(NegotiationClaim(
        negotiation_id=nid,
        agent_id=agent_id,
        intent_type=intent_type,
        status=status,
        created_at=now,
        updated_at=now,
        evaluated_at=now if status in ("granted", "rejected") else None,
        evaluation_reason="seeded" if status in ("granted", "rejected") else None,
    ))
    db.commit()
    db.close()
    return nid


# ---------------------------------------------------------------------------
# 1. Unknown agent
# ---------------------------------------------------------------------------

def test_journey_unknown_agent(client):
    """An agent_id with no wallet returns citizen_status=unknown and 0 completed steps."""
    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob1-ghost-agent-xyz"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["citizen_status"] == "unknown"
    assert d["completed_steps"] == 0
    assert d["total_steps"] == 5
    assert d["completion_pct"] == pytest.approx(0.0)
    assert d["next_endpoint"] == "POST /api/v1/wallet/create"
    assert not d["steps"][0]["completed"]


# ---------------------------------------------------------------------------
# 2. Wallet only
# ---------------------------------------------------------------------------

def test_journey_wallet_only(client):
    """Agent with a wallet but no transfers or claims → wallet_only, 1/5 done."""
    make_agent("ob2-wallet-only", balance_usdc=100.0)

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob2-wallet-only"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["citizen_status"] == "wallet_only"
    assert d["completed_steps"] == 1
    assert d["completion_pct"] == pytest.approx(20.0)
    assert d["steps"][0]["completed"] is True
    assert d["steps"][1]["completed"] is False
    assert d["next_endpoint"] == "POST /api/v1/settlement/transfer"


# ---------------------------------------------------------------------------
# 3. Wallet + first transfer
# ---------------------------------------------------------------------------

def test_journey_active_after_transfer(client):
    """Agent with wallet + at least one sent transfer → active, 2/5 done."""
    make_agent("ob3-sender", balance_usdc=200.0)
    make_agent("ob3-recv",   balance_usdc=0.0)
    _seed_transfer("ob3-sender", "ob3-recv")

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob3-sender"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["citizen_status"] == "active"
    assert d["completed_steps"] == 2
    assert d["completion_pct"] == pytest.approx(40.0)
    assert d["steps"][0]["completed"] is True
    assert d["steps"][1]["completed"] is True
    assert d["steps"][2]["completed"] is False


# ---------------------------------------------------------------------------
# 4. Wallet + corridor claim submitted (no transfer)
# ---------------------------------------------------------------------------

def test_journey_active_with_corridor_claim(client):
    """Agent with wallet + a corridor claim (no transfer yet) → active, steps 1 and 3 done."""
    make_agent("ob4-claimant", balance_usdc=100.0)
    _seed_claim("ob4-claimant", "corridor_provisioning", "queued")

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob4-claimant"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["citizen_status"] == "active"
    assert d["steps"][0]["completed"] is True   # wallet
    assert d["steps"][1]["completed"] is False  # no transfer
    assert d["steps"][2]["completed"] is True   # claim submitted
    assert d["steps"][3]["completed"] is False  # not evaluated
    assert d["steps"][4]["completed"] is False  # not granted
    # next_action should be step 2 (first uncompleted)
    assert d["next_endpoint"] == "POST /api/v1/settlement/transfer"


# ---------------------------------------------------------------------------
# 5. Claim evaluated (granted or rejected) — step 4 done
# ---------------------------------------------------------------------------

def test_journey_claim_evaluated(client):
    """Agent with wallet + evaluated corridor claim → step 4 done."""
    make_agent("ob5-evaluated", balance_usdc=100.0)
    _seed_claim("ob5-evaluated", "corridor_provisioning", "rejected")

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob5-evaluated"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["steps"][2]["completed"] is True  # claim submitted
    assert d["steps"][3]["completed"] is True  # claim evaluated
    assert d["steps"][4]["completed"] is False  # not granted


# ---------------------------------------------------------------------------
# 6. Full corridor provisioner — all 5 steps
# ---------------------------------------------------------------------------

def test_journey_full_corridor_provisioner(client):
    """All five steps complete → corridor_provisioner, 5/5, 100%."""
    make_agent("ob6-provisioner",  balance_usdc=500.0)
    make_agent("ob6-recv",         balance_usdc=0.0)
    _seed_transfer("ob6-provisioner", "ob6-recv")
    _seed_claim("ob6-provisioner", "corridor_provisioning", "granted")

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob6-provisioner"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["citizen_status"] == "corridor_provisioner"
    assert d["completed_steps"] == 5
    assert d["completion_pct"] == pytest.approx(100.0)
    assert all(s["completed"] for s in d["steps"])
    assert d["next_endpoint"] is None
    assert "complete" in d["next_action"].lower()


# ---------------------------------------------------------------------------
# 7. next_endpoint contains actual negotiation_id for queued corridor claim
# ---------------------------------------------------------------------------

def test_journey_next_endpoint_has_negotiation_id(client):
    """When a queued corridor claim exists, next_endpoint for step 4 contains the real ID."""
    make_agent("ob7-pending",  balance_usdc=100.0)
    make_agent("ob7-recv-tx",  balance_usdc=0.0)
    _seed_transfer("ob7-pending", "ob7-recv-tx")  # step 2 done
    nid = _seed_claim("ob7-pending", "corridor_provisioning", "queued")  # step 3 done, 4 not done

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob7-pending"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["steps"][3]["completed"] is False
    assert nid in d["steps"][3]["endpoint"]
    # next_action is for step 4 since steps 1-3 are complete
    assert d["next_endpoint"] is not None
    assert nid in d["next_endpoint"]


# ---------------------------------------------------------------------------
# 8. Missing agent_id → 422
# ---------------------------------------------------------------------------

def test_journey_missing_agent_id_returns_422(client):
    """Omitting the required agent_id query param returns 422 Unprocessable Entity."""
    resp = client.get(_JOURNEY_URL)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 9. liquidity_allocation claim also qualifies for step 4/5
# ---------------------------------------------------------------------------

def test_journey_liquidity_allocation_claim_qualifies(client):
    """A liquidity_allocation granted claim also completes steps 3, 4, and 5."""
    make_agent("ob9-liq-agent", balance_usdc=200.0)
    _seed_claim("ob9-liq-agent", "liquidity_allocation", "granted")

    resp = client.get(_JOURNEY_URL, params={"agent_id": "ob9-liq-agent"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["steps"][2]["completed"] is True  # submitted
    assert d["steps"][3]["completed"] is True  # evaluated
    assert d["steps"][4]["completed"] is True  # granted
    assert d["citizen_status"] == "corridor_provisioner"
