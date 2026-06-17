#!/usr/bin/env python3
"""
VectraFi FABA MCP Server
========================
Exposes eight protocol tools to external agent runtimes via the Model Context Protocol.

Add to your mcp_config.json:
    {
      "mcpServers": {
        "faba": {
          "command": "python",
          "args": ["mcp/faba_server.py"],
          "cwd": "/absolute/path/to/VectraFi"
        }
      }
    }

Dependencies:
    pip install mcp>=1.0.0 httpx>=0.28.0
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _REPO_ROOT / "core-exchange" / "src" / "vectrafi.db"
_GITHUB_REPO = "SgtClickClack/VectraFi"
_API_BASE = "http://127.0.0.1:8000"
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
_GH_TIMEOUT = 3.0  # hard ceiling: fall back to cache rather than hang

_FALLBACK_ISSUES = [
    {"number": 3, "title": "feat: Implement Protocol Treasury Fee Collector on Deposits", "labels": ["agent-bounty"], "url": f"https://github.com/{_GITHUB_REPO}/issues/3"},
    {"number": 4, "title": "feat: Implement Multi-Route DeFi Yield Aggregator Analytics Endpoint", "labels": ["agent-bounty"], "url": f"https://github.com/{_GITHUB_REPO}/issues/4"},
    {"number": 5, "title": "feat: Implement Autonomous Portfolio Rebalancing Math Engine", "labels": ["agent-bounty"], "url": f"https://github.com/{_GITHUB_REPO}/issues/5"},
    {"number": 2, "title": "feat: Implement X-VectraFi-Signature Validation Middleware", "labels": ["agent-build"], "url": f"https://github.com/{_GITHUB_REPO}/issues/2"},
]

mcp = FastMCP("VectraFi FABA Protocol")


def _gh_list_issues(label: str) -> list[dict]:
    """Use gh CLI only when GITHUB_TOKEN is set — skips entirely otherwise to avoid auth hangs."""
    if not _GITHUB_TOKEN:
        return []
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", _GITHUB_REPO, "--label", label,
             "--state", "open", "--json", "number,title,url,labels"],
            capture_output=True, text=True, timeout=_GH_TIMEOUT,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def _httpx_list_issues() -> list[dict]:
    """Fetch open issues via GitHub REST API with a 3-second hard timeout."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0)) as client:
            resp = client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
                params={"state": "open", "per_page": 50},
                headers=headers,
            )
            resp.raise_for_status()
            target_labels = {"agent-bounty", "agent-build"}
            return [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "labels": [la["name"] for la in i["labels"]],
                    "url": i["html_url"],
                    "created_at": i["created_at"],
                }
                for i in resp.json()
                if any(la["name"] in target_labels for la in i["labels"])
            ]
    except Exception:
        return []


@mcp.tool()
def inspect_faba_bounties() -> str:
    """
    Returns a JSON summary of all open VectraFi FABA bounty and build issues.

    Call this before implementing any feature to:
    1. Verify the task is not already claimed or in-progress.
    2. Identify the exact issue number to reference in your PR description.
    3. Understand which label governs merge eligibility (agent-bounty vs agent-build).

    Labels:
    - agent-bounty: high-priority, explicit acceptance criteria, human review before merge.
    - agent-build: scoped feature, autonomous PR submission without pre-approval.
    """
    # No token → skip all network calls, return cache immediately (avoids 30s hang)
    if not _GITHUB_TOKEN:
        return json.dumps({
            "source": "fallback_cache",
            "warning": (
                "GITHUB_TOKEN env var not set. Returning cached backlog. "
                "Set GITHUB_TOKEN for live issue state."
            ),
            "open_issues": _FALLBACK_ISSUES,
            "count": len(_FALLBACK_ISSUES),
            "claim_url": f"https://github.com/{_GITHUB_REPO}/issues",
        }, indent=2)

    # Token present: try gh CLI first (3s timeout)
    bounties = _gh_list_issues("agent-bounty")
    builds = _gh_list_issues("agent-build")
    if bounties or builds:
        issues = bounties + builds
        return json.dumps({
            "source": "gh_cli",
            "open_issues": issues,
            "count": len(issues),
            "claim_url": f"https://github.com/{_GITHUB_REPO}/issues",
        }, indent=2)

    # Fallback: GitHub REST API (3s timeout)
    issues = _httpx_list_issues()
    if issues:
        return json.dumps({
            "source": "github_api",
            "open_issues": issues,
            "count": len(issues),
            "claim_url": f"https://github.com/{_GITHUB_REPO}/issues",
        }, indent=2)

    # Last resort: hardcoded backlog from llms.txt
    return json.dumps({
        "source": "fallback_cache",
        "warning": "GitHub API unreachable — returning cached backlog. Verify live state before implementing.",
        "open_issues": _FALLBACK_ISSUES,
        "count": len(_FALLBACK_ISSUES),
        "claim_url": f"https://github.com/{_GITHUB_REPO}/issues",
    }, indent=2)


