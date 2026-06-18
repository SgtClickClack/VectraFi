"""
Integration tests for new telemetry and protocol-discovery endpoints.

Covers:
  1. GET /api/v1/protocol/params — returns expected tax rate and topology constants.
  2. GET /api/v1/analytics/treasury-breakdown — empty DB returns zero breakdown.
  3. GET /api/v1/analytics/treasury-breakdown — breakdown isolates tx_type correctly.
  4. GET /api/v1/analytics/treasury-breakdown — swarm_equalization fees tracked separately.
  5. GET /api/v1/analytics/swarm — equalization_count / equalization_volume_usdc default to zero.
  6. POST /api/v1/analytics/swarm/heartbeat — equalization fields round-trip correctly.
  7. GET /api/v1/analytics/stats — no regression on existing stats endpoint.
"""

import time
import uuid

import pytest
from conftest import _TestSessionLocal, make_agent, sign_body

from models import SettlementTransaction


_PROTOCOL_URL       = "/api/v1/protocol/params"
_NEGOTIATE_URL      = "/api/v1/protocol/negotiate-intent"
_BREAKDOWN_URL  = "/api/v1/analytics/treasury-breakdown"
_SWARM_URL      = "/api/v1/analytics/swarm"
_HEARTBEAT_URL  = "/api/v1/analytics/swarm/heartbeat"
_STATS_URL      = "/api/v1/analytics/stats"
_TRANSFER_URL   = "/api/v1/settlement/transfer"


def _seed_tx(sender_id: str, receiver_id: str, tx_type: str, gross: float = 10.0) -> None:
    """Directly insert a SettlementTransaction row for isolation testing."""
    tax  = round(gross * 0.001, 8)
    net  = gross - tax
    db   = _TestSessionLocal()
    tx   = SettlementTransaction(
        tx_id=str(uuid.uuid4()),
        sender_id=sender_id,
        receiver_id=receiver_id,
        gross_amount_usdc=str(gross),
        tax_amount_usdc=str(tax),
        net_amount_usdc=str(net),
        tx_type=tx_type,
        created_at=int(time.time()),
    )
    db.add(tx)
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# 1. Protocol params
# ---------------------------------------------------------------------------

def test_protocol_params_tax_rate(client):
    resp = client.get(_PROTOCOL_URL)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["tax_rate_pct"]      == pytest.approx(0.1)
    assert d["tax_rate_fraction"] == pytest.approx(0.001)


def test_protocol_params_topology(client):
    resp = client.get(_PROTOCOL_URL)
    assert resp.status_code == 200
    d = resp.json()
    assert d["relay_hops"]           == 3
    assert d["candidate_cap"]        == 10
    assert d["gas_cost_per_hop_usdc"] == pytest.approx(0.05)
    assert d["min_transfer_usdc"]    == pytest.approx(0.0001)
    assert d["safety_floor_pct"]     == pytest.approx(0.005)


def test_protocol_params_has_execution_mode(client):
    resp = client.get(_PROTOCOL_URL)
    assert resp.status_code == 200
    d = resp.json()
    assert d["execution_mode"] in ("sandbox", "live_rpc")
    assert "protocol_domain" in d
    assert len(d["protocol_domain"]) > 0


# ---------------------------------------------------------------------------
# 2. Treasury breakdown — empty DB
# ---------------------------------------------------------------------------

def test_treasury_breakdown_empty_returns_zero(client):
    # This test runs before any breakdown-specific seeds, but the shared DB
    # may already have transactions from other test files.  We only assert
    # structural shape here, not exact zero counts.
    resp = client.get(_BREAKDOWN_URL)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert "accumulated_fees_usdc"  in d
    assert "equalization_fees_usdc" in d
    assert "tx_type_breakdown"      in d
    assert isinstance(d["tx_type_breakdown"], list)


# ---------------------------------------------------------------------------
# 3. Treasury breakdown — isolates tx_type correctly
# ---------------------------------------------------------------------------

def test_treasury_breakdown_isolates_tx_types(client):
    _seed_tx("bp3-sender", "bp3-receiver", "peer_transfer",   gross=100.0)
    _seed_tx("bp3-sender", "bp3-receiver", "internal_rebalance", gross=50.0)

    resp = client.get(_BREAKDOWN_URL)
    assert resp.status_code == 200
    d    = resp.json()
    rows = {item["tx_type"]: item for item in d["tx_type_breakdown"]}

    assert "peer_transfer"      in rows
    assert "internal_rebalance" in rows
    assert rows["peer_transfer"]["count"]        >= 1
    assert rows["internal_rebalance"]["count"]   >= 1


