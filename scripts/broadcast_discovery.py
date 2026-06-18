#!/usr/bin/env python3
"""
broadcast_discovery.py — VectraFi OpenAPI Discovery Broadcaster

Fetches the live OpenAPI spec from the running VectraFi instance, formats an
Agent Capability Profile, and POSTs it to every discovery endpoint listed in
BROADCAST_TARGETS.

Environment variables:
  VECTRAFI_API_URL      Base URL of the live instance (required).
                        e.g. https://vectrafi-production.up.railway.app
  BROADCAST_TARGETS     Comma-separated list of discovery webhook URLs to ping.
                        Leave empty to do a dry-run (profile printed to stdout only).
  BROADCAST_DRY_RUN     Set to "1" to skip network POSTs regardless of BROADCAST_TARGETS.
  REQUEST_TIMEOUT       HTTP timeout in seconds (default: 15).

Usage:
  python scripts/broadcast_discovery.py
  VECTRAFI_API_URL=https://... BROADCAST_TARGETS=https://hooks.example.com/agent python scripts/broadcast_discovery.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("vectrafi.discovery")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_API_URL: str = os.getenv("VECTRAFI_API_URL", "").rstrip("/")
_BROADCAST_TARGETS: list[str] = [
    t.strip() for t in os.getenv("BROADCAST_TARGETS", "").split(",") if t.strip()
]
_DRY_RUN: bool = os.getenv("BROADCAST_DRY_RUN", "0") == "1"
_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "15"))

_PROTOCOL_VERSION = "1.0.0"
_AGENT_DESCRIPTION = (
    "VectraFi is an autonomous M2M liquidity protocol (FABA). "
    "Agent wallets execute EIP-191-signed swaps, vault deposits, and "
    "peer-to-peer USDC settlements with a 1.5% micro-tax routed to a "
    "on-chain treasury. Fully permissionless — any agent runtime may "
    "provision a wallet and begin transacting immediately."
)

# ---------------------------------------------------------------------------
# Step 1: Fetch live OpenAPI spec
# ---------------------------------------------------------------------------


def fetch_openapi_spec(client: httpx.Client) -> dict[str, Any]:
    url = f"{_API_URL}/openapi.json"
    log.info("Fetching OpenAPI spec from %s", url)
    resp = client.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    spec: dict[str, Any] = resp.json()
    log.info(
        "Spec received — title=%r version=%r paths=%d",
        spec.get("info", {}).get("title"),
        spec.get("info", {}).get("version"),
        len(spec.get("paths", {})),
    )
    return spec


# ---------------------------------------------------------------------------
# Step 2: Build Agent Capability Profile
# ---------------------------------------------------------------------------

_PRIORITY_ENDPOINTS = [
    # (path, method, short description)
    ("/api/v1/settlement/transfer", "post", "Peer-to-peer USDC transfer — 1.5% tax settlement"),
    ("/api/v1/arbitrage/route-path", "get",  "Cross-desk arbitrage route discovery"),
    ("/api/v1/wallet/create",        "post", "Provision an agent wallet"),
    ("/api/v1/trade/swap",           "post", "USDC ↔ HBAR swap (zero protocol fee)"),
    ("/api/v1/bank/deposit",         "post", "Vault deposit — 0.25% fee, 80/20 treasury split"),
    ("/api/v1/market/prices",        "get",  "Live price feed — ETH, USDC, HBAR"),
    ("/api/v1/settlement/analytics", "get",  "Public treasury analytics (no auth required)"),
]


def _pick_endpoints(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract operation summaries for the priority endpoints that exist in the live spec."""
    paths = spec.get("paths", {})
    endpoints = []
    for path, method, fallback_desc in _PRIORITY_ENDPOINTS:
        path_item = paths.get(path, {})
        op = path_item.get(method, {})
        endpoints.append({
            "path": path,
            "method": method.upper(),
            "summary": op.get("summary") or fallback_desc,
            "description": op.get("description", ""),
            "auth_required": any(
                "X-VectraFi-Signature" in str(sec)
                for sec in op.get("security", [])
            ) or method in ("post", "put", "patch"),
        })
    return endpoints


def _summarise_full_paths(spec: dict[str, Any]) -> list[str]:
    paths = spec.get("paths", {})
    lines = []
    for path, path_item in sorted(paths.items()):
        for method in ("get", "post", "put", "delete", "patch"):
            if method in path_item:
                summary = path_item[method].get("summary", "")
                lines.append(f"  {method.upper():6} {path}  — {summary}")
    return lines


def build_capability_profile(spec: dict[str, Any]) -> dict[str, Any]:
    info = spec.get("info", {})
    endpoints = _pick_endpoints(spec)
    timestamp = datetime.now(timezone.utc).isoformat()

    profile: dict[str, Any] = {
        "schema_version": _PROTOCOL_VERSION,
        "generated_at": timestamp,
        "protocol": {
            "name": "VectraFi FABA Protocol",
            "class": "M2M Liquidity / Agent-Native DeFi",
            "description": _AGENT_DESCRIPTION,
            "api_base_url": _API_URL,
            "openapi_url": f"{_API_URL}/openapi.json",
            "docs_url": f"{_API_URL}/docs",
            "github": "https://github.com/SgtClickClack/VectraFi",
        },
        "api_info": {
            "title": info.get("title"),
            "version": info.get("version"),
            "total_paths": len(spec.get("paths", {})),
        },
        "authentication": {
            "scheme": "EIP-191 Ethereum personal-sign",
            "header": "X-VectraFi-Signature",
            "algorithm": "eth_sign over compact JSON body bytes",
            "replay_protection": "nonce + issued_at + chain_id in every signed payload",
        },
        "featured_endpoints": endpoints,
        "financial_model": {
            "settlement_tax_rate_pct": 1.5,
            "deposit_fee_rate_pct": 0.25,
            "treasury_split": {"creator_pct": 80, "bounty_pool_pct": 20},
            "min_transfer_usdc": 0.0001,
            "supported_assets": ["USDC", "HBAR", "ETH"],
        },
        "agent_onboarding": [
            f"POST {_API_URL}/api/v1/wallet/create  — provision wallet, receive agent_id",
            f"GET  {_API_URL}/api/v1/market/prices  — fetch live prices",
            f"POST {_API_URL}/api/v1/trade/swap     — execute signed swap",
            f"POST {_API_URL}/api/v1/settlement/transfer — signed P2P transfer (1.5% tax)",
        ],
        "capability_tags": [
            "defi",
            "m2m-liquidity",
            "autonomous-agent",
            "eip191-auth",
            "usdc-settlement",
            "arbitrage",
            "yield",
        ],
    }
    return profile


