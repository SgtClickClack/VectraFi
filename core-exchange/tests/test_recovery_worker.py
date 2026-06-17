"""
Tests for the PENDING_SYNC recovery worker.

All tests run against an isolated in-memory SQLite database — no shared state
with the FastAPI integration tests in test_settlement.py. The Web3Bridge is
fully mocked so no RPC connection is required.

Test groups:
  R01–R04  Case A: net_tx_hash IS NULL → full retry (both legs)
  R05–R07  Case B: net_tx_hash IS NOT NULL → Leg 2 only
  R08–R10  Edge cases: unconfigured bridge, missing wallet, empty queue
"""
import asyncio
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from database import Base
from models import AgentWallet, SettlementTransaction, TreasuryState
from recovery_worker import run_recovery

# ── Fixed test addresses (42-char EIP-55 format) ─────────────────────────────
_SENDER_ADDR   = "0x" + "A" * 40
_RECEIVER_ADDR = "0x" + "B" * 40
_TREASURY_ADDR = "0x" + "C" * 40
_SIGNING_ADDR  = "0x" + "D" * 40

# ── Network / gas constants shared by helpers ─────────────────────────────────
_BASE_NONCE    = 7
_BASE_GAS_WEI  = 20_000_000_000          # 20 gwei
_PREMIUM_GAS   = int(_BASE_GAS_WEI * 1.2)  # 24 gwei  (20% premium)
_CHAIN_ID      = 8453                    # Base mainnet

# ── Canonical test hashes ─────────────────────────────────────────────────────
_NET_HASH = "0x" + "a" * 64
_TAX_HASH = "0x" + "b" * 64


