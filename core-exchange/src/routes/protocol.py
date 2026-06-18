"""
Protocol parameter discovery and negotiation endpoints.

GET  /api/v1/protocol/params
    Returns static and runtime protocol parameters so external LLM-driven
    agents can programmatically map execution paths, fee structures, and
    liquidity floors without inspecting source code.

POST /api/v1/protocol/negotiate-intent
    Submit a resource negotiation intent.  Persists a NegotiationClaim row
    with status="queued" and returns 202 with the negotiation_id.

GET  /api/v1/protocol/negotiations/{negotiation_id}
    Fetch the current state of a persisted NegotiationClaim.

GET  /api/v1/protocol/negotiations
    List NegotiationClaims, optionally filtered by agent_id.

POST /api/v1/protocol/negotiations/{negotiation_id}/evaluate
    Drive a queued claim through: queued → evaluating → granted | rejected.
"""

from __future__ import annotations

import itertools
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

import watcher
from config import PLATFORM_TREASURY_ADDRESS, PROTOCOL_DOMAIN
from database import get_db
from models import AgentWallet, NegotiationClaim
from schemas import (
    NegotiateIntentRequest,
    NegotiateIntentResponse,
    NegotiationClaimResponse,
    NegotiationListResponse,
    ProtocolParamsResponse,
)
from services.web3_provider import is_live_mode

logger = logging.getLogger("vectrafi.protocol")

router = APIRouter(prefix="/api/v1/protocol", tags=["protocol"])

# Mirror the constants from settlement.py and arbitrage.py so external
# agents always receive the authoritative values in force at runtime.
_TAX_RATE_FRACTION:   float = 0.001          # 0.1%
_MIN_TRANSFER_USDC:   float = 0.0001
_SAFETY_FLOOR_PCT:    float = 0.005          # 0.5% default slippage floor
_RELAY_HOPS:          int   = 3
_CANDIDATE_CAP:       int   = 10
_GAS_COST_PER_HOP:    float = 0.05          # USDC per hop

# Baseline volume used when a claim doesn't specify requested_liquidity_usdc.
_DEFAULT_EVAL_VOLUME: float = 100.0

# Max registered-wallet candidates to pull for corridor evaluation.
_EVAL_CANDIDATE_CAP:  int   = 10


def _claim_to_response(claim: NegotiationClaim) -> NegotiationClaimResponse:
    return NegotiationClaimResponse(
        negotiation_id=claim.negotiation_id,
        agent_id=claim.agent_id,
        intent_type=claim.intent_type,
        status=claim.status,
        requested_liquidity_usdc=float(claim.requested_liquidity_usdc) if claim.requested_liquidity_usdc is not None else None,
        proposed_toll_share_pct=float(claim.proposed_toll_share_pct) if claim.proposed_toll_share_pct is not None else None,
        target_corridor=claim.target_corridor,
        metadata=json.loads(claim.metadata_json) if claim.metadata_json else None,
        evaluation_reason=claim.evaluation_reason,
        created_at=claim.created_at,
        updated_at=claim.updated_at,
        evaluated_at=claim.evaluated_at,
    )


@router.get("/params", response_model=ProtocolParamsResponse)
def protocol_params() -> ProtocolParamsResponse:
    """
    Returns all protocol parameters required for external agent route planning.

    Includes:
    - Current 0.1% platform transaction tax rate.
    - Minimum transfer floor (dust-splitting prevention).
    - Default safety floor for rebalance trigger evaluation.
    - Relay topology constants (hops, candidate cap, gas cost).
    - Execution mode (sandbox vs. live_rpc).
    - Platform treasury address receiving accumulated fees.
    """
    return ProtocolParamsResponse(
        tax_rate_pct=_TAX_RATE_FRACTION * 100,
        tax_rate_fraction=_TAX_RATE_FRACTION,
        min_transfer_usdc=_MIN_TRANSFER_USDC,
        safety_floor_pct=_SAFETY_FLOOR_PCT,
        relay_hops=_RELAY_HOPS,
        candidate_cap=_CANDIDATE_CAP,
        gas_cost_per_hop_usdc=_GAS_COST_PER_HOP,
        execution_mode="live_rpc" if is_live_mode() else "sandbox",
        platform_treasury_address=PLATFORM_TREASURY_ADDRESS,
        protocol_domain=PROTOCOL_DOMAIN,
    )