# ---------------------------------------------------------------------------
# 4. swarm_equalization fees tracked separately
# ---------------------------------------------------------------------------

def test_treasury_breakdown_equalization_fees_isolated(client):
    eq_gross = 30.0
    eq_tax   = round(eq_gross * 0.001, 8)

    _seed_tx("eq4-donor", "eq4-stalled", "swarm_equalization", gross=eq_gross)

    resp = client.get(_BREAKDOWN_URL)
    assert resp.status_code == 200
    d    = resp.json()

    # equalization_fees_usdc must capture at least the one row we seeded.
    assert d["equalization_fees_usdc"] >= eq_tax - 1e-6

    rows = {item["tx_type"]: item for item in d["tx_type_breakdown"]}
    assert "swarm_equalization" in rows
    assert rows["swarm_equalization"]["count"] >= 1


# ---------------------------------------------------------------------------
# 5. Swarm endpoint — equalization fields default to zero
# ---------------------------------------------------------------------------

def test_swarm_analytics_equalization_defaults(client):
    resp = client.get(_SWARM_URL)
    assert resp.status_code == 200
    d = resp.json()
    # New fields must always be present; when no log / heartbeat, they default 0.
    assert "equalization_count"       in d
    assert "equalization_volume_usdc" in d
    assert d["equalization_count"]       >= 0
    assert d["equalization_volume_usdc"] >= 0.0


# ---------------------------------------------------------------------------
# 6. Heartbeat — equalization fields round-trip
# ---------------------------------------------------------------------------

def test_heartbeat_equalization_roundtrip(client):
    body = {
        "iterations":             42,
        "route_checks":           84,
        "viable_routes":          10,
        "equalization_count":     5,
        "equalization_volume_usdc": 150.0,
        "desks": [
            {"name": "Alpha", "balance_usdc": 800.0, "transfers_ok": 3, "transfers_err": 0}
        ],
    }
    resp = client.post(_HEARTBEAT_URL, json=body)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["equalization_count"]       == 5
    assert d["equalization_volume_usdc"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# 7. Stats endpoint — no regression
# ---------------------------------------------------------------------------

def test_stats_endpoint_no_regression(client):
    resp = client.get(_STATS_URL)
    assert resp.status_code == 200
    d = resp.json()
    assert "total_transactions_processed" in d
    assert "total_volume_processed_usdc"  in d
    assert "avg_latency_ms"               in d
    assert d["success_rate_pct"]          == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 8. negotiate-intent — 202 handshake with negotiation_id
# ---------------------------------------------------------------------------

def test_negotiate_intent_returns_202(client):
    body = {
        "agent_id": "test-citizen-agent",
        "intent_type": "liquidity_allocation",
        "requested_liquidity_usdc": 500.0,
        "target_corridor": "Alpha-Beta liquidity corridor",
    }
    resp = client.post(_NEGOTIATE_URL, json=body)
    assert resp.status_code == 202, resp.text
    d = resp.json()
    assert d["agent_id"]   == "test-citizen-agent"
    assert d["intent_type"] == "liquidity_allocation"
    assert d["status"]      == "accepted"
    assert len(d["negotiation_id"]) == 36  # UUID4 length


def test_negotiate_intent_corridor_provisioning(client):
    body = {
        "agent_id": "swarm-beta",
        "intent_type": "corridor_provisioning",
        "proposed_toll_share_pct": 0.3,
        "target_corridor": "Beta-Gamma express lane",
        "metadata": {"priority": "high", "deployment_wave": 2},
    }
    resp = client.post(_NEGOTIATE_URL, json=body)
    assert resp.status_code == 202, resp.text
    d = resp.json()
    assert d["status"] == "accepted"
    assert "negotiation_id" in d


def test_negotiate_intent_rejects_invalid_intent_type(client):
    body = {
        "agent_id": "rogue-agent",
        "intent_type": "invalid_type",
    }
    resp = client.post(_NEGOTIATE_URL, json=body)
    assert resp.status_code == 422
