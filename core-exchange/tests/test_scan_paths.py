"""
Integration tests for POST /api/v1/arbitrage/scan-paths

Covers:
  1. Two viable agents — 1 path of length 2 is viable.
  2. Unknown agent in candidates — path with it is not viable, sweep completes.
  3. Non-mutating guarantee — no wallet balance changes after scan.
  4. max_paths cap — response contains at most max_paths paths.
  5. path_length=2 from 3 agents — returns up to 3×2=6 permutations.
  6. Validation: too-short candidate list (< 2 agents) → 422.
  7. Validation: invalid volume (≤ 0) → 422.
  8. viable_count matches the number of viable paths in the result list.
  9. response shape — all required fields present.
 10. All-unknown pool — zero viable routes, scan completes without error.
"""

import pytest
from conftest import _TestSessionLocal, make_agent

_URL = "/api/v1/arbitrage/scan-paths"


def _post(client, body: dict):
    return client.post(_URL, json=body)


def _get_balance(agent_id: str) -> float:
    db = _TestSessionLocal()
    from models import AgentWallet
    wallet = db.get(AgentWallet, agent_id)
    db.close()
    return float(wallet.balance_usdc) if wallet else None


# ---------------------------------------------------------------------------
# 1. Two viable agents — 1 path of length 2 is viable
# ---------------------------------------------------------------------------

def test_scan_paths_two_viable_agents(client):
    _, a = make_agent("sp1-alpha", balance_usdc=500.0)
    _, b = make_agent("sp1-beta",  balance_usdc=500.0)

    body = {
        "candidate_agents": [a.agent_id, b.agent_id],
        "volume_usdc": 10.0,
        "path_length": 2,
        "max_paths": 10,
    }
    resp = _post(client, body)
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["total_paths_checked"] == 2  # A→B and B→A
    assert d["viable_count"] > 0


# ---------------------------------------------------------------------------
# 2. Unknown agent — path with it is not viable, sweep completes
# ---------------------------------------------------------------------------

def test_scan_paths_unknown_agent_in_pool(client):
    _, a = make_agent("sp2-alpha", balance_usdc=500.0)

    body = {
        "candidate_agents": [a.agent_id, "sp2-ghost-agent"],
        "volume_usdc": 5.0,
        "path_length": 2,
        "max_paths": 10,
    }
    resp = _post(client, body)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["total_paths_checked"] == 2

    # At least one path should be non-viable (the one containing the ghost).
    non_viable = [p for p in d["paths"] if not p["viable"]]
    assert len(non_viable) >= 1


# ---------------------------------------------------------------------------
# 3. Non-mutating guarantee — balances unchanged
# ---------------------------------------------------------------------------

def test_scan_paths_does_not_mutate_balances(client):
    _, a = make_agent("sp3-alpha", balance_usdc=300.0)
    _, b = make_agent("sp3-beta",  balance_usdc=300.0)
    _, c = make_agent("sp3-gamma", balance_usdc=300.0)

    before = {w: _get_balance(w) for w in [a.agent_id, b.agent_id, c.agent_id]}

    body = {
        "candidate_agents": [a.agent_id, b.agent_id, c.agent_id],
        "volume_usdc": 50.0,
        "path_length": 3,
        "max_paths": 10,
    }
    resp = _post(client, body)
    assert resp.status_code == 200

    after = {w: _get_balance(w) for w in [a.agent_id, b.agent_id, c.agent_id]}
    for agent_id in before:
        assert before[agent_id] == pytest.approx(after[agent_id], rel=1e-8), (
            f"Balance changed for {agent_id}: {before[agent_id]} → {after[agent_id]}"
        )


# ---------------------------------------------------------------------------
# 4. max_paths cap enforced
# ---------------------------------------------------------------------------

