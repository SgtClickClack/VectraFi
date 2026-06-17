from fastapi.testclient import TestClient


def test_health_returns_response_time_header(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "X-Response-Time-Ms" in r.headers
    elapsed = float(r.headers["X-Response-Time-Ms"])
    assert elapsed >= 0.0


def test_response_time_is_numeric(client):
    r = client.get("/health")
    header = r.headers["X-Response-Time-Ms"]
    float(header)
