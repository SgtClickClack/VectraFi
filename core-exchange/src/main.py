import logging
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from database import init_db
from routes.bank import router as bank_router
from routes.market import router as market_router
from routes.settlement import router as settlement_router
from routes.trade import router as trade_router
from routes.wallet import router as wallet_router
from services.web3_provider import is_live_mode


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger("vectrafi")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


configure_logging()
logger = logging.getLogger("vectrafi.app")

app = FastAPI(
    title="VectraFi Core Exchange",
    description="Agent-native exchange and banking gateway with dual-mode sandbox and live RPC routing",
    version="0.3.0",
)

app.include_router(market_router)
app.include_router(wallet_router)
app.include_router(trade_router)
app.include_router(bank_router)
app.include_router(settlement_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    logger.info(
        "%s %s -> %s (%.2fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    mode = "live_rpc" if is_live_mode() else "sandbox"
    logger.info("VectraFi core exchange started — mode=%s SQLite backend ready", mode)


@app.get("/health")
def health_check() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "vectrafi-core-exchange",
            "execution_mode": "live_rpc" if is_live_mode() else "sandbox",
        }
    )
