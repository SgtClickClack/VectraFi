import json
import logging
from typing import TypeVar

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from models import AgentWallet

logger = logging.getLogger("vectrafi.auth")

SIGNATURE_HEADER = "X-VectraFi-Signature"
T = TypeVar("T", bound=BaseModel)


def _normalize_address(address: str) -> str:
    return address.lower()


async def verify_signed_payload(request: Request, db: Session, model: type[T]) -> T:
    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body is required",
        )

    body_text = body_bytes.decode("utf-8")
    signature = request.headers.get(SIGNATURE_HEADER)
    if not signature:
        logger.warning("Rejected unsigned transaction — missing %s header", SIGNATURE_HEADER)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing required header: {SIGNATURE_HEADER}",
        )

    try:
        raw_payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON body: {exc.msg}",
        ) from exc

    if not isinstance(raw_payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object",
        )

    wallet_address = raw_payload.get("wallet_address")
    if not wallet_address:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="wallet_address is required in signed transaction payloads",
        )

    message = encode_defunct(text=body_text)
    try:
        recovered_address = Account.recover_message(message, signature=signature)
    except Exception as exc:
        logger.warning("Signature recovery failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid cryptographic signature",
        ) from exc

    if _normalize_address(recovered_address) != _normalize_address(wallet_address):
        logger.warning(
            "Signature mismatch — recovered=%s claimed=%s",
            recovered_address,
            wallet_address,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signature does not match wallet_address in payload",
        )

    agent_id = raw_payload.get("agent_id")
    if not agent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent_id is required",
        )

    wallet = db.get(AgentWallet, agent_id)
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for agent_id '{agent_id}'",
        )

    if _normalize_address(wallet.wallet_address) != _normalize_address(wallet_address):
        logger.warning(
            "Wallet address mismatch for agent=%s registered=%s claimed=%s",
            agent_id,
            wallet.wallet_address,
            wallet_address,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="wallet_address does not match registered agent wallet",
        )

    try:
        payload = model.model_validate(raw_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    logger.info("Verified signed transaction for agent=%s wallet=%s", agent_id, wallet_address)
    return payload
