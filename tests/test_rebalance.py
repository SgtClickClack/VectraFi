import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_rebalance_returns_200():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.5, "HBAR": 0.5},
        },
    )
    assert response.status_code == 200


def test_rebalance_has_required_fields():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.6, "HBAR": 0.4},
        },
    )
    data = response.json()
    assert "agent_id" in data
    assert "wallet_address" in data
    assert "target_allocations" in data
    assert "current_balances" in data
    assert "target_balances" in data
    assert "deltas" in data
    assert "total_portfolio_value_usdc" in data
    assert "execution_mode" in data
    assert "requires_signature" in data


def test_rebalance_allocation_sums_to_one():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.3, "HBAR": 0.3, "ETH": 0.4},
        },
    )
    assert response.status_code == 200
    data = response.json()
    total = sum(data["target_allocations"].values())
    assert abs(total - 1.0) < 1e-6


def test_rebalance_rejects_invalid_allocation_sum():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.6, "HBAR": 0.6},
        },
    )
    assert response.status_code == 422


def test_rebalance_rejects_invalid_wallet():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "invalid",
            "target_allocations": {"USDC": 0.5, "HBAR": 0.5},
        },
    )
    assert response.status_code == 422


def test_rebalance_rejects_empty_allocations():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {},
        },
    )
    assert response.status_code == 422


def test_rebalance_deltas_have_correct_actions():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.5, "HBAR": 0.5},
        },
    )
    data = response.json()
    for delta in data["deltas"]:
        if delta["action"] == "hold":
            assert abs(delta["delta"]) < 1e-10
        elif delta["action"] == "buy":
            assert delta["delta"] > 0
        elif delta["action"] == "sell":
            assert delta["delta"] < 0


def test_rebalance_requires_signature():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.5, "HBAR": 0.5},
        },
    )
    data = response.json()
    assert data["requires_signature"] is True


def test_rebalance_total_value_is_sum_of_balances():
    response = client.post(
        "/api/v1/agent/rebalance",
        json={
            "agent_id": "test-agent",
            "wallet_address": "0x" + "a" * 40,
            "target_allocations": {"USDC": 0.5, "HBAR": 0.5},
        },
    )
    data = response.json()
    total = sum(data["current_balances"].values())
    assert abs(data["total_portfolio_value_usdc"] - total) < 1e-6
