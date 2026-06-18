"""
Unit tests for the swarm process controller.

All tests mock asyncio.create_subprocess_exec so no real subprocess is
spawned and the suite runs offline without the exchange server.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# conftest.py already inserts core-exchange/src into sys.path and wires
# the TestClient + in-memory SQLite, so we only need to import what's new.
import routes.swarm_control as _ctrl
from routes.swarm_control import router


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_mock_proc(pid: int = 12345, returncode: int | None = None) -> MagicMock:
    """Return a mock that looks like an asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    # wait() is a coroutine
    proc.wait = AsyncMock(return_value=0)
    return proc


def _reset_state() -> None:
    """Clear module-level process state between tests."""
    _ctrl._proc = None
    _ctrl._proc_dry_run = False
    _ctrl._proc_start_time = None


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    """Guarantee a clean slate before and after every test."""
    _reset_state()
    yield
    _reset_state()


@pytest.fixture()
def client(client):  # noqa: F811 — shadows conftest fixture intentionally
    # Bypass the SWARM_OPERATOR_KEY gate so tests run without the env var set.
    from main import app
    from routes.swarm_control import _require_operator
    app.dependency_overrides[_require_operator] = lambda: None
    yield client
    app.dependency_overrides.pop(_require_operator, None)


# ── Status endpoint ────────────────────────────────────────────────────────

def test_status_idle(client):
    r = client.get("/api/v1/swarm/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is False
    assert data["pid"] is None
    assert data["uptime_seconds"] is None
    assert data["dry_run"] is False


def test_status_running(client):
    mock_proc = _make_mock_proc(pid=9999)
    _ctrl._proc = mock_proc
    _ctrl._proc_dry_run = True
    import time
    _ctrl._proc_start_time = time.perf_counter() - 10.0

    r = client.get("/api/v1/swarm/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is True
    assert data["pid"] == 9999
    assert data["dry_run"] is True
    assert data["uptime_seconds"] is not None
    assert data["uptime_seconds"] >= 10.0


# ── Start endpoint ─────────────────────────────────────────────────────────

def test_start_spawns_process(client):
    mock_proc = _make_mock_proc(pid=1111)

    async def _fake_exec(*args, **kwargs):
        return mock_proc

    with patch("routes.swarm_control.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        r = client.post("/api/v1/swarm/start", json={})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "started"
    assert data["pid"] == 1111
    assert data["dry_run"] is False
    assert _ctrl._proc is mock_proc


def test_start_with_dry_run_flag(client):
    mock_proc = _make_mock_proc(pid=2222)

    async def _fake_exec(*args, **kwargs):
        # Verify the env var was set
        assert kwargs.get("env", {}).get("SWARM_DRY_RUN") == "1"
        return mock_proc

    with patch("routes.swarm_control.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        r = client.post("/api/v1/swarm/start", json={"dry_run": True})

    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert _ctrl._proc_dry_run is True


def test_start_duplicate_returns_409(client):
    mock_proc = _make_mock_proc(pid=3333)
    _ctrl._proc = mock_proc  # simulate already-running

    r = client.post("/api/v1/swarm/start", json={})
    assert r.status_code == 409
    assert "already running" in r.json()["detail"].lower()


def test_start_spawn_failure_returns_500(client):
    async def _boom(*args, **kwargs):
        raise OSError("no such file")

    with patch("routes.swarm_control.asyncio.create_subprocess_exec", side_effect=_boom):
        r = client.post("/api/v1/swarm/start", json={})

    assert r.status_code == 500
    assert _ctrl._proc is None


# ── Stop endpoint ──────────────────────────────────────────────────────────

def test_stop_no_process_returns_404(client):
    r = client.post("/api/v1/swarm/stop")
    assert r.status_code == 404
    assert "no swarm" in r.json()["detail"].lower()


def test_stop_terminates_process(client):
    mock_proc = _make_mock_proc(pid=4444)
    _ctrl._proc = mock_proc

    r = client.post("/api/v1/swarm/stop")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "stopped"
    assert data["pid"] == 4444

    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_awaited()
    # State must be cleaned up
    assert _ctrl._proc is None
    assert _ctrl._proc_dry_run is False
    assert _ctrl._proc_start_time is None


def test_stop_kills_if_terminate_hangs(client):
    """If wait() times out after terminate(), kill() is called as fallback."""
    mock_proc = _make_mock_proc(pid=5555)
    _ctrl._proc = mock_proc

    call_count = 0

    async def _slow_wait():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # first await (after terminate) — simulate timeout
            raise asyncio.TimeoutError
        return 0  # second await (after kill)

    mock_proc.wait = _slow_wait

    r = client.post("/api/v1/swarm/stop")
    assert r.status_code == 200
    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()
    assert _ctrl._proc is None


# ── Start → Stop lifecycle ─────────────────────────────────────────────────

def test_start_stop_start_cycle(client):
    """Verify that stopping clears state so a second start succeeds."""
    mock_proc_a = _make_mock_proc(pid=6001)
    mock_proc_b = _make_mock_proc(pid=6002)
    _call = {"n": 0}

    async def _fake_exec(*args, **kwargs):
        _call["n"] += 1
        return mock_proc_a if _call["n"] == 1 else mock_proc_b

    with patch("routes.swarm_control.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        r1 = client.post("/api/v1/swarm/start", json={})
    assert r1.status_code == 200
    assert r1.json()["pid"] == 6001

    r2 = client.post("/api/v1/swarm/stop")
    assert r2.status_code == 200

    with patch("routes.swarm_control.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        r3 = client.post("/api/v1/swarm/start", json={})
    assert r3.status_code == 200
    assert r3.json()["pid"] == 6002
