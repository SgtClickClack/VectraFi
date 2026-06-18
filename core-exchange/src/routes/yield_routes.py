import logging

from fastapi import APIRouter

from schemas import YieldRouteResponse
from services.web3_provider import get_web3, is_live_mode

logger = logging.getLogger("vectrafi.yield")
router = APIRouter(prefix="/api/v1/yield", tags=["yield"])

SANDBOX_YIELD_ROUTES = (
    YieldRouteResponse(
        provider_name="local_sandbox",
        pool_identifier="usdc-hbar-sandbox-vault",
        base_apy=0.0425,
        gas_estimate_wei=0,
    ),
    YieldRouteResponse(
        provider_name="local_sandbox",
        pool_identifier="usdc-hbar-treasury-buffer",
        base_apy=0.018,
        gas_estimate_wei=0,
    ),
)

LIVE_ROUTE_TEMPLATES = (
    ("configured_rpc", "usdc-hbar-rpc-yield"),
    ("configured_rpc", "usdc-hbar-liquidity-buffer"),
)


def _live_gas_estimate_wei() -> int:
    web3 = get_web3()
    if web3 is None:
        return 0

    try:
        return int(web3.eth.gas_price)
    except Exception as exc:
        logger.warning("Failed to read live gas price for yield routes: %s", exc)
        return 0


@router.get("/routes", response_model=list[YieldRouteResponse])
def list_yield_routes() -> list[YieldRouteResponse]:
    if not is_live_mode():
        logger.info("Serving sandbox yield routes")
        return list(SANDBOX_YIELD_ROUTES)

    gas_estimate_wei = _live_gas_estimate_wei()
    logger.info("Serving live RPC yield routes gas_estimate_wei=%s", gas_estimate_wei)
    return [
        YieldRouteResponse(
            provider_name=provider_name,
            pool_identifier=pool_identifier,
            base_apy=base_apy,
            gas_estimate_wei=gas_estimate_wei,
        )
        for base_apy, (provider_name, pool_identifier) in zip(
            (0.0375, 0.014),
            LIVE_ROUTE_TEMPLATES,
            strict=True,
        )
    ]
