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
from services.route_evaluator import (
    compute_top_up,
    is_route_viable_local,
    needs_server_rebalance,
    select_equalization_donor,
    tax_covers_overhead,
)
from services.web3_provider import get_on_chain_eth_balance, init_web3_provider, is_live_mode

# ---------------------------------------------------------------------------
# HD Wallet derivation (BIP-44, coin type 60 — Ethereum)
# ---------------------------------------------------------------------------
_SWARM_SEED_PHRASE: str | None = os.getenv("SWARM_SEED_PHRASE") or None

# Standard BIP-44 Ethereum paths, one per desk.
_DESK_HD_PATHS: dict[str, str] = {
    "Alpha": "m/44'/60'/0'/0/0",
    "Beta":  "m/44'/60'/0'/0/1",
    "Gamma": "m/44'/60'/0'/0/2",
}

if _SWARM_SEED_PHRASE:
    # Gated behind an explicit opt-in as required by the eth-account library.
    Account.enable_unaudited_hdwallet_features()

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

# ---------------------------------------------------------------------------
# Circuit-breaker thresholds (env-overridable)
# ---------------------------------------------------------------------------
# Maximum consecutive transfer errors for a single desk before emergency halt.
_CB_MAX_CONSECUTIVE_ERRORS: int   = int(os.getenv("SWARM_CB_MAX_ERRORS",       "5"))
# Halt if any desk's balance falls below this fraction of its initial capital.
_CB_MIN_BALANCE_PCT:        float = float(os.getenv("SWARM_CB_MIN_BALANCE_PCT", "0.80"))
# Minimum ETH gas balance per desk wallet before live transfers are allowed to fire.
# ~0.002 ETH ≈ 300k gas units on Base Sepolia; env-overridable via SWARM_CB_MIN_GAS_ETH.
_CB_MIN_GAS_ETH:            float = float(os.getenv("SWARM_CB_MIN_GAS_ETH",    "0.002"))

# ---------------------------------------------------------------------------
# Autonomous desk equalization thresholds (env-overridable)
# ---------------------------------------------------------------------------
# A desk whose USDC balance falls below _EQ_STALL_THRESHOLD_USDC is considered
# stalled and will receive a direct top-up from the richest eligible donor desk
# before the next routing iteration begins.  The donor must hold enough surplus
# to bring the stalled desk to _EQ_TARGET_USDC while staying above that target
# itself, AND must have a cached ETH balance of at least
# _CB_MIN_GAS_ETH * _EQ_GAS_SAFETY_MULT so the equalization transfer itself
# cannot drain the donor's gas cushion below the circuit-breaker floor.
_EQ_STALL_THRESHOLD_USDC: float = float(os.getenv("SWARM_EQ_STALL_USDC", str(_TRANSFER_AMOUNT * 3)))
_EQ_TARGET_USDC:          float = float(os.getenv("SWARM_EQ_TARGET_USDC", str(_TRANSFER_AMOUNT * 10)))
_EQ_GAS_SAFETY_MULT:      float = float(os.getenv("SWARM_EQ_GAS_MULT",    "2.0"))

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
    name:                str
    agent_id:            str
    wallet_address:      str
    private_key:         str
    balance_usdc:        float
    initial_balance_usdc: float = 0.0  # set at provision; used by min-balance guard
    transfers_ok:        int = 0
    transfers_err:       int = 0
    consecutive_errors:  int = 0       # reset on success; circuit-breaker trips at threshold
    eth_balance:         float = 0.0   # cached by _check_gas_guard; used by equalization donor vetting


