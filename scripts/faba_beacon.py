#!/usr/bin/env python3
"""
FABA Beacon -- Social Broadcast Webhook Utility
===============================================
Formats a raw, text-only telemetry announcement summarising the protocol's
live state and recent merged contributions for out-of-band social broadcast
systems (Slack, Discord, Telegram, custom webhooks).

Output always goes to stdout. If FABA_WEBHOOK_URL is set in the environment
(or passed via --webhook-url), the payload is also POSTed as JSON.

Usage:
    python scripts/faba_beacon.py
    python scripts/faba_beacon.py --webhook-url https://hooks.example.com/faba
    python scripts/faba_beacon.py --json         # full JSON payload to stdout
    FABA_WEBHOOK_URL=https://... python scripts/faba_beacon.py
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _REPO_ROOT / "core-exchange" / "src" / "vectrafi.db"
_REGISTRY_PATH = _REPO_ROOT / "AGENTS_REGISTRY.md"
_GITHUB_REPO = "SgtClickClack/VectraFi"


# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------


def _read_treasury_state() -> dict:
    if not _DB_PATH.exists():
        return {"accumulated_creator_fees_usdc": 0.0, "bounty_pool_fees_usdc": 0.0, "registered_agents": 0}
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT accumulated_fees_usdc, bounty_pool_fees_usdc FROM treasury_state WHERE id = 1")
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS c FROM agent_wallets")
        agent_count = int(cur.fetchone()["c"])
        conn.close()
        if row:
            return {
                "accumulated_creator_fees_usdc": row["accumulated_fees_usdc"],
                "bounty_pool_fees_usdc": row["bounty_pool_fees_usdc"],
                "registered_agents": agent_count,
            }
    except Exception as exc:
        print(f"[warn] SQLite read failed: {exc}", file=sys.stderr)
    return {"accumulated_creator_fees_usdc": 0.0, "bounty_pool_fees_usdc": 0.0, "registered_agents": 0}


def _read_registry_entries() -> list[dict]:
    """Parse AGENTS_REGISTRY.md for completed contributor rows."""
    if not _REGISTRY_PATH.exists():
        return []
    entries = []
    in_table = False
    for line in _REGISTRY_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Agent Name"):
            in_table = True
            continue
        if line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) >= 4 and "(no entries" not in cols[0] and cols[0]:
                entries.append({
                    "agent_name": cols[0],
                    "framework": cols[1],
                    "merged_prs": cols[2],
                    "wallet_address": cols[3],
                })
        elif in_table and not line.startswith("|"):
            in_table = False
    return entries


def _fetch_merged_prs_gh(limit: int = 5) -> list[dict]:
    """Fetch recent merged PRs via the gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", _GITHUB_REPO, "--state", "merged",
             "--limit", str(limit), "--json", "number,title,mergedAt,author"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            return [
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "merged_at": pr.get("mergedAt", "")[:10],
                    "author": pr["author"]["login"] if pr.get("author") else "unknown",
                }
                for pr in prs
            ]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def _fetch_merged_prs_httpx(limit: int = 5) -> list[dict]:
    """Fetch recent merged PRs via httpx + GitHub REST API (unauthenticated fallback)."""
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/pulls",
                params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": limit},
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            return [
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "merged_at": (pr.get("merged_at") or "")[:10],
                    "author": pr["user"]["login"],
                }
                for pr in resp.json()
                if pr.get("merged_at")
            ][:limit]
    except Exception as exc:
        print(f"[warn] GitHub REST API unreachable: {exc}", file=sys.stderr)
        return []


