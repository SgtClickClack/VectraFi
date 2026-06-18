"""
Integration tests for POST /api/v1/settlement/transfer
                  and POST /api/v1/settlement/claim-bounty

Covers:
  1. Successful transfer — correct 1.5% tax math and treasury routing.
  2. Missing X-VectraFi-Signature → 401, zero state mutation.
  3. Forged/mismatched signature → 401, zero state mutation.
  4. Insufficient sender balance → 400, atomic rollback.
  5. Self-transfer guard → 400.
  6. Unknown receiver → 404, no mutation.
  7. Unit conservation: gross == tax + net.
  8. Treasury accumulates across sequential transfers.
  9. claim-bounty split — correct counterpart share and claimant retention.
 10. claim-bounty same-agent guard → 400.
"""

import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from eth_account import Account as _Acct
from conftest import _TestSessionLocal, get_balance, make_agent, sign_body

from models import NegotiationClaim
from web3_bridge import OnchainSettlementResult

_TRANSFER_URL = "/api/v1/settlement/transfer"
_BOUNTY_URL   = "/api/v1/settlement/claim-bounty"


def _seed_claim(agent_id: str, intent_type: str, status: str) -> str:
    """Insert a NegotiationClaim row directly into the test DB. Returns negotiation_id."""
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
        evaluation_reason="seeded by test" if status in ("granted", "rejected") else None,
    ))
    db.commit()
    db.close()
    return nid


# ---------------------------------------------------------------------------
# 1. Successful transfer — 1.5% tax math
# ---------------------------------------------------------------------------

def test_transfer_tax_math(client):
    acct_s, sender   = make_agent("t1-sender",   balance_usdc=500.0)
    _,       receiver = make_agent("t1-receiver", balance_usdc=0.0)

    body = {
        "agent_id":       sender.agent_id,
        "wallet_address": acct_s.address,
        "receiver_id":    receiver.agent_id,
        "amount_usdc":    100.0,
        "tx_type":        "test_transfer",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200, resp.text

    d = resp.json()
    assert d["gross_amount_usdc"]    == pytest.approx(100.0, rel=1e-6)
    assert d["tax_amount_usdc"]      == pytest.approx(0.1,   rel=1e-6)   # 0.1% of 100
    assert d["net_amount_usdc"]      == pytest.approx(99.9,  rel=1e-6)
    assert d["sender_balance_usdc"]   == pytest.approx(400.0, rel=1e-6)
    assert d["receiver_balance_usdc"] == pytest.approx(99.9,  rel=1e-6)


# ---------------------------------------------------------------------------
# 2. Missing signature header → 401, no mutation
# ---------------------------------------------------------------------------

def test_transfer_missing_signature_returns_401(client):
    acct_s, sender   = make_agent("t2-sender",   balance_usdc=200.0)
    _,       receiver = make_agent("t2-receiver", balance_usdc=0.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 50.0, "tx_type": "test",
    }
    resp = client.post(_TRANSFER_URL, json=body)   # no signature header
    assert resp.status_code == 401
    assert "X-VectraFi-Signature" in resp.json()["detail"]

    assert get_balance(sender.agent_id)   == pytest.approx(200.0)
    assert get_balance(receiver.agent_id) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. Forged / mismatched signature → 401, no mutation
# ---------------------------------------------------------------------------

def test_transfer_forged_signature_returns_401(client):
    acct_s, sender   = make_agent("t3-sender",   balance_usdc=200.0)
    _,       receiver = make_agent("t3-receiver", balance_usdc=0.0)
    intruder = _Acct.create()

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 50.0, "tx_type": "test",
    }
    forged_sig = sign_body(intruder, body)
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": forged_sig})
    assert resp.status_code == 401

    assert get_balance(sender.agent_id) == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# 4. Insufficient balance → 400, atomic rollback
# ---------------------------------------------------------------------------

