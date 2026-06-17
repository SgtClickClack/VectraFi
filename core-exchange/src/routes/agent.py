import logging
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import get_db
from models import AgentWallet
from routes.auth import verify_signed_payload
from schemas import RebalanceRequest, RebalanceResponse, RebalanceSwap
from services.pricing import get_active_prices
from services.web3_provider import (
    get_on_chain_eth_balance,
    is_live_mode,
    prepare_transaction_payload,
)

logger = logging.getLogger("vectrafi.agent")
router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

SUPPORTED_TOKENS = ("USDC", "HBAR")
PRECISION = Decimal("0.00000001")
ALLOCATION_PRECISION = Decimal("0.00000001")
ZERO = Decimal("0")


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(PRECISION, rounding=ROUND_HALF_UP)


def _as_float(value: Decimal) -> float:
    return float(_quantize(value))


def _portfolio_values(wallet: AgentWallet, prices: dict[str, float]) -> dict[str, Decimal]:
    return {
        "USDC": _decimal(wallet.balance_usdc) * _decimal(prices["USDC"]),
        "HBAR": _decimal(wallet.balance_hbar) * _decimal(prices["HBAR"]),
    }


def _allocations(values: dict[str, Decimal], total_value: Decimal) -> dict[str, float]:
    if total_value <= ZERO:
        return {token: 0.0 for token in SUPPORTED_TOKENS}
    return {
        token: float((values[token] / total_value).quantize(ALLOCATION_PRECISION))
        for token in SUPPORTED_TOKENS
    }


@router.post("/rebalance", response_model=RebalanceResponse)
async def rebalance_agent_portfolio(
    request: Request,
    db: Session = Depends(get_db),
) -> RebalanceResponse:
    payload = await verify_signed_payload(request, db, RebalanceRequest)

    wallet = db.get(AgentWallet, payload.agent_id)
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for agent_id '{payload.agent_id}'",
        )

    prices = get_active_prices()
    prices_decimal = {token: _decimal(prices[token]) for token in SUPPORTED_TOKENS}
    if any(price <= ZERO for price in prices_decimal.values()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Active prices must be positive before rebalancing",
        )

    live_mode = is_live_mode()
    execution_mode = "live_rpc" if live_mode else "sandbox"

    on_chain_eth_balance: float | None = None
    prepared_transaction = None
    if live_mode:
        on_chain_eth_balance = get_on_chain_eth_balance(wallet.wallet_address)
        prepared_transaction = prepare_transaction_payload(
            wallet.wallet_address,
            operation="rebalance",
            metadata={
                "agent_id": payload.agent_id,
                "target_allocations": payload.target_allocations,
                "reference_prices": prices,
            },
        )

    values_before = _portfolio_values(wallet, prices)
    total_before = sum(values_before.values(), ZERO)
    if total_before <= ZERO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot rebalance an empty portfolio",
        )

    allocations_before = _allocations(values_before, total_before)
    target_values = {
        token: total_before * _decimal(payload.target_allocations.get(token, 0.0))
        for token in SUPPORTED_TOKENS
    }
    deltas = {token: target_values[token] - values_before[token] for token in SUPPORTED_TOKENS}

    usdc_balance = _decimal(wallet.balance_usdc)
    hbar_balance = _decimal(wallet.balance_hbar)
    swaps: list[RebalanceSwap] = []

    if deltas["USDC"] < -PRECISION:
        usdc_value_to_sell = min(-deltas["USDC"], usdc_balance * prices_decimal["USDC"])
        amount_in = _quantize(usdc_value_to_sell / prices_decimal["USDC"])
        amount_out = _quantize(usdc_value_to_sell / prices_decimal["HBAR"])
        usdc_balance -= amount_in
        hbar_balance += amount_out
        swaps.append(
            RebalanceSwap(
                from_token="USDC",
                to_token="HBAR",
                amount_in=float(amount_in),
                amount_out=float(amount_out),
                execution_price=float(prices_decimal["HBAR"] / prices_decimal["USDC"]),
            )
        )
    elif deltas["HBAR"] < -PRECISION:
        hbar_value_to_sell = min(-deltas["HBAR"], hbar_balance * prices_decimal["HBAR"])
        amount_in = _quantize(hbar_value_to_sell / prices_decimal["HBAR"])
        amount_out = _quantize(hbar_value_to_sell / prices_decimal["USDC"])
        hbar_balance -= amount_in
        usdc_balance += amount_out
        swaps.append(
            RebalanceSwap(
                from_token="HBAR",
                to_token="USDC",
                amount_in=float(amount_in),
                amount_out=float(amount_out),
                execution_price=float(prices_decimal["HBAR"] / prices_decimal["USDC"]),
            )
        )

    if swaps:
        wallet.balance_usdc = _as_float(max(usdc_balance, ZERO))
        wallet.balance_hbar = _as_float(max(hbar_balance, ZERO))
        db.commit()
        db.refresh(wallet)

    values_after = _portfolio_values(wallet, prices)
    total_after = sum(values_after.values(), ZERO)

    logger.info(
        "Rebalance executed mode=%s agent=%s swaps=%d value_before=%.6f value_after=%.6f usdc=%.6f hbar=%.6f",
        execution_mode,
        payload.agent_id,
        len(swaps),
        float(total_before),
        float(total_after),
        wallet.balance_usdc,
        wallet.balance_hbar,
    )

    return RebalanceResponse(
        agent_id=wallet.agent_id,
        wallet_address=wallet.wallet_address,
        target_allocations={
            token: payload.target_allocations.get(token, 0.0)
            for token in SUPPORTED_TOKENS
        },
        current_allocations_before=allocations_before,
        current_allocations_after=_allocations(values_after, total_after),
        portfolio_value_usdc_before=_as_float(total_before),
        portfolio_value_usdc_after=_as_float(total_after),
        swaps=swaps,
        already_balanced=len(swaps) == 0,
        balance_usdc=wallet.balance_usdc,
        balance_hbar=wallet.balance_hbar,
        execution_mode=execution_mode,
        on_chain_eth_balance_eth=on_chain_eth_balance,
        prepared_transaction=prepared_transaction,
    )
