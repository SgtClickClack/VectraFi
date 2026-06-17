import logging

from eth_account import Account
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from config import DEFAULT_HBAR_BALANCE, DEFAULT_USDC_BALANCE
from database import get_db
from models import AgentWallet
from schemas import WalletCreateRequest, WalletCreateResponse

logger = logging.getLogger("vectrafi.wallet")
router = APIRouter(prefix="/api/v1/wallet", tags=["wallet"])


@router.post(
    "/create",
    response_model=WalletCreateResponse,
    responses={409: {"model": dict}},
)
def create_wallet(payload: WalletCreateRequest, db: Session = Depends(get_db)) -> WalletCreateResponse:
    existing = db.get(AgentWallet, payload.agent_id)
    if existing is not None:
        logger.warning("Wallet creation rejected — agent already exists: %s", payload.agent_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Wallet already exists for agent_id '{payload.agent_id}'",
        )

    account = Account.create()
    private_key = account.key.hex()
    if not private_key.startswith("0x"):
        private_key = f"0x{private_key}"

    wallet = AgentWallet(
        agent_id=payload.agent_id,
        wallet_address=account.address,
        balance_usdc=DEFAULT_USDC_BALANCE,
        balance_hbar=DEFAULT_HBAR_BALANCE,
        staked_yield_balance=0.0,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    logger.info(
        "Created cryptographic wallet for agent=%s address=%s usdc=%.2f (private key not persisted)",
        wallet.agent_id,
        wallet.wallet_address,
        wallet.balance_usdc,
    )

    return WalletCreateResponse(
        agent_id=wallet.agent_id,
        wallet_address=wallet.wallet_address,
        private_key=private_key,
        balance_usdc=float(wallet.balance_usdc),
        balance_hbar=float(wallet.balance_hbar),
        staked_yield_balance=float(wallet.staked_yield_balance),
    )
