"""
VectraFi — L2 Testnet Environment Verification
================================================
Validates the four required environment variables for live Layer 2 broadcasting
and performs an async RPC connectivity probe.

Usage:
    python core-exchange/src/verify_env.py

Exit codes:
    0  All checks passed
    1  One or more checks failed (actionable checklist printed)
"""

from __future__ import annotations

import asyncio
import os
import re
import sys


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_HEX_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _check_address(value: str | None, name: str) -> tuple[bool, str]:
    """Return (ok, message) for an EIP-55 hex address variable."""
    if not value:
        return False, f"{name} is missing or empty"
    if not _HEX_ADDRESS_RE.match(value):
        return False, f"{name}={value!r} does not match 0x + 40 hex chars"
    return True, f"{name} = {value}"


def _derive_public_address(raw_pk: str | None) -> tuple[bool, str]:
    """
    Derive the public wallet address from PROTOCOL_PRIVATE_KEY without ever
    printing the key itself.  Returns (ok, message).
    """
    if not raw_pk:
        return False, "PROTOCOL_PRIVATE_KEY is missing or empty"
    try:
        from eth_account import Account  # noqa: PLC0415

        pk = raw_pk if raw_pk.startswith("0x") else f"0x{raw_pk}"
        acct = Account.from_key(pk)
        return True, f"PROTOCOL_PRIVATE_KEY -> signing address: {acct.address}"
    except Exception as exc:
        return False, f"PROTOCOL_PRIVATE_KEY is corrupted or malformed: {exc}"


async def _probe_rpc(url: str) -> tuple[bool, str]:
    """
    Open an async HTTP connection to the RPC endpoint and call eth_blockNumber.
    Returns (ok, message).
    """
    try:
        from web3 import AsyncWeb3  # noqa: PLC0415
        from web3.providers import AsyncHTTPProvider  # noqa: PLC0415

        w3 = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": 15}))
        if not await w3.is_connected():
            return False, f"L2_PROVIDER_URL={url!r} — is_connected() returned False"
        block = await w3.eth.block_number
        chain_id = await w3.eth.chain_id
        return True, (
            f"L2_PROVIDER_URL reachable | chain_id={chain_id} latest_block={block}"
        )
    except Exception as exc:
        return False, f"L2_PROVIDER_URL={url!r} — RPC probe failed: {exc}"


# ---------------------------------------------------------------------------
# Main verification runner
# ---------------------------------------------------------------------------


async def _run_verification() -> int:
    """Execute all checks and return an exit code (0 = pass, 1 = fail)."""
    print()
    print("VectraFi - L2 Testnet Environment Verification")
    print("=" * 54)

    results: list[tuple[bool, str, str]] = []  # (ok, label, message)

    # 1. L2_PROVIDER_URL — connectivity probe (async)
    provider_url = os.getenv("L2_PROVIDER_URL", "").strip() or None
    if provider_url:
        ok, msg = await _probe_rpc(provider_url)
    else:
        ok, msg = False, "L2_PROVIDER_URL is missing or empty"
    results.append((ok, "L2_PROVIDER_URL", msg))

    # 2. PROTOCOL_PRIVATE_KEY — derive public address only
    raw_pk = os.getenv("PROTOCOL_PRIVATE_KEY", "").strip() or None
    ok, msg = _derive_public_address(raw_pk)
    results.append((ok, "PROTOCOL_PRIVATE_KEY", msg))

    # 3. USDC_CONTRACT_ADDRESS — hex format check
    ok, msg = _check_address(
        os.getenv("USDC_CONTRACT_ADDRESS", "").strip() or None,
        "USDC_CONTRACT_ADDRESS",
    )
    results.append((ok, "USDC_CONTRACT_ADDRESS", msg))

    # 4. PLATFORM_TREASURY_ADDRESS — hex format check
    ok, msg = _check_address(
        os.getenv("PLATFORM_TREASURY_ADDRESS", "").strip() or None,
        "PLATFORM_TREASURY_ADDRESS",
    )
    results.append((ok, "PLATFORM_TREASURY_ADDRESS", msg))

    # ---------------------------------------------------------------------------
    # Print results
    # ---------------------------------------------------------------------------
    failed: list[tuple[str, str]] = []
    for ok, label, msg in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {msg}")
        if not ok:
            failed.append((label, msg))

    print("=" * 54)

    if not failed:
        print("  Result: ALL CHECKS PASSED — ready for L2 Testnet broadcasting")
        print()
        return 0

    # ---------------------------------------------------------------------------
    # Actionable configuration checklist for Railway / local runtimes
    # ---------------------------------------------------------------------------
    print(f"  Result: {len(failed)} CHECK(S) FAILED\n")
    print("  Configuration checklist")
    print("  -" * 27)

    for label, reason in failed:
        print(f"\n  Variable : {label}")
        print(f"  Problem  : {reason}")

        if label == "L2_PROVIDER_URL":
            print(
                "  Fix      : Set L2_PROVIDER_URL to a valid HTTP(S) JSON-RPC endpoint."
            )
            print("             Examples:")
            print("               Base Testnet  - https://sepolia.base.org")
            print(
                "               Alchemy       - https://base-sepolia.g.alchemy.com/v2/<KEY>"
            )
            print("             On Railway: Settings > Variables > add L2_PROVIDER_URL")

        elif label == "PROTOCOL_PRIVATE_KEY":
            print(
                "  Fix      : Set PROTOCOL_PRIVATE_KEY to the 64-char hex private key"
            )
            print(
                "             of the platform gas/escrow account (with or without 0x prefix)."
            )
            print("             NEVER commit this value to source control.")
            print(
                "             On Railway: Settings > Variables > add PROTOCOL_PRIVATE_KEY"
            )

        elif label == "USDC_CONTRACT_ADDRESS":
            print(
                "  Fix      : Set USDC_CONTRACT_ADDRESS to the ERC-20 USDC contract on"
            )
            print("             your target L2 (0x + 40 hex chars, e.g. 0x036Cbd...).")
            print(
                "             Base Sepolia USDC: 0x036CbD53842c5426634e7929541eC2318f3dCF7e"
            )

        elif label == "PLATFORM_TREASURY_ADDRESS":
            print(
                "  Fix      : Set PLATFORM_TREASURY_ADDRESS to the wallet that receives"
            )
            print("             the 1.5% tax on every settlement (0x + 40 hex chars).")

    print()
    return 1


def main() -> None:
    exit_code = asyncio.run(_run_verification())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