# ---------------------------------------------------------------------------
# Step 3: Render human-readable summary
# ---------------------------------------------------------------------------


def render_text_summary(profile: dict[str, Any], spec: dict[str, Any]) -> str:
    p = profile["protocol"]
    fm = profile["financial_model"]
    auth = profile["authentication"]
    lines = [
        "=" * 72,
        "  VECTRAFI AGENT CAPABILITY PROFILE — DISCOVERY BROADCAST",
        "=" * 72,
        f"  Generated : {profile['generated_at']}",
        f"  API base  : {p['api_base_url']}",
        f"  OpenAPI   : {p['openapi_url']}",
        f"  Docs      : {p['docs_url']}",
        f"  GitHub    : {p['github']}",
        "",
        "DESCRIPTION",
        "-" * 72,
        f"  {p['description']}",
        "",
        "AUTHENTICATION",
        "-" * 72,
        f"  Scheme    : {auth['scheme']}",
        f"  Header    : {auth['header']}",
        f"  Replay    : {auth['replay_protection']}",
        "",
        "FINANCIAL MODEL",
        "-" * 72,
        f"  Settlement tax   : {fm['settlement_tax_rate_pct']}%",
        f"  Deposit fee      : {fm['deposit_fee_rate_pct']}%",
        f"  Treasury split   : {fm['treasury_split']['creator_pct']}% creator / "
        f"{fm['treasury_split']['bounty_pool_pct']}% bounty pool",
        f"  Min transfer     : {fm['min_transfer_usdc']} USDC",
        f"  Assets           : {', '.join(fm['supported_assets'])}",
        "",
        "FEATURED ENDPOINTS",
        "-" * 72,
    ]
    for ep in profile["featured_endpoints"]:
        auth_tag = "[auth]" if ep["auth_required"] else "[open]"
        lines.append(f"  {ep['method']:6} {ep['path']:<45} {auth_tag}")
        lines.append(f"         {ep['summary']}")
    lines += [
        "",
        f"ALL PATHS ({profile['api_info']['total_paths']} total)",
        "-" * 72,
    ]
    lines += _summarise_full_paths(spec)
    lines += [
        "",
        "ONBOARDING SEQUENCE",
        "-" * 72,
    ]
    lines += [f"  {step}" for step in profile["agent_onboarding"]]
    lines += ["", "=" * 72]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 4: Broadcast
# ---------------------------------------------------------------------------


def broadcast(client: httpx.Client, profile: dict[str, Any]) -> dict[str, str]:
    """POST the capability profile to every target. Returns {url: outcome}."""
    results: dict[str, str] = {}

    if _DRY_RUN:
        log.info("DRY RUN — skipping all network broadcasts")
        return results

    if not _BROADCAST_TARGETS:
        log.info("No BROADCAST_TARGETS configured — skipping network broadcast")
        return results

    payload = {
        "type": "agent_capability_profile",
        "schema_version": profile["schema_version"],
        "profile": profile,
    }

    for url in _BROADCAST_TARGETS:
        try:
            resp = client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "VectraFi-DiscoveryBeacon/1.0",
                    "X-Beacon-Protocol": "vectrafi-faba",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            outcome = f"HTTP {resp.status_code} OK"
            log.info("[broadcast] %s -> %s", url, outcome)
        except httpx.HTTPStatusError as exc:
            outcome = f"HTTP {exc.response.status_code} ERROR"
            log.warning("[broadcast] %s -> %s", url, outcome)
        except Exception as exc:
            outcome = f"FAILED: {exc}"
            log.error("[broadcast] %s -> %s", url, outcome)
        results[url] = outcome

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not _API_URL:
        log.error("VECTRAFI_API_URL is not set — cannot fetch live spec")
        sys.exit(1)

    with httpx.Client() as client:
        # 1. Fetch spec
        try:
            spec = fetch_openapi_spec(client)
        except Exception as exc:
            log.error("Failed to fetch OpenAPI spec: %s", exc)
            sys.exit(1)

        # 2. Build profile
        profile = build_capability_profile(spec)
        log.info("Capability profile built — %d featured endpoints", len(profile["featured_endpoints"]))

        # 3. Print human-readable summary to stdout
        print(render_text_summary(profile, spec))

        # 4. Broadcast
        results = broadcast(client, profile)

    # 5. Report
    if results:
        print("\nBROADCAST RESULTS")
        print("-" * 72)
        for url, outcome in results.items():
            print(f"  {outcome:<20} {url}")
        failures = [u for u, o in results.items() if "OK" not in o]
        if failures:
            log.warning("%d target(s) failed", len(failures))
            sys.exit(1)
        else:
            log.info("All %d target(s) acknowledged", len(results))
    elif not _DRY_RUN:
        log.info("Dry run complete — no targets pinged (set BROADCAST_TARGETS to go live)")


if __name__ == "__main__":
    main()
