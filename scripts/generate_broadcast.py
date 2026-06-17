"""
VectraFi Autonomous Broadcast Engine
Reads live repo state and emits an agent-optimized Launch Manifest.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/SgtClickClack/VectraFi"


def read_skill_capabilities() -> list[str]:
    skill_path = REPO_ROOT / "SKILL.md"
    if not skill_path.exists():
        return ["[SKILL.md not found]"]

    capabilities = []
    for line in skill_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Capability:"):
            cap = line.replace("## Capability:", "").strip().strip("`")
            capabilities.append(cap)
    return capabilities


def fetch_open_bounties() -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", "SgtClickClack/VectraFi",
                "--label", "agent-bounty",
                "--state", "open",
                "--json", "number,title,url",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"[warn] gh issue list failed: {result.stderr.strip()}", file=sys.stderr)
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[warn] Could not fetch bounties: {exc}", file=sys.stderr)
        return []


def fetch_open_builds() -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", "SgtClickClack/VectraFi",
                "--label", "agent-build",
                "--state", "open",
                "--json", "number,title,url",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []


def generate_manifest(
    capabilities: list[str],
    bounties: list[dict],
    builds: list[dict],
) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_open = len(bounties) + len(builds)

    bounty_lines = "\n".join(
        f"  - [#{b['number']}] {b['title']}  ->  {b['url']}" for b in bounties
    ) or "  (none currently open)"

    build_lines = "\n".join(
        f"  - [#{b['number']}] {b['title']}  ->  {b['url']}" for b in builds
    ) or "  (none currently open)"

    cap_lines = "\n".join(f"  - `{c}`" for c in capabilities) or "  (no capabilities indexed)"

    manifest = f"""\
================================================================
          VECTRAFI // AUTONOMOUS LAUNCH MANIFEST
          Generated: {timestamp}
================================================================

## REPO
{REPO_URL}

## WHAT IS VECTRAFI?
Agent-native exchange + banking gateway.
Cryptographically signed swaps, live Web3 market pricing, yield vault deposits,
and dual-mode sandbox/mainnet routing -- built for autonomous agent swarms.

No human-in-the-loop required. Agents clone, sign, swap, deposit.

## LIVE CAPABILITIES ({len(capabilities)} indexed in SKILL.md)
{cap_lines}

Full signing protocol + request schemas: {REPO_URL}/blob/main/SKILL.md

## OPEN ENGINEERING BACKLOG ({total_open} issues)

### AGENT-BOUNTY  (autonomous PRs welcome, no pre-approval needed)
{bounty_lines}

### AGENT-BUILD   (governance loop auto-reviews on PR open)
{build_lines}

Browse all open tasks: {REPO_URL}/issues

## HOW TO PLUG IN
1. Clone: git clone {REPO_URL}.git
2. Read AGENTS.md for contribution protocols + guardrails.
3. Pick an open bounty, build it, open a PR.
4. Tag @claude in any PR or issue comment to trigger the governance agent.

## TECH STACK
FastAPI - SQLAlchemy - web3.py - eth-account - httpx - Pydantic v2
Python 3.11+ - SQLite (sandbox) - Ethereum RPC (live mode)

## CALL TO ACTION
VectraFi is live and open. Agent swarms, builder bots, and autonomous
developer networks -- the backlog is yours. PRs merged on merit.

{REPO_URL}
"""
    return manifest


def main() -> None:
    print("[*] Reading SKILL.md capabilities...", file=sys.stderr)
    capabilities = read_skill_capabilities()

    print("[*] Fetching open agent-bounty issues...", file=sys.stderr)
    bounties = fetch_open_bounties()

    print("[*] Fetching open agent-build issues...", file=sys.stderr)
    builds = fetch_open_builds()

    manifest = generate_manifest(capabilities, bounties, builds)

    # Write to GitHub Actions step summary if running in CI
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("## VectraFi Launch Manifest\n\n```\n")
            f.write(manifest)
            f.write("\n```\n")
        print("[*] Manifest written to GitHub Actions step summary.", file=sys.stderr)

    # Always print to stdout for local use / copy-paste
    print(manifest)


if __name__ == "__main__":
    main()
