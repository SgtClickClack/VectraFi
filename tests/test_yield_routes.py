import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_yield_routes_returns_200():
    response = client.get("/api/v1/yield/routes")
    assert response.status_code == 200


def test_yield_routes_has_required_fields():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    assert "routes" in data
    assert "source" in data
    assert isinstance(data["routes"], list)


def test_yield_routes_source_is_sandbox():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    assert data["source"] == "sandbox"


def test_yield_routes_has_at_least_one_route():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    assert len(data["routes"]) >= 1


def test_yield_route_has_required_fields():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    for route in data["routes"]:
        assert "provider_name" in route
        assert "pool_identifier" in route
        assert "base_apy" in route
        assert "gas_estimate_wei" in route


def test_yield_route_provider_names():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    providers = [r["provider_name"] for r in data["routes"]]
    assert "Aave V3" in providers
    assert "Compound V3" in providers


def test_yield_route_apy_is_positive():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    for route in data["routes"]:
        assert route["base_apy"] > 0


def test_yield_route_gas_is_positive():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    for route in data["routes"]:
        assert route["gas_estimate_wei"] > 0


def test_yield_route_pool_identifier_format():
    response = client.get("/api/v1/yield/routes")
    data = response.json()
    for route in data["routes"]:
        assert "-" in route["pool_identifier"]
