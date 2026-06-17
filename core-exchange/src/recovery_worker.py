"""
Background recovery worker for PENDING_SYNC settlement transactions.

Recovery routing:
  net_tx_hash IS NULL     → Neither leg was broadcast. Re-submit both Leg 1
                            (net → receiver) and Leg 2 (tax → treasury) using
                            a fresh pending nonce from the node.
  net_tx_hash IS NOT NULL → Leg 1 was broadcast (nonce N consumed). Check
                            receipt; whether mined or still pending the nonce is
                            spent — skip Leg 1 and submit only Leg 2 using the
                            current pending nonce returned by the node.

All resubmissions apply a 20% gas price premium over the current network base
fee to unstick mempool-lagging transactions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AgentWallet, SettlementTransaction
from web3_bridge import Web3Bridge, Web3BridgeError
from web3_bridge import bridge as _default_bridge

logger = logging.getLogger("vectrafi.recovery_worker")

# 20% premium applied to the current network base fee on every retry.
_GAS_PREMIUM = Decimal("1.2")


@dataclass
class RecoveryOutcome:
    tx_id: str
    new_status: str             # "CONFIRMING" | "PENDING_SYNC" (unchanged)
    net_tx_hash: str | None = None
    tax_tx_hash: str | None = None
    leg1_skipped: bool = False  # True when Leg 1 hash already existed
    error: str | None = None


async def _recover_single(
    db: Session,
    tx: SettlementTransaction,
    bridge: Web3Bridge,
) -> RecoveryOutcome:
    """
    Attempt on-chain re-submission for a single PENDING_SYNC row.

    Mutates tx in place (status, hashes) but does NOT commit — run_recovery
    owns the commit boundary so a multi-row batch is atomic at the DB layer.

    Precondition: bridge.is_configured is True (enforced by run_recovery).
    """
    outcome = RecoveryOutcome(tx_id=tx.tx_id, new_status="PENDING_SYNC")

    receiver_wallet = db.execute(
        select(AgentWallet).where(AgentWallet.agent_id == tx.receiver_id)
    ).scalar_one_or_none()

    if receiver_wallet is None:
        outcome.error = f"receiver wallet not found for agent_id='{tx.receiver_id}'"
        logger.warning("recovery skip tx=%s: %s", tx.tx_id, outcome.error)
        return outcome

    try:
        w3 = await bridge._get_w3()
        decimals = await bridge._usdc_decimals_cached(w3)

        base_gas: int = await w3.eth.gas_price
        premium_gas: int = int(Decimal(base_gas) * _GAS_PREMIUM)
        chain_id: int = await w3.eth.chain_id

        net_wei = bridge._to_wei(Decimal(str(tx.net_amount_usdc)), decimals)
        tax_wei = bridge._to_wei(Decimal(str(tx.tax_amount_usdc)), decimals)

        # net_tx_hash starts as the already-stored value (may be None).
        net_tx_hash: str | None = tx.on_chain_net_tx_hash
        leg1_skipped = False

        if tx.on_chain_net_tx_hash is None:
            # Case A: neither leg broadcast — submit both from a fresh nonce.
            nonce: int = await w3.eth.get_transaction_count(
                bridge._account.address, "pending"
            )
            logger.info(
                "recovery tx=%s FULL_RETRY nonce=%d premium_gas_gwei=%.2f",
                tx.tx_id, nonce, premium_gas / 1e9,
            )
            net_tx_hash = await bridge._build_and_send_transfer(
                w3, receiver_wallet.wallet_address, net_wei,
                nonce=nonce, gas_price_wei=premium_gas, chain_id=chain_id,
            )
            logger.info("recovery tx=%s leg1_submitted net_hash=%s", tx.tx_id, net_tx_hash)
            nonce_for_tax = nonce + 1

        else:
            # Case B: Leg 1 hash exists; nonce N is consumed regardless of whether
            # the transaction is mined or still pending in the mempool.
            # get_transaction_receipt returns None for both pending and dropped txs;
            # treat either case conservatively — do not re-submit Leg 1.
            try:
                receipt = await w3.eth.get_transaction_receipt(tx.on_chain_net_tx_hash)
            except Exception:
                receipt = None

            leg1_skipped = True
            nonce_for_tax: int = await w3.eth.get_transaction_count(
                bridge._account.address, "pending"
            )
            status_note = "mined" if receipt else "pending/unknown"
            logger.info(
                "recovery tx=%s LEG2_ONLY leg1_status=%s nonce_for_tax=%d",
                tx.tx_id, status_note, nonce_for_tax,
            )

        tax_tx_hash = await bridge._build_and_send_transfer(
            w3, bridge.treasury_address, tax_wei,
            nonce=nonce_for_tax, gas_price_wei=premium_gas, chain_id=chain_id,
        )
        logger.info("recovery tx=%s leg2_submitted tax_hash=%s", tx.tx_id, tax_tx_hash)

        tx.on_chain_status = "CONFIRMING"
        tx.on_chain_net_tx_hash = net_tx_hash
        tx.on_chain_tax_tx_hash = tax_tx_hash
        db.flush()

        outcome.new_status = "CONFIRMING"
        outcome.net_tx_hash = net_tx_hash
        outcome.tax_tx_hash = tax_tx_hash
        outcome.leg1_skipped = leg1_skipped

    except Web3BridgeError as exc:
        outcome.error = str(exc)
        logger.error("recovery tx=%s bridge config error: %s", tx.tx_id, exc)
    except Exception as exc:  # noqa: BLE001
        outcome.error = str(exc)
        logger.exception("recovery tx=%s unexpected error: %s", tx.tx_id, exc)

    return outcome


async def run_recovery(
    db: Session,
    bridge: Web3Bridge | None = None,
) -> dict:
    """
    Scan for all PENDING_SYNC settlement rows and attempt on-chain re-submission.

    Args:
        db:     SQLAlchemy session. Caller owns rollback on unhandled error;
                this function commits internally after processing all rows.
        bridge: Web3Bridge instance. Defaults to the module-level singleton.

    Returns:
        {
            "total":     int,               # PENDING_SYNC rows found
            "recovered": int,               # rows promoted to CONFIRMING
            "skipped":   int,               # rows left PENDING_SYNC (no error)
            "errors":    int,               # rows that raised an exception
            "outcomes":  list[RecoveryOutcome],
        }
    """
    _bridge = bridge if bridge is not None else _default_bridge

    if not _bridge.is_configured:
        logger.info("recovery_worker: bridge not configured — skipping run")
        return {"total": 0, "recovered": 0, "skipped": 0, "errors": 0, "outcomes": []}

    rows = db.execute(
        select(SettlementTransaction).where(
            SettlementTransaction.on_chain_status == "PENDING_SYNC"
        )
    ).scalars().all()

    logger.info("recovery_worker: found %d PENDING_SYNC row(s)", len(rows))

    outcomes: list[RecoveryOutcome] = []
    recovered = skipped = errors = 0

    for tx in rows:
        outcome = await _recover_single(db, tx, _bridge)
        outcomes.append(outcome)
        if outcome.new_status == "CONFIRMING":
            recovered += 1
        elif outcome.error:
            errors += 1
        else:
            skipped += 1

    db.commit()

    return {
        "total":     len(rows),
        "recovered": recovered,
        "skipped":   skipped,
        "errors":    errors,
        "outcomes":  outcomes,
    }