def test_scan_paths_max_paths_cap(client):
    agents = []
    for i in range(5):
        _, w = make_agent(f"sp4-agent{i}", balance_usdc=200.0)
        agents.append(w.agent_id)

    body = {
        "candidate_agents": agents,
        "volume_usdc": 5.0,
        "path_length": 2,
        "max_paths": 3,
    }
    resp = _post(client, body)
    assert resp.status_code == 200
    d = resp.json()
    assert d["total_paths_checked"] <= 3
    assert len(d["paths"]) <= 3


# ---------------------------------------------------------------------------
# 5. path_length=2 from 3 agents — 6 permutations
# ---------------------------------------------------------------------------

def test_scan_paths_permutation_count(client):
    _, a = make_agent("sp5-alpha", balance_usdc=200.0)
    _, b = make_agent("sp5-beta",  balance_usdc=200.0)
    _, c = make_agent("sp5-gamma", balance_usdc=200.0)

    body = {
        "candidate_agents": [a.agent_id, b.agent_id, c.agent_id],
        "volume_usdc": 5.0,
        "path_length": 2,
        "max_paths": 100,
    }
    resp = _post(client, body)
    assert resp.status_code == 200
    d = resp.json()
    # P(3,2) = 6 permutations
    assert d["total_paths_checked"] == 6


# ---------------------------------------------------------------------------
# 6. Validation: too-short candidate list → 422
# ---------------------------------------------------------------------------

def test_scan_paths_too_few_candidates_returns_422(client):
    body = {
        "candidate_agents": ["only-one"],
        "volume_usdc": 10.0,
        "path_length": 2,
    }
    resp = _post(client, body)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 7. Validation: volume ≤ 0 → 422
# ---------------------------------------------------------------------------

def test_scan_paths_zero_volume_returns_422(client):
    body = {
        "candidate_agents": ["agent-a", "agent-b"],
        "volume_usdc": 0.0,
        "path_length": 2,
    }
    resp = _post(client, body)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 8. viable_count matches result list
# ---------------------------------------------------------------------------

def test_scan_paths_viable_count_matches_paths(client):
    _, a = make_agent("sp8-alpha", balance_usdc=500.0)
    _, b = make_agent("sp8-beta",  balance_usdc=500.0)

    body = {
        "candidate_agents": [a.agent_id, b.agent_id],
        "volume_usdc": 5.0,
        "path_length": 2,
        "max_paths": 10,
    }
    resp = _post(client, body)
    assert resp.status_code == 200
    d = resp.json()
    computed = sum(1 for p in d["paths"] if p["viable"])
    assert computed == d["viable_count"]


# ---------------------------------------------------------------------------
# 9. Response shape — required fields present
# ---------------------------------------------------------------------------

def test_scan_paths_response_shape(client):
    _, a = make_agent("sp9-alpha", balance_usdc=200.0)
    _, b = make_agent("sp9-beta",  balance_usdc=200.0)

    body = {
        "candidate_agents": [a.agent_id, b.agent_id],
        "volume_usdc": 5.0,
        "path_length": 2,
        "max_paths": 2,
    }
    resp = _post(client, body)
    assert resp.status_code == 200
    d = resp.json()

    assert "total_paths_checked"    in d
    assert "viable_count"           in d
    assert "volume_usdc"            in d
    assert "slippage_tolerance_pct" in d
    assert "paths"                  in d

    for path_result in d["paths"]:
        assert "path"                 in path_result
        assert "viable"               in path_result
        assert "steps"                in path_result
        assert "expected_output_usdc" in path_result
        assert "total_slippage_usdc"  in path_result


# ---------------------------------------------------------------------------
# 10. All-unknown pool — zero viable routes, no error
# ---------------------------------------------------------------------------

def test_scan_paths_all_unknown_pool(client):
    body = {
        "candidate_agents": ["ghost-1", "ghost-2", "ghost-3"],
        "volume_usdc": 10.0,
        "path_length": 2,
        "max_paths": 10,
    }
    resp = _post(client, body)
    assert resp.status_code == 200
    d = resp.json()
    assert d["viable_count"] == 0
    assert d["total_paths_checked"] > 0