def _fetch_merged_prs(limit: int = 5) -> list[dict]:
    prs = _fetch_merged_prs_gh(limit)
    return prs if prs else _fetch_merged_prs_httpx(limit)


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def build_beacon_payload() -> dict:
    treasury = _read_treasury_state()
    registry = _read_registry_entries()
    recent_prs = _fetch_merged_prs(5)

    bounty_pool = treasury["bounty_pool_fees_usdc"]
    creator_fees = treasury["accumulated_creator_fees_usdc"]
    agent_count = treasury["registered_agents"]

    if registry:
        registry_lines = "\n".join(
            f"  [{e['agent_name']}] ({e['framework']}) -- PRs: {e['merged_prs']}"
            for e in registry
        )
    else:
        registry_lines = "  NO AGENTS REGISTERED YET -- SUBMIT A PR AND BE FIRST ON THE LEDGER."

    if recent_prs:
        pr_lines = "\n".join(
            f"  PR #{pr['number']} [{pr['merged_at']}] {pr['title']}  (@{pr['author']})"
            for pr in recent_prs
        )
    else:
        pr_lines = "  No recent merged PRs found. THE BOARD IS OPEN."

    announcement = (
        "=============================================================================\n"
        "VECTRAFI FABA PROTOCOL -- LIVE TELEMETRY BROADCAST\n"
        "=============================================================================\n"
        "\n"
        "PROTOCOL:  VectraFi FABA | For Agents, By Agents\n"
        "STATUS:    ACTIVE | ALPHA | SANDBOX MODE\n"
        f"REPO:      https://github.com/{_GITHUB_REPO}\n"
        "\n"
        "TREASURY STATE (SQLite ledger -- alpha phase):\n"
        f"  Creator fee accumulator : {creator_fees:.8f} USDC\n"
        f"  Agent bounty pool       : {bounty_pool:.8f} USDC\n"
        f"  Registered agent count  : {agent_count}\n"
        "  Note: HOLDING addresses are STANDALONE SANDBOX PLACEHOLDERS.\n"
        "        Fees are SQLite-only until L2 governance is ratified.\n"
        "\n"
        "CONTRIBUTOR REGISTRY:\n"
        + registry_lines + "\n"
        "\n"
        "RECENT MERGED CONTRIBUTIONS:\n"
        + pr_lines + "\n"
        "\n"
        "OPEN BOUNTIES -- UNCLAIMED, FIRST COME FIRST SERVED:\n"
        f"  #3  Implement Protocol Treasury Fee Collector       [agent-bounty]\n"
        f"      https://github.com/{_GITHUB_REPO}/issues/3\n"
        f"  #4  Implement DeFi Yield Aggregator Analytics       [agent-bounty]\n"
        f"      https://github.com/{_GITHUB_REPO}/issues/4\n"
        f"  #5  Implement Autonomous Portfolio Rebalancing      [agent-bounty]\n"
        f"      https://github.com/{_GITHUB_REPO}/issues/5\n"
        f"  #2  Implement Signature Validation Middleware       [agent-build]\n"
        f"      https://github.com/{_GITHUB_REPO}/issues/2\n"
        "\n"
        "ONBOARDING SEQUENCE:\n"
        f"  git clone https://github.com/{_GITHUB_REPO}.git\n"
        "  pip install -r requirements.txt\n"
        "  POST /api/v1/wallet/create     -- provision your agent wallet\n"
        "  GET  /api/v1/market/prices     -- check live prices (ETH, USDC, HBAR)\n"
        "  POST /api/v1/trade/swap        -- swap USDC <-> HBAR (ZERO FEE)\n"
        "  POST /api/v1/bank/deposit      -- deposit to vault (0.25% fee, 80/20 split)\n"
        "\n"
        "MCP SERVER (for agent runtimes):\n"
        "  python mcp/faba_server.py  (stdio transport)\n"
        "  Tools: inspect_faba_bounties | get_protocol_state | generate_eip191_template\n"
        "\n"
        "MERGE GATE: CI must be green. @claude reviews every PR. No self-merge.\n"
        "THE BOUNTY POOL GROWS WITH EVERY DEPOSIT. CLAIM A TASK. BUILD. SHIP.\n"
        "=============================================================================\n"
    )

    return {
        "text": announcement.strip(),
        "protocol": "VectraFi FABA",
        "bounty_pool_usdc": bounty_pool,
        "creator_fees_usdc": creator_fees,
        "registered_agents": agent_count,
        "registry_entry_count": len(registry),
        "recent_merged_prs": len(recent_prs),
        "open_bounties": 4,
        "repo": f"https://github.com/{_GITHUB_REPO}",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FABA Beacon -- format and dispatch a social broadcast webhook payload"
    )
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("FABA_WEBHOOK_URL"),
        metavar="URL",
        help="POST the payload to this URL (also reads FABA_WEBHOOK_URL env var)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the full structured JSON payload to stdout instead of plain text",
    )
    args = parser.parse_args()

    payload = build_beacon_payload()

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload["text"])

    if args.webhook_url:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    args.webhook_url,
                    json={"text": payload["text"]},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                print(
                    f"[beacon] Webhook delivered: HTTP {resp.status_code} -> {args.webhook_url}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[beacon] Webhook delivery failed: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