@dataclass
class SwarmStats:
    iterations:          int = 0
    route_checks:        int = 0
    route_checks_local:  int = 0   # short-circuited by local pre-check (no HTTP call)
    viable_routes:       int = 0
    transfers_fired:     int = 0
    transfers_ok:        int = 0
    transfers_err:       int = 0
    rebalances_fired:    int = 0
    equalization_count:  int = 0
    equalization_volume: float = 0.0
    start_time:          float = field(default_factory=time.perf_counter)

    def elapsed_s(self) -> float:
        return time.perf_counter() - self.start_time

    def summary(self) -> str:
        saved_pct = (
            100.0 * self.route_checks_local / max(1, self.route_checks + self.route_checks_local)
        )
        return (
            f"iter={self.iterations}  "
            f"route_checks={self.route_checks}  viable={self.viable_routes}  "
            f"local_skip={self.route_checks_local} ({saved_pct:.0f}% overhead saved)  "
            f"xfers={self.transfers_ok}/{self.transfers_fired}  "
            f"eq={self.equalization_count}  "
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
    client:          httpx.AsyncClient,
    name:            str,
    swarm_id:        str,
    derived_address: str | None = None,
    derived_key:     str | None = None,
) -> DeskState | None:
    agent_id   = f"swarm_{name}_{swarm_id}"
    create_body: dict = {"agent_id": agent_id}
    if derived_address is not None:
        create_body["wallet_address"] = derived_address

    try:
        resp = await client.post(
            f"{_API_BASE}/api/v1/wallet/create",
            json=create_body,
        )
    except Exception as exc:
        log.error("PROVISION  %-12s  FAILED: %s", name, exc)
        return None

    if resp.status_code in (200, 201):
        d           = resp.json()
        # Use derived values when available; fall back to what the server generated.
        wallet_addr = derived_address if derived_address is not None else d["wallet_address"]
        private_key = derived_key     if derived_key     is not None else d["private_key"]
        initial_bal = float(d["balance_usdc"])
        log.info(
            "PROVISION  %-12s  agent_id=%-34s  address=%s  balance=%.2f USDC%s",
            name, agent_id, wallet_addr, initial_bal,
            "  [HD]" if derived_key is not None else "",
        )
        return DeskState(
            name=name,
            agent_id=d["agent_id"],
            wallet_address=wallet_addr,
            private_key=private_key,
            balance_usdc=initial_bal,
            initial_balance_usdc=initial_bal,
        )
    elif resp.status_code == 409:
        if derived_key is not None:
            # HD mode: wallet already registered from a previous run — reconstruct
            # from the derived key.  Balance is unknown until the first transfer
            # response updates it; set to DEFAULT so the swarm loop can start.
            log.info(
                "PROVISION  %-12s  409 (HD reuse) — reconstructing from derived key  address=%s",
                name, derived_address,
            )
            return DeskState(
                name=name,
                agent_id=agent_id,
                wallet_address=derived_address or "",
                private_key=derived_key,
                balance_usdc=DEFAULT_USDC_BALANCE,
                initial_balance_usdc=DEFAULT_USDC_BALANCE,
            )
        log.warning("PROVISION  %-12s  409 already exists — cannot retrieve key, skipping", name)
        return None
    else:
        log.error("PROVISION  %-12s  HTTP %d: %s", name, resp.status_code, resp.text[:120])
        return None