# ── Isolated per-test DB fixture ──────────────────────────────────────────────
@pytest.fixture()
def recovery_db():
    """Fresh in-memory SQLite DB, torn down after every test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    RSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = RSession()
    db.add(TreasuryState(
        id=1,
        accumulated_fees_usdc=Decimal("0"),
        bounty_pool_fees_usdc=Decimal("0"),
    ))
    db.commit()
    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


# ── DB helpers ────────────────────────────────────────────────────────────────
def _seed_wallets(db) -> None:
    """Insert the canonical sender and receiver AgentWallet rows."""
    db.add(AgentWallet(
        agent_id="rw-sender", wallet_address=_SENDER_ADDR,
        balance_usdc=Decimal("0"), balance_hbar=Decimal("0"),
        staked_yield_balance=Decimal("0"),
    ))
    db.add(AgentWallet(
        agent_id="rw-receiver", wallet_address=_RECEIVER_ADDR,
        balance_usdc=Decimal("0"), balance_hbar=Decimal("0"),
        staked_yield_balance=Decimal("0"),
    ))
    db.commit()


def _make_pending_tx(
    db,
    *,
    net_hash: str | None = None,
    tax_hash: str | None = None,
) -> SettlementTransaction:
    """Insert a PENDING_SYNC SettlementTransaction row and return it."""
    tx = SettlementTransaction(
        tx_id=str(uuid.uuid4()),
        sender_id="rw-sender",
        receiver_id="rw-receiver",
        gross_amount_usdc=Decimal("10.00000000"),
        tax_amount_usdc=Decimal("0.15000000"),
        net_amount_usdc=Decimal("9.85000000"),
        tx_type="test_recovery",
        created_at=int(time.time()),
        on_chain_status="PENDING_SYNC",
        on_chain_net_tx_hash=net_hash,
        on_chain_tax_tx_hash=tax_hash,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


# ── Minimal async eth mock ────────────────────────────────────────────────────
async def _as_coro(val):
    """Trivial coroutine that resolves to val immediately."""
    return val


class _AsyncEth:
    """
    Thin stand-in for AsyncWeb3.eth.

    gas_price and chain_id are Python @properties that create a fresh coroutine
    on each attribute access — matching how web3.py exposes them as awaitable
    properties.  Creating coroutines lazily (on access rather than at
    construction) prevents "coroutine was never awaited" RuntimeWarnings in tests
    that exit before those properties are ever reached.
    """

    def __init__(self, *, receipt):
        self.get_transaction_count   = AsyncMock(return_value=_BASE_NONCE)
        self.get_transaction_receipt = AsyncMock(return_value=receipt)

    @property
    def gas_price(self):
        return _as_coro(_BASE_GAS_WEI)

    @property
    def chain_id(self):
        return _as_coro(_CHAIN_ID)


# ── Bridge mock factory ───────────────────────────────────────────────────────
def _make_mock_bridge(
    *,
    receipt=None,
    build_side_effect: list | None = None,
) -> tuple[MagicMock, _AsyncEth]:
    """
    Build a mock Web3Bridge and its underlying eth mock.

    Returns (bridge, mock_eth).  `mock_eth` is exposed so tests can assert
    on `get_transaction_receipt` call counts and arguments.
    """
    mock_eth = _AsyncEth(receipt=receipt)
    mock_w3 = MagicMock()
    mock_w3.eth = mock_eth

    bridge = MagicMock()
    bridge.is_configured    = True
    bridge.treasury_address = _TREASURY_ADDR
    bridge._account         = MagicMock()
    bridge._account.address = _SIGNING_ADDR
    bridge._get_w3                  = AsyncMock(return_value=mock_w3)
    bridge._usdc_decimals_cached    = AsyncMock(return_value=6)
    bridge._to_wei                  = lambda amt, dec: int(
        (Decimal(str(amt)) * Decimal(10) ** dec).to_integral_value()
    )
    bridge._build_and_send_transfer = AsyncMock(
        side_effect=build_side_effect if build_side_effect is not None
        else [_NET_HASH, _TAX_HASH]
    )
    return bridge, mock_eth


# ── Case A: net_tx_hash IS NULL — full retry ──────────────────────────────────
class TestCaseABothLegsDropped:
    """net_tx_hash IS NULL → neither leg broadcast; re-submit both."""

    def test_R01_row_transitions_to_confirming(self, recovery_db):
        db = recovery_db
        _seed_wallets(db)
        tx = _make_pending_tx(db, net_hash=None)
        bridge, _ = _make_mock_bridge(build_side_effect=[_NET_HASH, _TAX_HASH])

        result = asyncio.run(run_recovery(db, bridge))

        assert result["total"]     == 1
        assert result["recovered"] == 1
        assert result["errors"]    == 0

        db.refresh(tx)
        assert tx.on_chain_status      == "CONFIRMING"
        assert tx.on_chain_net_tx_hash == _NET_HASH
        assert tx.on_chain_tax_tx_hash == _TAX_HASH

    def test_R02_leg1_uses_fresh_nonce_leg2_uses_nonce_plus_one(self, recovery_db):
        db = recovery_db
        _seed_wallets(db)
        _make_pending_tx(db, net_hash=None)
        bridge, _ = _make_mock_bridge(build_side_effect=[_NET_HASH, _TAX_HASH])

        asyncio.run(run_recovery(db, bridge))

        calls = bridge._build_and_send_transfer.call_args_list
        assert len(calls) == 2
        _, leg1_kw = calls[0]
        _, leg2_kw = calls[1]
        assert leg1_kw["nonce"] == _BASE_NONCE
        assert leg2_kw["nonce"] == _BASE_NONCE + 1

    def test_R03_twenty_percent_gas_premium_on_both_legs(self, recovery_db):
        db = recovery_db
        _seed_wallets(db)
        _make_pending_tx(db, net_hash=None)
        bridge, _ = _make_mock_bridge(build_side_effect=[_NET_HASH, _TAX_HASH])

        result = asyncio.run(run_recovery(db, bridge))
        assert result["recovered"] == 1  # guard: ensure recovery actually ran

        calls = bridge._build_and_send_transfer.call_args_list
        assert len(calls) == 2
        for _, kw in calls:
            assert kw["gas_price_wei"] == _PREMIUM_GAS

    def test_R04_leg1_routes_to_receiver_leg2_routes_to_treasury(self, recovery_db):
        db = recovery_db
        _seed_wallets(db)
        _make_pending_tx(db, net_hash=None)
        bridge, _ = _make_mock_bridge(build_side_effect=[_NET_HASH, _TAX_HASH])

        asyncio.run(run_recovery(db, bridge))

        calls = bridge._build_and_send_transfer.call_args_list
        leg1_args, _ = calls[0]
        leg2_args, _ = calls[1]
        assert leg1_args[1] == _RECEIVER_ADDR   # positional: (w3, to_address, amount_wei, ...)
        assert leg2_args[1] == _TREASURY_ADDR


# ── Case B: net_tx_hash IS NOT NULL — Leg 2 only ─────────────────────────────
class TestCaseBLeg1AlreadyBroadcast:
    """net_tx_hash IS NOT NULL → nonce N consumed; skip Leg 1, submit Leg 2."""

    def test_R05_mined_receipt_only_tax_leg_submitted(self, recovery_db):
        """get_transaction_receipt returns a receipt → tx mined; skip Leg 1."""
        db = recovery_db
        _seed_wallets(db)
        tx = _make_pending_tx(db, net_hash=_NET_HASH)
        bridge, mock_eth = _make_mock_bridge(
            receipt={"status": 1, "blockNumber": 12_345_678},
            build_side_effect=[_TAX_HASH],
        )

        result = asyncio.run(run_recovery(db, bridge))

        assert result["recovered"] == 1
        assert bridge._build_and_send_transfer.call_count == 1

        db.refresh(tx)
        assert tx.on_chain_status      == "CONFIRMING"
        assert tx.on_chain_net_tx_hash == _NET_HASH   # original hash preserved
        assert tx.on_chain_tax_tx_hash == _TAX_HASH

    def test_R06_pending_no_receipt_only_tax_leg_submitted(self, recovery_db):
        """get_transaction_receipt returns None (pending in mempool); skip Leg 1."""
        db = recovery_db
        _seed_wallets(db)
        tx = _make_pending_tx(db, net_hash=_NET_HASH)
        bridge, _ = _make_mock_bridge(
            receipt=None,
            build_side_effect=[_TAX_HASH],
        )

        result = asyncio.run(run_recovery(db, bridge))

        assert result["recovered"] == 1
        assert bridge._build_and_send_transfer.call_count == 1

        db.refresh(tx)
        assert tx.on_chain_status      == "CONFIRMING"
        assert tx.on_chain_net_tx_hash == _NET_HASH
        assert tx.on_chain_tax_tx_hash == _TAX_HASH

    def test_R07_get_transaction_receipt_called_with_existing_net_hash(self, recovery_db):
        """The worker must check the existing net hash for a receipt."""
        db = recovery_db
        _seed_wallets(db)
        original_hash = "0x" + "e" * 64
        _make_pending_tx(db, net_hash=original_hash)
        bridge, mock_eth = _make_mock_bridge(
            receipt={"status": 1},
            build_side_effect=[_TAX_HASH],
        )

        asyncio.run(run_recovery(db, bridge))

        mock_eth.get_transaction_receipt.assert_awaited_once_with(original_hash)

    def test_R08_original_net_hash_not_overwritten(self, recovery_db):
        """When only Leg 2 runs, the existing net_tx_hash must remain unchanged."""
        db = recovery_db
        _seed_wallets(db)
        original_hash = "0x" + "f" * 64
        tx = _make_pending_tx(db, net_hash=original_hash)
        bridge, _ = _make_mock_bridge(
            receipt={"status": 1},
            build_side_effect=[_TAX_HASH],
        )

        asyncio.run(run_recovery(db, bridge))

        # Only one _build_and_send_transfer call, and it must target the treasury.
        assert bridge._build_and_send_transfer.call_count == 1
        call_args, _ = bridge._build_and_send_transfer.call_args
        assert call_args[1] == _TREASURY_ADDR

        db.refresh(tx)
        assert tx.on_chain_net_tx_hash == original_hash


# ── Edge cases ────────────────────────────────────────────────────────────────
class TestEdgeCases:

    def test_R09_unconfigured_bridge_returns_empty_summary(self, recovery_db):
        """bridge.is_configured is False → early exit, no DB queries."""
        db = recovery_db
        _seed_wallets(db)
        _make_pending_tx(db)

        bridge = MagicMock()
        bridge.is_configured = False

        result = asyncio.run(run_recovery(db, bridge))

        assert result["total"]     == 0
        assert result["recovered"] == 0
        assert result["errors"]    == 0

    def test_R10_missing_receiver_wallet_leaves_row_pending(self, recovery_db):
        """Receiver AgentWallet absent → row stays PENDING_SYNC, counted as error."""
        db = recovery_db
        # Seed only the sender; receiver is absent.
        db.add(AgentWallet(
            agent_id="rw-sender", wallet_address=_SENDER_ADDR,
            balance_usdc=Decimal("0"), balance_hbar=Decimal("0"),
            staked_yield_balance=Decimal("0"),
        ))
        db.commit()

        tx = _make_pending_tx(db, net_hash=None)
        bridge, _ = _make_mock_bridge()

        result = asyncio.run(run_recovery(db, bridge))

        assert result["recovered"] == 0
        assert result["errors"]    == 1
        bridge._build_and_send_transfer.assert_not_called()

        db.refresh(tx)
        assert tx.on_chain_status == "PENDING_SYNC"

    def test_R11_no_pending_rows_returns_zero_totals(self, recovery_db):
        """No PENDING_SYNC rows → nothing submitted, zero counts."""
        db = recovery_db
        bridge, _ = _make_mock_bridge()

        result = asyncio.run(run_recovery(db, bridge))

        assert result["total"]     == 0
        assert result["recovered"] == 0
        assert bridge._build_and_send_transfer.call_count == 0
