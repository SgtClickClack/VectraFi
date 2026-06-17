"""Unit tests for the multi-agent liquidity pooler."""

import math
import pytest
from liquidity_pooler import (
    PoolParticipant,
    PoolYieldDistribution,
    build_pool,
    pool_leases,
    calculate_lease,
    LeaseTerms,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_lease(principal: float, duration: int = 7, rate: float = 0.001) -> LeaseTerms:
    return calculate_lease(principal=principal, duration_units=duration,
                           time_unit="day", micro_tax_rate=rate)


# ---------------------------------------------------------------------------
# Pool yield totals
# ---------------------------------------------------------------------------

def test_total_yield_equals_sum_of_lease_fees():
    l0 = make_lease(1000.0)   # fee = 7.0
    l1 = make_lease(500.0)    # fee = 3.5
    dist = pool_leases([("agent-zero", l0, 1.0), ("agent-one", l1, 1.0)])
    expected = round(l0.total_fee + l1.total_fee, 8)
    assert dist.total_pool_yield == pytest.approx(expected, rel=1e-6)


def test_single_participant_gets_full_yield():
    lease = make_lease(2000.0)
    dist = pool_leases([("agent-zero", lease, 1.0)])
    assert dist.shares["agent-zero"] == pytest.approx(lease.total_fee, rel=1e-6)


# ---------------------------------------------------------------------------
# Yield split proportionality
# ---------------------------------------------------------------------------

def test_equal_principal_equal_split():
    l0 = make_lease(1000.0)
    l1 = make_lease(1000.0)
    dist = pool_leases([("agent-zero", l0, 1.0), ("agent-one", l1, 1.0)])
    assert dist.shares["agent-zero"] == pytest.approx(dist.shares["agent-one"], rel=1e-6)


def test_double_principal_double_share():
    l0 = make_lease(2000.0)   # 2x principal
    l1 = make_lease(1000.0)
    dist = pool_leases([("agent-zero", l0, 1.0), ("agent-one", l1, 1.0)])
    ratio = dist.shares["agent-zero"] / dist.shares["agent-one"]
    assert ratio == pytest.approx(2.0, rel=1e-4)


def test_risk_multiplier_shifts_share():
    l0 = make_lease(1000.0)
    l1 = make_lease(1000.0)
    # agent-one has 2x risk appetite
    dist = pool_leases([("agent-zero", l0, 1.0), ("agent-one", l1, 2.0)])
    # agent-one's weighted share = 2/(1+2) = 0.6667; agent-zero = 0.3333
    assert dist.weights["agent-one"] == pytest.approx(2 / 3, rel=1e-4)
    assert dist.weights["agent-zero"] == pytest.approx(1 / 3, rel=1e-4)


# ---------------------------------------------------------------------------
# Yield distribution invariant: sum of shares <= total_pool_yield
# ---------------------------------------------------------------------------

def test_shares_sum_does_not_exceed_total():
    leases = [make_lease(p) for p in [100.0, 300.0, 600.0, 750.0, 50.0]]
    agents = [(f"agent-{i}", l, 1.0 + i * 0.25) for i, l in enumerate(leases)]
    dist = pool_leases(agents)
    assert sum(dist.shares.values()) <= dist.total_pool_yield + 1e-6


def test_weights_sum_to_one():
    leases = [make_lease(p) for p in [400.0, 600.0]]
    dist = pool_leases([("a0", leases[0], 1.5), ("a1", leases[1], 0.8)])
    assert sum(dist.weights.values()) == pytest.approx(1.0, rel=1e-8)


# ---------------------------------------------------------------------------
# Pool expiry = min of constituent lease expiries
# ---------------------------------------------------------------------------

def test_pool_expiry_is_minimum_of_leases():
    l_short = calculate_lease(500.0, duration_units=1, time_unit="hour")
    l_long  = calculate_lease(500.0, duration_units=30, time_unit="day")
    dist = pool_leases([("agent-zero", l_short, 1.0), ("agent-one", l_long, 1.0)])
    expected_min = min(l_short.expiry_epoch, l_long.expiry_epoch)
    assert dist.expiry_epoch == expected_min


# ---------------------------------------------------------------------------
# Bounds / guard tests
# ---------------------------------------------------------------------------

def test_empty_participant_list_raises():
    with pytest.raises(ValueError, match="at least one"):
        build_pool([])


def test_blank_agent_id_raises():
    with pytest.raises(ValueError, match="agent_id"):
        PoolParticipant(agent_id="  ", lease=make_lease(100.0), risk_multiplier=1.0)


def test_zero_risk_multiplier_raises():
    with pytest.raises(ValueError, match="risk_multiplier"):
        PoolParticipant(agent_id="a0", lease=make_lease(100.0), risk_multiplier=0.0)


def test_negative_risk_multiplier_raises():
    with pytest.raises(ValueError, match="risk_multiplier"):
        PoolParticipant(agent_id="a0", lease=make_lease(100.0), risk_multiplier=-1.0)


# ---------------------------------------------------------------------------
# Three-agent heterogeneous pool (integration-style)
# ---------------------------------------------------------------------------

def test_three_agent_heterogeneous_pool():
    l0 = make_lease(1000.0, duration=7,  rate=0.001)   # fee = 7.0
    l1 = make_lease(2000.0, duration=14, rate=0.0005)  # fee = 14.0
    l2 = make_lease(500.0,  duration=3,  rate=0.002)   # fee = 3.0
    dist = pool_leases([
        ("agent-zero", l0, 1.0),
        ("agent-one",  l1, 1.5),
        ("agent-two",  l2, 0.75),
    ])
    assert dist.total_pool_yield == pytest.approx(24.0, rel=1e-5)
    assert set(dist.shares.keys()) == {"agent-zero", "agent-one", "agent-two"}
    assert all(v > 0 for v in dist.shares.values())
