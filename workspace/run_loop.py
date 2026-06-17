#!/usr/bin/env python3
"""
VectraFi Agent Zero — Execution Runner
=======================================
Isolated execution heartbeat for Agent Zero.

Permissions:
  - READ-ONLY:  core-exchange/ (via MCP endpoint inspection only)
  - READ-WRITE: workspace/ (this directory tree)

Artifact lifecycle:
  workspace/drafts/      → in-progress agent artifacts
  workspace/validated/   → artifacts that passed the validation gate
  workspace/extensions/  → registry of approved (human-reviewed) extensions
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution & sandbox enforcement
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = Path(__file__).resolve().parent
_AGENT_DIR = _WORKSPACE / "agents" / "agent-zero"
_DRAFTS_DIR = _WORKSPACE / "drafts"
_VALIDATED_DIR = _WORKSPACE / "validated"
_EXTENSIONS_DIR = _WORKSPACE / "extensions"
_REGISTRY_FILE = _EXTENSIONS_DIR / "registry.json"
_COST_LOG = _AGENT_DIR / "cost_log.jsonl"

_ALLOWED_WRITE_ROOT = _WORKSPACE.resolve()
_CORE_EXCHANGE = (_REPO_ROOT / "core-exchange").resolve()
_MCP_SERVER = (_REPO_ROOT / "mcp" / "faba_server.py").resolve()

_MCP_READ_ENDPOINTS = [
    "get_bounties",
    "get_protocol_state",
    "inspect_route",
]


def _enforce_sandbox(path: Path) -> Path:
    """Raise if path escapes the workspace sandbox."""
    resolved = path.resolve()
    if not str(resolved).startswith(str(_ALLOWED_WRITE_ROOT)):
        raise PermissionError(
            f"Write denied: {resolved} is outside sandbox {_ALLOWED_WRITE_ROOT}"
        )
    return resolved


def _bootstrap_dirs() -> None:
    for d in [_DRAFTS_DIR, _VALIDATED_DIR, _EXTENSIONS_DIR, _AGENT_DIR]:
        _enforce_sandbox(d)
        d.mkdir(parents=True, exist_ok=True)
    if not _REGISTRY_FILE.exists():
        _REGISTRY_FILE.write_text(json.dumps({"extensions": []}, indent=2))


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------

def _log_cost(event: str, tokens: int = 0, elapsed_ms: float = 0.0) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "tokens": tokens,
        "elapsed_ms": round(elapsed_ms, 2),
    }
    with _COST_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# MCP state inspection (read-only)
# ---------------------------------------------------------------------------

def inspect_protocol_state() -> dict:
    """
    Runs the MCP server in --inspect mode to read protocol state.
    No writes. Returns a dict with available tool names and any cached state.
    """
    available_tools = _MCP_READ_ENDPOINTS.copy()
    mcp_reachable = _MCP_SERVER.exists()
    return {
        "mcp_server": str(_MCP_SERVER),
        "reachable": mcp_reachable,
        "read_tools": available_tools,
        "note": "Call faba_server.py directly via subprocess for live tool responses.",
    }


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------

def validate_artifact(artifact_path: Path) -> tuple[bool, str]:
    """
    Runs a validation suite against a draft artifact before it can advance
    to workspace/validated/.

    Checks:
      1. File is within workspace sandbox.
      2. File is valid JSON or Python (basic parse check).
      3. File does not reference core-exchange paths as write targets.
      4. Optional: runs pytest on any *_test.py companion file.
    """
    try:
        _enforce_sandbox(artifact_path)
    except PermissionError as e:
        return False, f"Sandbox violation: {e}"

    if not artifact_path.exists():
        return False, f"Artifact not found: {artifact_path}"

    content = artifact_path.read_text(encoding="utf-8")

    # Guard: no path operations targeting core-exchange (open/write/Path calls)
    import re as _re
    _CORE_PAT = _re.compile(
        r'(open|write_text|write_bytes|Path|os\.path)\s*\([^)]*core.exchange',
        _re.IGNORECASE,
    )
    if _CORE_PAT.search(content) or str(_CORE_EXCHANGE) in content:
        return False, "Artifact targets core-exchange in a file-write operation"

    # Basic syntax check
    suffix = artifact_path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {e}"
    elif suffix == ".py":
        try:
            compile(content, str(artifact_path), "exec")
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

    # Run companion test file if present
    test_file = artifact_path.parent / f"{artifact_path.stem}_test.py"
    if test_file.exists():
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-q", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return False, f"Tests failed:\n{result.stdout}\n{result.stderr}"

    return True, "ok"


def promote_artifact(artifact_path: Path) -> Path:
    """Moves a validated artifact from drafts/ to validated/."""
    _enforce_sandbox(artifact_path)
    dest = _VALIDATED_DIR / artifact_path.name
    _enforce_sandbox(dest)
    artifact_path.rename(dest)
    return dest


def register_extension(artifact_path: Path, meta: dict) -> None:
    """
    Appends a validated artifact entry to the extensions registry.
    Registry entries are proposals only — not live in core-exchange.
    """
    _enforce_sandbox(artifact_path)
    registry = json.loads(_REGISTRY_FILE.read_text())
    registry["extensions"].append({
        "name": artifact_path.stem,
        "path": str(artifact_path.relative_to(_WORKSPACE)),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_human_review",
        **meta,
    })
    _REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------

def run_loop(max_iterations: int = 0, interval_s: float = 5.0) -> None:
    """
    Main heartbeat loop. Scans workspace/drafts/ each tick, validates any
    new artifacts, and promotes passing ones to workspace/validated/.

    Args:
        max_iterations: 0 = run indefinitely.
        interval_s:     seconds between ticks.
    """
    _bootstrap_dirs()
    _log_cost("loop_start")
    print(f"[agent-zero] Execution runner started — sandbox: {_WORKSPACE}")
    print(f"[agent-zero] MCP state: {inspect_protocol_state()}")

    iteration = 0
    try:
        while True:
            tick_start = time.monotonic()
            iteration += 1

            candidates = [
                p for p in _DRAFTS_DIR.iterdir()
                if p.is_file() and not p.name.endswith("_test.py")
            ]
            if candidates:
                print(f"[tick {iteration}] Found {len(candidates)} draft(s)")
            for draft in candidates:
                t0 = time.monotonic()
                passed, reason = validate_artifact(draft)
                elapsed = (time.monotonic() - t0) * 1000
                _log_cost(f"validate:{draft.name}", elapsed_ms=elapsed)

                if passed:
                    validated = promote_artifact(draft)
                    register_extension(validated, {"validated_reason": reason})
                    print(f"[tick {iteration}] PROMOTED {draft.name} -> validated/")
                else:
                    print(f"[tick {iteration}] REJECTED {draft.name}: {reason}")

            tick_elapsed = (time.monotonic() - tick_start) * 1000
            _log_cost("tick", elapsed_ms=tick_elapsed)

            if max_iterations and iteration >= max_iterations:
                break

            time.sleep(interval_s)

    except KeyboardInterrupt:
        print("\n[agent-zero] Loop interrupted.")
    finally:
        _log_cost("loop_stop")
        print("[agent-zero] Runner stopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VectraFi Agent Zero execution runner")
    parser.add_argument("--iterations", type=int, default=0,
                        help="Max loop iterations (0 = infinite)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Seconds between ticks")
    args = parser.parse_args()

    run_loop(max_iterations=args.iterations, interval_s=args.interval)
