#!/usr/bin/env python3
"""
VectraFi Seed Swarm — autonomous multi-agent stress daemon.

Provisions three mock trading desks (Alpha, Beta, Gamma) and runs a
persistent polling loop that:

  1. Checks arbitrage route viability across the full agent chain every ~1 s.
  2. Fires signed settlement transfers between a random desk pair with a
     50–200 ms async jitter to simulate organic parallel trading activity
     and stress-test pessimistic row-locking under persistent concurrent load.
  3. Periodically triggers the rebalance engine when any desk falls below
     its safety floor.
  4. Logs all activity to both stdout and logs/swarm_activity.log.

Usage (exchange server must be running):
    cd core-exchange/src && python run.py            # terminal A
    python core-exchange/src/seed_swarm.py           # terminal B

Environment overrides:
    VECTRAFI_API_URL     — canonical API base; used by both swarm and stress runner
    SWARM_API_BASE       — overrides VECTRAFI_API_URL for this script only (legacy compat)
                           default http://127.0.0.1:8000
    SWARM_DRY_RUN        — set to "1" to skip real transfers (dry-run polling only)
    SWARM_POLL_MS        — polling interval in milliseconds (default 1000)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

# ---------------------------------------------------------------------------
# Path bootstrap — runnable from project root OR from core-exchange/src/
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import DEFAULT_USDC_BALANCE, PROTOCOL_DOMAIN

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_API_BASE    = (
    os.getenv("SWARM_API_BASE")
    or os.getenv("VECTRAFI_API_URL")
    or "http://127.0.0.1:8000"
).rstrip("/")
_DRY_RUN     = os.getenv("SWARM_DRY_RUN", "0") == "1"
_POLL_MS     = int(os.getenv("SWARM_POLL_MS", "1000"))
_JITTER_MIN  = 0.050   # 50 ms
_JITTER_MAX  = 0.200   # 200 ms
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# Safety floor: trigger rebalance when any desk balance drops below this.
_SAFETY_FLOOR_PCT  = 0.005
_TRANSFER_AMOUNT   = 10.0   # USDC per hop — small enough to sustain long runs
_REBALANCE_VOLUME  = 50.0

# Agent identities — fixed prefix so re-runs reuse existing wallets (409 = ok).
_DESK_NAMES = ("Alpha", "Beta", "Gamma")

# ---------------------------------------------------------------------------
# Log setup — file + stdout
# ---------------------------------------------------------------------------
_LOG_DIR  = _THIS_DIR.parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "swarm_activity.log"

_fmt = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

_file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger("swarm")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DeskState:
    name:           str
    agent_id:       str
    wallet_address: str
    private_key:    str
    balance_usdc:   float
    transfers_ok:   int = 0
    transfers_err:  int = 0


@dataclass
class SwarmStats:
    iterations:       int = 0
    route_checks:     int = 0
    viable_routes:    int = 0
    transfers_fired:  int = 0
    transfers_ok:     int = 0
    transfers_err:    int = 0
    rebalances_fired: int = 0
    start_time:       float = field(default_factory=time.perf_counter)

    def elapsed_s(self) -> float:
        return time.perf_counter() - self.start_time

    def summary(self) -> str:
        return (
            f"iter={self.iterations}  "
            f"route_checks={self.route_checks}  viable={self.viable_routes}  "
            f"xfers={self.transfers_ok}/{self.transfers_fired}  "
            f"rebalances={self.rebalances_fired}  "
            f"uptime={self.elapsed_s():.0f}s"
        )


# ---------------------------------------------------------------------------
# Signing helper (identical contract to run_testnet_stress.py)
# ---------------------------------------------------------------------------

def _signed_body(body: dict, private_key: str) -> tuple[bytes, str]:
    compact = json.dumps(body, separators=(",", ":"))
    msg     = encode_defunct(text=compact)
    sig     = Account.sign_message(msg, private_key=private_key)
    return compact.encode("utf-8"), sig.signature.hex()


# ---------------------------------------------------------------------------
# Wallet provisioning
# ---------------------------------------------------------------------------

async def _provision_desk(
    client:  httpx.AsyncClient,
    name:    str,
    swarm_id: str,
) -> DeskState | None:
    agent_id = f"swarm_{name}_{swarm_id}"
    try:
        resp = await client.post(
            f"{_API_BASE}/api/v1/wallet/create",
            json={"agent_id": agent_id},
        )
    except Exception as exc:
        log.error("PROVISION  %-12s  FAILED: %s", name, exc)
        return None

    if resp.status_code in (200, 201):
        d = resp.json()
        log.info(
            "PROVISION  %-12s  agent_id=%-34s  balance=%.2f USDC",
            name, agent_id, d["balance_usdc"],
        )
        return DeskState(
            name=name,
            agent_id=d["agent_id"],
            wallet_address=d["wallet_address"],
            private_key=d["private_key"],
            balance_usdc=d["balance_usdc"],
        )
    elif resp.status_code == 409:
        log.warning("PROVISION  %-12s  409 already exists — cannot retrieve key, skipping", name)
        return None
    else:
        log.error("PROVISION  %-12s  HTTP %d: %s", name, resp.status_code, resp.text[:120])
        return None


async def provision_desks(client: httpx.AsyncClient, swarm_id: str) -> list[DeskState]:
    tasks   = [_provision_desk(client, name, swarm_id) for name in _DESK_NAMES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    desks   = []
    for r in results:
        if isinstance(r, BaseException):
            log.error("PROVISION  Unhandled exception: %s", r)
        elif r is not None:
            desks.append(r)
    return desks


# ---------------------------------------------------------------------------
# Arbitrage route-path check
# ---------------------------------------------------------------------------

async def check_route(
    client: httpx.AsyncClient,
    desks:  list[DeskState],
    stats:  SwarmStats,
) -> bool:
    chain = [d.agent_id for d in desks]
    body  = {
        "entry_asset":           "USDC",
        "exit_asset":            "USDC",
        "volume_usdc":           _TRANSFER_AMOUNT,
        "agent_chain":           chain,
        "slippage_tolerance_pct": _SAFETY_FLOOR_PCT,
    }
    stats.route_checks += 1
    try:
        resp = await client.post(
            f"{_API_BASE}/api/v1/arbitrage/route-path",
            json=body,
        )
        if resp.status_code != 200:
            log.warning("ROUTE-CHECK  HTTP %d: %s", resp.status_code, resp.text[:80])
            return False
        data   = resp.json()
        viable = data.get("viable", False)
        if viable:
            stats.viable_routes += 1
        log.debug(
            "ROUTE-CHECK  viable=%s  chain=%s  slip=%.6f",
            viable,
            " → ".join(d.name for d in desks),
            data.get("total_slippage_usdc", 0),
        )
        return viable
    except httpx.TimeoutException as exc:
        log.warning("ROUTE-CHECK  RPC_TIMEOUT: %s", exc)
        return False
    except Exception as exc:
        log.error("ROUTE-CHECK  UNEXPECTED: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Signed settlement transfer (with async jitter)
# ---------------------------------------------------------------------------

async def fire_transfer(
    client:   httpx.AsyncClient,
    sender:   DeskState,
    receiver: DeskState,
    stats:    SwarmStats,
) -> None:
    # Organic jitter: simulate different desk reaction times
    jitter = random.uniform(_JITTER_MIN, _JITTER_MAX)
    await asyncio.sleep(jitter)

    body = {
        "agent_id":       sender.agent_id,
        "wallet_address": sender.wallet_address,
        "receiver_id":    receiver.agent_id,
        "amount_usdc":    _TRANSFER_AMOUNT,
        "tx_type":        "swarm_transfer",
        "nonce":          str(uuid.uuid4()),
        "issued_at":      int(time.time()),
        "chain_id":       PROTOCOL_DOMAIN,
    }
    raw_body, sig_hex = _signed_body(body, sender.private_key)

    stats.transfers_fired += 1
    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"{_API_BASE}/api/v1/settlement/transfer",
            content=raw_body,
            headers={
                "Content-Type":         "application/json",
                "X-VectraFi-Signature": sig_hex,
            },
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if resp.status_code == 200:
            d = resp.json()
            sender.balance_usdc   = d.get("sender_balance_usdc",   sender.balance_usdc)
            receiver.balance_usdc = d.get("receiver_balance_usdc", receiver.balance_usdc)
            stats.transfers_ok   += 1
            sender.transfers_ok  += 1
            log.info(
                "TRANSFER  %-7s → %-7s  $%5.2f  %5.0fms  OK  "
                "sender_bal=%.2f  receiver_bal=%.2f",
                sender.name, receiver.name, _TRANSFER_AMOUNT, elapsed_ms,
                sender.balance_usdc, receiver.balance_usdc,
            )
        else:
            stats.transfers_err  += 1
            sender.transfers_err += 1
            log.warning(
                "TRANSFER  %-7s → %-7s  $%5.2f  %5.0fms  ERR  HTTP %d: %s",
                sender.name, receiver.name, _TRANSFER_AMOUNT, elapsed_ms,
                resp.status_code, resp.text[:100],
            )

    except httpx.TimeoutException as exc:
        stats.transfers_err  += 1
        sender.transfers_err += 1
        log.warning(
            "TRANSFER  %-7s → %-7s  RPC_TIMEOUT: %s",
            sender.name, receiver.name, exc,
        )
    except Exception as exc:
        stats.transfers_err  += 1
        sender.transfers_err += 1
        log.error(
            "TRANSFER  %-7s → %-7s  UNEXPECTED: %s",
            sender.name, receiver.name, exc,
        )


# ---------------------------------------------------------------------------
# Rebalance trigger
# ---------------------------------------------------------------------------

async def maybe_rebalance(
    client: httpx.AsyncClient,
    desk:   DeskState,
    stats:  SwarmStats,
) -> None:
    body = {
        "target_agent_id":       desk.agent_id,
        "volume_usdc":           _REBALANCE_VOLUME,
        "slippage_tolerance_pct": _SAFETY_FLOOR_PCT,
    }
    stats.rebalances_fired += 1
    try:
        resp = await client.post(f"{_API_BASE}/api/v1/arbitrage/rebalance", json=body)
        if resp.status_code == 200:
            d = resp.json()
            if d.get("rebalanced"):
                desk.balance_usdc = d.get("post_balance_usdc", desk.balance_usdc)
                log.info(
                    "REBALANCE  %-7s  post_balance=%.2f  hops=%d",
                    desk.name, desk.balance_usdc, len(d.get("transactions", [])),
                )
            else:
                log.debug("REBALANCE  %-7s  skipped: %s", desk.name, d.get("rejection_reason"))
        else:
            log.warning("REBALANCE  %-7s  HTTP %d: %s", desk.name, resp.status_code, resp.text[:80])
    except httpx.TimeoutException as exc:
        log.warning("REBALANCE  %-7s  RPC_TIMEOUT: %s", desk.name, exc)
    except Exception as exc:
        log.error("REBALANCE  %-7s  UNEXPECTED: %s", desk.name, exc)


# ---------------------------------------------------------------------------
# Main swarm loop
# ---------------------------------------------------------------------------

async def _post_heartbeat(
    client: httpx.AsyncClient,
    desks:  list[DeskState],
    stats:  SwarmStats,
) -> None:
    """Push swarm state to the dashboard's in-memory store (fire-and-forget)."""
    body = {
        "iterations":    stats.iterations,
        "route_checks":  stats.route_checks,
        "viable_routes": stats.viable_routes,
        "dry_run":       _DRY_RUN,
        "desks": [
            {
                "name":          d.name,
                "balance_usdc":  d.balance_usdc,
                "transfers_ok":  d.transfers_ok,
                "transfers_err": d.transfers_err,
            }
            for d in desks
        ],
    }
    try:
        await client.post(
            f"{_API_BASE}/api/v1/analytics/swarm/heartbeat",
            json=body,
            timeout=httpx.Timeout(3.0),
        )
    except Exception as exc:
        log.debug("HEARTBEAT  send failed (non-fatal): %s", exc)