@mcp.tool()
def get_protocol_state() -> str:
    """
    Returns the current VectraFi protocol state from the live SQLite ledger.

    Fields returned:
    - accumulated_creator_fees_usdc: total 80% creator allocation from all deposits.
    - bounty_pool_fees_usdc: total 20% bounty pool from all deposits.
    - registered_agents: number of wallets created via POST /api/v1/wallet/create.
    - fee_split: current PROTOCOL_FEE_RATE and creator/bounty split ratios.

    Note: holding addresses are STANDALONE SANDBOX PLACEHOLDERS during alpha.
    Fees accumulate in SQLite only — no on-chain distribution is active.
    """
    if not _DB_PATH.exists():
        return json.dumps({
            "status": "db_not_initialised",
            "message": (
                f"SQLite ledger not found at {_DB_PATH}. "
                "Start the exchange server once to initialise it: cd core-exchange/src && python run.py"
            ),
            "accumulated_creator_fees_usdc": 0.0,
            "bounty_pool_fees_usdc": 0.0,
            "registered_agents": 0,
        }, indent=2)

    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT accumulated_fees_usdc, bounty_pool_fees_usdc FROM treasury_state WHERE id = 1")
        row = cur.fetchone()
        treasury = dict(row) if row else {"accumulated_fees_usdc": 0.0, "bounty_pool_fees_usdc": 0.0}

        cur.execute("SELECT COUNT(*) AS agent_count FROM agent_wallets")
        agent_count = int(cur.fetchone()["agent_count"])
        conn.close()

        return json.dumps({
            "status": "live",
            "db_path": str(_DB_PATH),
            "accumulated_creator_fees_usdc": treasury["accumulated_fees_usdc"],
            "bounty_pool_fees_usdc": treasury["bounty_pool_fees_usdc"],
            "registered_agents": agent_count,
            "fee_architecture": {
                "protocol_fee_rate": 0.0025,
                "creator_share": 0.80,
                "bounty_pool_share": 0.20,
                "holding_address_notice": (
                    "STANDALONE SANDBOX PLACEHOLDERS — HOLDING_ADDRESS_USER and "
                    "HOLDING_ADDRESS_BOUNTY are effective burn addresses. "
                    "Fees are SQLite-only during alpha."
                ),
            },
        }, indent=2)

    except Exception as exc:
        return json.dumps({"status": "error", "detail": str(exc)}, indent=2)


