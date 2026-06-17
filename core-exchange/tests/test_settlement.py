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

import pytest
from eth_account import Account as _Acct
from conftest import get_balance, make_agent, sign_body

_TRANSFER_URL = "/api/v1/settlement/transfer"
_BOUNTY_URL   = "/api/v1/settlement/claim-bounty"


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
    assert d["tax_amount_usdc"]      == pytest.approx(1.5,   rel=1e-6)   # 1.5% of 100
    assert d["net_amount_usdc"]      == pytest.approx(98.5,  rel=1e-6)
    assert d["sender_balance_usdc"]   == pytest.approx(400.0, rel=1e-6)
    assert d["receiver_balance_usdc"] == pytest.approx(98.5,  rel=1e-6)


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
        total_expected_tax += round(amount * 0.015, 8)
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
    # 1.5% tax on 100.0 = 1.5
    assert d["tax_amount_usdc"]         == pytest.approx(1.5,   rel=1e-4)
    # counterpart receives 98.5 net
    assert d["counterpart_net_usdc"]    == pytest.approx(98.5,  rel=1e-4)
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
