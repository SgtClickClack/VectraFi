import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from sqlalchemy import func

from database import get_db
from models import AgentWallet, SettlementTransaction, TreasuryState
from routes.auth import verify_signed_payload
from schemas import (
    BountyClaimRequest,
    BountyClaimResponse,
    SettlementTransferRequest,
    SettlementTransferResponse,
    TreasuryAnalyticsResponse,
)

logger = logging.getLogger("vectrafi.settlement")
router = APIRouter(prefix="/api/v1/settlement", tags=["settlement"])

_MICRO_TAX_RATE: float = 0.015   # 1.5% — 15 basis points


def _now() -> int:
    return int(time.time())


def _get_wallet_or_404(db: Session, agent_id: str) -> AgentWallet:
    wallet = db.get(AgentWallet, agent_id)
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for agent_id '{agent_id}'",
        )
    return wallet


def _get_or_init_treasury(db: Session) -> TreasuryState:
    treasury = db.get(TreasuryState, 1)
    if treasury is None:
        treasury = TreasuryState(id=1, accumulated_fees_usdc=0.0, bounty_pool_fees_usdc=0.0)
        db.add(treasury)
    return treasury


def _execute_transfer(
    db: Session,
    sender: AgentWallet,
    receiver: AgentWallet,
    amount_usdc: float,
    tx_type: str,
) -> SettlementTransaction:
    """
    Core transfer primitive — called inside an open SQLAlchemy session.
    Deducts full amount from sender, skims 1.5% tax to treasury, credits
    net to receiver. Raises HTTP 400 on insufficient sender balance.
    All mutations are flushed but NOT committed here — caller owns the commit.
    """
    tax_amount  = round(amount_usdc * _MICRO_TAX_RATE, 8)
    net_amount  = round(amount_usdc - tax_amount, 8)

    if sender.balance_usdc < amount_usdc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Insufficient balance: {sender.agent_id} has "
                f"{sender.balance_usdc:.8f} USDC, needs {amount_usdc:.8f}"
            ),
        )

    sender.balance_usdc   = round(sender.balance_usdc - amount_usdc, 8)
    receiver.balance_usdc = round(receiver.balance_usdc + net_amount, 8)

    treasury = _get_or_init_treasury(db)
    treasury.accumulated_fees_usdc = round(
        treasury.accumulated_fees_usdc + tax_amount, 8
    )

    tx = SettlementTransaction(
        tx_id=str(uuid.uuid4()),
        sender_id=sender.agent_id,
        receiver_id=receiver.agent_id,
        gross_amount_usdc=amount_usdc,
        tax_amount_usdc=tax_amount,
        net_amount_usdc=net_amount,
        tx_type=tx_type,
        created_at=_now(),
    )
    db.add(tx)
    db.flush()  # assign IDs; caller commits
    return tx


# ---------------------------------------------------------------------------
# POST /api/v1/settlement/transfer
# ---------------------------------------------------------------------------