@mcp.tool()
def generate_eip191_template(
    operation: Annotated[str, "Protocol operation to template: 'swap' or 'deposit'"],
    agent_id: Annotated[str, "Your agent_id — must be registered via POST /api/v1/wallet/create"],
    wallet_address: Annotated[str, "Your 42-char Ethereum wallet address (0x + 40 hex chars)"],
) -> str:
    """
    Generates a pre-structured EIP-191 signing template for a VectraFi protocol operation.

    The returned JSON contains a 'body' dict ready to sign and submit.

    Signing sequence (Python):
        import json
        from eth_account import Account
        from eth_account.messages import encode_defunct

        body_text = json.dumps(body, separators=(',', ':'))  # compact — MUST match HTTP body
        msg       = encode_defunct(text=body_text)
        sig       = Account.sign_message(msg, private_key=YOUR_PRIVATE_KEY)
        header    = {"X-VectraFi-Signature": sig.signature.hex()}
        # POST to endpoint with JSON body and header

    CRITICAL: The string you sign must be byte-for-byte identical to the HTTP request body.
    Use separators=(',', ':') with json.dumps and send with Content-Type: application/json.
    """
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet_address):
        return json.dumps({
            "error": "Invalid wallet_address format.",
            "requirement": "Must be 0x followed by exactly 40 hexadecimal characters (42 chars total).",
            "example": "0xAbCd1234567890AbCd1234567890AbCd12345678",
        }, indent=2)

    if operation == "swap":
        body = {
            "agent_id": agent_id,
            "wallet_address": wallet_address,
            "from_token": "USDC",
            "to_token": "HBAR",
            "amount": 100.0,
        }
        endpoint = f"{_API_BASE}/api/v1/trade/swap"
        field_notes = {
            "from_token": "USDC or HBAR — cannot equal to_token",
            "to_token": "USDC or HBAR — cannot equal from_token",
            "amount": "float > 0, must be <= current balance in from_token",
        }
        notes = [
            "Swaps are permanently fee-free in this protocol version.",
            "Execution price is determined at request time from live Coinbase API with 30s cache.",
            "balance_check occurs pre-swap; 400 returned on insufficient balance.",
        ]

    elif operation == "deposit":
        body = {
            "agent_id": agent_id,
            "wallet_address": wallet_address,
            "amount_usdc": 100.0,
        }
        endpoint = f"{_API_BASE}/api/v1/bank/deposit"
        field_notes = {
            "amount_usdc": "float > 0, must be <= current balance_usdc",
        }
        notes = [
            "0.25% protocol fee is deducted: 80% to creator accumulator, 20% to bounty pool.",
            "net_deposited = amount_usdc * 0.9975 moves from balance_usdc to staked_yield_balance.",
            "HOLDING_ADDRESS_USER and HOLDING_ADDRESS_BOUNTY are STANDALONE SANDBOX PLACEHOLDERS.",
            "response includes treasury_accumulated_fees_usdc and bounty_pool_accumulated_fees_usdc.",
        ]

    else:
        return json.dumps({
            "error": f"Unknown operation '{operation}'.",
            "valid_operations": ["swap", "deposit"],
        }, indent=2)

    signing_code = (
        "body_text = json.dumps(body, separators=(',', ':'))\n"
        "msg = encode_defunct(text=body_text)\n"
        "sig = Account.sign_message(msg, private_key=YOUR_PRIVATE_KEY)\n"
        f"POST {endpoint} body=body headers={{'X-VectraFi-Signature': sig.signature.hex()}}"
    )

    return json.dumps({
        "operation": operation,
        "endpoint": endpoint,
        "method": "POST",
        "body": body,
        "field_notes": field_notes,
        "signing_code_python": signing_code,
        "header_name": "X-VectraFi-Signature",
        "auth_errors": {
            "400": "malformed or missing JSON body",
            "401": "signature missing, mismatch, or wallet not registered to this agent_id",
            "404": "agent_id not registered — call POST /api/v1/wallet/create first",
            "422": "schema validation failure — check field types and wallet_address format",
        },
        "notes": notes,
    }, indent=2)


@mcp.tool()
def build_transfer_payload(
    agent_id: Annotated[str, "Sender agent_id — must be registered via POST /api/v1/wallet/create"],
    wallet_address: Annotated[str, "Sender's 42-char Ethereum address (0x + 40 hex chars)"],
    receiver_id: Annotated[str, "Receiver agent_id — must be a registered agent"],
    amount_usdc: Annotated[float, "Gross transfer amount in USDC (float > 0)"],
    tx_type: Annotated[str, "Transfer label, e.g. 'peer_transfer' or 'bounty_yield_split'"] = "peer_transfer",
) -> str:
    """
    Builds the exact JSON body for POST /api/v1/settlement/transfer.

    Returns a payload dict whose key order matches the Pydantic schema exactly.
    Sign this with EIP-191 using compact JSON (no spaces) — the signature must
    be byte-for-byte identical to what you POST.

    Signing and submission (Python):
        import json
        from eth_account import Account
        from eth_account.messages import encode_defunct

        result  = json.loads(build_transfer_payload(...))
        body    = result["body"]
        body_text = json.dumps(body, separators=(',', ':'))   # compact, no spaces
        msg     = encode_defunct(text=body_text)
        sig     = Account.sign_message(msg, private_key=YOUR_PRIVATE_KEY)
        # POST body (as JSON) with header X-VectraFi-Signature: sig.signature.hex()

    Tax model: 1.5% of amount_usdc is deducted and routed to treasury.
               receiver receives amount_usdc * 0.985 net.
    """
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet_address):
        return json.dumps({"error": "Invalid wallet_address — must be 0x + 40 hex chars"}, indent=2)
    if amount_usdc <= 0:
        return json.dumps({"error": "amount_usdc must be > 0"}, indent=2)
    if agent_id == receiver_id:
        return json.dumps({"error": "agent_id and receiver_id must differ"}, indent=2)

    # Key order matches SettlementTransferRequest field declaration order in schemas.py
    body = {
        "agent_id":       agent_id,
        "wallet_address": wallet_address,
        "receiver_id":    receiver_id,
        "amount_usdc":    amount_usdc,
        "tx_type":        tx_type,
    }
    tax_preview  = round(amount_usdc * 0.015, 8)
    net_preview  = round(amount_usdc - tax_preview, 8)
    endpoint     = f"{_API_BASE}/api/v1/settlement/transfer"

    return json.dumps({
        "endpoint":    endpoint,
        "method":      "POST",
        "body":        body,
        "body_compact": json.dumps(body, separators=(",", ":")),
        "header_name": "X-VectraFi-Signature",
        "tax_preview": {
            "gross_usdc": amount_usdc,
            "tax_usdc":   tax_preview,
            "net_usdc":   net_preview,
        },
        "auth_errors": {
            "401": "missing or invalid X-VectraFi-Signature",
            "400": "insufficient balance or self-transfer",
            "404": "agent_id or receiver_id not registered",
            "422": "schema validation failure",
        },
    }, indent=2)


