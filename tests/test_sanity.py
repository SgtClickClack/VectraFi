"""
Baseline sanity tests — must pass on a clean repo checkout with no external services.

These establish the test suite foundation that contributor agents build on.
All tests run in sandbox mode (no RPC_PROVIDER_URL set).
"""
import pytest
from pydantic import ValidationError

from config import FEE_SPLIT_BOUNTY_RATE, FEE_SPLIT_CREATOR_RATE, PROTOCOL_FEE_RATE
from schemas import DepositRequest, SwapRequest


# ---------------------------------------------------------------------------
# Health and market data
# ---------------------------------------------------------------------------


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["execution_mode"] in ("sandbox", "live_rpc")


def test_market_prices_structure(client):
    r = client.get("/api/v1/market/prices")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"ETH", "USDC", "HBAR", "currency", "source"}
    assert body["currency"] == "USD"
    assert body["source"] in ("live", "fallback")
    assert body["ETH"] > 0
    assert body["USDC"] > 0
    assert body["HBAR"] > 0


# ---------------------------------------------------------------------------
# Wallet lifecycle
# ---------------------------------------------------------------------------


def test_wallet_create_succeeds(client):
    r = client.post("/api/v1/wallet/create", json={"agent_id": "sanity-wallet-001"})
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "sanity-wallet-001"
    assert body["wallet_address"].startswith("0x")
    assert len(body["wallet_address"]) == 42
    assert body["balance_usdc"] == 1000.0
    assert body["balance_hbar"] == 0.0
    assert "private_key" in body


def test_wallet_create_duplicate_returns_409(client):
    client.post("/api/v1/wallet/create", json={"agent_id": "sanity-dup-agent"})
    r = client.post("/api/v1/wallet/create", json={"agent_id": "sanity-dup-agent"})
    assert r.status_code == 409


def test_wallet_create_empty_agent_id_returns_422(client):
    r = client.post("/api/v1/wallet/create", json={"agent_id": ""})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Fee invariants (no HTTP required — pure constant assertions)
# ---------------------------------------------------------------------------


def test_fee_rate_is_25_bps():
    assert PROTOCOL_FEE_RATE == 0.0025


def test_fee_split_sums_to_unity():
    assert abs(FEE_SPLIT_CREATOR_RATE + FEE_SPLIT_BOUNTY_RATE - 1.0) < 1e-9


def test_fee_split_values():
    assert FEE_SPLIT_CREATOR_RATE == 0.80
    assert FEE_SPLIT_BOUNTY_RATE == 0.20


def test_fee_math_roundtrip():
    gross = 1000.0
    fee = round(gross * PROTOCOL_FEE_RATE, 8)
    creator = round(fee * FEE_SPLIT_CREATOR_RATE, 8)
    bounty = round(fee * FEE_SPLIT_BOUNTY_RATE, 8)
    assert round(creator + bounty, 8) == fee
    assert round(gross - fee, 8) == round(gross * (1 - PROTOCOL_FEE_RATE), 8)


# ---------------------------------------------------------------------------
# Ethereum address validator (schema-level, no HTTP required)
# ---------------------------------------------------------------------------


def test_wallet_address_validator_rejects_plaintext():
    with pytest.raises(ValidationError) as exc_info:
        DepositRequest(agent_id="x", wallet_address="not-a-wallet", amount_usdc=10.0)
    assert any(e["loc"] == ("wallet_address",) for e in exc_info.value.errors())


def test_wallet_address_validator_rejects_truncated_hex():
    with pytest.raises(ValidationError):
        SwapRequest(
            agent_id="x",
            wallet_address="0x1234",
            from_token="USDC",
            to_token="HBAR",
            amount=1.0,
        )


def test_wallet_address_validator_rejects_missing_0x_prefix():
    with pytest.raises(ValidationError):
        DepositRequest(
            agent_id="x",
            wallet_address="abcdef1234567890abcdef1234567890abcdef12",
            amount_usdc=10.0,
        )


def test_wallet_address_validator_accepts_valid_mixed_case():
    req = DepositRequest(
        agent_id="x",
        wallet_address="0xAbCd1234567890AbCd1234567890AbCd12345678",
        amount_usdc=10.0,
    )
    assert req.wallet_address == "0xAbCd1234567890AbCd1234567890AbCd12345678"


def test_wallet_address_validator_accepts_all_lowercase():
    req = SwapRequest(
        agent_id="x",
        wallet_address="0x" + "a" * 40,
        from_token="USDC",
        to_token="HBAR",
        amount=1.0,
    )
    assert req.wallet_address.startswith("0x")


# ---------------------------------------------------------------------------
# Authentication boundary tests (unsigned requests must be rejected)
# ---------------------------------------------------------------------------


def test_unsigned_swap_returns_401(client):
    r = client.post(
        "/api/v1/trade/swap",
        json={
            "agent_id": "any",
            "wallet_address": "0x" + "a" * 40,
            "from_token": "USDC",
            "to_token": "HBAR",
            "amount": 1.0,
        },
    )
    assert r.status_code == 401


def test_unsigned_deposit_returns_401(client):
    r = client.post(
        "/api/v1/bank/deposit",
        json={
            "agent_id": "any",
            "wallet_address": "0x" + "a" * 40,
            "amount_usdc": 1.0,
        },
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Signed deposit — fee split accuracy
# ---------------------------------------------------------------------------


def test_signed_deposit_fee_split(client, registered_wallet, sign_body):
    amount = 1000.0
    body = {
        "agent_id": registered_wallet["agent_id"],
        "wallet_address": registered_wallet["wallet_address"],
        "amount_usdc": amount,
    }
    sig = sign_body(registered_wallet["private_key"], body)
    r = client.post(
        "/api/v1/bank/deposit",
        json=body,
        headers={"X-VectraFi-Signature": sig},
    )
    assert r.status_code == 200
    data = r.json()

    expected_fee = round(amount * PROTOCOL_FEE_RATE, 8)
    assert data["protocol_fee_usdc"] == expected_fee
    assert data["creator_fee_usdc"] == round(expected_fee * FEE_SPLIT_CREATOR_RATE, 8)
    assert data["bounty_pool_fee_usdc"] == round(expected_fee * FEE_SPLIT_BOUNTY_RATE, 8)
    assert data["net_deposited_usdc"] == round(amount - expected_fee, 8)
    assert data["execution_mode"] == "sandbox"
