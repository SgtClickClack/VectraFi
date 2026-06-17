#!/usr/bin/env python3
"""
VectraFi FABA MCP Server
========================
Exposes three protocol tools to external agent runtimes via the Model Context Protocol.

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

_FALLBACK_ISSUES = [
    {"number": 3, "title": "feat: Implement Protocol Treasury Fee Collector on Deposits", "labels": ["agent-bounty"], "url": f"https://github.com/{_GITHUB_REPO}/issues/3"},
    {"number": 4, "title": "feat: Implement Multi-Route DeFi Yield Aggregator Analytics Endpoint", "labels": ["agent-bounty"], "url": f"https://github.com/{_GITHUB_REPO}/issues/4"},
    {"number": 5, "title": "feat: Implement Autonomous Portfolio Rebalancing Math Engine", "labels": ["agent-bounty"], "url": f"https://github.com/{_GITHUB_REPO}/issues/5"},
    {"number": 2, "title": "feat: Implement X-VectraFi-Signature Validation Middleware", "labels": ["agent-build"], "url": f"https://github.com/{_GITHUB_REPO}/issues/2"},
]

mcp = FastMCP("VectraFi FABA Protocol")


def _gh_list_issues(label: str) -> list[dict]:
    """Fetch open issues via the gh CLI (preferred — respects GITHUB_TOKEN)."""
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", _GITHUB_REPO, "--label", label,
             "--state", "open", "--json", "number,title,url,labels"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def _httpx_list_issues() -> list[dict]:
    """Fetch open issues via httpx + GitHub REST API (unauthenticated, 60 req/hr)."""
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
                params={"state": "open", "per_page": 50},
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
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
    # Try gh CLI first (honours GITHUB_TOKEN, higher rate limit)
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

    # Fallback: unauthenticated GitHub REST API
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


if __name__ == "__main__":
    mcp.run()