def test_transfer_insufficient_balance_returns_400(client):
    acct_s, sender   = make_agent("t4-sender",   balance_usdc=10.0)
    _,       receiver = make_agent("t4-receiver", balance_usdc=0.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 999.0, "tx_type": "test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 400
    assert "Insufficient" in resp.json()["detail"]

    assert get_balance(sender.agent_id)   == pytest.approx(10.0)
    assert get_balance(receiver.agent_id) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 5. Self-transfer guard → 400
# ---------------------------------------------------------------------------

def test_transfer_self_transfer_returns_400(client):
    acct_s, sender = make_agent("t5-sender", balance_usdc=100.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": sender.agent_id, "amount_usdc": 50.0, "tx_type": "test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 400
    assert "differ" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. Unknown receiver → 404, no mutation
# ---------------------------------------------------------------------------

def test_transfer_unknown_receiver_returns_404(client):
    acct_s, sender = make_agent("t6-sender", balance_usdc=100.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": "does-not-exist", "amount_usdc": 50.0, "tx_type": "test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 404

    assert get_balance(sender.agent_id) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 7. Unit conservation: gross == tax + net
# ---------------------------------------------------------------------------

def test_transfer_unit_conservation(client):
    acct_s, sender   = make_agent("t7-sender",   balance_usdc=750.0)
    _,       receiver = make_agent("t7-receiver", balance_usdc=0.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 300.0, "tx_type": "test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200
    d = resp.json()
    assert d["gross_amount_usdc"] == pytest.approx(
        d["tax_amount_usdc"] + d["net_amount_usdc"], rel=1e-8
    )


# ---------------------------------------------------------------------------
# 8. Treasury accumulates across sequential transfers
# ---------------------------------------------------------------------------

def test_treasury_accumulates_across_transfers(client):
    acct_s, sender   = make_agent("t8-sender",   balance_usdc=1000.0)
    _,       receiver = make_agent("t8-receiver", balance_usdc=0.0)

    total_expected_tax = 0.0
    last_resp = None
    for amount in [100.0, 200.0, 150.0]:
        body = {
            "agent_id": sender.agent_id, "wallet_address": acct_s.address,
            "receiver_id": receiver.agent_id, "amount_usdc": amount, "tx_type": "seq",
        }
        resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
        assert resp.status_code == 200
        total_expected_tax += round(amount * 0.001, 8)
        last_resp = resp

    # Treasury balance must have grown by at least our total tax contribution
    assert last_resp.json()["treasury_accumulated_fees_usdc"] >= total_expected_tax - 1e-6


# ---------------------------------------------------------------------------
# 9. claim-bounty: correct split and tax routing
# ---------------------------------------------------------------------------

def test_claim_bounty_split_and_tax(client):
    acct_c, claimant   = make_agent("b9-claimant",   balance_usdc=500.0)
    _,       counterpart = make_agent("b9-counterpart", balance_usdc=0.0)

    body = {
        "agent_id":              claimant.agent_id,
        "wallet_address":        acct_c.address,
        "counterpart_id":        counterpart.agent_id,
        "bounty_amount_usdc":    300.0,
        "counterpart_share_pct": 1/3,
    }
    resp = client.post(_BOUNTY_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_c, body)})
    assert resp.status_code == 200, resp.text
    d = resp.json()

    # counterpart_gross ≈ 100.0 (300 * 1/3)
    assert d["counterpart_gross_usdc"]  == pytest.approx(100.0, rel=1e-4)
    # 0.1% tax on 100.0 = 0.1
    assert d["tax_amount_usdc"]         == pytest.approx(0.1,   rel=1e-4)
    # counterpart receives 99.9 net
    assert d["counterpart_net_usdc"]    == pytest.approx(99.9,  rel=1e-4)
    # claimant keeps 200 of the bounty, balance drops by the 100 transferred
    assert d["claimant_share_usdc"]     == pytest.approx(200.0, rel=1e-4)
    assert d["claimant_balance_usdc"]   == pytest.approx(400.0, rel=1e-4)


# ---------------------------------------------------------------------------
# 10. claim-bounty same-agent guard → 400
# ---------------------------------------------------------------------------

def test_claim_bounty_self_claim_returns_400(client):
    acct_c, claimant = make_agent("b10-claimant", balance_usdc=200.0)

    body = {
        "agent_id":              claimant.agent_id,
        "wallet_address":        acct_c.address,
        "counterpart_id":        claimant.agent_id,
        "bounty_amount_usdc":    100.0,
        "counterpart_share_pct": 0.5,
    }
    resp = client.post(_BOUNTY_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_c, body)})
    assert resp.status_code == 400
    assert "differ" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 11. toll_rate_applied_pct always present in transfer response
# ---------------------------------------------------------------------------

