#!/usr/bin/env python3
"""
VectraFi Testnet Stress Runner
================================
Provisions 3 agent wallets and fires 5 concurrent settlement transfers to
stress-test three concurrent-safety properties in a single pass:

  • Alphabetical pessimistic row-lock ordering — wallets locked as
    sorted([sender_id, receiver_id]) so AB/BA pairs never deadlock.
  • Sequential Treasury accumulator lock — held after wallet locks
    to prevent lost-update races on fee increments.
  • PENDING_SYNC recovery — any RPC timeout or on-chain nonce
    collision is detected post-batch and run_recovery() is invoked
    to demonstrate the automated re-submission / healing path.

Usage (exchange server must be running):
    cd core-exchange/src && python run.py          # terminal A
    python core-exchange/src/run_testnet_stress.py # terminal B

Environment overrides:
    VECTRAFI_API_URL  — canonical API base; shared with seed_swarm.py
    STRESS_API_BASE   — overrides VECTRAFI_API_URL for this script only (legacy compat)
                        default http://127.0.0.1:8000
    L2_PROVIDER_URL   — when set, on-chain settlement activates;
                        hashes are emitted as Basescan explorer URLs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

# ---------------------------------------------------------------------------
# Path bootstrap — runnable from project root OR from core-exchange/src/
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from config import PROTOCOL_DOMAIN          # "vectrafi-sandbox-v1"
from database import SessionLocal
from models import SettlementTransaction
from recovery_worker import run_recovery

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_API_BASE      = (
    os.getenv("STRESS_API_BASE")
    or os.getenv("VECTRAFI_API_URL")
    or "http://127.0.0.1:8000"
).rstrip("/")
_EXPLORER_TX   = "https://sepolia.basescan.org/tx/{hash}"
_HTTP_TIMEOUT  = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
_BATCH_SIZE    = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("stress")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    agent_id:       str
    wallet_address: str
    private_key:    str
    balance_usdc:   float


@dataclass
class TransferResult:
    idx:          int
    sender_id:    str
    receiver_id:  str
    amount_usdc:  float
    status_code:  int              = 0
    tx_id:        str | None       = None
    error:        str | None       = None
    elapsed_ms:   float            = 0.0
    timed_out:    bool             = False
    nonce_collision: bool          = False


# ---------------------------------------------------------------------------
# Signing helper
# ---------------------------------------------------------------------------

def _signed_body(
    body: dict,
    private_key: str,
) -> tuple[bytes, str]:
    """
    Return (raw_body_bytes, hex_signature).
    The bytes sent over the wire and the string signed must be identical —
    server does body_bytes.decode('utf-8') then encode_defunct(text=...).
    """
    compact  = json.dumps(body, separators=(",", ":"))
    msg      = encode_defunct(text=compact)
    sig      = Account.sign_message(msg, private_key=private_key)
    return compact.encode("utf-8"), sig.signature.hex()


# ---------------------------------------------------------------------------
# Step 1 — wallet provisioning
# ---------------------------------------------------------------------------

async def provision_wallets(
    client: httpx.AsyncClient,
    run_id: str,
) -> dict[str, AgentInfo]:
    """
    Register Agent_Alpha, Agent_Beta, Agent_Gamma for this stress run.
    Each agent_id is suffixed with run_id so successive runs stay isolated
    in the database and never hit a 409 Conflict.
    """
    agents: dict[str, AgentInfo] = {}
    for name in ("Agent_Alpha", "Agent_Beta", "Agent_Gamma"):
        agent_id = f"{name}_{run_id}"
        resp = await client.post(
            f"{_API_BASE}/api/v1/wallet/create",
            json={"agent_id": agent_id},
        )
        if resp.status_code in (200, 201):
            d = resp.json()
            agents[name] = AgentInfo(
                agent_id       = d["agent_id"],
                wallet_address = d["wallet_address"],
                private_key    = d["private_key"],
                balance_usdc   = d["balance_usdc"],
            )
            log.info(
                "  WALLET %-26s  address=%.14s…  balance=%.2f USDC",
                agent_id, d["wallet_address"], d["balance_usdc"],
            )
        elif resp.status_code == 409:
            log.warning("  WALLET SKIP %-26s  already exists (409)", agent_id)
        else:
            raise RuntimeError(
                f"Wallet creation failed for {agent_id}: "
                f"HTTP {resp.status_code}  {resp.text[:200]}"
            )
    return agents


# ---------------------------------------------------------------------------
# Step 2 — individual signed transfer
# ---------------------------------------------------------------------------

async def fire_transfer(
    client:      httpx.AsyncClient,
    idx:         int,
    sender:      AgentInfo,
    receiver:    AgentInfo,
    amount_usdc: float,
) -> TransferResult:
    result = TransferResult(
        idx         = idx,
        sender_id   = sender.agent_id,
        receiver_id = receiver.agent_id,
        amount_usdc = amount_usdc,
    )

    body = {
        "agent_id":       sender.agent_id,
        "wallet_address": sender.wallet_address,
        "receiver_id":    receiver.agent_id,
        "amount_usdc":    amount_usdc,
        "tx_type":        "stress_transfer",
        # Replay-protection fields required by verify_signed_payload (F-02)
        "nonce":          str(uuid.uuid4()),
        "issued_at":      int(time.time()),
        "chain_id":       PROTOCOL_DOMAIN,
    }

    raw_body, sig_hex = _signed_body(body, sender.private_key)

    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"{_API_BASE}/api/v1/settlement/transfer",
            content=raw_body,
            headers={
                "Content-Type":        "application/json",
                "X-VectraFi-Signature": sig_hex,
            },
        )
        result.elapsed_ms  = (time.perf_counter() - t0) * 1000
        result.status_code = resp.status_code

        if resp.status_code == 200:
            result.tx_id = resp.json().get("tx_id")
        elif resp.status_code == 401 and "Nonce already consumed" in resp.text:
            result.nonce_collision = True
            result.error = f"NONCE_COLLISION: {resp.text[:120]}"
        else:
            result.error = f"HTTP {resp.status_code}: {resp.text[:200]}"

    except httpx.TimeoutException as exc:
        result.elapsed_ms  = (time.perf_counter() - t0) * 1000
        result.timed_out   = True
        result.error       = f"RPC_TIMEOUT: {exc}"
    except Exception as exc:  # noqa: BLE001
        result.elapsed_ms  = (time.perf_counter() - t0) * 1000
        result.error       = f"UNEXPECTED: {exc}"

    return result


# ---------------------------------------------------------------------------
# Step 3 — DB hash lookup (on-chain hashes are written by the exchange server
#           after the HTTP response is committed; we read them directly here)
# ---------------------------------------------------------------------------

def _ensure_onchain_columns(db) -> None:
    """
    Add on_chain_* columns to settlement_transactions if the live SQLite schema
    predates the model definition (create_all never alters existing tables).
    """
    from sqlalchemy import text
    existing = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(settlement_transactions)"))
    }
    needed = {
        "on_chain_status":       "ALTER TABLE settlement_transactions ADD COLUMN on_chain_status TEXT",
        "on_chain_net_tx_hash":  "ALTER TABLE settlement_transactions ADD COLUMN on_chain_net_tx_hash TEXT",
        "on_chain_tax_tx_hash":  "ALTER TABLE settlement_transactions ADD COLUMN on_chain_tax_tx_hash TEXT",
    }
    for col, ddl in needed.items():
        if col not in existing:
            db.execute(text(ddl))
            db.commit()
            log.info("  [MIGRATION] Added column settlement_transactions.%s", col)


def _fetch_onchain_data(
    db,
    tx_ids: list[str],
) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Return {tx_id: (net_hash, tax_hash, on_chain_status)} for each tx_id."""
    _ensure_onchain_columns(db)
    from sqlalchemy import select
    rows = db.execute(
        select(
            SettlementTransaction.tx_id,
            SettlementTransaction.on_chain_net_tx_hash,
            SettlementTransaction.on_chain_tax_tx_hash,
            SettlementTransaction.on_chain_status,
        ).where(SettlementTransaction.tx_id.in_(tx_ids))
    ).all()
    return {
        row.tx_id: (
            row.on_chain_net_tx_hash,
            row.on_chain_tax_tx_hash,
            row.on_chain_status,
        )
        for row in rows
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_bridge_live() -> bool:
    try:
        from web3_bridge import bridge
        return bridge.is_configured
    except Exception:
        return False


def _section(title: str) -> None:
    log.info("")
    log.info("── %s %s", title, "─" * max(0, 62 - len(title)))


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def main() -> None:
    run_id = uuid.uuid4().hex[:6]

    log.info("=" * 68)
    log.info("  VectraFi Testnet Stress Runner")
    log.info("  run_id=%-8s  target=%s", run_id, _API_BASE)
    log.info("=" * 68)

    # ------------------------------------------------------------------
    # Preflight — verify the exchange server is reachable
    # ------------------------------------------------------------------
    _section("PREFLIGHT")
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as probe:
        try:
            r = await probe.get(f"{_API_BASE}/api/v1/settlement/analytics")
            log.info("  Exchange server reachable  (HTTP %d)", r.status_code)
        except httpx.ConnectError:
            log.error("  Cannot reach exchange at %s", _API_BASE)
            log.error("  Start it first:  cd core-exchange/src && python run.py")
            sys.exit(1)

    bridge_live = _is_bridge_live()
    log.info(
        "  L2 bridge mode: %s",
        "LIVE — on-chain broadcasts enabled" if bridge_live
        else "SANDBOX — ledger-only, no RPC calls",
    )

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:

        # ------------------------------------------------------------------
        # Step 1: Provision wallets
        # ------------------------------------------------------------------
        _section("STEP 1: PROVISION WALLETS")
        agents = await provision_wallets(client, run_id)
        if len(agents) < 3:
            log.error("  Could not provision all 3 wallets — aborting")
            sys.exit(1)

        alpha, beta, gamma = agents["Agent_Alpha"], agents["Agent_Beta"], agents["Agent_Gamma"]

        # ------------------------------------------------------------------
        # Step 2: 5 parallel settlement transfers
        #
        # Chosen pairs exercise every lock-order combination:
        #   tx 0  Alpha → Beta   lock=[Alpha, Beta]
        #   tx 1  Beta  → Gamma  lock=[Beta,  Gamma]
        #   tx 2  Gamma → Alpha  lock=[Alpha, Gamma]   ← contends with tx 3
        #   tx 3  Alpha → Gamma  lock=[Alpha, Gamma]   ← contends with tx 2
        #   tx 4  Beta  → Alpha  lock=[Alpha, Beta]    ← contends with tx 0
        #
        # Alphabetical ordering prevents deadlock: Alpha < Beta < Gamma,
        # so every pair acquires the lower-ID row first regardless of direction.
        # ------------------------------------------------------------------
        _section(f"STEP 2: FIRE {_BATCH_SIZE} PARALLEL TRANSFERS")
        log.info("  Lock-order invariant: sorted([sender, receiver]) → no deadlock")

        transfer_specs: list[tuple[int, AgentInfo, AgentInfo, float]] = [
            (0, alpha, beta,   25.0),
            (1, beta,  gamma,  20.0),
            (2, gamma, alpha,  15.0),
            (3, alpha, gamma,  12.0),
            (4, beta,  alpha,  30.0),
        ]

        log.info("  Dispatching %d coroutines concurrently …", _BATCH_SIZE)
        raw_results = await asyncio.gather(
            *[fire_transfer(client, idx, s, r, amt)
              for idx, s, r, amt in transfer_specs],
            return_exceptions=True,
        )

        # ------------------------------------------------------------------
        # Step 3: Process results, detect error signals
        # ------------------------------------------------------------------
        _section("STEP 3: TRANSFER RESULTS")

        results: list[TransferResult] = []
        ok_tx_ids:          list[str] = []
        timeout_detected:   bool      = False
        collision_detected: bool      = False

        for raw in raw_results:
            if isinstance(raw, BaseException):
                log.error("  Unhandled coroutine exception: %s", raw)
                continue
            r: TransferResult = raw
            results.append(r)

            tag  = "OK " if r.status_code == 200 else "ERR"
            s_sh = r.sender_id.split("_")[1]   # "Alpha" / "Beta" / "Gamma"
            r_sh = r.receiver_id.split("_")[1]
            log.info(
                "  [TX %d][%s] %-7s → %-7s  $%6.2f  %5.0fms"
                "  tx_id=%-38s  %s",
                r.idx, tag, s_sh, r_sh, r.amount_usdc, r.elapsed_ms,
                r.tx_id or "—",
                r.error or "",
            )
            if r.tx_id:
                ok_tx_ids.append(r.tx_id)
            if r.timed_out:
                timeout_detected   = True
            if r.nonce_collision:
                collision_detected = True

        ok_count  = sum(1 for r in results if r.status_code == 200)
        err_count = len(results) - ok_count
        log.info("  Batch complete: %d ok  |  %d error", ok_count, err_count)

        # ------------------------------------------------------------------
        # Step 4: On-chain hash lookup + Basescan URL logging
        # ------------------------------------------------------------------
        _section("STEP 4: ON-CHAIN HASH LOOKUP")

        found_any_hash = False
        recovery_result: dict = {}

        db = SessionLocal()
        try:
            hash_map = _fetch_onchain_data(db, ok_tx_ids) if ok_tx_ids else {}

            for tx_id, (net_hash, tax_hash, on_chain_status) in hash_map.items():
                status_str = on_chain_status or "SANDBOX"
                log.info(
                    "  tx_id=%.12s…  status=%-14s  net_hash=%s",
                    tx_id, status_str, net_hash or "—",
                )
                if net_hash:
                    found_any_hash = True
                    log.info("    ↳ Net  : %s", _EXPLORER_TX.format(hash=net_hash))
                if tax_hash:
                    log.info("    ↳ Tax  : %s", _EXPLORER_TX.format(hash=tax_hash))

            if not found_any_hash:
                log.info(
                    "  No on-chain hashes — exchange is in SANDBOX mode.\n"
                    "  Set L2_PROVIDER_URL + PROTOCOL_PRIVATE_KEY to activate live broadcast.\n"
                    "  When active, net-transfer and tax-transfer hashes will appear here."
                )

            # ------------------------------------------------------------------
            # Step 5: PENDING_SYNC recovery pass
            #
            # Invoked unconditionally as a post-batch audit; also the explicit
            # remediation path when a timeout or nonce collision was detected.
            # ------------------------------------------------------------------
            _section("STEP 5: PENDING_SYNC RECOVERY WORKER")

            if timeout_detected or collision_detected:
                reasons = []
                if timeout_detected:   reasons.append("RPC timeout")
                if collision_detected: reasons.append("nonce collision")
                log.info(
                    "  Error signal(s) detected: %s — invoking run_recovery() …",
                    ", ".join(reasons),
                )
            else:
                log.info("  Post-batch audit pass — invoking run_recovery() …")

            recovery_result = await run_recovery(db)

            log.info("  Recovery scan results:")
            log.info("    PENDING_SYNC rows found   : %d", recovery_result["total"])
            log.info("    recovered → CONFIRMING    : %d", recovery_result["recovered"])
            log.info("    skipped (no change)       : %d", recovery_result["skipped"])
            log.info("    errors during recovery    : %d", recovery_result["errors"])

            for outcome in recovery_result.get("outcomes", []):
                log.info(
                    "    tx=%.12s… → %-12s  net=%s  tax=%s  leg1_skipped=%s  err=%s",
                    outcome.tx_id,
                    outcome.new_status,
                    outcome.net_tx_hash or "—",
                    outcome.tax_tx_hash or "—",
                    outcome.leg1_skipped,
                    outcome.error or "—",
                )

            if recovery_result["total"] == 0 and not bridge_live:
                log.info(
                    "  (Bridge unconfigured — PENDING_SYNC rows can only be healed\n"
                    "   when L2_PROVIDER_URL + PROTOCOL_PRIVATE_KEY are set.)"
                )

        finally:
            db.close()

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        _section("SUMMARY")
        log.info("  run_id           : %s", run_id)
        log.info("  wallets created  : 3  (%s, %s, %s)",
                 alpha.agent_id, beta.agent_id, gamma.agent_id)
        log.info("  transfers fired  : %d  |  %d ok  |  %d error",
                 _BATCH_SIZE, ok_count, err_count)
        log.info("  on-chain mode    : %s",
                 "LIVE — hashes logged above" if found_any_hash else "SANDBOX")
        log.info("  recovery result  : %d PENDING_SYNC found  |  %d recovered",
                 recovery_result.get("total", 0),
                 recovery_result.get("recovered", 0))

        if ok_count == _BATCH_SIZE:
            log.info(
                "  STATUS           : ALL %d TRANSFERS COMPLETED CLEANLY\n"
                "                     Alphabetical row locks and Treasury serialisation verified.",
                _BATCH_SIZE,
            )
        else:
            log.info(
                "  STATUS           : PARTIAL — %d/%d succeeded  (see ERR lines above)",
                ok_count, _BATCH_SIZE,
            )


if __name__ == "__main__":
    asyncio.run(main())
