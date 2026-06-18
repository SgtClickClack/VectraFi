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
from collections import deque
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import AgentWallet, SettlementTransaction, TreasuryState
from schemas import (
    AnalyticsStatsResponse,
    AnalyticsTreasuryResponse,
    RecentTransactionItem,
    SwarmAnalyticsResponse,
    SwarmDeskState,
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
    total_volume = (
        db.query(func.sum(SettlementTransaction.gross_amount_usdc)).scalar()
        or Decimal("0")
    )
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
    total_volume = (
        db.query(func.sum(SettlementTransaction.gross_amount_usdc)).scalar()
        or Decimal("0")
    )
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
    Path(__file__).resolve().parent  # routes/
    .parent                          # src/
    .parent                          # core-exchange/
    .parent                          # <project root>
    / "logs" / "swarm_activity.log"
)
_TAIL_READ   = 100   # lines scanned for state parsing
_TAIL_RETURN = 30    # lines returned to the UI terminal

_RE_DESK  = re.compile(
    r"DESK\s+(\w+)\s+balance=([\d.]+)\s+USDC\s+ok=(\d+)\s+err=(\d+)"
)
_RE_SWARM = re.compile(
    r"SWARM\s+iter=(\d+)\s+route_checks=(\d+)\s+viable=(\d+)"
)
_RE_TS    = re.compile(r"^(\d{2}:\d{2}:\d{2})")


def _tail_log(path: Path, n: int) -> list[str]:
    """Return up to the last n non-empty lines from path."""
    buf: deque[str] = deque(maxlen=n)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.rstrip("\n\r")
            if stripped:
                buf.append(stripped)
    return list(buf)


@router.get("/swarm", response_model=SwarmAnalyticsResponse)
def analytics_swarm() -> SwarmAnalyticsResponse:
    """
    Tail-reads swarm_activity.log and returns parsed desk states plus the last
    30 raw log lines for the live terminal view in the telemetry dashboard.
    Returns an empty/inactive response when the log file does not yet exist.
    """
    if not _SWARM_LOG.exists():
        return SwarmAnalyticsResponse(
            swarm_active=False,
            log_lines=[],
            desks=[],
        )

    lines = _tail_log(_SWARM_LOG, _TAIL_READ)

    desks: dict[str, SwarmDeskState] = {}
    iterations:    int | None = None
    route_checks:  int | None = None
    viable_routes: int | None = None
    last_activity: str | None = None

    for line in lines:
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
            iterations    = int(m2.group(1))
            route_checks  = int(m2.group(2))
            viable_routes = int(m2.group(3))
        ts = _RE_TS.match(line)
        if ts:
            last_activity = ts.group(1)

    display_lines = lines[-_TAIL_RETURN:]

    return SwarmAnalyticsResponse(
        swarm_active=bool(desks or iterations is not None),
        log_lines=display_lines,
        desks=list(desks.values()),
        iterations=iterations,
        route_checks=route_checks,
        viable_routes=viable_routes,
        last_activity=last_activity,
    )