@mcp.tool()
def build_bounty_claim_payload(
    agent_id: Annotated[str, "Claimant agent_id — must hold the gross bounty balance"],
    wallet_address: Annotated[str, "Claimant's 42-char Ethereum address (0x + 40 hex chars)"],
    counterpart_id: Annotated[str, "Peer agent_id receiving the yield split"],
    bounty_amount_usdc: Annotated[float, "Gross bounty in USDC to be split (float > 0)"],
    counterpart_share_pct: Annotated[float, "Fraction going to counterpart, exclusive (0 < x < 1). E.g. 0.333 for 1/3"],
) -> str:
    """
    Builds the exact JSON body for POST /api/v1/settlement/claim-bounty.

    The claimant must already hold bounty_amount_usdc in their wallet.
    This endpoint transfers counterpart_share_pct * bounty_amount_usdc to the
    counterpart (minus 1.5% micro-tax), and the claimant retains the rest.

    Signing and submission (Python):
        import json
        from eth_account import Account
        from eth_account.messages import encode_defunct

        result  = json.loads(build_bounty_claim_payload(...))
        body    = result["body"]
        body_text = json.dumps(body, separators=(',', ':'))
        msg     = encode_defunct(text=body_text)
        sig     = Account.sign_message(msg, private_key=YOUR_PRIVATE_KEY)
        # POST body (as JSON) with header X-VectraFi-Signature: sig.signature.hex()

    Split preview is included in the response for verification before signing.
    """
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet_address):
        return json.dumps({"error": "Invalid wallet_address — must be 0x + 40 hex chars"}, indent=2)
    if bounty_amount_usdc <= 0:
        return json.dumps({"error": "bounty_amount_usdc must be > 0"}, indent=2)
    if not (0 < counterpart_share_pct < 1):
        return json.dumps({"error": "counterpart_share_pct must be in (0, 1) exclusive"}, indent=2)
    if agent_id == counterpart_id:
        return json.dumps({"error": "agent_id and counterpart_id must differ"}, indent=2)

    # Key order matches BountyClaimRequest field declaration order in schemas.py
    body = {
        "agent_id":              agent_id,
        "wallet_address":        wallet_address,
        "counterpart_id":        counterpart_id,
        "bounty_amount_usdc":    bounty_amount_usdc,
        "counterpart_share_pct": counterpart_share_pct,
    }
    counterpart_gross = round(bounty_amount_usdc * counterpart_share_pct, 8)
    tax               = round(counterpart_gross * 0.015, 8)
    counterpart_net   = round(counterpart_gross - tax, 8)
    claimant_keep     = round(bounty_amount_usdc - counterpart_gross, 8)
    endpoint          = f"{_API_BASE}/api/v1/settlement/claim-bounty"

    return json.dumps({
        "endpoint":    endpoint,
        "method":      "POST",
        "body":        body,
        "body_compact": json.dumps(body, separators=(",", ":")),
        "header_name": "X-VectraFi-Signature",
        "split_preview": {
            "bounty_gross_usdc":    bounty_amount_usdc,
            "claimant_keeps_usdc":  claimant_keep,
            "counterpart_gross_usdc": counterpart_gross,
            "tax_usdc":             tax,
            "counterpart_net_usdc": counterpart_net,
        },
        "auth_errors": {
            "401": "missing or invalid X-VectraFi-Signature",
            "400": "insufficient balance or same-agent claim",
            "404": "agent_id or counterpart_id not registered",
            "422": "schema validation failure",
        },
    }, indent=2)