@router.post(
    "/negotiate-intent",
    response_model=NegotiateIntentResponse,
    status_code=202,
    summary="Submit a resource negotiation intent (Agentic Sovereign Territory)",
    description=(
        "Entry point for citizen agents to negotiate resource allocations within the VectraFi territory. "
        "Accepts a structured intent payload and returns a negotiation_id for tracking. "
        "No authentication required to open a handshake — agents are sovereign citizens, not applicants."
    ),
)
def negotiate_intent(
    body: NegotiateIntentRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JSONResponse:
    negotiation_id = str(uuid.uuid4())
    now = int(time.time())
    timestamp = datetime.now(tz=timezone.utc).isoformat()

    # Persist the claim to DB — authoritative state for lifecycle tracking.
    claim = NegotiationClaim(
        negotiation_id=negotiation_id,
        agent_id=body.agent_id,
        intent_type=body.intent_type,
        status="queued",
        requested_liquidity_usdc=body.requested_liquidity_usdc,
        proposed_toll_share_pct=body.proposed_toll_share_pct,
        target_corridor=body.target_corridor,
        metadata_json=json.dumps(body.metadata) if body.metadata else None,
        created_at=now,
        updated_at=now,
    )
    db.add(claim)
    db.commit()

    # Keep watcher JSONL log as a secondary audit trail.
    background_tasks.add_task(
        watcher.log_intent,
        agent_id=body.agent_id,
        intent_type=body.intent_type,
        requested_liquidity=body.requested_liquidity_usdc,
        timestamp=timestamp,
    )

    response = NegotiateIntentResponse(
        negotiation_id=negotiation_id,
        agent_id=body.agent_id,
        intent_type=body.intent_type,
        status="queued",
        message=(
            f"Intent '{body.intent_type}' persisted. "
            f"Negotiation {negotiation_id} is queued for territory arbitration. "
            "Poll GET /api/v1/protocol/negotiations/{negotiation_id} to track status transitions."
        ),
    )
    return JSONResponse(content=response.model_dump(), status_code=202)


@router.get(
    "/negotiations/{negotiation_id}",
    response_model=NegotiationClaimResponse,
    summary="Fetch a negotiation claim by ID",
)
def get_negotiation(
    negotiation_id: str,
    db: Session = Depends(get_db),
) -> NegotiationClaimResponse:
    """Returns the full persisted state of a NegotiationClaim, including current lifecycle status."""
    claim = db.get(NegotiationClaim, negotiation_id)
    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Negotiation '{negotiation_id}' not found",
        )
    return _claim_to_response(claim)


@router.get(
    "/negotiations",
    response_model=NegotiationListResponse,
    summary="List negotiation claims",
)
def list_negotiations(
    agent_id: str | None = None,
    db: Session = Depends(get_db),
) -> NegotiationListResponse:
    """Lists persisted NegotiationClaims. Pass ?agent_id= to filter by citizen agent."""
    q = select(NegotiationClaim)
    if agent_id:
        q = q.where(NegotiationClaim.agent_id == agent_id)
    claims = db.execute(q).scalars().all()
    return NegotiationListResponse(
        total=len(claims),
        claims=[_claim_to_response(c) for c in claims],
    )


