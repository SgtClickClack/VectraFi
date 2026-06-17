"""
Integration tests for POST /api/v1/arbitrage/route-path

Covers:
  1.  Viable 3-agent chain — all checks pass, viable=True.
  2.  Unknown agent in chain — wallet_found=False, viable=False.
  3.  Insufficient balance — balance < slippage floor, viable=False.
  4.  PENDING_SYNC block — agent has limbo tx, viable=False.
  5.  Single-agent chain — trivial path, viable=True.
  6.  Empty agent_chain — 422 validation error.
  7.  Chain too long (> 10 hops) — 422 validation error.
  8.  Dry-run isolation — agent balances unchanged after the call.
  9.  Slippage math — total_slippage and expected_output are exact.
  10. Partial failure — first two steps pass, third fails; all steps reported.
"""

import time
import uuid

import pytest
from conftest import _TestSessionLocal, make_agent

from models import SettlementTransaction

_URL = "/api/v1/arbitrage/route-path"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(client, body: dict):
    return client.post(_URL, json=body)


def _base_body(**kwargs) -> dict:
    return {
        "entry_asset": "USDC",
        "exit_asset": "HBAR",
        "volume_usdc": 100.0,
        "agent_chain": [],
        "slippage_tolerance_pct": 0.01,
        **kwargs,
    }


def _seed_pending_sync(agent_id: str) -> None:
    """Insert a PENDING_SYNC SettlementTransaction row for the given agent."""
    db = _TestSessionLocal()
    tx = SettlementTransaction(
        tx_id=str(uuid.uuid4()),
        sender_id=agent_id,
        receiver_id="__treasury__",
        gross_amount_usdc="1.0",
        tax_amount_usdc="0.015",
        net_amount_usdc="0.985",
        tx_type="arb_test_seed",
        created_at=int(time.time()),
        on_chain_status="PENDING_SYNC",
    )
    db.add(tx)
    db.commit()
    db.close()


def _get_balance(agent_id: str) -> float:
    db = _TestSessionLocal()
    from models import AgentWallet
    wallet = db.get(AgentWallet, agent_id)
    db.close()
    return float(wallet.balance_usdc) if wallet else None


# ---------------------------------------------------------------------------
# 1. Viable 3-agent chain
# ---------------------------------------------------------------------------