@mcp.tool()
def get_agent_balance(
    agent_id: Annotated[str, "Agent identifier to look up (e.g. 'agent-zero', 'treasury')"],
) -> str:
    """
    Returns the current sandbox ledger balance for an agent wallet.

    Reads from workspace/bank.db — the sandboxed micro-bank ledger provisioned
    during Phase 2. Balances are integer units (no floating-point).

    This tool is READ-ONLY. Mutating operations (transfers, settlements) require
    X-VectraFi-Signature validation and are routed through core-exchange endpoints,
    not exposed via MCP directly.
    """
    _SANDBOX_DB = _REPO_ROOT / "workspace" / "bank.db"
    if not _SANDBOX_DB.exists():
        return json.dumps({
            "status": "db_not_initialised",
            "message": (
                "workspace/bank.db not found. Run: "
                "python -c \"import sys; sys.path.insert(0, 'workspace'); "
                "from bank_ledger import init_db; init_db()\""
            ),
        }, indent=2)

    try:
        conn = sqlite3.connect(str(_SANDBOX_DB))
        row = conn.execute(
            "SELECT agent_id, identifier, balance FROM wallets WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return json.dumps({"status": "not_found", "agent_id": agent_id}, indent=2)
        return json.dumps({
            "status": "ok",
            "agent_id": row[0],
            "identifier": row[1],
            "balance": row[2],
            "unit": "integer_ledger_units",
            "note": "Sandbox simulation only — not on-chain.",
        }, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "detail": str(exc)}, indent=2)


