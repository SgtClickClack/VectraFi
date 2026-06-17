import logging
from typing import Literal

from fastapi import APIRouter

from schemas import YieldRouteResponse, YieldRoutesResponse
from services.yield_provider import fetch_yield_routes

logger = logging.getLogger("vectrafi.yield")
router = APIRouter(prefix="/api/v1/yield", tags=["yield"])


@router.get("/routes", response_model=YieldRoutesResponse)
async def get_yield_routes() -> YieldRoutesResponse:
    routes = await fetch_yield_routes()
    logger.info(
        "Yield routes served — source=%s count=%d",
        routes.source,
        len(routes.routes),
    )
    return routes
