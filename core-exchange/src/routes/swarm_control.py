"""
Swarm process controller — programmatic start / stop / status for seed_swarm.py.

POST /api/v1/swarm/start   — spawn seed_swarm.py as a background subprocess
POST /api/v1/swarm/stop    — terminate the running process (SIGTERM → SIGKILL)
GET  /api/v1/swarm/status  — return liveness, PID, and active runtime flags
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger("vectrafi.swarm_control")

router = APIRouter(prefix="/api/v1/swarm", tags=["swarm-control"])

# G-1 fix: require a secret operator key to access all swarm control endpoints.
# Set SWARM_OPERATOR_KEY in environment to enable; leave unset to disable entirely.
_OPERATOR_KEY: str = os.getenv("SWARM_OPERATOR_KEY", "").strip()


def _require_operator(x_operator_key: str | None = Header(default=None)) -> None:
    """Dependency that gates all swarm endpoints behind a shared operator secret."""
    if not _OPERATOR_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Swarm control is disabled — set the SWARM_OPERATOR_KEY "
                "environment variable to enable this interface"
            ),
        )
    if x_operator_key != _OPERATOR_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Operator-Key header",
        )


# ── Path to the swarm script (sibling of the routes/ package)
_SWARM_SCRIPT = Path(__file__).resolve().parent.parent / "seed_swarm.py"

# ── Module-level process state
_proc: Optional[asyncio.subprocess.Process] = None
_proc_dry_run: bool = False
_proc_start_time: Optional[float] = None


# ── Request / response schemas ─────────────────────────────────────────────

class SwarmStartRequest(BaseModel):
    dry_run: bool = False


class SwarmStartResponse(BaseModel):
    status: str
    pid: int
    dry_run: bool


class SwarmStopResponse(BaseModel):
    status: str
    pid: Optional[int] = None


class SwarmStatusResponse(BaseModel):
    running: bool
    pid: Optional[int] = None
    dry_run: bool
    uptime_seconds: Optional[float] = None


# ── Helpers ────────────────────────────────────────────────────────────────

def _is_alive() -> bool:
    return _proc is not None and _proc.returncode is None


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/start", response_model=SwarmStartResponse, status_code=200, dependencies=[Depends(_require_operator)])
async def start_swarm(body: SwarmStartRequest = SwarmStartRequest()) -> SwarmStartResponse:
    global _proc, _proc_dry_run, _proc_start_time

    if _is_alive():
        raise HTTPException(
            status_code=409,
            detail=f"Swarm is already running (PID {_proc.pid}). Stop it first.",
        )

    import os
    env = os.environ.copy()
    env["SWARM_DRY_RUN"] = "1" if body.dry_run else "0"

    try:
        _proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_SWARM_SCRIPT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        logger.error("Failed to spawn seed_swarm.py: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to spawn swarm: {exc}")

    _proc_dry_run = body.dry_run
    _proc_start_time = time.perf_counter()
    logger.info("Swarm started — PID=%d  dry_run=%s", _proc.pid, body.dry_run)
    return SwarmStartResponse(status="started", pid=_proc.pid, dry_run=body.dry_run)


@router.post("/stop", response_model=SwarmStopResponse, status_code=200, dependencies=[Depends(_require_operator)])
async def stop_swarm() -> SwarmStopResponse:
    global _proc, _proc_dry_run, _proc_start_time

    if not _is_alive():
        raise HTTPException(status_code=404, detail="No swarm process is currently running.")

    pid = _proc.pid
    try:
        _proc.terminate()
        try:
            await asyncio.wait_for(_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Swarm PID=%d did not exit after SIGTERM — sending SIGKILL", pid)
            _proc.kill()
            await _proc.wait()
    except Exception as exc:
        logger.error("Error while stopping swarm PID=%d: %s", pid, exc)
        raise HTTPException(status_code=500, detail=f"Error stopping swarm: {exc}")
    finally:
        _proc = None
        _proc_dry_run = False
        _proc_start_time = None

    logger.info("Swarm stopped — PID=%d", pid)
    return SwarmStopResponse(status="stopped", pid=pid)


@router.get("/status", response_model=SwarmStatusResponse, dependencies=[Depends(_require_operator)])
def swarm_status() -> SwarmStatusResponse:
    alive = _is_alive()
    uptime = (
        round(time.perf_counter() - _proc_start_time, 1)
        if alive and _proc_start_time is not None
        else None
    )
    return SwarmStatusResponse(
        running=alive,
        pid=_proc.pid if alive else None,
        dry_run=_proc_dry_run,
        uptime_seconds=uptime,
    )
