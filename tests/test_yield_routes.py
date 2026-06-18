import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core-exchange" / "src"))

from main import app
from routes import yield_routes


def test_sandbox_yield_routes_response_structure(monkeypatch):
    monkeypatch.setattr(yield_routes, "is_live_mode", lambda: False)

    response = TestClient(app).get("/api/v1/yield/routes")

    assert response.status_code == 200
    routes = response.json()
    assert len(routes) >= 1

    for route in routes:
        assert set(route) == {
            "provider_name",
            "pool_identifier",
            "base_apy",
            "gas_estimate_wei",
        }
        assert route["provider_name"] == "local_sandbox"
        assert route["pool_identifier"]
        assert route["base_apy"] >= 0
        assert route["gas_estimate_wei"] == 0
