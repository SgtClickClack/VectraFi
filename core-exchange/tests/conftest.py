"""
Shared fixtures for core-exchange integration tests.

Uses a single in-memory SQLite database for the test session.
Tests are isolated by unique agent_id prefixes — no per-test rollback needed.
"""

import json
import sys
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from database import Base, get_db
from main import app
from models import AgentWallet, TreasuryState

_engine = create_engine(
    "sqlite://",          # in-memory
    connect_args={"check_same_thread": False},
    poolclass=StaticPool, # single shared connection across all threads
)
_TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=_engine)
    db = _TestSessionLocal()
    treasury = db.get(TreasuryState, 1)
    if treasury is None:
        db.add(TreasuryState(id=1, accumulated_fees_usdc=0.0, bounty_pool_fees_usdc=0.0))
        db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def make_agent(agent_id: str, balance_usdc: float = 1000.0):
    """Create a fresh eth_account key pair and register an AgentWallet in the test DB."""
    acct = Account.create()
    db = _TestSessionLocal()
    wallet = AgentWallet(
        agent_id=agent_id,
        wallet_address=acct.address,
        balance_usdc=balance_usdc,
        balance_hbar=0.0,
        staked_yield_balance=0.0,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    db.close()
    return acct, wallet


def get_balance(agent_id: str) -> float:
    db = _TestSessionLocal()
    wallet = db.get(AgentWallet, agent_id)
    db.close()
    return wallet.balance_usdc if wallet else None


def sign_body(acct, body: dict) -> str:
    body_text = json.dumps(body, separators=(",", ":"))
    msg = encode_defunct(text=body_text)
    signed = acct.sign_message(msg)
    return signed.signature.hex()
