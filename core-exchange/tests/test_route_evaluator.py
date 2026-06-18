"""
Unit tests for services/route_evaluator.py — Cognitive Token Cost Throttling.

All tests are pure-Python: no HTTP, no database, no event loop.
The evaluator is designed to be importable without any FastAPI or SQLAlchemy
dependency, so these tests run in isolation from the rest of the test suite.

Covers:
  E01  is_route_viable_local: empty desk list → False.
  E02  is_route_viable_local: all desks above floor → True.
  E03  is_route_viable_local: one desk below floor → False.
  E04  is_route_viable_local: exactly at floor boundary → True (inclusive).
  E05  is_route_viable_local: single-agent chain above floor → True.
  E06  is_route_viable_local: custom gas cost applied correctly.
  E07  compute_top_up: balance above threshold → None.
  E08  compute_top_up: balance below threshold → correct gap returned.
  E09  compute_top_up: balance exactly at threshold → None.
  E10  select_equalization_donor: no eligible donor → None.
  E11  select_equalization_donor: single eligible donor selected.
  E12  select_equalization_donor: richest eligible donor wins.
  E13  select_equalization_donor: gas guard excludes low-ETH donors in live mode.
  E14  select_equalization_donor: gas guard bypassed in sandbox mode.
  E15  needs_server_rebalance: balance above floor → False.
  E16  needs_server_rebalance: balance below floor → True.
  E17  needs_server_rebalance: balance exactly at floor → False.
  E18  tax_covers_overhead: tax > overhead → True.
  E19  tax_covers_overhead: tax < overhead → False.
  E20  tax_covers_overhead: tax == overhead → False (strict inequality).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from services.route_evaluator import (
    compute_top_up,
    is_route_viable_local,
    needs_server_rebalance,
    select_equalization_donor,
    tax_covers_overhead,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _desk(balance_usdc: float, eth_balance: float = 1.0) -> SimpleNamespace:
    """Lightweight stand-in for seed_swarm.DeskState (no swarm import needed)."""
    return SimpleNamespace(balance_usdc=balance_usdc, eth_balance=eth_balance)


# ---------------------------------------------------------------------------
# E01–E06: is_route_viable_local
# ---------------------------------------------------------------------------

def test_E01_empty_balances_not_viable():
    assert is_route_viable_local([], volume_usdc=100.0, slippage_pct=0.01) is False


def test_E02_all_above_floor_viable():
    # volume=100, slip=0.01, n=2 → floor_each=0.5; gas=0.05 → per_leg=0.55
    # balances=[10, 10] → both >= 0.55 → True
    assert is_route_viable_local([10.0, 10.0], volume_usdc=100.0, slippage_pct=0.01) is True


def test_E03_one_below_floor_not_viable():
    # per_leg = 0.5 + 0.05 = 0.55; balance of 0.3 < 0.55 → False
    assert is_route_viable_local([10.0, 0.3], volume_usdc=100.0, slippage_pct=0.01) is False


def test_E04_exactly_at_floor_viable():
    # volume=100, slip=0.01, n=1 → floor=1.0; gas=0.05 → per_leg=1.05
    # balance=1.05 → exactly at floor → True (inclusive >=)
    assert is_route_viable_local([1.05], volume_usdc=100.0, slippage_pct=0.01) is True


def test_E05_single_agent_above_floor_viable():
    # volume=10, slip=0.005, n=1 → floor=0.05; gas=0.05 → per_leg=0.1
    # balance=5.0 → viable
    assert is_route_viable_local([5.0], volume_usdc=10.0, slippage_pct=0.005) is True


def test_E06_custom_gas_cost_applied():
    # volume=100, slip=0.01, n=1 → slip_floor=1.0; custom gas=2.0 → per_leg=3.0
    # balance=2.5 < 3.0 → not viable
    assert is_route_viable_local(
        [2.5], volume_usdc=100.0, slippage_pct=0.01, gas_cost_per_hop=2.0
    ) is False
    # balance=3.0 exactly → viable
    assert is_route_viable_local(
        [3.0], volume_usdc=100.0, slippage_pct=0.01, gas_cost_per_hop=2.0
    ) is True


# ---------------------------------------------------------------------------
# E07–E09: compute_top_up
# ---------------------------------------------------------------------------

def test_E07_above_threshold_returns_none():
    assert compute_top_up(100.0, stall_threshold=30.0, target_usdc=100.0) is None


def test_E08_below_threshold_returns_gap():
    gap = compute_top_up(20.0, stall_threshold=30.0, target_usdc=100.0)
    assert gap == pytest.approx(80.0)


def test_E09_exactly_at_threshold_returns_none():
    assert compute_top_up(30.0, stall_threshold=30.0, target_usdc=100.0) is None


# ---------------------------------------------------------------------------
# E10–E14: select_equalization_donor
# ---------------------------------------------------------------------------

def test_E10_no_eligible_donor_returns_none():
    stalled = _desk(10.0)
    # Only other desk has exactly target+top_up but not MORE
    other   = _desk(90.0)   # needs 80 top-up + 100 target = 180; other has 90 < 180
    result  = select_equalization_donor(
        [stalled, other], stalled,
        top_up_usdc=80.0, target_usdc=100.0,
        gas_floor_eth=0.002, live_mode=False,
    )
    assert result is None


def test_E11_single_eligible_donor_selected():
    stalled = _desk(10.0)
    donor   = _desk(300.0, eth_balance=0.1)
    result  = select_equalization_donor(
        [stalled, donor], stalled,
        top_up_usdc=90.0, target_usdc=100.0,
        gas_floor_eth=0.002, live_mode=False,
    )
    assert result is donor


def test_E12_richest_eligible_donor_wins():
    stalled = _desk(5.0)
    poor    = _desk(210.0, eth_balance=0.1)   # 210 > 100+100=200 → eligible
    rich    = _desk(500.0, eth_balance=0.5)   # also eligible, richer
    result  = select_equalization_donor(
        [stalled, poor, rich], stalled,
        top_up_usdc=100.0, target_usdc=100.0,
        gas_floor_eth=0.002, live_mode=False,
    )
    assert result is rich


def test_E13_gas_guard_excludes_low_eth_in_live_mode():
    stalled    = _desk(5.0)
    low_gas    = _desk(500.0, eth_balance=0.001)   # eth < gas_floor
    enough_gas = _desk(300.0, eth_balance=0.005)   # eth >= gas_floor
    result     = select_equalization_donor(
        [stalled, low_gas, enough_gas], stalled,
        top_up_usdc=50.0, target_usdc=100.0,
        gas_floor_eth=0.002, live_mode=True,
    )
    assert result is enough_gas


def test_E14_gas_guard_bypassed_in_sandbox_mode():
    stalled  = _desk(5.0)
    low_gas  = _desk(500.0, eth_balance=0.0)   # would fail gas check in live mode
    result   = select_equalization_donor(
        [stalled, low_gas], stalled,
        top_up_usdc=50.0, target_usdc=100.0,
        gas_floor_eth=0.002, live_mode=False,   # sandbox
    )
    assert result is low_gas   # eth check bypassed


# ---------------------------------------------------------------------------
# E15–E17: needs_server_rebalance
# ---------------------------------------------------------------------------

def test_E15_above_floor_no_rebalance():
    # floor = 50.0 * 0.005 = 0.25; balance=5.0 > 0.25 → False
    assert needs_server_rebalance(5.0, rebalance_volume=50.0, safety_floor_pct=0.005) is False


def test_E16_below_floor_needs_rebalance():
    # floor = 50.0 * 0.005 = 0.25; balance=0.1 < 0.25 → True
    assert needs_server_rebalance(0.1, rebalance_volume=50.0, safety_floor_pct=0.005) is True


def test_E17_exactly_at_floor_no_rebalance():
    # floor = 50.0 * 0.005 = 0.25; balance=0.25 — not strictly less than floor → False
    assert needs_server_rebalance(0.25, rebalance_volume=50.0, safety_floor_pct=0.005) is False


# ---------------------------------------------------------------------------
# E18–E20: tax_covers_overhead
# ---------------------------------------------------------------------------

def test_E18_tax_covers_overhead_true():
    # 10 USDC × 1.5% = 0.15 USDC tax; overhead = 0.001 → 0.15 > 0.001 → True
    assert tax_covers_overhead(10.0, estimated_overhead_usdc=0.001) is True


def test_E19_tax_below_overhead_false():
    # 0.01 USDC × 1.5% = 0.00015 USDC tax; overhead = 0.001 → 0.00015 < 0.001 → False
    assert tax_covers_overhead(0.01, estimated_overhead_usdc=0.001) is False


def test_E20_tax_equal_overhead_false():
    # Strict inequality: tax must be *greater than* overhead, not equal.
    # 1.0 USDC × 1.5% = 0.015; overhead = 0.015 → not strictly greater → False
    assert tax_covers_overhead(1.0, estimated_overhead_usdc=0.015) is False
