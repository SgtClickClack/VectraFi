"""
Cross-agent arbitrage router — POST /api/v1/arbitrage/route-path

Performs a fully isolated dry-run viability check for a proposed arbitrage
execution path across an ordered chain of agents.  The entire simulation runs
inside a single SQLAlchemy savepoint (db.begin_nested()) that is ALWAYS rolled
back — no ledger rows are written or modified.

Per-leg checks (for every agent_id in agent_chain):
  1. Active registered wallet exists in agent_wallets.
  2. Current balance >= per-leg slippage floor (volume * slip_pct / n).
  3. Zero SettlementTransaction rows with on_chain_status='PENDING_SYNC' as
     either sender or receiver (on-chain limbo blocks safe routing).

Slippage model:
  - total_slippage   = volume_usdc * slippage_tolerance_pct
  - slip_floor_i     = total_slippage / len(agent_chain)  (evenly distributed)
  - expected_output  = volume_usdc - total_slippage
  - Simulation deducts slip_floor_i from each agent's balance in order so that
    an agent appearing multiple times in the chain is charged for each leg
    separately (cumulative cost correctly detected).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db
from models import AgentWallet, SettlementTransaction
from schemas import ArbitrageRouteRequest, ArbitrageRouteResponse, ArbitrageStepResult

logger = logging.getLogger("vectrafi.arbitrage")
router = APIRouter(prefix="/api/v1/arbitrage", tags=["arbitrage"])


@router.post("/route-path", response_model=ArbitrageRouteResponse)
def arbitrage_route_path(
    payload: ArbitrageRouteRequest,
    db: Session = Depends(get_db),
) -> ArbitrageRouteResponse:
    """
    Dry-run viability check for a proposed cross-agent arbitrage route.

    No funds are moved. The simulation runs inside a savepoint that is always
    rolled back, leaving the ledger untouched regardless of the result.
    """
    chain = payload.agent_chain
    n = len(chain)
    volume = Decimal(str(payload.volume_usdc))
    slip_pct = Decimal(str(payload.slippage_tolerance_pct))

    total_slippage = (volume * slip_pct).quantize(Decimal("0.00000001"))
    slip_floor = (total_slippage / Decimal(n)).quantize(Decimal("0.00000001"))
    expected_output = volume - total_slippage

    steps: list[ArbitrageStepResult] = []
    rejection_reason: str | None = None

    savepoint = db.begin_nested()
    try:
        for i, agent_id in enumerate(chain):
            wallet = db.get(AgentWallet, agent_id)

            if wallet is None:
                steps.append(
                    ArbitrageStepResult(
                        step=i,
                        agent_id=agent_id,
                        balance_usdc=0.0,
                        slippage_floor_usdc=float(slip_floor),
                        balance_sufficient=False,
                        pending_sync_blocked=False,
                        wallet_found=False,
                    )
                )
                if rejection_reason is None:
                    rejection_reason = (
                        f"step {i}: agent '{agent_id}' has no registered wallet"
                    )
                continue

            # Check for on-chain limbo: any PENDING_SYNC row where this agent
            # is sender or receiver blocks them from participating in new routes.
            pending_count: int = (
                db.query(func.count(SettlementTransaction.tx_id))
                .filter(
                    SettlementTransaction.on_chain_status == "PENDING_SYNC",
                    or_(
                        SettlementTransaction.sender_id == agent_id,
                        SettlementTransaction.receiver_id == agent_id,
                    ),
                )
                .scalar()
                or 0
            )
            is_blocked = pending_count > 0

            balance_before = Decimal(str(wallet.balance_usdc))
            sufficient = balance_before >= slip_floor

            # Simulate the per-leg slippage deduction so that agents appearing
            # multiple times in the chain are charged cumulatively.  The
            # savepoint rollback below discards all mutations after the check.
            wallet.balance_usdc = balance_before - slip_floor
            db.flush()

            steps.append(
                ArbitrageStepResult(
                    step=i,
                    agent_id=agent_id,
                    balance_usdc=float(balance_before),
                    slippage_floor_usdc=float(slip_floor),
                    balance_sufficient=sufficient,
                    pending_sync_blocked=is_blocked,
                    wallet_found=True,
                )
            )

            if rejection_reason is None:
                if is_blocked:
                    rejection_reason = (
                        f"step {i}: agent '{agent_id}' has {pending_count} "
                        "PENDING_SYNC transaction(s) blocking participation"
                    )
                elif not sufficient:
                    rejection_reason = (
                        f"step {i}: agent '{agent_id}' balance "
                        f"{float(balance_before):.6f} USDC is below "
                        f"slippage floor {float(slip_floor):.6f} USDC"
                    )
    finally:
        savepoint.rollback()

    viable = all(
        s.wallet_found and s.balance_sufficient and not s.pending_sync_blocked
        for s in steps
    )

    logger.info(
        "Arbitrage route-path dry-run: viable=%s agents=%d volume=%.4f "
        "total_slip=%.4f reason=%s",
        viable,
        n,
        float(volume),
        float(total_slippage),
        rejection_reason,
    )

    return ArbitrageRouteResponse(
        viable=viable,
        entry_asset=payload.entry_asset,
        exit_asset=payload.exit_asset,
        volume_usdc=float(volume),
        agent_chain=chain,
        slippage_tolerance_pct=float(slip_pct),
        steps=steps,
        total_slippage_usdc=float(total_slippage),
        expected_output_usdc=float(expected_output),
        rejection_reason=rejection_reason,
    )
