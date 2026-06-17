"""
End-to-end MCP → Core-Exchange production bridge tests.

Proves the full chain:
  MCP tool (build_transfer_payload / build_bounty_claim_payload)
    → local eth_account signing
    → FastAPI TestClient (settlement routes)

Tests:
  1. build_transfer_payload emits valid JSON and correct field set.
  2. build_bounty_claim_payload emits valid JSON and correct field set.
  3. body_compact is byte-identical to json.dumps(body, separators=(',',':')).
  4. Transfer payload → sign → POST /settlement/transfer → 200 + correct tax math.
  5. Bounty payload → sign → POST /settlement/claim-bounty → 200 + correct split.
  6. MCP guard: amount_usdc=0 returns error JSON, not a signable body.
  7. MCP guard: counterpart_share_pct out of range returns error JSON.
  8. MCP guard: self-transfer (same agent_id == receiver_id) returns error JSON.
  9. Forged body (modified after signing) → 401 from core-exchange.
 10. build_transfer_payload tax_preview matches actual settlement response.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT    = Path(__file__).resolve().parent.parent
_EXCHANGE_SRC = _REPO_ROOT / "core-exchange" / "src"
_MCP_PATH     = _REPO_ROOT / "mcp" / "faba_server.py"

sys.path.insert(0, str(_EXCHANGE_SRC))

# ---------------------------------------------------------------------------
# In-memory exchange DB (mirrors core-exchange/tests/conftest.py pattern)
# ---------------------------------------------------------------------------

from database import Base, get_db
from main import app
from models import AgentWallet, TreasuryState

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _override_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_db


@pytest.fixture(scope="module", autouse=True)
def setup_exchange_db():
    Base.metadata.create_all(bind=_engine)
    db = _Session()
    if db.get(TreasuryState, 1) is None:
        db.add(TreasuryState(id=1, accumulated_fees_usdc=0.0, bounty_pool_fees_usdc=0.0))
        db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture(scope="module")
def exchange_client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Load MCP server module (system Python — no FastMCP startup side-effects)
# ---------------------------------------------------------------------------

def _load_mcp():
    spec = importlib.util.spec_from_file_location("faba_server_bridge", _MCP_PATH)
    mod  = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
    sys.modules["faba_server_bridge"] = mod
    spec.loader.exec_module(mod)                   # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mcp():
    return _load_mcp()


# ---------------------------------------------------------------------------
# Agent provisioning helpers
# ---------------------------------------------------------------------------

def _make_agent(agent_id: str, balance: float = 1000.0):
    acct = Account.create()
    db   = _Session()
    db.add(AgentWallet(
        agent_id=agent_id,
        wallet_address=acct.address,
        balance_usdc=balance,
        balance_hbar=0.0,
        staked_yield_balance=0.0,
    ))
    db.commit()
    db.close()
    return acct


def _sign(acct, body: dict) -> str:
    body_text = json.dumps(body, separators=(",", ":"))
    msg  = encode_defunct(text=body_text)
    sig  = acct.sign_message(msg)
    return sig.signature.hex()


# ---------------------------------------------------------------------------
# 1. build_transfer_payload — structure
# ---------------------------------------------------------------------------

def test_transfer_payload_structure(mcp):
    acct = Account.create()
    raw  = mcp.build_transfer_payload("a1", acct.address, "b1", 50.0)
    d    = json.loads(raw)
    assert "body" in d
    assert "body_compact" in d
    assert "tax_preview" in d
    body = d["body"]
    assert set(body.keys()) == {"agent_id", "wallet_address", "receiver_id", "amount_usdc", "tx_type"}


# ---------------------------------------------------------------------------
# 2. build_bounty_claim_payload — structure
# ---------------------------------------------------------------------------

def test_bounty_payload_structure(mcp):
    acct = Account.create()
    raw  = mcp.build_bounty_claim_payload("a2", acct.address, "b2", 200.0, 0.4)
    d    = json.loads(raw)
    assert "body" in d
    assert "body_compact" in d
    assert "split_preview" in d
    body = d["body"]
    assert set(body.keys()) == {
        "agent_id", "wallet_address", "counterpart_id",
        "bounty_amount_usdc", "counterpart_share_pct",
    }


# ---------------------------------------------------------------------------
# 3. body_compact is byte-identical to canonical compact serialisation
# ---------------------------------------------------------------------------

def test_body_compact_is_canonical(mcp):
    acct = Account.create()

    raw_t = mcp.build_transfer_payload("a3", acct.address, "b3", 75.0)
    d_t   = json.loads(raw_t)
    assert d_t["body_compact"] == json.dumps(d_t["body"], separators=(",", ":"))

    raw_b = mcp.build_bounty_claim_payload("a3b", acct.address, "b3b", 100.0, 0.25)
    d_b   = json.loads(raw_b)
    assert d_b["body_compact"] == json.dumps(d_b["body"], separators=(",", ":"))


# ---------------------------------------------------------------------------
# 4. Transfer: MCP → sign → POST → 200 + correct tax
# ---------------------------------------------------------------------------

def test_transfer_end_to_end(mcp, exchange_client):
    acct_s = _make_agent("bridge-s1", 500.0)
    _make_agent("bridge-r1", 0.0)

    raw  = mcp.build_transfer_payload("bridge-s1", acct_s.address, "bridge-r1", 100.0)
    d    = json.loads(raw)
    body = d["body"]
    sig  = _sign(acct_s, body)

    resp = exchange_client.post(
        "/api/v1/settlement/transfer",
        content=d["body_compact"],
        headers={"Content-Type": "application/json", "X-VectraFi-Signature": sig},
    )
    assert resp.status_code == 200, resp.text
    rd = resp.json()
    assert rd["gross_amount_usdc"] == pytest.approx(100.0)
    assert rd["tax_amount_usdc"]   == pytest.approx(1.5)
    assert rd["net_amount_usdc"]   == pytest.approx(98.5)


# ---------------------------------------------------------------------------
# 5. Bounty claim: MCP → sign → POST → 200 + correct split
# ---------------------------------------------------------------------------

def test_bounty_claim_end_to_end(mcp, exchange_client):
    acct_c = _make_agent("bridge-c1", 600.0)
    _make_agent("bridge-p1", 0.0)

    raw  = mcp.build_bounty_claim_payload("bridge-c1", acct_c.address, "bridge-p1", 300.0, 1/3)
    d    = json.loads(raw)
    body = d["body"]
    sig  = _sign(acct_c, body)

    resp = exchange_client.post(
        "/api/v1/settlement/claim-bounty",
        content=d["body_compact"],
        headers={"Content-Type": "application/json", "X-VectraFi-Signature": sig},
    )
    assert resp.status_code == 200, resp.text
    rd = resp.json()
    assert rd["counterpart_gross_usdc"] == pytest.approx(100.0, rel=1e-4)
    assert rd["tax_amount_usdc"]        == pytest.approx(1.5,   rel=1e-4)
    assert rd["counterpart_net_usdc"]   == pytest.approx(98.5,  rel=1e-4)


# ---------------------------------------------------------------------------
# 6. MCP guard: amount_usdc=0 returns error dict
# ---------------------------------------------------------------------------

def test_transfer_payload_rejects_zero_amount(mcp):
    acct = Account.create()
    raw  = mcp.build_transfer_payload("a6", acct.address, "b6", 0.0)
    d    = json.loads(raw)
    assert "error" in d
    assert "body" not in d


# ---------------------------------------------------------------------------
# 7. MCP guard: counterpart_share_pct out of range
# ---------------------------------------------------------------------------

def test_bounty_payload_rejects_bad_share_pct(mcp):
    acct = Account.create()
    for bad_pct in [0.0, 1.0, 1.5, -0.1]:
        raw = mcp.build_bounty_claim_payload("a7", acct.address, "b7", 100.0, bad_pct)
        d   = json.loads(raw)
        assert "error" in d, f"Expected error for pct={bad_pct}"


# ---------------------------------------------------------------------------
# 8. MCP guard: self-transfer (agent_id == receiver_id)
# ---------------------------------------------------------------------------

def test_transfer_payload_rejects_self_transfer(mcp):
    acct = Account.create()
    raw  = mcp.build_transfer_payload("agent-self", acct.address, "agent-self", 50.0)
    d    = json.loads(raw)
    assert "error" in d


# ---------------------------------------------------------------------------
# 9. Forged body (mutated after signing) → 401
# ---------------------------------------------------------------------------

def test_forged_body_after_signing_returns_401(mcp, exchange_client):
    acct_s = _make_agent("bridge-forge-s", 200.0)
    _make_agent("bridge-forge-r", 0.0)

    raw  = mcp.build_transfer_payload("bridge-forge-s", acct_s.address, "bridge-forge-r", 50.0)
    d    = json.loads(raw)
    body = d["body"]
    sig  = _sign(acct_s, body)

    # Mutate the amount AFTER signing
    forged = dict(body)
    forged["amount_usdc"] = 9999.0
    forged_compact = json.dumps(forged, separators=(",", ":"))

    resp = exchange_client.post(
        "/api/v1/settlement/transfer",
        content=forged_compact,
        headers={"Content-Type": "application/json", "X-VectraFi-Signature": sig},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 10. tax_preview in MCP response matches actual settlement response
# ---------------------------------------------------------------------------

def test_tax_preview_matches_settlement(mcp, exchange_client):
    acct_s = _make_agent("bridge-tp-s", 300.0)
    _make_agent("bridge-tp-r", 0.0)

    raw  = mcp.build_transfer_payload("bridge-tp-s", acct_s.address, "bridge-tp-r", 80.0)
    d    = json.loads(raw)
    body = d["body"]
    sig  = _sign(acct_s, body)

    resp = exchange_client.post(
        "/api/v1/settlement/transfer",
        content=d["body_compact"],
        headers={"Content-Type": "application/json", "X-VectraFi-Signature": sig},
    )
    assert resp.status_code == 200
    rd = resp.json()

    preview = d["tax_preview"]
    assert preview["gross_usdc"] == pytest.approx(rd["gross_amount_usdc"])
    assert preview["tax_usdc"]   == pytest.approx(rd["tax_amount_usdc"],  rel=1e-6)
    assert preview["net_usdc"]   == pytest.approx(rd["net_amount_usdc"],  rel=1e-6)