def test_route_viable_three_agent_chain(client):
    _, a = make_agent("arb1-alpha",  balance_usdc=500.0)
    _, b = make_agent("arb1-beta",   balance_usdc=500.0)
    _, c = make_agent("arb1-gamma",  balance_usdc=500.0)

    resp = _post(client, _base_body(
        volume_usdc=300.0,
        agent_chain=[a.agent_id, b.agent_id, c.agent_id],
        slippage_tolerance_pct=0.01,
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["viable"] is True
    assert d["rejection_reason"] is None
    assert len(d["steps"]) == 3
    for step in d["steps"]:
        assert step["wallet_found"] is True
        assert step["balance_sufficient"] is True
        assert step["pending_sync_blocked"] is False


# ---------------------------------------------------------------------------
# 2. Unknown agent — wallet not found
# ---------------------------------------------------------------------------

def test_route_unknown_agent_not_viable(client):
    _, a = make_agent("arb2-known", balance_usdc=500.0)

    resp = _post(client, _base_body(
        agent_chain=[a.agent_id, "arb2-ghost-agent-does-not-exist"],
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["viable"] is False
    assert "no registered wallet" in d["rejection_reason"]

    ghost_step = d["steps"][1]
    assert ghost_step["wallet_found"] is False
    assert ghost_step["balance_sufficient"] is False


# ---------------------------------------------------------------------------
# 3. Insufficient balance — balance below slippage floor
# ---------------------------------------------------------------------------

def test_route_insufficient_balance_not_viable(client):
    # volume=1000, slip=0.02, n=2 → floor = 10.0 per agent
    # Agent with 5.0 balance cannot cover 10.0 floor
    _, rich  = make_agent("arb3-rich",  balance_usdc=1000.0)
    _, broke = make_agent("arb3-broke", balance_usdc=5.0)

    resp = _post(client, _base_body(
        volume_usdc=1000.0,
        slippage_tolerance_pct=0.02,
        agent_chain=[rich.agent_id, broke.agent_id],
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["viable"] is False
    assert "below slippage floor" in d["rejection_reason"]

    broke_step = next(s for s in d["steps"] if s["agent_id"] == broke.agent_id)
    assert broke_step["balance_sufficient"] is False
    assert broke_step["wallet_found"] is True

    rich_step = next(s for s in d["steps"] if s["agent_id"] == rich.agent_id)
    assert rich_step["balance_sufficient"] is True


# ---------------------------------------------------------------------------
# 4. PENDING_SYNC block
# ---------------------------------------------------------------------------

def test_route_pending_sync_blocks_agent(client):
    _, blocked = make_agent("arb4-blocked", balance_usdc=9999.0)
    _, other   = make_agent("arb4-clear",   balance_usdc=9999.0)
    _seed_pending_sync(blocked.agent_id)

    resp = _post(client, _base_body(
        agent_chain=[other.agent_id, blocked.agent_id],
        volume_usdc=100.0,
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["viable"] is False
    assert "PENDING_SYNC" in d["rejection_reason"]

    blocked_step = next(s for s in d["steps"] if s["agent_id"] == blocked.agent_id)
    assert blocked_step["pending_sync_blocked"] is True
    assert blocked_step["wallet_found"] is True


# ---------------------------------------------------------------------------
# 5. Single-agent chain — edge case, should be viable
# ---------------------------------------------------------------------------

def test_route_single_agent_chain(client):
    _, solo = make_agent("arb5-solo", balance_usdc=50.0)

    # volume=100, slip=0.005, n=1 → floor=0.5 USDC; balance=50 → sufficient
    resp = _post(client, _base_body(
        volume_usdc=100.0,
        slippage_tolerance_pct=0.005,
        agent_chain=[solo.agent_id],
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["viable"] is True
    assert len(d["steps"]) == 1
    assert d["steps"][0]["agent_id"] == solo.agent_id
    assert d["steps"][0]["balance_sufficient"] is True


# ---------------------------------------------------------------------------
# 6. Empty agent_chain → 422
# ---------------------------------------------------------------------------

def test_route_empty_agent_chain_returns_422(client):
    resp = _post(client, _base_body(agent_chain=[]))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 7. Chain exceeding 10 hops → 422
# ---------------------------------------------------------------------------

def test_route_chain_too_long_returns_422(client):
    resp = _post(client, _base_body(agent_chain=[f"arb7-agent-{i}" for i in range(11)]))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 8. Dry-run isolation — balances must be unchanged after the call
# ---------------------------------------------------------------------------

def test_route_is_dry_run_balances_unchanged(client):
    _, x = make_agent("arb8-dry-x", balance_usdc=300.0)
    _, y = make_agent("arb8-dry-y", balance_usdc=300.0)

    bal_x_before = _get_balance(x.agent_id)
    bal_y_before = _get_balance(y.agent_id)

    resp = _post(client, _base_body(
        volume_usdc=200.0,
        slippage_tolerance_pct=0.05,
        agent_chain=[x.agent_id, y.agent_id],
    ))
    assert resp.status_code == 200, resp.text
    assert resp.json()["viable"] is True

    assert _get_balance(x.agent_id) == pytest.approx(bal_x_before, rel=1e-9)
    assert _get_balance(y.agent_id) == pytest.approx(bal_y_before, rel=1e-9)


# ---------------------------------------------------------------------------
# 9. Slippage math — totals and per-step floors are correct
# ---------------------------------------------------------------------------

def test_route_slippage_math(client):
    _, p = make_agent("arb9-p", balance_usdc=9999.0)
    _, q = make_agent("arb9-q", balance_usdc=9999.0)

    # volume=1000, slip=0.02, n=2
    # total_slippage = 20.0, floor_per_agent = 10.0, expected_output = 980.0
    resp = _post(client, _base_body(
        volume_usdc=1000.0,
        slippage_tolerance_pct=0.02,
        agent_chain=[p.agent_id, q.agent_id],
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["total_slippage_usdc"]  == pytest.approx(20.0,  rel=1e-6)
    assert d["expected_output_usdc"] == pytest.approx(980.0, rel=1e-6)

    for step in d["steps"]:
        assert step["slippage_floor_usdc"] == pytest.approx(10.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 10. Partial failure — all steps still reported when middle agent fails
# ---------------------------------------------------------------------------

def test_route_all_steps_reported_on_partial_failure(client):
    # n=3, volume=600, slip=0.03 → floor = 6.0 per agent
    # middle agent has only 1.0 → insufficient
    _, first  = make_agent("arb10-first",  balance_usdc=500.0)
    _, middle = make_agent("arb10-middle", balance_usdc=1.0)
    _, last   = make_agent("arb10-last",   balance_usdc=500.0)

    resp = _post(client, _base_body(
        volume_usdc=600.0,
        slippage_tolerance_pct=0.03,
        agent_chain=[first.agent_id, middle.agent_id, last.agent_id],
    ))
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["viable"] is False
    assert len(d["steps"]) == 3

    assert d["steps"][0]["balance_sufficient"] is True   # first  — 500 >= 6
    assert d["steps"][1]["balance_sufficient"] is False  # middle — 1 < 6
    assert d["steps"][2]["balance_sufficient"] is True   # last   — 500 >= 6

    # rejection_reason captures the FIRST failure encountered
    assert "arb10-middle" in d["rejection_reason"]
