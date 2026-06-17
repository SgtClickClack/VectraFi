import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import get_db
from models import AgentWallet
from routes.auth import verify_signed_payload
from schemas import SwapRequest, SwapResponse
from services.pricing import get_active_prices
from services.web3_provider import (
    get_on_chain_eth_balance,
    is_live_mode,
    prepare_transaction_payload,
)

logger = logging.getLogger("vectrafi.trade")
router = APIRouter(prefix="/api/v1/trade", tags=["trade"])


def _get_wallet_or_404(db: Session, agent_id: str) -> AgentWallet:
    wallet = db.get(AgentWallet, agent_id)
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for agent_id '{agent_id}'",
        )
    return wallet


@router.post("/swap", response_model=SwapResponse)
async def execute_swap(request: Request, db: Session = Depends(get_db)) -> SwapResponse:
    payload = await verify_signed_payload(request, db, SwapRequest)

    if payload.from_token == payload.to_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_token and to_token must differ",
        )

    wallet = _get_wallet_or_404(db, payload.agent_id)
    prices = get_active_prices()
    live_mode = is_live_mode()
    execution_mode = "live_rpc" if live_mode else "sandbox"

    on_chain_eth_balance: float | None = None
    prepared_transaction = None
    if live_mode:
        on_chain_eth_balance = get_on_chain_eth_balance(wallet.wallet_address)
        prepared_transaction = prepare_transaction_payload(
            wallet.wallet_address,
            operation="swap",
            metadata={
                "agent_id": payload.agent_id,
                "from_token": payload.from_token,
                "to_token": payload.to_token,
                "amount": payload.amount,
                "reference_prices": prices,
            },
        )
        logger.info(
            "Live RPC swap routing — agent=%s on_chain_eth=%.8f",
            payload.agent_id,
            on_chain_eth_balance,
        )

    if payload.from_token == "USDC":
        if wallet.balance_usdc < payload.amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insufficient USDC balance",
            )
        execution_price = prices["HBAR"] / prices["USDC"]
        amount_out = payload.amount / execution_price
        wallet.balance_usdc -= payload.amount
        wallet.balance_hbar += amount_out
    else:
        if wallet.balance_hbar < payload.amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insufficient HBAR balance",
            )
        execution_price = prices["HBAR"] / prices["USDC"]
        amount_out = payload.amount * execution_price
        wallet.balance_hbar -= payload.amount
        wallet.balance_usdc += amount_out

    db.commit()
    db.refresh(wallet)

    logger.info(
        "Swap executed mode=%s agent=%s %s->%s in=%.6f out=%.6f usdc=%.6f hbar=%.6f",
        execution_mode,
        payload.agent_id,
        payload.from_token,
        payload.to_token,
        payload.amount,
        amount_out,
        wallet.balance_usdc,
        wallet.balance_hbar,
    )

    return SwapResponse(
        agent_id=wallet.agent_id,
        wallet_address=wallet.wallet_address,
        from_token=payload.from_token,
        to_token=payload.to_token,
        amount_in=payload.amount,
        amount_out=amount_out,
        execution_price=execution_price,
        balance_usdc=wallet.balance_usdc,
        balance_hbar=wallet.balance_hbar,
        execution_mode=execution_mode,
        on_chain_eth_balance_eth=on_chain_eth_balance,
        prepared_transaction=prepared_transaction,
    )
