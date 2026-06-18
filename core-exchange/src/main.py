import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_TEMPLATES_DIR   = Path(__file__).resolve().parent / "templates"
_WELL_KNOWN_DIR  = Path(__file__).resolve().parent / ".well-known"

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
# ALLOWED_ORIGINS env var controls access policy:
#   "*"                → wildcard mode: any origin, no credentials (agentic/public)
#   unset or empty     → dev defaults only (localhost), with credentials
#   comma-separated    → explicit allowlist, with credentials
#
# Wildcard mode intentionally omits allow_credentials because starlette
# rejects the combination of allow_origins=["*"] + allow_credentials=True.
# Programmatic M2M agents do not use cookie-based auth so this is safe.
# ---------------------------------------------------------------------------
_AGENTIC_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-VectraFi-Signature",
    "X-Agent-ID",
    "X-Request-ID",
]
_AGENTIC_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]

_raw_allowed = os.getenv("ALLOWED_ORIGINS", "").strip()
_wildcard_cors = _raw_allowed == "*"

if _wildcard_cors:
    # Public / agentic mode — any origin, all methods and headers allowed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    _DEFAULT_ORIGINS = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ]
    _env_origins = [o.strip() for o in _raw_allowed.split(",") if o.strip()]
    _origins = list(dict.fromkeys(_DEFAULT_ORIGINS + _env_origins))  # dedupe, preserve order
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=_AGENTIC_METHODS,
        allow_headers=_AGENTIC_HEADERS,
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

# Passive agent discovery — served at /.well-known/agent.json (and siblings).
# Mounted after all API routers so the static path cannot shadow any endpoint.
# StaticFiles resolves relative to the directory where uvicorn starts (core-exchange/src/).
if _WELL_KNOWN_DIR.is_dir():
    app.mount("/.well-known", StaticFiles(directory=str(_WELL_KNOWN_DIR)), name="well-known")


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
    cors_mode = "wildcard (*)" if _wildcard_cors else "restricted origins"
    logger.info(
        "VectraFi core exchange started — mode=%s CORS=%s "
        "OpenAPI: /openapi.json  Swagger UI: /docs",
        mode, cors_mode,
    )


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
