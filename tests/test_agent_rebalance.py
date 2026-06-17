import json
import sys
from pathlib import Path
from uuid import uuid4

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "core-exchange" / "src"
sys.path.insert(0, str(SRC))

from main import app  # noqa: E402


def _create_wallet(client: TestClient) -> dict:
    agent_id = f"rebalance-test-{uuid4().hex}"
    response = client.post("/api/v1/wallet/create", json={"agent_id": agent_id})
    assert response.status_code == 200
    return response.json()


def _signed_body(payload: dict, private_key: str) -> tuple[str, str]:
    body = json.dumps(payload, separators=(",", ":"))
    signature = Account.sign_message(
        encode_defunct(text=body),
        private_key=private_key,
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = f"0x{signature}"
    return body, signature


def _signed_rebalance(client: TestClient, wallet: dict, target_allocations: dict[str, float]):
    body, signature = _signed_body(
        {
            "agent_id": wallet["agent_id"],
            "wallet_address": wallet["wallet_address"],
            "target_allocations": target_allocations,
        },
        wallet["private_key"],
    )
    return client.post(
        "/api/v1/agent/rebalance",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-VectraFi-Signature": signature,
        },
    )


def test_rebalance_executes_signed_swap_to_target_allocations():
    with TestClient(app) as client:
        wallet = _create_wallet(client)

        response = _signed_rebalance(client, wallet, {"USDC": 0.6, "HBAR": 0.4})

    assert response.status_code == 200
    data = response.json()
    assert data["already_balanced"] is False
    assert data["swaps"] == [
        {
            "from_token": "USDC",
            "to_token": "HBAR",
            "amount_in": 400.0,
            "amount_out": 2222.22222222,
            "execution_price": 0.18,
        }
    ]
    assert data["balance_usdc"] == 600.0
    assert data["balance_hbar"] == 2222.22222222
    assert data["current_allocations_after"] == {"USDC": 0.6, "HBAR": 0.4}
    assert data["execution_mode"] == "sandbox"


def test_rebalance_returns_noop_when_portfolio_is_already_balanced():
    with TestClient(app) as client:
        wallet = _create_wallet(client)

        response = _signed_rebalance(client, wallet, {"USDC": 1.0})

    assert response.status_code == 200
    data = response.json()
    assert data["already_balanced"] is True
    assert data["swaps"] == []
    assert data["balance_usdc"] == 1000.0
    assert data["balance_hbar"] == 0.0
    assert data["current_allocations_after"] == {"USDC": 1.0, "HBAR": 0.0}


def test_rebalance_requires_signature_header():
    with TestClient(app) as client:
        wallet = _create_wallet(client)
        body = json.dumps(
            {
                "agent_id": wallet["agent_id"],
                "wallet_address": wallet["wallet_address"],
                "target_allocations": {"USDC": 1.0},
            },
            separators=(",", ":"),
        )

        response = client.post(
            "/api/v1/agent/rebalance",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401


def test_rebalance_rejects_allocations_that_do_not_sum_to_one():
    with TestClient(app) as client:
        wallet = _create_wallet(client)

        response = _signed_rebalance(client, wallet, {"USDC": 0.6, "HBAR": 0.5})

    assert response.status_code == 422
    assert "target_allocations must sum to exactly 1.0" in str(response.json()["detail"])
