import logging
import time
from typing import Any

import httpx

from config import (
    COINBASE_API_BASE,
    FALLBACK_PRICES,
    HTTP_TIMEOUT_SECONDS,
    PRICE_CACHE_TTL_SECONDS,
)
from schemas import MarketPricesResponse

logger = logging.getLogger("vectrafi.pricing")

_price_cache: dict[str, float] | None = None
_cache_timestamp: float = 0.0
_last_source: str = "fallback"


async def _fetch_coinbase_spot(client: httpx.AsyncClient, pair: str) -> float:
    response = await client.get(f"{COINBASE_API_BASE}/{pair}/spot")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    amount = payload["data"]["amount"]
    return float(amount)


async def fetch_live_prices() -> MarketPricesResponse:
    global _price_cache, _cache_timestamp, _last_source

    prices = dict(FALLBACK_PRICES)
    live_assets: list[str] = []
    failed_assets: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            for asset, pair in (
                ("ETH", "ETH-USD"),
                ("USDC", "USDC-USD"),
                ("HBAR", "HBAR-USD"),
            ):
                try:
                    prices[asset] = await _fetch_coinbase_spot(client, pair)
                    live_assets.append(asset)
                except (
                    httpx.TimeoutException,
                    httpx.HTTPError,
                    KeyError,
                    ValueError,
                    TypeError,
                ) as exc:
                    failed_assets.append(asset)
                    logger.warning(
                        "Failed to fetch %s spot price — using fallback: %s", asset, exc
                    )
    except httpx.TimeoutException as exc:
        logger.warning(
            "Market price request timed out — using full fallback set: %s", exc
        )
        return _build_fallback_response()

    if live_assets:
        _price_cache = prices
        _cache_timestamp = time.time()
        _last_source = "live" if not failed_assets else "fallback"

        source: str = "live" if len(live_assets) == 3 else "fallback"
        if failed_assets:
            logger.info(
                "Partial live prices — live=%s fallback=%s",
                ",".join(live_assets),
                ",".join(failed_assets),
            )
        else:
            logger.info(
                "Live market prices fetched — ETH=%.4f USDC=%.6f HBAR=%.6f",
                prices["ETH"],
                prices["USDC"],
                prices["HBAR"],
            )

        return MarketPricesResponse(
            ETH=prices["ETH"],
            USDC=prices["USDC"],
            HBAR=prices["HBAR"],
            source=source,  # type: ignore[arg-type]
        )

    return _build_fallback_response()


def _build_fallback_response(reason: str | None = None) -> MarketPricesResponse:
    global _price_cache, _cache_timestamp, _last_source

    _price_cache = dict(FALLBACK_PRICES)
    _cache_timestamp = time.time()
    _last_source = "fallback"

    if reason:
        logger.info("Serving fallback prices due to: %s", reason)

    return MarketPricesResponse(
        ETH=FALLBACK_PRICES["ETH"],
        USDC=FALLBACK_PRICES["USDC"],
        HBAR=FALLBACK_PRICES["HBAR"],
        source="fallback",
    )


def get_active_prices() -> dict[str, float]:
    if _price_cache and (time.time() - _cache_timestamp) < PRICE_CACHE_TTL_SECONDS:
        return dict(_price_cache)
    return dict(FALLBACK_PRICES)


def get_last_price_source() -> str:
    return _last_source