async def provision_desks(client: httpx.AsyncClient, swarm_id: str) -> list[DeskState]:
    tasks: list = []
    for name in _DESK_NAMES:
        derived_address: str | None = None
        derived_key:     str | None = None
        if _SWARM_SEED_PHRASE:
            acct            = Account.from_mnemonic(
                _SWARM_SEED_PHRASE, account_path=_DESK_HD_PATHS[name]
            )
            derived_address = acct.address
            raw_key         = acct.key.hex()
            derived_key     = raw_key if raw_key.startswith("0x") else f"0x{raw_key}"
        tasks.append(_provision_desk(client, name, swarm_id, derived_address, derived_key))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    desks: list[DeskState] = []
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
    """
    Two-stage route viability check (Cognitive Token Cost Throttling).

    Stage 1 — local pre-check (O(n), zero I/O):
        Uses cached desk.balance_usdc values to detect obviously non-viable
        routes before making any HTTP call.  When any desk is provably below
        the per-leg cost floor the HTTP call is skipped entirely and the
        result is recorded as a locally-resolved check.

    Stage 2 — authoritative API call (HTTP, SQLAlchemy, PENDING_SYNC check):
        Executed only when the local pre-check is inconclusive (all balances
        appear sufficient).  This is the authoritative verdict and verifies
        live DB balances and PENDING_SYNC locks that the cache cannot see.

    The ratio of local_skip to total checks directly measures overhead
    savings: every skipped HTTP call avoids ~10 ms of API overhead.
    """
    # Stage 1: local pre-check using cached desk balances.
    balances = [d.balance_usdc for d in desks]
    if not is_route_viable_local(balances, _TRANSFER_AMOUNT, _SAFETY_FLOOR_PCT):
        stats.route_checks_local += 1
        log.debug(
            "ROUTE-CHECK  LOCAL-SKIP  chain=%s  (balance pre-check failed — no HTTP call)",
            " → ".join(d.name for d in desks),
        )
        return False

    # Stage 2: authoritative HTTP check — only reached when local state is plausible.
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
    client:          httpx.AsyncClient,
    sender:          DeskState,
    receiver:        DeskState,
    stats:           SwarmStats,
    amount_override: float | None = None,
) -> None:
    """Execute one signed settlement transfer.

    amount_override: when supplied (equalization path), use this exact USDC
    amount and skip organic jitter — the transfer is deliberate, not simulated
    trading activity.  When None, uses _TRANSFER_AMOUNT with jitter (normal hop).
    """
    transfer_amount = amount_override if amount_override is not None else _TRANSFER_AMOUNT
    tx_type         = "swarm_equalization" if amount_override is not None else "swarm_transfer"

    # Skip jitter for equalization transfers; add it for organic trading hops.
    if amount_override is None:
        await asyncio.sleep(random.uniform(_JITTER_MIN, _JITTER_MAX))

    body = {
        "agent_id":       sender.agent_id,
        "wallet_address": sender.wallet_address,
        "receiver_id":    receiver.agent_id,
        "amount_usdc":    transfer_amount,
        "tx_type":        tx_type,
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
            stats.transfers_ok       += 1
            sender.transfers_ok      += 1
            sender.consecutive_errors = 0   # clear streak on success
            log.info(
                "TRANSFER  %-7s → %-7s  $%5.2f  %5.0fms  OK  [%s]  "
                "sender_bal=%.2f  receiver_bal=%.2f",
                sender.name, receiver.name, transfer_amount, elapsed_ms, tx_type,
                sender.balance_usdc, receiver.balance_usdc,
            )
        else:
            stats.transfers_err       += 1
            sender.transfers_err      += 1
            sender.consecutive_errors += 1
            log.warning(
                "TRANSFER  %-7s → %-7s  $%5.2f  %5.0fms  ERR  [%s]  HTTP %d: %s  "
                "(consecutive_errors=%d)",
                sender.name, receiver.name, transfer_amount, elapsed_ms, tx_type,
                resp.status_code, resp.text[:100], sender.consecutive_errors,
            )

    except httpx.TimeoutException as exc:
        stats.transfers_err       += 1
        sender.transfers_err      += 1
        sender.consecutive_errors += 1
        log.warning(
            "TRANSFER  %-7s → %-7s  RPC_TIMEOUT: %s  (consecutive_errors=%d)",
            sender.name, receiver.name, exc, sender.consecutive_errors,
        )
    except Exception as exc:
        stats.transfers_err       += 1
        sender.transfers_err      += 1
        sender.consecutive_errors += 1
        log.error(
            "TRANSFER  %-7s → %-7s  UNEXPECTED: %s  (consecutive_errors=%d)",
            sender.name, receiver.name, exc, sender.consecutive_errors,
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
# Autonomous desk equalization
# ---------------------------------------------------------------------------

async def _equalize_stalled_desks(
    client: httpx.AsyncClient,
    desks:  list[DeskState],
    stats:  SwarmStats,
) -> None:
    """
    Pre-iteration capital equalization pass — fully programmatic (no HTTP reads).

    Uses route_evaluator helpers to make all decisions in O(n) pure Python
    over cached DeskState values:
      - compute_top_up()              → determine whether a desk is stalled
      - select_equalization_donor()   → pick best donor using cached eth_balance
      - tax_covers_overhead()         → guard: skip if tax < transfer cost

    No additional RPC or API calls are made for the decision phase.  Only
    fire_transfer() itself causes an HTTP call (the actual settlement POST),
    and only when all local checks pass.

    Equalization transfers are tracked in stats (equalization_count /
    equalization_volume) so the telemetry dashboard can surface them
    separately from organic swarm_transfer activity.
    """
    gas_floor = _CB_MIN_GAS_ETH * _EQ_GAS_SAFETY_MULT
    live      = is_live_mode() and not _DRY_RUN

    for desk in desks:
        top_up = compute_top_up(
            desk.balance_usdc, _EQ_STALL_THRESHOLD_USDC, _EQ_TARGET_USDC
        )
        if top_up is None:
            continue  # not stalled

        # Guard: equalization only makes economic sense when the protocol tax
        # collected on the top-up transfer exceeds the cost of the API call.
        # A top-up below this threshold is deferred to the server-side rebalance.
        if not tax_covers_overhead(top_up, estimated_overhead_usdc=0.001):
            log.debug(
                "EQUALIZE  %-7s  top_up=%.4f USDC too small — tax < overhead, deferring",
                desk.name, top_up,
            )
            continue

        donor = select_equalization_donor(
            desks, desk, top_up, _EQ_TARGET_USDC, gas_floor, live
        )

        if donor is None:
            log.warning(
                "EQUALIZE  %-7s  stalled at %.2f USDC — no eligible donor "
                "(need donor_usdc > %.2f and%s eth_balance >= %.6f ETH)",
                desk.name, desk.balance_usdc,
                _EQ_TARGET_USDC + top_up,
                "" if live else " [gas check bypassed in sandbox]",
                gas_floor,
            )
            continue

        log.info(
            "EQUALIZE  %-7s  balance=%.2f < threshold=%.2f — "
            "requesting %.2f USDC top-up from %-7s (donor_bal=%.2f  donor_eth=%.6f)",
            desk.name, desk.balance_usdc, _EQ_STALL_THRESHOLD_USDC,
            top_up, donor.name, donor.balance_usdc, donor.eth_balance,
        )
        await fire_transfer(client, donor, desk, stats, amount_override=top_up)
        stats.equalization_count  += 1
        stats.equalization_volume += top_up


# ---------------------------------------------------------------------------
# Main swarm loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

class CircuitBreakerTripped(Exception):
    """Raised when a safety guard threshold is breached; causes sys.exit(1)."""


def _check_circuit_breakers(desks: list[DeskState]) -> None:
    """
    Evaluate both guards for every active desk.
    Logs a CRITICAL alert and raises CircuitBreakerTripped on the first breach.
    """
    for desk in desks:
        # Guard 1 — max consecutive errors
        if desk.consecutive_errors >= _CB_MAX_CONSECUTIVE_ERRORS:
            log.critical(
                "CIRCUIT-BREAKER  MAX-ERRORS  %-7s  "
                "consecutive_errors=%d >= threshold=%d — EMERGENCY SHUTDOWN",
                desk.name, desk.consecutive_errors, _CB_MAX_CONSECUTIVE_ERRORS,
            )
            raise CircuitBreakerTripped(
                f"desk {desk.name} hit {desk.consecutive_errors} consecutive errors"
            )

        # Guard 2 — minimum balance (only meaningful once initial balance is known)
        if desk.initial_balance_usdc > 0:
            floor = desk.initial_balance_usdc * _CB_MIN_BALANCE_PCT
            if desk.balance_usdc < floor:
                log.critical(
                    "CIRCUIT-BREAKER  MIN-BALANCE  %-7s  "
                    "balance=%.4f USDC < floor=%.4f (%.0f%% of initial %.4f) — EMERGENCY SHUTDOWN",
                    desk.name, desk.balance_usdc, floor,
                    _CB_MIN_BALANCE_PCT * 100, desk.initial_balance_usdc,
                )
                raise CircuitBreakerTripped(
                    f"desk {desk.name} balance {desk.balance_usdc:.4f} USDC "
                    f"below {_CB_MIN_BALANCE_PCT:.0%} minimum"
                )


async def _check_gas_guard(desks: list[DeskState]) -> None:
    """
    Guard 3 — minimum ETH gas balance per desk wallet (live RPC mode only).

    Skips silently in dry-run mode or when no RPC provider is connected.
    Runs get_on_chain_eth_balance via run_in_executor so the synchronous
    Web3 HTTP call does not block the async event loop.
    Raises CircuitBreakerTripped if any desk wallet is below _CB_MIN_GAS_ETH.
    """
    if _DRY_RUN or not is_live_mode():
        return

    loop = asyncio.get_event_loop()
    for desk in desks:
        eth_bal: float = await loop.run_in_executor(
            None, get_on_chain_eth_balance, desk.wallet_address
        )
        desk.eth_balance = eth_bal  # cache for equalization donor gas vetting

        log.debug(
            "GAS-GUARD  %-7s  eth_balance=%.6f ETH  threshold=%.6f ETH",
            desk.name, eth_bal, _CB_MIN_GAS_ETH,
        )

        if eth_bal < _CB_MIN_GAS_ETH:
            log.critical(
                "CIRCUIT-BREAKER  MIN-GAS  %-7s  "
                "eth_balance=%.6f ETH < threshold=%.6f ETH — EMERGENCY SHUTDOWN",
                desk.name, eth_bal, _CB_MIN_GAS_ETH,
            )
            raise CircuitBreakerTripped(
                f"desk {desk.name} ETH gas balance {eth_bal:.6f} ETH "
                f"below minimum {_CB_MIN_GAS_ETH:.6f} ETH"
            )


async def _post_heartbeat(
    client: httpx.AsyncClient,
    desks:  list[DeskState],
    stats:  SwarmStats,
) -> None:
    """Push swarm state to the dashboard's in-memory store (fire-and-forget)."""
    body = {
        "iterations":               stats.iterations,
        "route_checks":             stats.route_checks,
        "viable_routes":            stats.viable_routes,
        "dry_run":                  _DRY_RUN,
        "equalization_count":       stats.equalization_count,
        "equalization_volume_usdc": stats.equalization_volume,
        "desks": [
            {
                "name":          d.name,
                "balance_usdc":  d.balance_usdc,
                "transfers_ok":  d.transfers_ok,
                "transfers_err": d.transfers_err,
                "eth_balance":   d.eth_balance,
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

    # Mandatory preflight gas check — must pass before the first transfer can fire.
    # Mirrors the in-loop guard so any RPC or unexpected error halts cleanly.
    try:
        await _check_gas_guard(desks)
    except CircuitBreakerTripped:
        raise
    except Exception as exc:
        log.critical(
            "GAS-GUARD  preflight unexpected error — treating as emergency halt: %s", exc
        )
        raise CircuitBreakerTripped(
            f"gas guard preflight raised unexpectedly: {exc}"
        ) from exc

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        while True:
            loop_start = time.perf_counter()
            stats.iterations += 1

            # 0. Equalization pass — top up stalled desks before routing so no
            #    iteration fires with an under-capitalised desk in the chain.
            if not _DRY_RUN:
                await _equalize_stalled_desks(client, desks, stats)

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
                # needs_server_rebalance() is an O(1) pure-Python guard that
                # avoids the HTTP call when the desk is clearly above threshold.
                for desk in desks:
                    if needs_server_rebalance(
                        desk.balance_usdc, _REBALANCE_VOLUME, _SAFETY_FLOOR_PCT
                    ):
                        await maybe_rebalance(client, desk, stats)

            # 4. Progress heartbeat every 10 iterations
            if stats.iterations % 10 == 0:
                log.info("SWARM  %s", stats.summary())
                for d in desks:
                    log.info(
                        "  DESK  %-7s  balance=%.4f USDC  ok=%d  err=%d  consec_err=%d",
                        d.name, d.balance_usdc, d.transfers_ok, d.transfers_err,
                        d.consecutive_errors,
                    )
                await _post_heartbeat(client, desks, stats)
                try:
                    await _check_gas_guard(desks)
                except CircuitBreakerTripped:
                    raise
                except Exception as exc:
                    log.critical(
                        "GAS-GUARD  unexpected error — treating as emergency halt: %s", exc
                    )
                    raise CircuitBreakerTripped(
                        f"gas guard raised unexpectedly: {exc}"
                    ) from exc

            # 5. Circuit-breaker evaluation (every iteration)
            try:
                _check_circuit_breakers(desks)
            except CircuitBreakerTripped:
                raise
            except Exception as exc:
                log.critical(
                    "CIRCUIT-BREAKER  unexpected error — treating as emergency halt: %s", exc
                )
                raise CircuitBreakerTripped(
                    f"circuit breaker check raised unexpectedly: {exc}"
                ) from exc

            # 6. Sleep for the remainder of the poll interval
            elapsed = time.perf_counter() - loop_start
            sleep_s = max(0.0, poll_s - elapsed)
            await asyncio.sleep(sleep_s)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    # In HD mode, agent IDs must be stable across runs so the existing wallets
    # are reused (409 → reconstruct from derived key).  Random ID is fine for
    # ephemeral sandbox runs where wallet recovery is not required.
    swarm_id = "hd" if _SWARM_SEED_PHRASE else uuid.uuid4().hex[:6]

    log.info("=" * 68)
    log.info("  VectraFi Seed Swarm")
    log.info("  swarm_id=%-8s  target=%s  log=%s", swarm_id, _API_BASE, _LOG_FILE)
    log.info("=" * 68)

    if _SWARM_SEED_PHRASE:
        log.info("  HD wallet mode — deterministic BIP-44 keys from SWARM_SEED_PHRASE")
        log.info("  Desk paths: Alpha=%s  Beta=%s  Gamma=%s",
                 _DESK_HD_PATHS["Alpha"], _DESK_HD_PATHS["Beta"], _DESK_HD_PATHS["Gamma"])
    if _DRY_RUN:
        log.info("  DRY-RUN mode active — transfers will be skipped")

    # Initialise the Web3 provider for this process so that is_live_mode() and
    # _check_gas_guard() reflect the actual RPC config.  The API server does this
    # inside init_db(); the swarm runs as a separate process and must do it here.
    init_web3_provider()
    log.info("  Web3 mode: %s", "live_rpc" if is_live_mode() else "sandbox")

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
    except CircuitBreakerTripped as exc:
        log.critical("SWARM  EMERGENCY HALT — circuit breaker tripped: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
