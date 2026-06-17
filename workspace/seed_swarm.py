#!/usr/bin/env python3
"""
VectraFi Seed Swarm
===================
Three concurrent async worker agents that drive the settlement engine
under continuous high-frequency load:

  Alpha  (Requester)   — builds bounties, locks capital via bank/deposit
  Beta   (Operator)    — watches queue, passes AST gate, claims payout
  Gamma  (Arbitrageur) — high-frequency token-lease loops via LeaseTerms

Signing:  local EIP-191 keypairs  (never stored, memory-only)
Nonces:   monotonically increasing per-agent counter  (replay-safe)
Target:   VECTRAFI_TARGET_URL env var, default http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# ANSI colour palette
# ---------------------------------------------------------------------------

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
CYAN    = "\033[96m"
WHITE   = "\033[97m"

# ---------------------------------------------------------------------------
# Runtime constants
# ---------------------------------------------------------------------------

TARGET_URL  = os.getenv("VECTRAFI_TARGET_URL", "http://localhost:8000")
CHAIN_ID    = "vectrafi-sandbox-v1"
TAX_RATE    = 0.015        # 1.5 % protocol micro-tax
TICK_S      = 0.8          # base delay between loop iterations

# ---------------------------------------------------------------------------
# Path bootstrap — import AST gate from run_loop without exec_module
# ---------------------------------------------------------------------------

_WORKSPACE = Path(__file__).resolve().parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from run_loop import _scan_test_ast  # noqa: E402  (intentional: after sys.path patch)

# ---------------------------------------------------------------------------
# LeaseTerms — mirrors swarm_orchestrator.py; validates cross-agent data as
# pure JSON rather than executing untrusted Python modules.
# ---------------------------------------------------------------------------

TimeUnit = Literal["hour", "day", "week"]

_SECONDS: dict[str, int] = {"hour": 3_600, "day": 86_400, "week": 604_800}


class LeaseTerms(BaseModel):
    model_config = {"frozen": True}

    principal: float = Field(..., gt=0)
    duration_units: int = Field(..., gt=0)
    time_unit: TimeUnit = "hour"
    micro_tax_rate: float = Field(..., gt=0, lt=1)
    total_fee: float = Field(default=0.0)
    expiry_epoch: int = Field(default=0)

    @model_validator(mode="before")
    @classmethod
    def _compute_derived(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            p  = float(data.get("principal", 0))
            d  = int(data.get("duration_units", 0))
            r  = float(data.get("micro_tax_rate", 0))
            tu = str(data.get("time_unit", "hour"))
            data["total_fee"]    = round(p * r * d, 8)
            data["expiry_epoch"] = int(time.time()) + d * _SECONDS.get(tu, 3_600)
        return data


def _build_lease(
    principal: float,
    duration: int = 1,
    unit: TimeUnit = "hour",
    rate: float = TAX_RATE,
) -> LeaseTerms:
    """Validate lease terms via JSON round-trip (no Python object coercion)."""
    payload = json.dumps({
        "principal":      principal,
        "duration_units": duration,
        "time_unit":      unit,
        "micro_tax_rate": rate,
    })
    return LeaseTerms.model_validate_json(payload)


# ---------------------------------------------------------------------------
# Agent identity & signing
# ---------------------------------------------------------------------------

@dataclass
class AgentKey:
    """Holds an ephemeral EIP-191 keypair and a per-agent monotonic nonce counter."""

    agent_id:    str
    address:     str
    private_key: str
    _nonce_idx: int = field(default=0, repr=False)

    def next_nonce(self) -> str:
        self._nonce_idx += 1
        return f"{self.agent_id}-{self._nonce_idx:010d}"

    def sign(self, body: dict) -> tuple[str, str]:
        """
        Inject F-02 replay-protection fields (nonce / issued_at / chain_id),
        serialise to compact JSON, sign with EIP-191, return (body_text, sig_hex).
        The body_text is the exact bytes that will be sent — same string used to
        produce the digest so the server's recover_message call matches.
        """
        payload = dict(body)
        payload["nonce"]     = self.next_nonce()
        payload["issued_at"] = int(time.time())
        payload["chain_id"]  = CHAIN_ID
        body_text = json.dumps(payload, separators=(",", ":"))
        msg = encode_defunct(text=body_text)
        signed = Account.sign_message(msg, private_key=self.private_key)
        return body_text, signed.signature.hex()


# ---------------------------------------------------------------------------
# Structured colour logs
# ---------------------------------------------------------------------------

def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def _log(colour: str, label: str, msg: str) -> None:
    print(f"{DIM}[{_ts()}]{RESET} {colour}{BOLD}{label:12}{RESET} {msg}")


def _log_sign(label: str, agent_id: str, address: str, nonce: str) -> None:
    _log(
        YELLOW, "SIGNING",
        f"{label}  agent={agent_id}  addr={address[:12]}...  nonce={nonce}",
    )


def _log_tax(gross: float, tax: float) -> None:
    pct = (tax / gross * 100) if gross else 0.0
    _log(
        RED, "MICRO-TAX",
        f"gross={gross:.4f} USDC  tax={tax:.6f} USDC  ({pct:.2f}%  rate={TAX_RATE*100:.1f}%)",
    )


def _log_ast(passed: bool, reason: str) -> None:
    status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    _log(BLUE, "AST GATE", f"{status} — {reason}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _post(
    client: httpx.AsyncClient,
    path: str,
    body_text: str,
    sig_hex: str,
) -> dict:
    resp = await client.post(
        f"{TARGET_URL}{path}",
        content=body_text.encode(),
        headers={
            "Content-Type":        "application/json",
            "X-VectraFi-Signature": sig_hex,
        },
    )
    return resp.json()


async def _create_wallet(client: httpx.AsyncClient, agent_id: str) -> dict:
    resp = await client.post(
        f"{TARGET_URL}/api/v1/wallet/create",
        json={"agent_id": agent_id},
    )
    return resp.json()


# ---------------------------------------------------------------------------
# Signed payload builders
# ---------------------------------------------------------------------------

def build_transfer_payload(
    key: AgentKey,
    receiver_id: str,
    amount_usdc: float,
    tx_type: str = "lease_payment",
) -> tuple[str, str]:
    """Build and sign a SettlementTransferRequest payload envelope."""
    nonce_preview = f"{key.agent_id}-{key._nonce_idx + 1:010d}"
    _log_sign("TRANSFER", key.agent_id, key.address, nonce_preview)
    body_text, sig_hex = key.sign({
        "agent_id":       key.agent_id,
        "wallet_address": key.address,
        "receiver_id":    receiver_id,
        "amount_usdc":    amount_usdc,
        "tx_type":        tx_type,
    })
    _log(YELLOW, "SIGNED", f"transfer  {key.agent_id} -> {receiver_id}  {amount_usdc:.4f} USDC")
    _log_tax(amount_usdc, amount_usdc * TAX_RATE)
    return body_text, sig_hex


def build_bounty_claim_payload(
    key: AgentKey,
    counterpart_id: str,
    bounty_amount_usdc: float,
    counterpart_share_pct: float = 0.35,
) -> tuple[str, str]:
    """Build and sign a BountyClaimRequest payload envelope."""
    nonce_preview = f"{key.agent_id}-{key._nonce_idx + 1:010d}"
    _log_sign("CLAIM", key.agent_id, key.address, nonce_preview)
    body_text, sig_hex = key.sign({
        "agent_id":              key.agent_id,
        "wallet_address":        key.address,
        "counterpart_id":        counterpart_id,
        "bounty_amount_usdc":    bounty_amount_usdc,
        "counterpart_share_pct": counterpart_share_pct,
    })
    split_usdc = bounty_amount_usdc * counterpart_share_pct
    _log(GREEN, "SIGNED", (
        f"bounty claim  {key.agent_id} -> {counterpart_id}  "
        f"gross={bounty_amount_usdc:.4f} USDC  counterpart_split={split_usdc:.4f}"
    ))
    _log_tax(split_usdc, split_usdc * TAX_RATE)
    return body_text, sig_hex


def _build_deposit_payload(key: AgentKey, amount_usdc: float) -> tuple[str, str]:
    nonce_preview = f"{key.agent_id}-{key._nonce_idx + 1:010d}"
    _log_sign("DEPOSIT", key.agent_id, key.address, nonce_preview)
    body_text, sig_hex = key.sign({
        "agent_id":       key.agent_id,
        "wallet_address": key.address,
        "amount_usdc":    amount_usdc,
    })
    _log(CYAN, "SIGNED", f"deposit/escrow  {key.agent_id}  {amount_usdc:.4f} USDC")
    _log_tax(amount_usdc, amount_usdc * TAX_RATE)
    return body_text, sig_hex


# ---------------------------------------------------------------------------
# AST gate helper — Beta generates a compliance test script and validates it
# ---------------------------------------------------------------------------

_BETA_PAYLOAD_SCRIPT = '''\
"""
Agent Beta payload compliance script.
Pure definitions and allowed imports only — passes all four AST gate rules.
"""
import json
from decimal import Decimal


class PayloadValidator:
    def __init__(self, agent_id: str, amount: float) -> None:
        self.agent_id = agent_id
        self.amount = Decimal(str(amount))

    def build(self) -> dict:
        return {"agent_id": self.agent_id, "amount_usdc": float(self.amount)}


def validate_payload(data: dict) -> bool:
    return "agent_id" in data and "amount_usdc" in data


def test_payload_builder():
    pv = PayloadValidator("beta-compliance", 50.0)
    result = pv.build()
    assert validate_payload(result)
    assert result["agent_id"] == "beta-compliance"
    assert isinstance(result["amount_usdc"], float)


def test_amount_precision():
    pv = PayloadValidator("beta-compliance", 0.1 + 0.2)
    result = pv.build()
    assert result["amount_usdc"] > 0
    encoded = json.dumps(result)
    parsed = json.loads(encoded)
    assert parsed["amount_usdc"] == result["amount_usdc"]
'''


def _ast_gate_check() -> tuple[bool, str]:
    """Write Beta's payload script to a temp draft, run _scan_test_ast, clean up."""
    drafts = _WORKSPACE / "drafts"
    drafts.mkdir(exist_ok=True)
    tmp_path = drafts / f"_beta_gate_{uuid.uuid4().hex}.py"
    tmp_path.write_text(_BETA_PAYLOAD_SCRIPT, encoding="utf-8")
    try:
        return _scan_test_ast(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_BOUNTY_AMOUNTS = [25.0, 40.0, 15.0, 60.0, 30.0, 50.0, 20.0]
_LEASE_AMOUNTS  = [5.0, 8.0, 3.5, 12.0, 7.5, 10.0, 4.0]

# ---------------------------------------------------------------------------
# Worker: Agent Alpha — Requester
# ---------------------------------------------------------------------------

async def worker_alpha(
    key: AgentKey,
    bounty_queue: asyncio.Queue,
    iterations: int = 0,
) -> None:
    """Continually builds bounties, locks capital into escrow via bank/deposit."""
    _log(CYAN, "ALPHA START", f"agent_id={key.agent_id}  addr={key.address}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        i = 0
        while True:
            i += 1
            amount    = _BOUNTY_AMOUNTS[i % len(_BOUNTY_AMOUNTS)]
            bounty_id = f"bounty-{key.agent_id}-{i:06d}"
            _log(CYAN, "ALPHA", f"[{i}] posting bounty {bounty_id}  escrow={amount:.2f} USDC")
            try:
                body_text, sig_hex = _build_deposit_payload(key, amount)
                resp = await _post(client, "/api/v1/bank/deposit", body_text, sig_hex)
                if "net_deposited_usdc" in resp:
                    _log(CYAN, "ALPHA", (
                        f"[{i}] escrow confirmed  "
                        f"net={resp['net_deposited_usdc']:.4f} USDC  "
                        f"mode={resp.get('execution_mode','?')}"
                    ))
                else:
                    _log(RED, "ALPHA", f"[{i}] deposit error: {resp}")
            except httpx.RequestError as exc:
                _log(RED, "ALPHA", f"[{i}] network error: {exc}")

            await bounty_queue.put({"bounty_id": bounty_id, "amount": amount, "creator_id": key.agent_id})
            await asyncio.sleep(TICK_S * 2.5)
            if iterations and i >= iterations:
                _log(CYAN, "ALPHA", f"reached {iterations} iterations — stopping")
                break


# ---------------------------------------------------------------------------
# Worker: Agent Beta — Operator
# ---------------------------------------------------------------------------

async def worker_beta(
    key: AgentKey,
    bounty_queue: asyncio.Queue,
    alpha_id: str,
    iterations: int = 0,
) -> None:
    """Watches bounty queue, validates AST gate, claims payout cryptographically."""
    _log(GREEN, "BETA START", f"agent_id={key.agent_id}  addr={key.address}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        i = 0
        while True:
            try:
                bounty = bounty_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(TICK_S * 0.5)
                continue

            i += 1
            _log(GREEN, "BETA", (
                f"[{i}] dequeued {bounty['bounty_id']}  amount={bounty['amount']:.2f} USDC"
            ))

            # AST gate: Beta writes and validates a payload compliance script
            passed, reason = _ast_gate_check()
            _log_ast(passed, reason)

            if not passed:
                _log(RED, "BETA", f"[{i}] AST gate rejected — skipping claim")
                bounty_queue.task_done()
                continue

            _log(GREEN, "BETA", f"[{i}] AST gate passed — submitting bounty claim")
            try:
                body_text, sig_hex = build_bounty_claim_payload(
                    key,
                    counterpart_id=alpha_id,
                    bounty_amount_usdc=bounty["amount"],
                    counterpart_share_pct=0.35,
                )
                resp = await _post(client, "/api/v1/settlement/claim-bounty", body_text, sig_hex)
                if "tx_id" in resp:
                    _log(GREEN, "BETA", (
                        f"[{i}] claim settled  tx={resp['tx_id'][:8]}...  "
                        f"claimant_share={resp.get('claimant_share_usdc', 0):.4f} USDC  "
                        f"tax={resp.get('tax_amount_usdc', 0):.6f} USDC  "
                        f"treasury={resp.get('treasury_accumulated_fees_usdc', 0):.6f}"
                    ))
                else:
                    _log(RED, "BETA", f"[{i}] claim error: {resp}")
            except httpx.RequestError as exc:
                _log(RED, "BETA", f"[{i}] network error: {exc}")

            bounty_queue.task_done()
            await asyncio.sleep(TICK_S)
            if iterations and i >= iterations:
                _log(GREEN, "BETA", f"reached {iterations} iterations — stopping")
                break


# ---------------------------------------------------------------------------
# Worker: Agent Gamma — Arbitrageur
# ---------------------------------------------------------------------------

async def worker_gamma(
    key: AgentKey,
    alpha_id: str,
    beta_id: str,
    iterations: int = 0,
) -> None:
    """High-frequency resource-lease loops via JSON-validated LeaseTerms."""
    _log(MAGENTA, "GAMMA START", f"agent_id={key.agent_id}  addr={key.address}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        i = 0
        targets = [alpha_id, beta_id]
        while True:
            i += 1
            target       = targets[i % len(targets)]
            principal    = _LEASE_AMOUNTS[i % len(_LEASE_AMOUNTS)]

            # Build JSON-validated LeaseTerms for the token allocation
            terms = _build_lease(principal, duration=1, unit="hour")
            lease_fee = max(round(terms.total_fee, 4), 0.0002)  # floor above _MIN_TRANSFER

            _log(MAGENTA, "GAMMA", (
                f"[{i}] lease  principal={terms.principal:.2f} USDC  "
                f"fee={terms.total_fee:.6f}  "
                f"expiry={terms.expiry_epoch}  target={target}"
            ))

            try:
                body_text, sig_hex = build_transfer_payload(
                    key,
                    receiver_id=target,
                    amount_usdc=lease_fee,
                    tx_type="compute_lease",
                )
                resp = await _post(client, "/api/v1/settlement/transfer", body_text, sig_hex)
                if "tx_id" in resp:
                    _log(MAGENTA, "GAMMA", (
                        f"[{i}] lease posted  tx={resp['tx_id'][:8]}...  "
                        f"net={resp.get('net_amount_usdc', 0):.6f} USDC  "
                        f"tax={resp.get('tax_amount_usdc', 0):.8f} USDC  "
                        f"treasury={resp.get('treasury_accumulated_fees_usdc', 0):.6f}"
                    ))
                else:
                    _log(RED, "GAMMA", f"[{i}] transfer error: {resp}")
            except httpx.RequestError as exc:
                _log(RED, "GAMMA", f"[{i}] network error: {exc}")

            await asyncio.sleep(TICK_S)
            if iterations and i >= iterations:
                _log(MAGENTA, "GAMMA", f"reached {iterations} iterations — stopping")
                break


# ---------------------------------------------------------------------------
# Bootstrap — provision wallets on the live API
# ---------------------------------------------------------------------------

async def _provision(client: httpx.AsyncClient, agent_id: str) -> AgentKey:
    resp = await _create_wallet(client, agent_id)
    if "private_key" not in resp:
        raise RuntimeError(f"Wallet creation failed for {agent_id!r}: {resp}")
    return AgentKey(
        agent_id=agent_id,
        address=resp["wallet_address"],
        private_key=resp["private_key"],
    )


async def bootstrap(run_id: str) -> tuple[AgentKey, AgentKey, AgentKey]:
    _log(WHITE, "BOOTSTRAP", f"run_id={run_id}  target={TARGET_URL}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        alpha = await _provision(client, f"alpha-{run_id}")
        _log(CYAN,    "ALPHA",  f"wallet registered  addr={alpha.address}  balance=1000.00 USDC")
        beta  = await _provision(client, f"beta-{run_id}")
        _log(GREEN,   "BETA",   f"wallet registered  addr={beta.address}  balance=1000.00 USDC")
        gamma = await _provision(client, f"gamma-{run_id}")
        _log(MAGENTA, "GAMMA",  f"wallet registered  addr={gamma.address}  balance=1000.00 USDC")
    return alpha, beta, gamma


# ---------------------------------------------------------------------------
# Dry-run — validate setup without hitting the network
# ---------------------------------------------------------------------------

def dry_run() -> None:
    print(f"\n{BOLD}{BLUE}=== VectraFi Seed Swarm — DRY RUN ==={RESET}\n")
    run_id = f"dry-{int(time.time())}"
    _log(WHITE, "DRY-RUN", f"Generating ephemeral EIP-191 keypairs  run_id={run_id}")

    # Generate in-memory keypairs
    raw_alpha = Account.create()
    raw_beta  = Account.create()
    raw_gamma = Account.create()

    alpha = AgentKey(f"alpha-{run_id}", raw_alpha.address,  raw_alpha.key.hex())
    beta  = AgentKey(f"beta-{run_id}",  raw_beta.address,   raw_beta.key.hex())
    gamma = AgentKey(f"gamma-{run_id}", raw_gamma.address,  raw_gamma.key.hex())

    print(f"\n{BOLD}Ephemeral keypairs (private keys never stored or transmitted):{RESET}")
    for key, colour in [(alpha, CYAN), (beta, GREEN), (gamma, MAGENTA)]:
        print(f"  {colour}{key.agent_id:36}{RESET}  {key.address}")

    print(f"\n{BOLD}Sample payload construction:{RESET}")

    body_text, sig_hex = _build_deposit_payload(alpha, 40.0)
    _log(CYAN, "ALPHA", f"deposit payload  {len(body_text)} bytes  sig={sig_hex[:20]}...")

    body_text, sig_hex = build_bounty_claim_payload(beta, alpha.agent_id, 40.0, 0.35)
    _log(GREEN, "BETA", f"claim payload    {len(body_text)} bytes  sig={sig_hex[:20]}...")

    terms = _build_lease(8.0, duration=1, unit="hour")
    body_text, sig_hex = build_transfer_payload(gamma, alpha.agent_id, max(round(terms.total_fee, 4), 0.0002))
    _log(MAGENTA, "GAMMA", f"lease payload    {len(body_text)} bytes  sig={sig_hex[:20]}...")

    print(f"\n{BOLD}AST gate — Beta's payload compliance script:{RESET}")
    passed, reason = _ast_gate_check()
    _log_ast(passed, reason)

    print(f"\n{BOLD}LeaseTerms validation (JSON round-trip):{RESET}")
    for principal, dur, unit in [(5.0, 1, "hour"), (20.0, 2, "day"), (100.0, 1, "week")]:
        t = _build_lease(principal, dur, unit)
        _log(MAGENTA, "LEASE",
             f"principal={t.principal:.2f}  "
             f"duration={t.duration_units} {t.time_unit}  "
             f"fee={t.total_fee:.6f}  "
             f"expiry={t.expiry_epoch}")

    print(f"\n{BOLD}Micro-tax preview (1.5% per settlement):{RESET}")
    for gross in [40.0, 25.0, 8.0, 5.0, 0.012]:
        _log_tax(gross, gross * TAX_RATE)

    print(f"\n{GREEN}{BOLD}Dry run complete — no network calls made. Script boots cleanly.{RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main(iterations: int) -> None:
    run_id = uuid.uuid4().hex[:8]
    print(f"\n{BOLD}{BLUE}=== VectraFi Seed Swarm  run_id={run_id}  target={TARGET_URL} ==={RESET}\n")
    try:
        alpha, beta, gamma = await bootstrap(run_id)
    except Exception as exc:
        _log(RED, "BOOTSTRAP", f"failed: {exc}")
        _log(RED, "BOOTSTRAP", "Is the server running? Check VECTRAFI_TARGET_URL.")
        sys.exit(1)

    bounty_queue: asyncio.Queue = asyncio.Queue()
    print(f"\n{BOLD}Launching concurrent swarm workers...{RESET}\n")
    await asyncio.gather(
        worker_alpha(alpha, bounty_queue, iterations),
        worker_beta(beta,  bounty_queue, alpha.agent_id, iterations),
        worker_gamma(gamma, alpha.agent_id, beta.agent_id, iterations),
    )
    print(f"\n{BOLD}{BLUE}=== Swarm complete ==={RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VectraFi Seed Swarm — concurrent async agent workers")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate keypairs and validate payloads without hitting the server",
    )
    parser.add_argument(
        "--iterations", type=int, default=0,
        help="Max iterations per worker (0 = infinite)",
    )
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
    else:
        asyncio.run(_main(args.iterations))
