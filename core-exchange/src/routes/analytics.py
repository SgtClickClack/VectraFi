"""
Analytics endpoints — backs the VectraFi telemetry dashboard.

GET /api/v1/analytics/stats
    Aggregate counts and a rolling average latency from the last 200 requests.

GET /api/v1/analytics/treasury
    Treasury fee accumulator and total settled volume.

GET /api/v1/analytics/recent-transactions
    Last 10 SettlementTransaction rows ordered newest-first.

All three endpoints are read-only and require no authentication.
They degrade gracefully on an empty database (return zero-value responses).
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import PLATFORM_TREASURY_ADDRESS
from database import get_db
from models import AgentWallet, SettlementTransaction, TreasuryState
from schemas import (
    AnalyticsStatsResponse,
    AnalyticsTreasuryResponse,
    RecentTransactionItem,
    SwarmAnalyticsResponse,
    SwarmDeskState,
    SwarmHeartbeatRequest,
    TreasuryBreakdownResponse,
    TxTypeBreakdown,
)

logger = logging.getLogger("vectrafi.analytics")
router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

# ---------------------------------------------------------------------------
# Rolling latency tracker — updated by main.py middleware via record_latency().
# deque(maxlen=200) keeps only the most recent 200 request timings so the
# average stays representative under long-running servers.
# ---------------------------------------------------------------------------
_latency_window: deque[float] = deque(maxlen=200)


def record_latency(elapsed_ms: float) -> None:
    """Called by the HTTP middleware after every request completes."""
    _latency_window.append(elapsed_ms)


def _avg_latency() -> float:
    if not _latency_window:
        return 0.0
    return sum(_latency_window) / len(_latency_window)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_treasury(db: Session) -> TreasuryState:
    treasury = db.get(TreasuryState, 1)
    if treasury is None:
        treasury = TreasuryState(
            id=1,
            accumulated_fees_usdc=Decimal("0"),
            bounty_pool_fees_usdc=Decimal("0"),
        )
    return treasury


# ---------------------------------------------------------------------------
# GET /api/v1/analytics/stats
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=AnalyticsStatsResponse)
def analytics_stats(db: Session = Depends(get_db)) -> AnalyticsStatsResponse:
    """
    Returns aggregate settlement statistics and rolling average request latency.
    Safe on an empty database — all counts default to zero.
    """
    tx_count = db.query(func.count(SettlementTransaction.tx_id)).scalar() or 0
    total_volume = db.query(
        func.sum(SettlementTransaction.gross_amount_usdc)
    ).scalar() or Decimal("0")
    wallet_count = db.query(func.count(AgentWallet.agent_id)).scalar() or 0

    return AnalyticsStatsResponse(
        total_transactions_processed=int(tx_count),
        total_volume_processed_usdc=float(total_volume),
        active_wallets_count=int(wallet_count),
        success_rate_pct=100.0,
        failure_count=0,
        avg_latency_ms=round(_avg_latency(), 3),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/analytics/treasury
# ---------------------------------------------------------------------------


@router.get("/treasury", response_model=AnalyticsTreasuryResponse)
def analytics_treasury(db: Session = Depends(get_db)) -> AnalyticsTreasuryResponse:
    """
    Returns treasury fee accumulator and total settled volume.
    Mirrors /api/v1/settlement/analytics but in the shape the dashboard expects.
    """
    treasury = _get_treasury(db)
    total_volume = db.query(
        func.sum(SettlementTransaction.gross_amount_usdc)
    ).scalar() or Decimal("0")
    return AnalyticsTreasuryResponse(
        accumulated_fees_usdc=float(treasury.accumulated_fees_usdc),
        total_volume_processed_usdc=float(total_volume),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/analytics/recent-transactions
# ---------------------------------------------------------------------------


@router.get("/recent-transactions", response_model=list[RecentTransactionItem])
def analytics_recent_transactions(
    db: Session = Depends(get_db),
) -> list[RecentTransactionItem]:
    """
    Returns the 10 most recent settlement transactions, newest first.
    Returns an empty list when no transactions have been recorded yet.
    """
    rows = (
        db.query(SettlementTransaction)
        .order_by(SettlementTransaction.created_at.desc())
        .limit(10)
        .all()
    )
    return [
        RecentTransactionItem(
            tx_id=row.tx_id,
            sender_id=row.sender_id,
            receiver_id=row.receiver_id,
            gross_amount_usdc=float(row.gross_amount_usdc),
            tax_amount_usdc=float(row.tax_amount_usdc),
            net_amount_usdc=float(row.net_amount_usdc),
            tx_type=row.tx_type,
            created_at=row.created_at,
            on_chain_status=row.on_chain_status,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# GET /api/v1/analytics/swarm
# ---------------------------------------------------------------------------

# Path to the swarm daemon log — written by seed_swarm.py.
# analytics.py lives at  <project>/core-exchange/src/routes/analytics.py
# swarm log lives at     <project>/logs/swarm_activity.log
_SWARM_LOG = (
    Path(__file__)
    .resolve()
    .parent.parent.parent.parent  # routes/  # src/  # core-exchange/  # <project root>
    / "logs"
    / "swarm_activity.log"
)
_TAIL_READ = 100  # lines scanned for state parsing
_TAIL_RETURN = 30  # lines returned to the UI terminal

# ---------------------------------------------------------------------------
# In-memory swarm heartbeat store — populated by POST /swarm/heartbeat.
# Allows a swarm running anywhere (locally or on Railway) to push its state
# to the dashboard without relying on a shared log file.
# ---------------------------------------------------------------------------
_heartbeat: SwarmAnalyticsResponse | None = None
_heartbeat_ts: float = 0.0
_HEARTBEAT_TTL = 90.0  # seconds; beyond this the swarm is considered stopped

_RE_DESK = re.compile(r"DESK\s+(\w+)\s+balance=([\d.]+)\s+USDC\s+ok=(\d+)\s+err=(\d+)")
_RE_SWARM = re.compile(r"SWARM\s+iter=(\d+)\s+route_checks=(\d+)\s+viable=(\d+)")
_RE_TS = re.compile(r"^(\d{2}:\d{2}:\d{2})")
_RE_GAS_GUARD = re.compile(r"GAS-GUARD\s+(\w+)\s+eth_balance=([\d.]+)")
_RE_EQUALIZE = re.compile(r"EQUALIZE\s+\w+\s+.*requesting\s+([\d.]+)\s+USDC")
# Matches: TRANSFER  Alpha   → Beta    $  30.00   50ms  OK  [swarm_equalization]
_RE_EQ_TRANSFER = re.compile(
    r"TRANSFER\s+\S+\s+→\s+\S+\s+\$([\d.]+)\s+\S+\s+OK\s+\[swarm_equalization\]"
)


def _tail_log(path: Path, n: int) -> list[str]:
    """Return up to the last n non-empty lines from path."""
    buf: deque[str] = deque(maxlen=n)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.rstrip("\n\r")
            if stripped:
                buf.append(stripped)
    return list(buf)


@router.post(
    "/swarm/heartbeat", response_model=SwarmAnalyticsResponse, include_in_schema=False
)
def swarm_heartbeat(body: SwarmHeartbeatRequest) -> SwarmAnalyticsResponse:
    """
    Accepts a push heartbeat from seed_swarm.py (wherever it runs).
    Stored in memory; read by GET /swarm with a 90-second TTL.
    """
    global _heartbeat, _heartbeat_ts
    _heartbeat = SwarmAnalyticsResponse(
        swarm_active=True,
        log_lines=[],
        desks=body.desks,
        iterations=body.iterations,
        route_checks=body.route_checks,
        viable_routes=body.viable_routes,
        last_activity=time.strftime("%H:%M:%S", time.localtime()),
        equalization_count=body.equalization_count,
        equalization_volume_usdc=body.equalization_volume_usdc,
    )
    _heartbeat_ts = time.monotonic()
    return _heartbeat


def _read_log_tail() -> list[str]:
    """Return the last _TAIL_RETURN non-empty log lines, or [] if the file is absent."""
    if not _SWARM_LOG.exists():
        return []
    return _tail_log(_SWARM_LOG, _TAIL_READ)[-_TAIL_RETURN:]


@router.get("/swarm", response_model=SwarmAnalyticsResponse)
def analytics_swarm() -> SwarmAnalyticsResponse:
    """
    Returns current swarm state. Checks the push-heartbeat store first
    (populated by seed_swarm.py via POST /swarm/heartbeat); falls back to
    tail-reading swarm_activity.log for swarms started via the dashboard button.
    Returns an inactive response when neither source has data.

    Log lines are always sourced from the file tail so the dashboard terminal
    is never empty while the swarm is running — the heartbeat carries metrics
    only (no log text).
    """
    # Always read the current log tail for the terminal panel.
    log_lines = _read_log_tail()

    # Prefer push-based heartbeat (works for both local and remote swarms)
    if _heartbeat is not None:
        age = time.monotonic() - _heartbeat_ts
        if age <= _HEARTBEAT_TTL:
            # Merge live log tail into the cached heartbeat state.
            return SwarmAnalyticsResponse(
                swarm_active=_heartbeat.swarm_active,
                log_lines=log_lines,
                desks=_heartbeat.desks,
                iterations=_heartbeat.iterations,
                route_checks=_heartbeat.route_checks,
                viable_routes=_heartbeat.viable_routes,
                last_activity=_heartbeat.last_activity,
                equalization_count=_heartbeat.equalization_count,
                equalization_volume_usdc=_heartbeat.equalization_volume_usdc,
            )
        # Heartbeat expired — swarm has stopped; fall through to log

    if not log_lines:
        return SwarmAnalyticsResponse(
            swarm_active=False,
            log_lines=[],
            desks=[],
        )

    # Parse the wider scan window for structured state, display the trimmed tail.
    scan_lines = _tail_log(_SWARM_LOG, _TAIL_READ)

    desks: dict[str, SwarmDeskState] = {}
    iterations: int | None = None
    route_checks: int | None = None
    viable_routes: int | None = None
    last_activity: str | None = None
    equalization_count: int = 0
    equalization_volume: float = 0.0
    eth_balances: dict[str, float] = {}

    for line in scan_lines:
        m = _RE_DESK.search(line)
        if m:
            name = m.group(1)
            desks[name] = SwarmDeskState(
                name=name,
                balance_usdc=float(m.group(2)),
                transfers_ok=int(m.group(3)),
                transfers_err=int(m.group(4)),
            )
        m2 = _RE_SWARM.search(line)
        if m2:
            iterations = int(m2.group(1))
            route_checks = int(m2.group(2))
            viable_routes = int(m2.group(3))
        ts = _RE_TS.match(line)
        if ts:
            last_activity = ts.group(1)
        meq = _RE_EQ_TRANSFER.search(line)
        if meq:
            equalization_count += 1
            equalization_volume += float(meq.group(1))
        mg = _RE_GAS_GUARD.search(line)
        if mg:
            eth_balances[mg.group(1)] = float(mg.group(2))

    for dname, eth_bal in eth_balances.items():
        if dname in desks:
            desks[dname] = desks[dname].model_copy(update={"eth_balance": eth_bal})

    return SwarmAnalyticsResponse(
        swarm_active=bool(desks or iterations is not None),
        log_lines=log_lines,
        desks=list(desks.values()),
        iterations=iterations,
        route_checks=route_checks,
        viable_routes=viable_routes,
        last_activity=last_activity,
        equalization_count=equalization_count,
        equalization_volume_usdc=equalization_volume,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/analytics/treasury-breakdown
# ---------------------------------------------------------------------------


@router.get("/treasury-breakdown", response_model=TreasuryBreakdownResponse)
def analytics_treasury_breakdown(
    db: Session = Depends(get_db),
) -> TreasuryBreakdownResponse:
    """
    Returns treasury fee accumulation broken down by transaction type.

    Isolates swarm_equalization transactions from organic trades so the
    telemetry engine can surface accurate platform tax routing metrics.
    Feeds directly into the visualization panel without shared-memory overhead.
    """
    from sqlalchemy import func as sa_func

    treasury = _get_treasury(db)

    rows = (
        db.query(
            SettlementTransaction.tx_type,
            sa_func.count(SettlementTransaction.tx_id).label("cnt"),
            sa_func.sum(SettlementTransaction.gross_amount_usdc).label("vol"),
            sa_func.sum(SettlementTransaction.tax_amount_usdc).label("tax"),
        )
        .group_by(SettlementTransaction.tx_type)
        .all()
    )

    breakdown: list[TxTypeBreakdown] = []
    equalization_fees: float = 0.0

    for row in rows:
        tx_type, cnt, vol, tax = row
        tax_f = float(tax or 0)
        breakdown.append(
            TxTypeBreakdown(
                tx_type=tx_type,
                count=int(cnt or 0),
                total_volume_usdc=float(vol or 0),
                total_tax_usdc=tax_f,
            )
        )
        if tx_type == "swarm_equalization":
            equalization_fees = tax_f

    breakdown.sort(key=lambda b: b.tx_type)

    return TreasuryBreakdownResponse(
        accumulated_fees_usdc=float(treasury.accumulated_fees_usdc),
        equalization_fees_usdc=equalization_fees,
        platform_treasury_address=PLATFORM_TREASURY_ADDRESS,
        tx_type_breakdown=breakdown,
    )