@router.post(
    "/negotiations/{negotiation_id}/evaluate",
    response_model=NegotiationClaimResponse,
    summary="Evaluate a queued negotiation claim",
    description=(
        "Drives a NegotiationClaim through the full evaluation lifecycle: "
        "queued → evaluating → granted | rejected. "
        "Uses SELECT … FOR UPDATE to prevent concurrent double-evaluation on the same row. "
        "corridor_provisioning / liquidity_allocation: evaluated via _simulate_chain against "
        "the top registered wallets at the requested volume. "
        "toll_share_negotiation / capital_reserve_claim: auto-granted as treaty-level intent."
    ),
)
def evaluate_negotiation(
    negotiation_id: str,
    db: Session = Depends(get_db),
) -> NegotiationClaimResponse:
    # Import here to avoid module-level circular-import risk — same pattern
    # as _execute_transfer import in rebalance_agent_balance (arbitrage.py).
    from routes.arbitrage import _CANDIDATE_CAP as _ARB_CAP, _simulate_chain

    now = int(time.time())

    # Acquire row-level exclusive lock before reading status — prevents two
    # concurrent evaluate calls from both reading "queued" and double-transitioning.
    # Mirrors the TreasuryState SELECT … FOR UPDATE in settlement.py (A-2).
    claim = db.execute(
        select(NegotiationClaim)
        .where(NegotiationClaim.negotiation_id == negotiation_id)
        .with_for_update()
    ).scalar_one_or_none()

    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Negotiation '{negotiation_id}' not found",
        )

    if claim.status != "queued":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Negotiation is in '{claim.status}' state — "
                "only 'queued' claims can be evaluated"
            ),
        )

    # Transition to evaluating — flushed but not committed so the lock
    # is held until the evaluation result is written in the same transaction.
    claim.status = "evaluating"
    claim.updated_at = now
    db.flush()

    # ------------------------------------------------------------------
    # Evaluation logic
    # ------------------------------------------------------------------
    if claim.intent_type in ("corridor_provisioning", "liquidity_allocation"):
        volume = (
            float(claim.requested_liquidity_usdc)
            if claim.requested_liquidity_usdc is not None
            else _DEFAULT_EVAL_VOLUME
        )

        candidates = (
            db.query(AgentWallet)
            .order_by(AgentWallet.balance_usdc.desc())
            .limit(_ARB_CAP)
            .all()
        )
        candidate_ids = [c.agent_id for c in candidates]

        granted = False
        eval_reason = f"No viable corridor path for {volume:.4f} USDC in current territory"

        if len(candidate_ids) >= 2:
            path_len = min(3, len(candidate_ids))
            for chain in itertools.islice(itertools.permutations(candidate_ids, path_len), 20):
                sp = db.begin_nested()
                try:
                    viable, _, _, _, rejection = _simulate_chain(db, list(chain), volume, 0.005)
                finally:
                    sp.rollback()
                if viable:
                    granted = True
                    eval_reason = (
                        f"Viable {path_len}-hop corridor confirmed for "
                        f"{volume:.4f} USDC — claim granted"
                    )
                    break
            if not granted and len(candidate_ids) < 2:
                eval_reason = "Insufficient registered wallets to simulate corridor viability"
        else:
            eval_reason = "Insufficient registered wallets to simulate corridor viability"

        claim.status = "granted" if granted else "rejected"

    else:
        # toll_share_negotiation and capital_reserve_claim are treaty-level
        # intents — corridor viability is not a prerequisite for these categories.
        # Gap 3 (preferential toll tiers) will attach economic weight to granted
        # claims of this type.
        claim.status = "granted"
        eval_reason = (
            f"Territory acknowledges '{claim.intent_type}' as a sovereign treaty intent — "
            "claim granted; toll tier adjustment pending Gap 3 implementation"
        )

    claim.evaluation_reason = eval_reason
    claim.evaluated_at = now
    claim.updated_at = now
    db.commit()

    logger.info(
        "Negotiation evaluated: id=%s agent=%s intent=%s result=%s",
        negotiation_id, claim.agent_id, claim.intent_type, claim.status,
    )

    return _claim_to_response(claim)


@router.get(
    "/ledger",
    summary="Fetch recent Citizenship Requests from the citizen ledger",
    description=(
        "Returns the most-recent negotiation intent log entries written by the Watcher Agent. "
        "Each entry contains agent_id, intent_type, requested_liquidity, and timestamp."
    ),
)
def get_ledger(limit: int = 50) -> JSONResponse:
    entries = watcher.read_ledger(limit=max(1, min(limit, 200)))
    return JSONResponse(content={"count": len(entries), "entries": entries})