async def swarm_loop(desks: list[DeskState]) -> None:
    stats = SwarmStats()
    poll_s = _POLL_MS / 1000.0

    log.info("SWARM  started  desks=%d  poll=%dms  dry_run=%s", len(desks), _POLL_MS, _DRY_RUN)
    log.info("SWARM  agents: %s", "  |  ".join(
        f"{d.name}={d.agent_id}" for d in desks
    ))

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        while True:
            loop_start = time.perf_counter()
            stats.iterations += 1

            # 1. Route-path viability check
            viable = await check_route(client, desks, stats)

            if not _DRY_RUN:
                # 2. Fire concurrent transfers with jitter when route is viable
                if viable and len(desks) >= 2:
                    # Pick a random sender/receiver pair from the active desks
                    sender, receiver = random.sample(desks, 2)
                    # Only skip if sender is nearly broke
                    if sender.balance_usdc > _TRANSFER_AMOUNT * 2:
                        await fire_transfer(client, sender, receiver, stats)
                    else:
                        log.debug(
                            "TRANSFER  SKIP  %-7s  balance=%.2f below 2× transfer amount",
                            sender.name, sender.balance_usdc,
                        )

                # 3. Rebalance any desk whose balance has fallen below floor
                for desk in desks:
                    floor = _REBALANCE_VOLUME * _SAFETY_FLOOR_PCT
                    if desk.balance_usdc < floor:
                        await maybe_rebalance(client, desk, stats)

            # 4. Progress heartbeat every 10 iterations
            if stats.iterations % 10 == 0:
                log.info("SWARM  %s", stats.summary())
                for d in desks:
                    log.info(
                        "  DESK  %-7s  balance=%.4f USDC  ok=%d  err=%d",
                        d.name, d.balance_usdc, d.transfers_ok, d.transfers_err,
                    )
                await _post_heartbeat(client, desks, stats)

            # 5. Sleep for the remainder of the poll interval
            elapsed = time.perf_counter() - loop_start
            sleep_s = max(0.0, poll_s - elapsed)
            await asyncio.sleep(sleep_s)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    swarm_id = uuid.uuid4().hex[:6]

    log.info("=" * 68)
    log.info("  VectraFi Seed Swarm")
    log.info("  swarm_id=%-8s  target=%s  log=%s", swarm_id, _API_BASE, _LOG_FILE)
    log.info("=" * 68)

    if _DRY_RUN:
        log.info("  DRY-RUN mode active — transfers will be skipped")

    # Preflight: confirm exchange is reachable
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as probe:
        try:
            r = await probe.get(f"{_API_BASE}/api/v1/settlement/analytics")
            log.info("  Exchange reachable  (HTTP %d)", r.status_code)
        except httpx.ConnectError:
            log.error("  Cannot reach exchange at %s", _API_BASE)
            log.error("  Start it first:  cd core-exchange/src && python run.py")
            sys.exit(1)

    # Provision wallets
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        desks = await provision_desks(client, swarm_id)

    if len(desks) < 2:
        log.error("  Need at least 2 active desks to run — provisioned only %d", len(desks))
        sys.exit(1)

    log.info("  Provisioned %d/%d desks — entering swarm loop …", len(desks), len(_DESK_NAMES))

    try:
        await swarm_loop(desks)
    except KeyboardInterrupt:
        log.info("SWARM  interrupted by user — exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
