"""
Cross-agent arbitrage and autonomous rebalancing router.

  POST /api/v1/arbitrage/route-path  — dry-run viability check for an
      ordered agent chain (no ledger mutations committed).

  POST /api/v1/arbitrage/rebalance   — autonomous 3-hop liquidity rebalance
      triggered when a target agent's balance breaches its safety floor.

Dry-run simulation model (route-path and rebalance phase 1 share _simulate_chain):
  For every agent in agent_chain:
    1. Active registered wallet exists in agent_wallets.
    2. Current balance >= per-leg slippage floor (volume * slip_pct / n).
    3. Zero SettlementTransaction rows with on_chain_status='PENDING_SYNC' as
       either sender or receiver (on-chain limbo blocks safe routing).

Rebalance execution model (3-hop relay chain):
  Relay amounts cascade so each relay agent forwards the net it just received:
    hop 0: relay_0 → relay_1   gross = volume_usdc
    hop 1: relay_1 → relay_2   gross = volume * (1 − 0.1%)
    hop 2: relay_2 → target    gross = volume * (1 − 0.1%)²

  Net effects on each participant:
    relay_0 : loses volume_usdc (full gross initiator)
    relay_1 : loses only the 0.1% tax on hop_1 gross (~0.0999% of volume)
    relay_2 : loses only the 0.1% tax on hop_2 gross (~0.0998% of volume)
    target  : gains volume * 0.999³ ≈ 99.70% of volume
"""

from __future__ import annotations

import itertools
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import AgentWallet, SettlementTransaction
from services.pricing import get_active_prices
from schemas import (
    ArbitrageRouteRequest,
    ArbitrageRouteResponse,
    ArbitrageStepResult,
    PathScanResult,
    RebalanceHop,
    RebalanceRequest,
    RebalanceResponse,
    ScanPathsRequest,
    ScanPathsResponse,
)

logger = logging.getLogger("vectrafi.arbitrage")
router = APIRouter(prefix="/api/v1/arbitrage", tags=["arbitrage"])

_RELAY_HOPS = 3
_CANDIDATE_CAP = 15  # top-N agents queried as relay candidates
_TAX_NET = Decimal("0.999")  # 1 − 0.1%
_GAS_COST_PER_HOP = Decimal("0.03")  # static L2 gas friction per leg (USDC)


# ---------------------------------------------------------------------------
# Shared simulation primitive — used by both route-path and rebalance
# ---------------------------------------------------------------------------


def _simulate_chain(
    db: Session,
    chain: list[str],
    volume_usdc: float,
    slippage_tolerance_pct: float,
) -> tuple[bool, list[ArbitrageStepResult], float, float, str | None]:
    """
    Dry-run viability check on an ordered agent chain.

    Pure read-only: fetches all wallets in a single batched IN query and
    evaluates balance sufficiency in Python — no savepoint, no DB writes,
    no rollback required.

    Per-leg cost = slippage_floor + GAS_COST_PER_HOP.

    Returns:
        (viable, steps, total_slippage_usdc, expected_output_usdc, rejection_reason)
    """
    n = len(chain)
    volume = Decimal(str(volume_usdc))
    slip = Decimal(str(slippage_tolerance_pct))

    total_slippage = (volume * slip).quantize(Decimal("0.00000001"))
    slip_floor = (total_slippage / Decimal(n)).quantize(Decimal("0.00000001"))
    # expected_output is the value the trader receives after slippage.
    # Gas is captured in per_leg_cost (the viability floor) rather than
    # deducted from the output, keeping both quantities independently meaningful.
    expected_output = volume - total_slippage

    # ------------------------------------------------------------------
    # Batch-fetch all wallets in one query; build agent_id → wallet map.
    # ------------------------------------------------------------------
    wallet_rows: list[AgentWallet] = (
        db.query(AgentWallet).filter(AgentWallet.agent_id.in_(chain)).all()
    )
    wallet_map: dict[str, AgentWallet] = {w.agent_id: w for w in wallet_rows}

    # Batch-fetch all PENDING_SYNC agent IDs touching any chain member.
    pending_rows = (
        db.query(SettlementTransaction.sender_id)
        .filter(
            SettlementTransaction.on_chain_status == "PENDING_SYNC",
            SettlementTransaction.sender_id.in_(chain),
        )
        .union(
            db.query(SettlementTransaction.receiver_id).filter(
                SettlementTransaction.on_chain_status == "PENDING_SYNC",
                SettlementTransaction.receiver_id.in_(chain),
            )
        )
        .all()
    )
    blocked_ids: set[str] = {row[0] for row in pending_rows}

    # ------------------------------------------------------------------
    # Pure in-memory evaluation — no DB writes, no savepoint needed.
    # ------------------------------------------------------------------
    steps: list[ArbitrageStepResult] = []
    rejection: str | None = None
    # Track running balance so cumulative deductions reflect realistic state.
    running_balance: dict[str, Decimal] = {}

    per_leg_cost = (slip_floor + _GAS_COST_PER_HOP).quantize(Decimal("0.00000001"))

    for i, agent_id in enumerate(chain):
        wallet = wallet_map.get(agent_id)

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
            if rejection is None:
                rejection = f"step {i}: agent '{agent_id}' has no registered wallet"
            continue

        is_blocked = agent_id in blocked_ids

        # Use running balance so each leg reflects prior deductions in this chain.
        if agent_id not in running_balance:
            running_balance[agent_id] = Decimal(str(wallet.balance_usdc))
        balance_before = running_balance[agent_id]
        sufficient = balance_before >= per_leg_cost

        # Deduct cost in-memory (not persisted).
        running_balance[agent_id] = balance_before - per_leg_cost

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

        if rejection is None:
            if is_blocked:
                rejection = (
                    f"step {i}: agent '{agent_id}' has PENDING_SYNC "
                    "transaction(s) blocking participation"
                )
            elif not sufficient:
                rejection = (
                    f"step {i}: agent '{agent_id}' balance "
                    f"{float(balance_before):.6f} USDC is below "
                    f"slippage floor {float(per_leg_cost):.6f} USDC "
                    f"(slip={float(slip_floor):.6f} + gas={float(_GAS_COST_PER_HOP):.6f})"
                )

    viable = all(
        s.wallet_found and s.balance_sufficient and not s.pending_sync_blocked
        for s in steps
    )
    return viable, steps, float(total_slippage), float(expected_output), rejection