def test_transfer_response_includes_toll_rate_pct(client):
    """Standard agent (no corridor claim) pays 0.1% and the field is always present."""
    acct_s, sender   = make_agent("t11-sender",   balance_usdc=200.0)
    _,       receiver = make_agent("t11-receiver", balance_usdc=0.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 100.0, "tx_type": "test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert "toll_rate_applied_pct" in d
    assert d["toll_rate_applied_pct"] == pytest.approx(0.1, rel=1e-6)


# ---------------------------------------------------------------------------
# 12. Preferential toll — granted corridor provisioner pays 0.05%
# ---------------------------------------------------------------------------

def test_preferential_toll_for_corridor_provisioner(client):
    """Agent with a granted corridor_provisioning claim pays 0.05% (half standard)."""
    acct_s, sender   = make_agent("t12-corridor-sender",   balance_usdc=500.0)
    _,       receiver = make_agent("t12-corridor-receiver", balance_usdc=0.0)

    _seed_claim(sender.agent_id, "corridor_provisioning", "granted")

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 200.0, "tx_type": "corridor_transfer",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200, resp.text
    d = resp.json()

    # 0.05% of 200.0 = 0.1 USDC (ROUND_UP at 8dp)
    assert d["tax_amount_usdc"]      == pytest.approx(0.1,   rel=1e-6)
    assert d["net_amount_usdc"]      == pytest.approx(199.9, rel=1e-6)
    assert d["toll_rate_applied_pct"] == pytest.approx(0.05,  rel=1e-6)


# ---------------------------------------------------------------------------
# 13. Preferential toll — liquidity_allocation also qualifies
# ---------------------------------------------------------------------------

def test_preferential_toll_for_liquidity_allocation(client):
    """Agent with a granted liquidity_allocation claim also pays 0.05%."""
    acct_s, sender   = make_agent("t13-liq-sender",   balance_usdc=1000.0)
    _,       receiver = make_agent("t13-liq-receiver", balance_usdc=0.0)

    _seed_claim(sender.agent_id, "liquidity_allocation", "granted")

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 100.0, "tx_type": "liq_test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["toll_rate_applied_pct"] == pytest.approx(0.05, rel=1e-6)
    assert d["tax_amount_usdc"]       == pytest.approx(0.05, rel=1e-6)  # 0.05% of 100


# ---------------------------------------------------------------------------
# 14. Standard toll preserved — queued claim does not unlock preferential rate
# ---------------------------------------------------------------------------

def test_queued_claim_does_not_unlock_preferential_toll(client):
    """A claim that is only 'queued' (not 'granted') leaves the standard 0.1% in force."""
    acct_s, sender   = make_agent("t14-queued-sender",   balance_usdc=300.0)
    _,       receiver = make_agent("t14-queued-receiver", balance_usdc=0.0)

    _seed_claim(sender.agent_id, "corridor_provisioning", "queued")

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 100.0, "tx_type": "standard_test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["toll_rate_applied_pct"] == pytest.approx(0.1,  rel=1e-6)
    assert d["tax_amount_usdc"]       == pytest.approx(0.1,  rel=1e-6)  # standard 0.1% of 100


# ---------------------------------------------------------------------------
# 15. On-chain settlement — sandbox no-op
# ---------------------------------------------------------------------------

def test_onchain_settlement_sandbox_noop(client):
    """Sandbox mode (bridge not configured): response includes on_chain_toll_tx=None, no RPC call."""
    acct_s, sender   = make_agent("t15-sandbox-sender",   balance_usdc=200.0)
    _,       receiver = make_agent("t15-sandbox-receiver", balance_usdc=0.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 100.0, "tx_type": "sandbox_test",
    }
    resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert "on_chain_toll_tx" in d
    assert d["on_chain_toll_tx"] is None


# ---------------------------------------------------------------------------
# 16. On-chain settlement — live RPC path (mocked bridge)
# ---------------------------------------------------------------------------

def test_onchain_settlement_live_rpc_dispatches(client):
    """Live RPC mode: process_onchain_settlement is called with correct tax amount."""
    fake_result = OnchainSettlementResult(
        status="CONFIRMING",
        settlement_mode="escrow",
        net_tx_hash="0x" + "a" * 64,
        tax_tx_hash="0x" + "b" * 64,
    )

    acct_s, sender   = make_agent("t16-live-sender",   balance_usdc=200.0)
    _,       receiver = make_agent("t16-live-receiver", balance_usdc=0.0)

    body = {
        "agent_id": sender.agent_id, "wallet_address": acct_s.address,
        "receiver_id": receiver.agent_id, "amount_usdc": 100.0, "tx_type": "live_rpc_test",
    }

    with patch("routes.settlement._w3_bridge") as mock_bridge:
        mock_bridge.is_configured = True
        mock_bridge.process_onchain_settlement = AsyncMock(return_value=fake_result)

        resp = client.post(_TRANSFER_URL, json=body, headers={"X-VectraFi-Signature": sign_body(acct_s, body)})

    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["on_chain_toll_tx"] is None  # background runs after response; field is always None immediately

    mock_bridge.process_onchain_settlement.assert_called_once()
    call_kwargs = mock_bridge.process_onchain_settlement.call_args.kwargs
    assert call_kwargs["tax_amount"] == pytest.approx(Decimal("0.1"), rel=1e-6)
    assert call_kwargs["gross_amount"] == pytest.approx(Decimal("100.0"), rel=1e-6)
