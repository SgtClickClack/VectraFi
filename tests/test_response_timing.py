import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core-exchange" / "src"))

from main import app


def test_response_time_header_is_added_to_requests():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert "X-Response-Time-Ms" in response.headers
    assert float(response.headers["X-Response-Time-Ms"]) >= 0