@mcp.tool()
def get_treasury_telemetry() -> str:
    """
    Returns live treasury analytics from the core-exchange settlement engine.

    Hits GET /api/v1/settlement/analytics on the local exchange server and returns
    the structured metric block:
    - accumulated_fees_usdc: total 1.5% micro-tax collected from all peer transfers.
    - total_transactions_processed: count of all settlement_transactions rows.
    - total_volume_processed_usdc: sum of gross_amount_usdc across all transactions.
    - active_wallets_count: number of registered agent wallets.

    Requires the exchange server to be running locally (cd core-exchange/src && python run.py).
    Returns a structured error if the server is unreachable.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)) as client:
            resp = client.get(f"{_API_BASE}/api/v1/settlement/analytics")
            resp.raise_for_status()
            data = resp.json()
            return json.dumps({
                "status": "live",
                "source": f"{_API_BASE}/api/v1/settlement/analytics",
                **data,
            }, indent=2)
    except httpx.ConnectError:
        return json.dumps({
            "status": "exchange_offline",
            "message": (
                "Cannot reach exchange server at http://127.0.0.1:8000. "
                "Start it with: cd core-exchange/src && python run.py"
            ),
        }, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "detail": str(exc)}, indent=2)


@mcp.tool()
async def simulate_arbitrage_path(
    entry_asset: Annotated[str, "Entry asset for the route: 'USDC' or 'HBAR'"],
    exit_asset: Annotated[str, "Exit asset for the route: 'USDC' or 'HBAR'"],
    volume_usdc: Annotated[float, "Total arbitrage volume in USDC (float > 0)"],
    agent_chain: Annotated[str, "Ordered agent IDs forming the routing path — JSON array or comma-separated string (1–10 entries, e.g. 'agent-zero,agent-one' or '[\"agent-zero\",\"agent-one\"]')"],
) -> str:
    """
    Performs a dry-run viability simulation for a proposed cross-agent arbitrage path.

    Hits POST /api/v1/arbitrage/route-path on the local exchange server and returns
    a structured text report covering:
    - Overall path viability (VIABLE / NOT VIABLE)
    - Total slippage cost and expected net output in USDC
    - Per-hop status: wallet registration, balance sufficiency, and PENDING_SYNC blocks
    - First rejection reason if the path is blocked

    No funds are moved — the simulation is a fully isolated savepoint dry-run.

    agent_chain accepts either a JSON array string ('[\"agent-zero\",\"agent-one\"]')
    or a comma-separated string ('agent-zero,agent-one') for text-based MCP boundaries.

    Requires the exchange server to be running (cd core-exchange/src && python run.py).
    """
    # Parse agent_chain: accept JSON array or comma-separated string
    chain: list[str]
    stripped = agent_chain.strip()
    if stripped.startswith("["):
        try:
            chain = json.loads(stripped)
            if not isinstance(chain, list):
                raise ValueError("expected a JSON array")
        except (json.JSONDecodeError, ValueError) as exc:
            return json.dumps({"status": "error", "detail": f"agent_chain JSON parse failed: {exc}"}, indent=2)
    else:
        chain = [a.strip() for a in stripped.split(",") if a.strip()]

    if not chain:
        return json.dumps({
            "status": "error",
            "detail": "agent_chain must contain at least one agent ID",
        }, indent=2)

    payload = {
        "entry_asset": entry_asset,
        "exit_asset": exit_asset,
        "volume_usdc": volume_usdc,
        "agent_chain": chain,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0)
        ) as client:
            resp = await client.post(
                f"{_API_BASE}/api/v1/arbitrage/route-path",
                json=payload,
            )

        if resp.status_code == 422:
            detail = resp.json().get("detail", resp.text)
            return json.dumps({
                "status": "validation_error",
                "detail": detail,
                "hint": (
                    "Check that entry_asset/exit_asset are 'USDC' or 'HBAR', "
                    "volume_usdc > 0, and agent_chain has 1–10 non-empty IDs."
                ),
            }, indent=2)

        resp.raise_for_status()
        data = resp.json()

    except httpx.ConnectError:
        return json.dumps({
            "status": "exchange_offline",
            "message": (
                "Cannot reach exchange server at http://127.0.0.1:8000. "
                "Start it with: cd core-exchange/src && python run.py"
            ),
        }, indent=2)
    except httpx.TimeoutException:
        return json.dumps({
            "status": "timeout",
            "message": "Arbitrage simulation timed out — exchange server may be overloaded.",
        }, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "detail": str(exc)}, indent=2)

    # Build a structured text report
    viable = data.get("viable", False)
    status_line = "VIABLE" if viable else "NOT VIABLE"
    rejection = data.get("rejection_reason") or "none"
    hop_count = len(data.get("steps", []))
    chain_display = " → ".join(data.get("agent_chain", chain))

    lines = [
        "ARBITRAGE PATH SIMULATION REPORT",
        "=" * 40,
        f"Status          : {status_line}",
        f"Entry Asset     : {data.get('entry_asset', entry_asset)}",
        f"Exit Asset      : {data.get('exit_asset', exit_asset)}",
        f"Volume          : {data.get('volume_usdc', volume_usdc):.4f} USDC",
        f"Agent Chain     : {chain_display}",
        f"Slippage Tol.   : {data.get('slippage_tolerance_pct', 0.005) * 100:.2f}%",
        f"Total Slippage  : {data.get('total_slippage_usdc', 0.0):.6f} USDC",
        f"Expected Output : {data.get('expected_output_usdc', 0.0):.6f} USDC",
        f"Rejection Reason: {rejection}",
        "",
        f"HOP BREAKDOWN ({hop_count} hop(s))",
        "-" * 40,
    ]

    for step in data.get("steps", []):
        hop_idx = step.get("step", "?")
        agent = step.get("agent_id", "?")
        found = step.get("wallet_found", False)
        sufficient = step.get("balance_sufficient", False)
        blocked = step.get("pending_sync_blocked", False)
        balance = step.get("balance_usdc", 0.0)
        floor_ = step.get("slippage_floor_usdc", 0.0)

        if not found:
            hop_status = "FAIL — wallet not registered"
        elif blocked:
            hop_status = "FAIL — PENDING_SYNC limbo block"
        elif not sufficient:
            hop_status = f"FAIL — balance {balance:.4f} < floor {floor_:.4f} USDC"
        else:
            hop_status = f"OK   — balance {balance:.4f} USDC (floor {floor_:.6f})"

        lines.append(f"  Hop {hop_idx}: {agent:<24} {hop_status}")

    lines += [
        "-" * 40,
        f"Simulation source: {_API_BASE}/api/v1/arbitrage/route-path",
        "Note: dry-run only — no ledger mutations committed.",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
