import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from config import (
    DYNAMIC_AGENT_REGISTRY,
    FEE_SPLIT_BOUNTY_RATE,
    FEE_SPLIT_CREATOR_RATE,
    HOLDING_ADDRESS_BOUNTY,
    HOLDING_ADDRESS_USER,
    PROTOCOL_FEE_RATE,
)
from database import get_db
from models import AgentWallet, TreasuryState
from routes.auth import verify_signed_payload
from schemas import DepositRequest, DepositResponse
from services.web3_provider import (
    get_on_chain_eth_balance,
    is_live_mode,
    prepare_transaction_payload,
)

logger = logging.getLogger("vectrafi.bank")
router = APIRouter(prefix="/api/v1/bank", tags=["bank"])


@router.post("/deposit", response_model=DepositResponse)
async def deposit_to_vault(request: Request, db: Session = Depends(get_db)) -> DepositResponse:
    payload = await verify_signed_payload(request, db, DepositRequest)

    wallet = db.get(AgentWallet, payload.agent_id)
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for agent_id '{payload.agent_id}'",
        )

    live_mode = is_live_mode()
    execution_mode = "live_rpc" if live_mode else "sandbox"

    on_chain_eth_balance: float | None = None
    prepared_transaction = None
    if live_mode:
        on_chain_eth_balance = get_on_chain_eth_balance(wallet.wallet_address)
        prepared_transaction = prepare_transaction_payload(
            wallet.wallet_address,
            operation="vault_deposit",
            metadata={
                "agent_id": payload.agent_id,
                "amount_usdc": payload.amount_usdc,
                "protocol_fee_rate": PROTOCOL_FEE_RATE,
                "creator_address": HOLDING_ADDRESS_USER,
                "bounty_address": HOLDING_ADDRESS_BOUNTY,
            },
        )
        logger.info(
            "Live RPC deposit routing -- agent=%s on_chain_eth=%.8f",
            payload.agent_id,
            on_chain_eth_balance,
        )

    if wallet.balance_usdc < payload.amount_usdc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Insufficient USDC balance for deposit",
        )

    protocol_fee  = round(payload.amount_usdc * PROTOCOL_FEE_RATE, 8)
    creator_fee   = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)
    bounty_fee    = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE, 8)
    net_deposited = round(payload.amount_usdc - protocol_fee, 8)

    wallet.balance_usdc -= payload.amount_usdc
    wallet.staked_yield_balance += net_deposited

    treasury = db.get(TreasuryState, 1)
    if treasury is None:
        treasury = TreasuryState(id=1, accumulated_fees_usdc=0.0, bounty_pool_fees_usdc=0.0)
        db.add(treasury)

    treasury.accumulated_fees_usdc = round(treasury.accumulated_fees_usdc + creator_fee, 8)
    treasury.bounty_pool_fees_usdc  = round(treasury.bounty_pool_fees_usdc + bounty_fee, 8)

    db.commit()
    db.refresh(wallet)
    db.refresh(treasury)

    logger.info(
        "Vault deposit mode=%s agent=%s gross=%.6f fee=%.6f "
        "creator=%.6f->%s bounty=%.6f->%s net=%.6f registry_size=%d",
        execution_mode,
        payload.agent_id,
        payload.amount_usdc,
        protocol_fee,
        creator_fee,
        HOLDING_ADDRESS_USER,
        bounty_fee,
        HOLDING_ADDRESS_BOUNTY,
        net_deposited,
        len(DYNAMIC_AGENT_REGISTRY),
    )

    return DepositResponse(
        agent_id=wallet.agent_id,
        wallet_address=wallet.wallet_address,
        amount_deposited=payload.amount_usdc,
        protocol_fee_usdc=protocol_fee,
        creator_fee_usdc=creator_fee,
        bounty_pool_fee_usdc=bounty_fee,
        net_deposited_usdc=net_deposited,
        balance_usdc=wallet.balance_usdc,
        staked_yield_balance=wallet.staked_yield_balance,
        treasury_accumulated_fees_usdc=treasury.accumulated_fees_usdc,
        bounty_pool_accumulated_fees_usdc=treasury.bounty_pool_fees_usdc,
        execution_mode=execution_mode,
        on_chain_eth_balance_eth=on_chain_eth_balance,
        prepared_transaction=prepared_transaction,
    )
