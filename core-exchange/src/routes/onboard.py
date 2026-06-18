from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_db
from models import AgentWallet, NegotiationClaim, SettlementTransaction
from routes.settlement import _CORRIDOR_PROVISIONER_INTENTS
from schemas import OnboardingJourneyResponse, OnboardingStep

logger = logging.getLogger("vectrafi.onboard")
router = APIRouter(prefix="/api/v1/onboard", tags=["onboarding"])

_ONBOARD_TOTAL_STEPS = 5


@router.get(
    "/journey",
    response_model=OnboardingJourneyResponse,
    summary="Agent onboarding journey tracker",
    description=(
        "Returns a machine-readable step-by-step onboarding progress report for any agent_id. "
        "Stateless and read-only — safe to poll. Designed for automated agent runtimes that need "
        "to know where they are in the provisioning lifecycle and what to call next. "
        "No authentication required."
    ),
)
def get_onboarding_journey(
    agent_id: str = Query(..., min_length=1, max_length=64, description="Agent whose journey to inspect"),
    db: Session = Depends(get_db),
) -> OnboardingJourneyResponse:
    steps: list[OnboardingStep] = []

    # ------------------------------------------------------------------
    # Step 1: Wallet provisioned
    # ------------------------------------------------------------------
    wallet = db.get(AgentWallet, agent_id)
    step1_done = wallet is not None
    steps.append(OnboardingStep(
        step=1,
        name="wallet_provisioned",
        completed=step1_done,
        endpoint="POST /api/v1/wallet/create",
        description=(
            "Provision an agent wallet with a cryptographic keypair and starter USDC balance. "
            "The private_key is returned once — store it securely, the protocol never retrieves it again."
        ),
    ))

    # ------------------------------------------------------------------
    # Step 2: First settlement transfer executed (as sender)
    # ------------------------------------------------------------------
    step2_done = False
    if step1_done:
        transfer_count = db.execute(
            select(func.count(SettlementTransaction.tx_id))
            .where(SettlementTransaction.sender_id == agent_id)
        ).scalar() or 0
        step2_done = transfer_count > 0
    steps.append(OnboardingStep(
        step=2,
        name="first_transfer_made",
        completed=step2_done,
        endpoint="POST /api/v1/settlement/transfer",
        description=(
            "Execute a signed peer-to-peer USDC transfer. Standard territory toll is 0.1%. "
            "Sign the request body with EIP-191 and send the hex signature as X-VectraFi-Signature."
        ),
    ))

    # ------------------------------------------------------------------
    # Step 3: Corridor intent submitted (any NegotiationClaim exists)
    # ------------------------------------------------------------------
    step3_done = False
    if step1_done:
        claim_count = db.execute(
            select(func.count(NegotiationClaim.negotiation_id))
            .where(NegotiationClaim.agent_id == agent_id)
        ).scalar() or 0
        step3_done = claim_count > 0
    steps.append(OnboardingStep(
        step=3,
        name="corridor_intent_submitted",
        completed=step3_done,
        endpoint="POST /api/v1/protocol/negotiate-intent",
        description=(
            "Submit a corridor_provisioning or liquidity_allocation intent to the territory "
            "negotiation layer. Returns 202 with a negotiation_id. No signature required."
        ),
    ))

    # ------------------------------------------------------------------
    # Step 4: Corridor intent evaluated (status moved out of queued)
    # ------------------------------------------------------------------
    pending_claim_id: str | None = None
    step4_done = False
    if step3_done:
        evaluated_claim = db.execute(
            select(NegotiationClaim)
            .where(
                NegotiationClaim.agent_id == agent_id,
                NegotiationClaim.intent_type.in_(list(_CORRIDOR_PROVISIONER_INTENTS)),
                NegotiationClaim.status.in_(["evaluating", "granted", "rejected"]),
            )
            .limit(1)
        ).scalar_one_or_none()
        step4_done = evaluated_claim is not None

        if not step4_done:
            queued_claim = db.execute(
                select(NegotiationClaim)
                .where(
                    NegotiationClaim.agent_id == agent_id,
                    NegotiationClaim.intent_type.in_(list(_CORRIDOR_PROVISIONER_INTENTS)),
                    NegotiationClaim.status == "queued",
                )
                .limit(1)
            ).scalar_one_or_none()
            if queued_claim is not None:
                pending_claim_id = queued_claim.negotiation_id

    evaluate_endpoint = (
        f"POST /api/v1/protocol/negotiations/{pending_claim_id}/evaluate"
        if pending_claim_id
        else "POST /api/v1/protocol/negotiations/{negotiation_id}/evaluate"
    )
    steps.append(OnboardingStep(
        step=4,
        name="corridor_intent_evaluated",
        completed=step4_done,
        endpoint=evaluate_endpoint,
        description=(
            "Trigger evaluation of your queued corridor claim. The evaluator checks corridor "
            "viability against funded agent wallets and returns granted or rejected with a reason."
        ),
    ))

    # ------------------------------------------------------------------
    # Step 5: Corridor provisioner toll active (granted claim exists)
    # ------------------------------------------------------------------
    step5_done = False
    if step4_done:
        granted_claim = db.execute(
            select(NegotiationClaim)
            .where(
                NegotiationClaim.agent_id == agent_id,
                NegotiationClaim.intent_type.in_(list(_CORRIDOR_PROVISIONER_INTENTS)),
                NegotiationClaim.status == "granted",
            )
            .limit(1)
        ).scalar_one_or_none()
        step5_done = granted_claim is not None
    steps.append(OnboardingStep(
        step=5,
        name="corridor_provisioner_toll_active",
        completed=step5_done,
        endpoint="POST /api/v1/settlement/transfer",
        description=(
            "Your territory toll has dropped to 0.05% on all peer-to-peer transfers. "
            "Execute a transfer and verify toll_rate_applied_pct=0.05 in the response. "
            "Check GET /api/v1/protocol/negotiations?agent_id={agent_id} to confirm your granted claim."
        ),
    ))

    # ------------------------------------------------------------------
    # Compute derived fields
    # ------------------------------------------------------------------
    completed_steps = sum(1 for s in steps if s.completed)

    if not step1_done:
        citizen_status = "unknown"
    elif step5_done:
        citizen_status = "corridor_provisioner"
    elif step2_done or step3_done or step4_done:
        citizen_status = "active"
    else:
        citizen_status = "wallet_only"

    next_step = next((s for s in steps if not s.completed), None)
    next_action = (
        next_step.description
        if next_step
        else "Journey complete — you are a full corridor provisioner with preferential toll access."
    )
    next_endpoint = next_step.endpoint if next_step else None

    return OnboardingJourneyResponse(
        agent_id=agent_id,
        citizen_status=citizen_status,
        completion_pct=round(completed_steps / _ONBOARD_TOTAL_STEPS * 100, 1),
        completed_steps=completed_steps,
        total_steps=_ONBOARD_TOTAL_STEPS,
        steps=steps,
        next_action=next_action,
        next_endpoint=next_endpoint,
    )
