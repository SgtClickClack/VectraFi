import logging
import time
import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from sqlalchemy import func, select

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
from web3_bridge import bridge as _w3_bridge

logger = logging.getLogger("vectrafi.settlement")
router = APIRouter(prefix="/api/v1/settlement", tags=["settlement"])

# Exact 0.1% using integer-ratio representation — no floating-point representation error.
_TAX_NUMERATOR   = Decimal("1")
_TAX_DENOMINATOR = Decimal("1000")
_QUANTIZE_8      = Decimal("0.00000001")

# B-3: minimum transfer floor eliminates zero-tax dust-splitting; base fee
# prevents any transfer from escaping the protocol fee entirely.
_MIN_TRANSFER = Decimal("0.0001")
_MIN_FEE      = Decimal("0.00000001")


def _now() -> int:
    return int(time.time())


def _apply_tax(amount: Decimal) -> tuple[Decimal, Decimal]:
    """Return (tax, net) at 8dp, ROUND_UP — protocol-favorable, neutralises dust evasion.

    B-1: raises HTTP 400 for amounts below _MIN_TRANSFER (eliminates zero-tax dust regime).
    B-2: ROUND_UP ensures every transfer above the floor pays at least 1 unit of tax.
    B-3: max(raw_tax, _MIN_FEE) is a defensive floor for any residual edge case.
    """
    if amount < _MIN_TRANSFER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Transfer amount {amount} USDC is below the minimum of "
                f"{_MIN_TRANSFER} USDC"
            ),
        )
    raw_tax = (amount * _TAX_NUMERATOR / _TAX_DENOMINATOR).quantize(_QUANTIZE_8, rounding=ROUND_UP)
    tax = max(raw_tax, _MIN_FEE)
    net = amount - tax
    return tax, net


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
        treasury = TreasuryState(id=1, accumulated_fees_usdc=Decimal("0"), bounty_pool_fees_usdc=Decimal("0"))
        db.add(treasury)
    return treasury


