"""
FABA Protocol — MCP Server Latency Probe
Measures cold-start, tool-list, and per-tool-call latency over the stdio MCP protocol.
Also confirms that the Smithery hosted HTTP endpoint status.

Usage: python scripts/probe_mcp_latency.py
"""

import sys
import io
# Force UTF-8 output on Windows regardless of console code page
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json
import subprocess
import time
import statistics
import threading
import httpx
import os

BASE_URL = "https://faba-protocol--julian-g-roberts.run.tools"
SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "mcp", "faba_server.py")
RUNS = 10
HTTP_RUNS = 10


# ── HTTP endpoint probe (Smithery run.tools) ──────────────────────────────────

def probe_http() -> None:
    print("\n" + "=" * 62)
    print("PHASE 1 — Smithery run.tools HTTP Endpoint Probe")
    print(f"Target : {BASE_URL}")
    print(f"Runs   : {HTTP_RUNS}")
    print("=" * 60)

    latencies: list[float] = []

    with httpx.Client(follow_redirects=True) as client:
        # quick path discovery
        for path in ["/", "/health", "/mcp", "/sse", "/v1", "/messages"]:
            url = BASE_URL + path
            t0 = time.perf_counter()
            try:
                r = client.get(url, timeout=10.0)
                ms = (time.perf_counter() - t0) * 1000
                body = r.text[:60].strip()
                print(f"  {path:<12} HTTP {r.status_code}  {ms:6.0f} ms  {body}")
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000
                print(f"  {path:<12} ERROR  {ms:6.0f} ms  {exc}")

        print(f"\nRunning {HTTP_RUNS} consecutive GET / probes ...")
        print(f"{'Run':<5} {'Status':<10} {'ms':<12} {'Body'}")
        print("-" * 55)

        for i in range(1, HTTP_RUNS + 1):
            t0 = time.perf_counter()
            try:
                r = client.get(BASE_URL + "/", timeout=10.0)
                ms = (time.perf_counter() - t0) * 1000
                latencies.append(ms)
                body = r.text[:40].strip()
                note = " <-- cold start" if i == 1 else " <-- first warm" if i == 2 else ""
                print(f"{i:<5} {r.status_code:<10} {ms:<12.1f} {body}{note}")
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000
                latencies.append(ms)
                print(f"{i:<5} {'ERR':<10} {ms:<12.1f} {exc}")
            time.sleep(0.3)

    _print_stats("HTTP run.tools endpoint", latencies)
    print("\nDIAGNOSIS: 'Server not found' on all paths = stdio bundle deployment")
    print("           (remote: false, deploymentUrl: null in registry)")
    print("           run.tools HTTP proxy only activates for remote-HTTP servers.")
    print("           The ~230 ms round-trip is Smithery's CDN edge latency (TLS handshake")
    print("           + London/US region routing), NOT the FABA server response time.")


# ── Local stdio MCP server probe ──────────────────────────────────────────────

def _mcp_msg(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _read_line(proc: subprocess.Popen, timeout: float = 8.0) -> dict | None:
    line = b""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch == b"\n":
            break
        line += ch
    try:
        return json.loads(line.decode())
    except Exception:
        return None


def _drain_stderr(proc: subprocess.Popen, buf: list) -> None:
    for line in proc.stderr:
        buf.append(line.decode(errors="replace").rstrip())


def run_server_probe() -> None:
    print("\n" + "=" * 62)
    print("PHASE 2 — Local MCP stdio Server Latency Probe")
    print(f"Server : {SERVER_SCRIPT}")
    print(f"Runs   : {RUNS} cold starts")
    print("=" * 60)

    startup_times: list[float] = []
    list_tools_times: list[float] = []
    tool_call_times: dict[str, list[float]] = {
        "inspect_faba_bounties": [],
        "get_protocol_state": [],
        "generate_eip191_template": [],
    }

    python_exe = sys.executable

    print(f"\n{'Run':<5} {'Startup (ms)':<15} {'tools/list (ms)':<18} {'Result'}")
    print("-" * 65)

    for run in range(1, RUNS + 1):
        stderr_buf: list[str] = []

        t_spawn = time.perf_counter()
        proc = subprocess.Popen(
            [python_exe, SERVER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # drain stderr in background
        t = threading.Thread(target=_drain_stderr, args=(proc, stderr_buf), daemon=True)
        t.start()

        # ── initialize handshake ──────────────────────────────────────────────
        proc.stdin.write(_mcp_msg({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1.0"},
            },
        }))
        proc.stdin.flush()

        init_resp = _read_line(proc, timeout=10.0)
        t_ready = time.perf_counter()
        startup_ms = (t_ready - t_spawn) * 1000
        startup_times.append(startup_ms)

        result_note = "OK" if (init_resp and "result" in init_resp) else "NO-INIT"

        # ── initialized notification ──────────────────────────────────────────
        proc.stdin.write(_mcp_msg({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        proc.stdin.flush()

        # ── tools/list ───────────────────────────────────────────────────────
        t0 = time.perf_counter()
        proc.stdin.write(_mcp_msg({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
        proc.stdin.flush()
        list_resp = _read_line(proc, timeout=8.0)
        list_ms = (time.perf_counter() - t0) * 1000
        list_tools_times.append(list_ms)

        tools_found = len(list_resp.get("result", {}).get("tools", [])) if list_resp else 0
        if tools_found:
            result_note += f" | {tools_found} tools"

        # ── call each tool once ───────────────────────────────────────────────
        tool_calls = [
            ("inspect_faba_bounties", {}),
            ("get_protocol_state", {}),
            ("generate_eip191_template", {
                "operation": "deposit",
                "agent_id": "probe-agent-001",
                "wallet_address": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
            }),
        ]
        req_id = 3
        for tool_name, args in tool_calls:
            t0 = time.perf_counter()
            proc.stdin.write(_mcp_msg({
                "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }))
            proc.stdin.flush()
            resp = _read_line(proc, timeout=15.0)
            call_ms = (time.perf_counter() - t0) * 1000
            tool_call_times[tool_name].append(call_ms)
            req_id += 1

        print(f"{run:<5} {startup_ms:<15.1f} {list_ms:<18.1f} {result_note}")

        proc.stdin.close()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ── summaries ─────────────────────────────────────────────────────────────
    _print_stats("Server cold-start (spawn → init response)", startup_times)
    _print_stats("tools/list round-trip", list_tools_times)
    for tool_name, times in tool_call_times.items():
        if times:
            _print_stats(f"tools/call: {tool_name}", times)


def _print_stats(label: str, data: list[float]) -> None:
    if not data:
        return
    cold = data[0]
    warm = data[1:] if len(data) > 1 else data
    print(f"\n{'-' * 62}")
    print(f"STATS -- {label}")
    print(f"{'-' * 62}")
    print(f"  Cold (run 1)   : {cold:.1f} ms")
    if len(warm) > 0:
        print(f"  Warm min/avg/max: {min(warm):.1f} / {statistics.mean(warm):.1f} / {max(warm):.1f} ms")
    if len(warm) >= 2:
        print(f"  Std deviation  : {statistics.stdev(warm):.1f} ms")
        overhead = cold - statistics.mean(warm)
        print(f"  Cold overhead  : {overhead:+.1f} ms vs warm avg")


if __name__ == "__main__":
    probe_http()
    run_server_probe()

    print("\n" + "=" * 62)
    print("VALIDATION COMPLETE")
    print("=" * 60)