# ---------------------------------------------------------------------------
# POST /api/v1/arbitrage/route-path
# ---------------------------------------------------------------------------


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
    viable, steps, total_slip, expected_out, rejection = _simulate_chain(
        db,
        chain,
        payload.volume_usdc,
        payload.slippage_tolerance_pct,
    )

    # Compute native-asset output using cached spot prices.
    # expected_output_usdc stays USDC-denominated (simulation invariant).
    # expected_output_native expresses the same value in exit_asset units.
    prices = get_active_prices()
    entry_price_usd = Decimal(str(prices.get(payload.entry_asset, 1.0)))
    exit_price_usd = Decimal(str(prices.get(payload.exit_asset, 1.0)))

    if exit_price_usd == Decimal("0"):
        conversion_factor = Decimal("1")
    else:
        conversion_factor = (entry_price_usd / exit_price_usd).quantize(
            Decimal("0.00000001")
        )

    expected_out_native = float(Decimal(str(expected_out)) * conversion_factor)

    logger.info(
        "Arbitrage route-path dry-run: viable=%s agents=%d volume=%.4f "
        "total_slip=%.4f entry=%s exit=%s factor=%.8f reason=%s",
        viable,
        len(chain),
        payload.volume_usdc,
        total_slip,
        payload.entry_asset,
        payload.exit_asset,
        float(conversion_factor),
        rejection,
    )

    return ArbitrageRouteResponse(
        viable=viable,
        entry_asset=payload.entry_asset,
        exit_asset=payload.exit_asset,
        volume_usdc=payload.volume_usdc,
        agent_chain=chain,
        slippage_tolerance_pct=payload.slippage_tolerance_pct,
        steps=steps,
        total_slippage_usdc=total_slip,
        expected_output_usdc=expected_out,
        expected_output_native=expected_out_native,
        rejection_reason=rejection,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/arbitrage/rebalance
# ---------------------------------------------------------------------------


@router.post("/rebalance", response_model=RebalanceResponse)
def rebalance_agent_balance(
    payload: RebalanceRequest,
    db: Session = Depends(get_db),
) -> RebalanceResponse:
    """
    Autonomously route liquidity to a target agent whose balance has breached
    its safety floor via a 3-hop relay chain drawn from the most-liquid agents.

    No EIP-191 signature is required — this is an internal engine operation.
    All three relay transfers are executed atomically inside a single DB
    transaction using the same pessimistic-locking primitive as settlement/transfer.
    If any hop fails at execution time, the entire batch is rolled back.

    Safety floor = volume_usdc × slippage_tolerance_pct.
    Rebalance triggers only when target.balance_usdc < safety_floor.
    """
    # Import here to avoid a module-level circular-import risk; the function
    # is a stable internal primitive that will not move or be renamed.
    from routes.settlement import _execute_transfer  # noqa: PLC0415

    # ------------------------------------------------------------------
    # 1. Resolve target wallet
    # ------------------------------------------------------------------
    target = db.get(AgentWallet, payload.target_agent_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No wallet found for target_agent_id '{payload.target_agent_id}'",
        )

    volume = Decimal(str(payload.volume_usdc))
    floor = (volume * Decimal(str(payload.slippage_tolerance_pct))).quantize(
        Decimal("0.00000001")
    )
    pre_balance = Decimal(str(target.balance_usdc))

    # ------------------------------------------------------------------
    # 2. Breach check
    # ------------------------------------------------------------------
    if pre_balance >= floor:
        return RebalanceResponse(
            rebalanced=False,
            target_agent_id=payload.target_agent_id,
            volume_usdc=payload.volume_usdc,
            relay_path=[],
            transactions=[],
            pre_balance_usdc=float(pre_balance),
            post_balance_usdc=float(pre_balance),
            total_tax_usdc=0.0,
            rejection_reason=(
                f"target balance {float(pre_balance):.8f} USDC is already at or "
                f"above safety floor {float(floor):.8f} USDC — rebalance not required"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Find relay candidates: top-N by balance, no PENDING_SYNC, not target
    # ------------------------------------------------------------------
    pending_rows = (
        db.query(SettlementTransaction.sender_id)
        .filter(SettlementTransaction.on_chain_status == "PENDING_SYNC")
        .union(
            db.query(SettlementTransaction.receiver_id).filter(
                SettlementTransaction.on_chain_status == "PENDING_SYNC"
            )
        )
        .all()
    )
    blocked_ids: set[str] = {row[0] for row in pending_rows}

    q = db.query(AgentWallet).filter(AgentWallet.agent_id != payload.target_agent_id)
    if blocked_ids:
        q = q.filter(AgentWallet.agent_id.notin_(blocked_ids))
    candidates: list[AgentWallet] = (
        q.order_by(AgentWallet.balance_usdc.desc()).limit(_CANDIDATE_CAP).all()
    )

    if len(candidates) < _RELAY_HOPS:
        return RebalanceResponse(
            rebalanced=False,
            target_agent_id=payload.target_agent_id,
            volume_usdc=payload.volume_usdc,
            relay_path=[],
            transactions=[],
            pre_balance_usdc=float(pre_balance),
            post_balance_usdc=float(pre_balance),
            total_tax_usdc=0.0,
            rejection_reason=(
                f"insufficient relay candidates: {len(candidates)} available, "
                f"need at least {_RELAY_HOPS}"
            ),
        )

    relay_ids: list[str] = [c.agent_id for c in candidates[:_RELAY_HOPS]]

    # ------------------------------------------------------------------
    # 4. Simulate viability (dry-run, savepoint rolled back)
    # ------------------------------------------------------------------
    viable, _, _, _, sim_rejection = _simulate_chain(
        db,
        relay_ids,
        payload.volume_usdc,
        payload.slippage_tolerance_pct,
    )

    # Hard check: relay_0 must hold the full gross volume to initiate hop 0.
    if viable:
        relay_0_balance = Decimal(str(candidates[0].balance_usdc))
        if relay_0_balance < volume:
            viable = False
            sim_rejection = (
                f"relay_0 '{relay_ids[0]}' balance {float(relay_0_balance):.8f} USDC "
                f"is below required volume {payload.volume_usdc:.8f} USDC"
            )

    if not viable:
        return RebalanceResponse(
            rebalanced=False,
            target_agent_id=payload.target_agent_id,
            volume_usdc=payload.volume_usdc,
            relay_path=relay_ids,
            transactions=[],
            pre_balance_usdc=float(pre_balance),
            post_balance_usdc=float(pre_balance),
            total_tax_usdc=0.0,
            rejection_reason=sim_rejection,
        )

    # ------------------------------------------------------------------
    # 5. Execute 3-hop relay chain atomically
    #
    # Amounts cascade: gross of hop n = net of hop n−1.
    #   hop_amounts[0] = volume_usdc
    #   hop_amounts[1] = volume * 0.999
    #   hop_amounts[2] = volume * 0.999²
    # ------------------------------------------------------------------
    hop_gross: list[Decimal] = [volume]
    for _ in range(_RELAY_HOPS - 1):
        hop_gross.append((hop_gross[-1] * _TAX_NET).quantize(Decimal("0.00000001")))

    senders = [relay_ids[0], relay_ids[1], relay_ids[2]]
    receivers = [relay_ids[1], relay_ids[2], payload.target_agent_id]

    executed: list[RebalanceHop] = []
    total_tax = Decimal("0")

    try:
        for i, (sender_id, receiver_id, gross) in enumerate(
            zip(senders, receivers, hop_gross)
        ):
            tx = _execute_transfer(
                db,
                sender_id,
                receiver_id,
                float(gross),
                "internal_rebalance",
            )
            executed.append(
                RebalanceHop(
                    hop=i,
                    sender_id=tx.sender_id,
                    receiver_id=tx.receiver_id,
                    gross_amount_usdc=float(tx.gross_amount_usdc),
                    tax_amount_usdc=float(tx.tax_amount_usdc),
                    net_amount_usdc=float(tx.net_amount_usdc),
                    tx_id=tx.tx_id,
                )
            )
            total_tax += Decimal(str(tx.tax_amount_usdc))

        db.commit()

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rebalance execution failed — all hops have been rolled back",
        )

    db.refresh(target)
    post_balance = Decimal(str(target.balance_usdc))

    logger.info(
        "Rebalance complete: target=%s pre=%.6f post=%.6f " "relay=%s total_tax=%.6f",
        payload.target_agent_id,
        float(pre_balance),
        float(post_balance),
        " → ".join(relay_ids),
        float(total_tax),
    )

    return RebalanceResponse(
        rebalanced=True,
        target_agent_id=payload.target_agent_id,
        volume_usdc=payload.volume_usdc,
        relay_path=relay_ids,
        transactions=executed,
        pre_balance_usdc=float(pre_balance),
        post_balance_usdc=float(post_balance),
        total_tax_usdc=float(total_tax),
        rejection_reason=None,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/arbitrage/scan-paths
# ---------------------------------------------------------------------------


@router.post("/scan-paths", response_model=ScanPathsResponse)
def scan_paths(
    payload: ScanPathsRequest,
    db: Session = Depends(get_db),
) -> ScanPathsResponse:
    """
    Expanded non-mutating path simulation across a pool of candidate agents.

    Generates all ordered permutations of `path_length` agents drawn from
    `candidate_agents` (up to `max_paths` total) and runs each through the
    dry-run viability check. Every simulation is wrapped in a DB savepoint
    (`db.begin_nested()`) that is always rolled back, guaranteeing zero state
    mutation even if the underlying query infrastructure is ever extended to
    flush speculative rows. RPC or network timeouts are captured per-path so
    a single slow pool does not abort the entire sweep.
    """
    candidates = list(dict.fromkeys(payload.candidate_agents))  # dedupe, preserve order

    # Generate permutations up to max_paths cap — use permutations so each
    # ordering is treated as a distinct routing path (A→B≠B→A).
    path_gen = itertools.islice(
        itertools.permutations(candidates, payload.path_length),
        payload.max_paths,
    )

    results: list[PathScanResult] = []
    viable_count = 0

    for chain in path_gen:
        chain_list = list(chain)
        try:
            # Savepoint guarantees non-mutation even if _simulate_chain is
            # ever extended to flush speculative writes.
            sp = db.begin_nested()
            try:
                viable, steps, total_slip, expected_out, rejection = _simulate_chain(
                    db,
                    chain_list,
                    payload.volume_usdc,
                    payload.slippage_tolerance_pct,
                )
            finally:
                sp.rollback()
        except Exception as exc:
            # Gracefully degrade on RPC/network timeout or unexpected DB error
            # so a single bad pool cannot abort the full sweep.
            logger.warning("scan-paths: path %s raised %s — skipping", chain_list, exc)
            results.append(
                PathScanResult(
                    path=chain_list,
                    viable=False,
                    steps=[],
                    expected_output_usdc=0.0,
                    total_slippage_usdc=0.0,
                    rejection_reason=f"simulation error: {exc}",
                )
            )
            continue

        if viable:
            viable_count += 1

        results.append(
            PathScanResult(
                path=chain_list,
                viable=viable,
                steps=steps,
                expected_output_usdc=expected_out,
                total_slippage_usdc=total_slip,
                rejection_reason=rejection,
            )
        )

    logger.info(
        "scan-paths: candidates=%d path_len=%d checked=%d viable=%d",
        len(candidates),
        payload.path_length,
        len(results),
        viable_count,
    )

    return ScanPathsResponse(
        total_paths_checked=len(results),
        viable_count=viable_count,
        volume_usdc=payload.volume_usdc,
        slippage_tolerance_pct=payload.slippage_tolerance_pct,
        paths=results,
    )
