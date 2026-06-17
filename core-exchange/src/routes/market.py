import logging

from fastapi import APIRouter

from schemas import MarketPricesResponse
from services.pricing import fetch_live_prices

logger = logging.getLogger("vectrafi.market")
router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/prices", response_model=MarketPricesResponse)
async def get_market_prices() -> MarketPricesResponse:
    prices = await fetch_live_prices()
    logger.info(
        "Market prices served — source=%s ETH=%.4f USDC=%.6f HBAR=%.6f",
        prices.source,
        prices.ETH,
        prices.USDC,
        prices.HBAR,
    )
    return prices
