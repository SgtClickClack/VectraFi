import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "core-exchange" / "src"
sys.path.insert(0, str(SRC))

from config import PROTOCOL_DOMAIN  # noqa: E402
from main import app  # noqa: E402


def _create_wallet(client: TestClient) -> dict:
    agent_id = f"fee-test-{uuid4().hex}"
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


def _signed_deposit(client: TestClient, wallet: dict, amount_usdc: float):
    body, signature = _signed_body(
        {
            "agent_id": wallet["agent_id"],
            "wallet_address": wallet["wallet_address"],
            "nonce": uuid4().hex,
            "issued_at": int(time.time()),
            "chain_id": PROTOCOL_DOMAIN,
            "amount_usdc": amount_usdc,
        },
        wallet["private_key"],
    )
    return client.post(
        "/api/v1/bank/deposit",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-VectraFi-Signature": signature,
        },
    )


def test_deposit_routes_protocol_fee_to_creator_and_bounty_pool():
    with TestClient(app) as client:
        wallet = _create_wallet(client)

        response = _signed_deposit(client, wallet, amount_usdc=100.0)

    assert response.status_code == 200
    data = response.json()
    assert data["amount_deposited"] == 100.0
    assert data["protocol_fee_usdc"] == 0.25
    assert data["creator_fee_usdc"] == 0.2
    assert data["bounty_pool_fee_usdc"] == 0.05
    assert data["net_deposited_usdc"] == 99.75
    assert data["balance_usdc"] == 900.0
    assert data["staked_yield_balance"] == 99.75
    assert data["treasury_accumulated_fees_usdc"] >= 0.2
    assert data["bounty_pool_accumulated_fees_usdc"] >= 0.05
    assert data["execution_mode"] == "sandbox"


def test_deposit_rejects_zero_amount_without_changing_balances():
    with TestClient(app) as client:
        wallet = _create_wallet(client)

        response = _signed_deposit(client, wallet, amount_usdc=0)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(error["loc"] == ["amount_usdc"] for error in errors)
