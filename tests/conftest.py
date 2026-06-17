"""
VectraFi baseline test infrastructure.
"""
import sys
import time as _time
import uuid as _uuid
from pathlib import Path

# Ensure core-exchange src is importable regardless of working directory
_EXCHANGE_SRC = Path(__file__).resolve().parent.parent / "core-exchange" / "src"
if str(_EXCHANGE_SRC) not in sys.path:
    sys.path.insert(0, str(_EXCHANGE_SRC))

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import AgentWallet, TreasuryState

# Must match config.PROTOCOL_DOMAIN
_PROTOCOL_DOMAIN = "vectrafi-sandbox-v1"

# In-memory SQLite with StaticPool ensures all sessions share the same DB connection.
_TEST_DB_URL = "sqlite:///:memory:"
_test_engine = create_engine(
    _TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


def _override_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


def _sign_body(private_key: str, body: dict) -> str:
    """
    EIP-191 personal-sign of a JSON body dict — mirrors auth.py verification exactly.

    Injects F-02 replay-protection fields (nonce/issued_at/chain_id) before signing,
    mutating body in-place so the caller's POST body matches what was signed.
    """
    body.setdefault("nonce", str(_uuid.uuid4()))
    body.setdefault("issued_at", int(_time.time()))
    body.setdefault("chain_id", _PROTOCOL_DOMAIN)
    body_text = json.dumps(body, separators=(",", ":"))
    msg = encode_defunct(text=body_text)
    return Account.sign_message(msg, private_key=private_key).signature.hex()


@pytest.fixture(scope="session")
def client():
    """Shared FastAPI TestClient backed by an isolated in-memory SQLite database."""
    Base.metadata.create_all(bind=_test_engine)
    with _TestSession() as db:
        if db.get(TreasuryState, 1) is None:
            from decimal import Decimal
            db.add(TreasuryState(id=1, accumulated_fees_usdc=Decimal("0"), bounty_pool_fees_usdc=Decimal("0")))
            db.commit()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=_test_engine)


@pytest.fixture
def test_account():
    """Fresh Ethereum keypair generated for each test function."""
    account = Account.create()
    pk = account.key.hex()
    return {
        "private_key": pk if pk.startswith("0x") else f"0x{pk}",
        "address": account.address,
    }


@pytest.fixture(scope="session")
def registered_wallet(client):
    """
    A wallet whose keypair is known to the test suite, pre-seeded into the test database.
    Balance is set high enough to survive multiple deposit tests in a single session.
    """
    account = Account.create()
    pk = account.key.hex()
    if not pk.startswith("0x"):
        pk = f"0x{pk}"

    with _TestSession() as db:
        if db.get(AgentWallet, "fixture-agent") is None:
            db.add(
                AgentWallet(
                    agent_id="fixture-agent",
                    wallet_address=account.address,
                    balance_usdc=100_000.0,
                    balance_hbar=0.0,
                    staked_yield_balance=0.0,
                )
            )
            db.commit()

    return {
        "agent_id": "fixture-agent",
        "wallet_address": account.address,
        "private_key": pk,
    }


@pytest.fixture
def sign_body():
    """Returns the EIP-191 body-signing helper for use in test functions."""
    return _sign_body