@router.post("/transfer", response_model=SettlementTransferResponse)
async def settlement_transfer(
    request: Request,
    db: Session = Depends(get_db),
) -> SettlementTransferResponse:
    """
    Execute a signature-verified peer-to-peer USDC transfer with 1.5% micro-tax.

    Auth: X-VectraFi-Signature required (EIP-191 signature over compact JSON body).
    Tax:  1.5% of gross_amount deducted and routed to treasury.accumulated_fees_usdc.
    Atomicity: SQLAlchemy session rolls back on any error before commit.
    """
    payload = await verify_signed_payload(request, db, SettlementTransferRequest)

    if payload.agent_id == payload.receiver_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sender and receiver must differ",
        )

    sender   = _get_wallet_or_404(db, payload.agent_id)
    receiver = _get_wallet_or_404(db, payload.receiver_id)

    tx = _execute_transfer(db, sender, receiver, payload.amount_usdc, payload.tx_type)

    db.commit()
    db.refresh(sender)
    db.refresh(receiver)
    treasury = _get_or_init_treasury(db)

    logger.info(
        "Settlement transfer tx=%s %s->%s gross=%.8f tax=%.8f net=%.8f",
        tx.tx_id, payload.agent_id, payload.receiver_id,
        tx.gross_amount_usdc, tx.tax_amount_usdc, tx.net_amount_usdc,
    )

    return SettlementTransferResponse(
        tx_id=tx.tx_id,
        sender_id=tx.sender_id,
        receiver_id=tx.receiver_id,
        gross_amount_usdc=tx.gross_amount_usdc,
        tax_amount_usdc=tx.tax_amount_usdc,
        net_amount_usdc=tx.net_amount_usdc,
        tx_type=tx.tx_type,
        sender_balance_usdc=sender.balance_usdc,
        receiver_balance_usdc=receiver.balance_usdc,
        treasury_accumulated_fees_usdc=treasury.accumulated_fees_usdc,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/settlement/claim-bounty
# ---------------------------------------------------------------------------

@router.post("/claim-bounty", response_model=BountyClaimResponse)
async def claim_bounty(
    request: Request,
    db: Session = Depends(get_db),
) -> BountyClaimResponse:
    """
    Signature-verified automated bounty yield-split with micro-tax settlement.

    The claimant holds the gross bounty in their wallet. This endpoint:
      1. Computes the counterpart's gross share: bounty_amount * counterpart_share_pct.
      2. Executes a signed transfer claimant -> counterpart (1.5% tax deducted).
      3. The claimant retains the remainder in their wallet untouched.

    Auth: X-VectraFi-Signature required.
    """
    payload = await verify_signed_payload(request, db, BountyClaimRequest)

    if payload.agent_id == payload.counterpart_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="claimant and counterpart must differ",
        )

    claimant    = _get_wallet_or_404(db, payload.agent_id)
    counterpart = _get_wallet_or_404(db, payload.counterpart_id)

    counterpart_gross = round(payload.bounty_amount_usdc * payload.counterpart_share_pct, 8)
    claimant_share    = round(payload.bounty_amount_usdc - counterpart_gross, 8)

    tx = _execute_transfer(db, claimant, counterpart, counterpart_gross, "bounty_yield_split")

    db.commit()
    db.refresh(claimant)
    db.refresh(counterpart)
    treasury = _get_or_init_treasury(db)

    logger.info(
        "Bounty claim tx=%s claimant=%s counterpart=%s "
        "gross=%.8f claimant_keep=%.8f counterpart_gross=%.8f tax=%.8f",
        tx.tx_id, payload.agent_id, payload.counterpart_id,
        payload.bounty_amount_usdc, claimant_share,
        counterpart_gross, tx.tax_amount_usdc,
    )

    return BountyClaimResponse(
        tx_id=tx.tx_id,
        claimant_id=payload.agent_id,
        counterpart_id=payload.counterpart_id,
        bounty_amount_usdc=payload.bounty_amount_usdc,
        claimant_share_usdc=claimant_share,
        counterpart_gross_usdc=counterpart_gross,
        tax_amount_usdc=tx.tax_amount_usdc,
        counterpart_net_usdc=tx.net_amount_usdc,
        claimant_balance_usdc=claimant.balance_usdc,
        counterpart_balance_usdc=counterpart.balance_usdc,
        treasury_accumulated_fees_usdc=treasury.accumulated_fees_usdc,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/settlement/analytics
# ---------------------------------------------------------------------------

@router.get("/analytics", response_model=TreasuryAnalyticsResponse)
def settlement_analytics(db: Session = Depends(get_db)) -> TreasuryAnalyticsResponse:
    """
    Public read-only endpoint: returns aggregate settlement statistics.

    No auth required. Queries:
    - treasury.accumulated_fees_usdc (1.5% micro-tax accumulator)
    - COUNT(*) of all settlement_transactions rows
    - SUM(gross_amount_usdc) across all settlement_transactions
    - COUNT(*) of registered agent_wallets
    """
    treasury = _get_or_init_treasury(db)

    tx_count = db.query(func.count(SettlementTransaction.tx_id)).scalar() or 0
    total_volume = db.query(func.sum(SettlementTransaction.gross_amount_usdc)).scalar() or 0.0
    wallet_count = db.query(func.count(AgentWallet.agent_id)).scalar() or 0

    return TreasuryAnalyticsResponse(
        accumulated_fees_usdc=treasury.accumulated_fees_usdc,
        total_transactions_processed=int(tx_count),
        total_volume_processed_usdc=float(total_volume),
        active_wallets_count=int(wallet_count),
    )
