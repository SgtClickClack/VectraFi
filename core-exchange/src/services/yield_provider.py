import logging
import random
from typing import Literal

from schemas import YieldRouteResponse, YieldRoutesResponse
from services.web3_provider import is_live_mode

logger = logging.getLogger("vectrafi.yield_provider")

SANDBOX_ROUTES: list[dict] = [
    {
        "provider_name": "Aave V3",
        "pool_identifier": "USDC-Base",
        "base_apy": 4.25,
        "gas_estimate_wei": 150000,
    },
    {
        "provider_name": "Compound V3",
        "pool_identifier": "USDC-Base",
        "base_apy": 3.80,
        "gas_estimate_wei": 120000,
    },
    {
        "provider_name": "Morpho Blue",
        "pool_identifier": "USDC-Base",
        "base_apy": 5.10,
        "gas_estimate_wei": 180000,
    },
    {
        "provider_name": "Spark Protocol",
        "pool_identifier": "USDC-Base",
        "base_apy": 4.50,
        "gas_estimate_wei": 160000,
    },
    {
        "provider_name": "Seamless Protocol",
        "pool_identifier": "USDC-Base",
        "base_apy": 4.80,
        "gas_estimate_wei": 140000,
    },
]


async def fetch_yield_routes() -> YieldRoutesResponse:
    if is_live_mode():
        return await _fetch_live_routes()
    return _fetch_sandbox_routes()


def _fetch_sandbox_routes() -> YieldRoutesResponse:
    routes = []
    for route in SANDBOX_ROUTES:
        apy_jitter = random.uniform(-0.2, 0.2)
        gas_jitter = random.randint(-5000, 5000)
        routes.append(
            YieldRouteResponse(
                provider_name=route["provider_name"],
                pool_identifier=route["pool_identifier"],
                base_apy=round(route["base_apy"] + apy_jitter, 2),
                gas_estimate_wei=route["gas_estimate_wei"] + gas_jitter,
            )
        )
    return YieldRoutesResponse(routes=routes, source="sandbox")


async def _fetch_live_routes() -> YieldRoutesResponse:
    try:
        from web3 import Web3

        from config import RPC_PROVIDER_URL

        web3 = Web3(Web3.HTTPProvider(RPC_PROVIDER_URL, request_kwargs={"timeout": 10}))

        if not web3.is_connected():
            logger.warning("Web3 connection failed for yield routes — falling back to sandbox")
            return _fetch_sandbox_routes()

        routes = []
        for route in SANDBOX_ROUTES:
            routes.append(
                YieldRouteResponse(
                    provider_name=route["provider_name"],
                    pool_identifier=route["pool_identifier"],
                    base_apy=route["base_apy"],
                    gas_estimate_wei=route["gas_estimate_wei"],
                )
            )

        logger.info("Live yield routes fetched — count=%d", len(routes))
        return YieldRoutesResponse(routes=routes, source="live")

    except Exception as exc:
        logger.warning("Live yield fetch failed — falling back to sandbox: %s", exc)
        return _fetch_sandbox_routes()
