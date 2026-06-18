import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

from database import init_db
from routes.analytics import record_latency
from routes.analytics import router as analytics_router
from routes.arbitrage import router as arbitrage_router
from routes.bank import router as bank_router
from routes.market import router as market_router
from routes.protocol import router as protocol_router
from routes.settlement import router as settlement_router
from routes.swarm_control import router as swarm_control_router
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

# ---------------------------------------------------------------------------
# CORS
# ALLOWED_ORIGINS env var: comma-separated list of permitted origins.
# Local dev defaults are always included so unset == safe for development.
# ---------------------------------------------------------------------------
_DEFAULT_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8000",
]
_env_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
_origins = list(dict.fromkeys(_DEFAULT_ORIGINS + _env_origins))  # dedupe, preserve order

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-VectraFi-Signature"],
)

app.include_router(analytics_router)
app.include_router(arbitrage_router)
app.include_router(market_router)
app.include_router(wallet_router)
app.include_router(trade_router)
app.include_router(bank_router)
app.include_router(settlement_router)
app.include_router(swarm_control_router)
app.include_router(protocol_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    record_latency(elapsed_ms)
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


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    return HTMLResponse((_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
def health_check() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "vectrafi-core-exchange",
            "execution_mode": "live_rpc" if is_live_mode() else "sandbox",
        }
    )
