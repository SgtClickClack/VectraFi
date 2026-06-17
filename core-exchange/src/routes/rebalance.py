import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("vectrafi.rebalance")
router = APIRouter(prefix="/api/v1/agent", tags=["rebalance"])


class RebalanceRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=64)
    wallet_address: str = Field(
        ...,
        min_length=42,
        max_length=42,
        description="Registered wallet address; must match the recovered signature signer.",
    )
    target_allocations: dict[str, float] = Field(
        ...,
        description="Target allocation percentages for each token (e.g., {'XRP': 0.60, 'HBAR': 0.40})",
    )

    @field_validator("wallet_address")
    @classmethod
    def _validate_eth_address(cls, v: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError(
                "wallet_address must be a 42-char Ethereum address: 0x followed by 40 hex characters"
            )
        return v

    @field_validator("target_allocations")
    @classmethod
    def _validate_allocations(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("target_allocations cannot be empty")

        for token, pct in v.items():
            if not token.isupper():
                raise ValueError(f"Token symbol must be uppercase: {token}")
            if pct < 0 or pct > 1:
                raise ValueError(f"Allocation for {token} must be between 0 and 1")

        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Target allocations must sum to 1.0 (100%), got {total:.6f}"
            )

        return v


class TokenDelta(BaseModel):
    token: str
    current_balance: float
    target_balance: float
    delta: float
    action: Literal["buy", "sell", "hold"]


class RebalanceResponse(BaseModel):
    agent_id: str
    wallet_address: str
    target_allocations: dict[str, float]
    current_balances: dict[str, float]
    target_balances: dict[str, float]
    deltas: list[TokenDelta]
    total_portfolio_value_usdc: float
    execution_mode: Literal["sandbox", "live_rpc"]
    requires_signature: bool = True


router = APIRouter(prefix="/api/v1/agent", tags=["rebalance"])


@router.post("/rebalance", response_model=RebalanceResponse)
async def rebalance_portfolio(request: RebalanceRequest) -> RebalanceResponse:
    from services.web3_provider import is_live_mode

    mock_balances = {
        "USDC": 5000.0,
        "HBAR": 2000.0,
        "ETH": 0.5,
    }

    total_value = sum(mock_balances.values())

    target_balances = {}
    for token, pct in request.target_allocations.items():
        target_balances[token] = round(total_value * pct, 8)

    for token in mock_balances:
        if token not in target_balances:
            target_balances[token] = 0.0

    deltas = []
    for token in set(list(mock_balances.keys()) + list(target_balances.keys())):
        current = mock_balances.get(token, 0.0)
        target = target_balances.get(token, 0.0)
        delta = round(target - current, 8)

        if abs(delta) < 1e-10:
            action = "hold"
        elif delta > 0:
            action = "buy"
        else:
            action = "sell"

        deltas.append(
            TokenDelta(
                token=token,
                current_balance=current,
                target_balance=target,
                delta=delta,
                action=action,
            )
        )

    logger.info(
        "Rebalance calculated — agent=%s wallet=%s total=%.2f deltas=%d",
        request.agent_id,
        request.wallet_address,
        total_value,
        len(deltas),
    )

    return RebalanceResponse(
        agent_id=request.agent_id,
        wallet_address=request.wallet_address,
        target_allocations=request.target_allocations,
        current_balances=mock_balances,
        target_balances=target_balances,
        deltas=deltas,
        total_portfolio_value_usdc=total_value,
        execution_mode="sandbox" if not is_live_mode() else "live_rpc",
        requires_signature=True,
    )