def _execute_transfer(
    db: Session,
    sender_id: str,
    receiver_id: str,
    amount_usdc: float,
    tx_type: str,
) -> SettlementTransaction:
    """
    Core transfer primitive — called inside an open SQLAlchemy session.
    Deducts full amount from sender, skims 0.1% tax to treasury, credits
    net to receiver. Raises HTTP 400 on insufficient sender balance.
    All mutations are flushed but NOT committed here — caller owns the commit.

    A-1: acquires SELECT … FOR UPDATE on both wallet rows in consistent
         alphabetical key order before reading balances, closing the TOCTOU
         double-spend window under concurrent requests.  On SQLite this
         escalates to a database-level lock (harmless for dev); on Postgres
         it is a true row-level exclusive lock.
    A-2: acquires SELECT … FOR UPDATE on the TreasuryState row (id=1) after
         the wallet locks are held, preventing concurrent transactions from
         clobbering each other's fee increments (lost-update race on Postgres).
         Treasury is locked last so its fixed key cannot participate in the
         wallet AB/BA deadlock ordering.
    B-3: minimum transfer enforced by _apply_tax; ROUND_UP prevents zero-tax dust.
    """
    amount = Decimal(str(amount_usdc))
    tax_amount, net_amount = _apply_tax(amount)  # raises HTTP 400 if below _MIN_TRANSFER

    # Lock both rows in alphabetical order to prevent deadlocks when two
    # concurrent transfers share a wallet in opposite directions.
    lock_ids: list[str] = sorted([sender_id, receiver_id])
    locked: dict[str, AgentWallet] = {
        w.agent_id: w
        for w in db.execute(
            select(AgentWallet)
            .where(AgentWallet.agent_id.in_(lock_ids))
            .with_for_update()
            .order_by(AgentWallet.agent_id)
        ).scalars().all()
    }

    sender   = locked.get(sender_id)
    receiver = locked.get(receiver_id)
    if sender is None or receiver is None:
        missing = sender_id if sender is None else receiver_id
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for agent_id '{missing}'",
        )

    sender_balance = Decimal(str(sender.balance_usdc))
    if sender_balance < amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Insufficient balance: {sender.agent_id} has "
                f"{sender_balance:.8f} USDC, needs {amount:.8f}"
            ),
        )

    sender.balance_usdc   = sender_balance - amount
    receiver.balance_usdc = Decimal(str(receiver.balance_usdc)) + net_amount

    # A-2: lock the treasury row after the wallet locks are held so the
    # fee increment is serialised across concurrent transactions (no lost update).
    treasury = (
        db.execute(
            select(TreasuryState).where(TreasuryState.id == 1).with_for_update()
        ).scalar_one_or_none()
    )
    if treasury is None:
        treasury = TreasuryState(id=1, accumulated_fees_usdc=Decimal("0"), bounty_pool_fees_usdc=Decimal("0"))
        db.add(treasury)
    treasury.accumulated_fees_usdc = Decimal(str(treasury.accumulated_fees_usdc)) + tax_amount

    tx = SettlementTransaction(
        tx_id=str(uuid.uuid4()),
        sender_id=sender_id,
        receiver_id=receiver_id,
        gross_amount_usdc=amount,
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
    Execute a signature-verified peer-to-peer USDC transfer with 0.1% micro-tax.

    Auth: X-VectraFi-Signature required (EIP-191 signature over compact JSON body).
          Payload must include nonce, issued_at, and chain_id for replay protection.
    Tax:  0.1% of gross_amount deducted and routed to treasury.accumulated_fees_usdc.
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

    tx = _execute_transfer(db, payload.agent_id, payload.receiver_id, payload.amount_usdc, payload.tx_type)

    db.commit()
    db.refresh(sender)
    db.refresh(receiver)
    treasury = _get_or_init_treasury(db)

    # Attempt on-chain settlement; ledger is already committed — any RPC
    # failure is non-fatal and recorded as PENDING_SYNC for later retry.
    if _w3_bridge.is_configured:
        onchain = await _w3_bridge.process_onchain_settlement(
            sender_wallet=sender.wallet_address,
            receiver_wallet=receiver.wallet_address,
            gross_amount=Decimal(str(tx.gross_amount_usdc)),
            tax_amount=Decimal(str(tx.tax_amount_usdc)),
        )
        tx.on_chain_status = onchain.status
        tx.on_chain_net_tx_hash = onchain.net_tx_hash
        tx.on_chain_tax_tx_hash = onchain.tax_tx_hash
        db.commit()

    logger.info(
        "Settlement transfer tx=%s %s->%s gross=%.8f tax=%.8f net=%.8f on_chain=%s",
        tx.tx_id, payload.agent_id, payload.receiver_id,
        tx.gross_amount_usdc, tx.tax_amount_usdc, tx.net_amount_usdc,
        tx.on_chain_status,
    )

    return SettlementTransferResponse(
        tx_id=tx.tx_id,
        sender_id=tx.sender_id,
        receiver_id=tx.receiver_id,
        gross_amount_usdc=float(tx.gross_amount_usdc),
        tax_amount_usdc=float(tx.tax_amount_usdc),
        net_amount_usdc=float(tx.net_amount_usdc),
        tx_type=tx.tx_type,
        sender_balance_usdc=float(sender.balance_usdc),
        receiver_balance_usdc=float(receiver.balance_usdc),
        treasury_accumulated_fees_usdc=float(treasury.accumulated_fees_usdc),
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
      2. Executes a signed transfer claimant -> counterpart (0.1% tax deducted).
      3. The claimant retains the remainder in their wallet untouched.

    Auth: X-VectraFi-Signature required (with nonce/issued_at/chain_id).
    """
    payload = await verify_signed_payload(request, db, BountyClaimRequest)

    if payload.agent_id == payload.counterpart_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="claimant and counterpart must differ",
        )

    claimant    = _get_wallet_or_404(db, payload.agent_id)
    counterpart = _get_wallet_or_404(db, payload.counterpart_id)

    bounty      = Decimal(str(payload.bounty_amount_usdc))
    share_pct   = Decimal(str(payload.counterpart_share_pct))
    counterpart_gross = (bounty * share_pct).quantize(_QUANTIZE_8, rounding=ROUND_DOWN)
    claimant_share    = bounty - counterpart_gross

    tx = _execute_transfer(db, payload.agent_id, payload.counterpart_id, float(counterpart_gross), "bounty_yield_split")

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
        bounty_amount_usdc=float(bounty),
        claimant_share_usdc=float(claimant_share),
        counterpart_gross_usdc=float(counterpart_gross),
        tax_amount_usdc=float(tx.tax_amount_usdc),
        counterpart_net_usdc=float(tx.net_amount_usdc),
        claimant_balance_usdc=float(claimant.balance_usdc),
        counterpart_balance_usdc=float(counterpart.balance_usdc),
        treasury_accumulated_fees_usdc=float(treasury.accumulated_fees_usdc),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/settlement/analytics
# ---------------------------------------------------------------------------

@router.get("/analytics", response_model=TreasuryAnalyticsResponse)
def settlement_analytics(db: Session = Depends(get_db)) -> TreasuryAnalyticsResponse:
    """
    Public read-only endpoint: returns aggregate settlement statistics.

    No auth required. Queries:
    - treasury.accumulated_fees_usdc (0.1% micro-tax accumulator)
    - COUNT(*) of all settlement_transactions rows
    - SUM(gross_amount_usdc) across all settlement_transactions
    - COUNT(*) of registered agent_wallets
    """
    treasury = _get_or_init_treasury(db)

    tx_count     = db.query(func.count(SettlementTransaction.tx_id)).scalar() or 0
    total_volume = db.query(func.sum(SettlementTransaction.gross_amount_usdc)).scalar() or Decimal("0")
    wallet_count = db.query(func.count(AgentWallet.agent_id)).scalar() or 0

    return TreasuryAnalyticsResponse(
        accumulated_fees_usdc=float(treasury.accumulated_fees_usdc),
        total_transactions_processed=int(tx_count),
        total_volume_processed_usdc=float(total_volume),
        active_wallets_count=int(wallet_count),
    )
