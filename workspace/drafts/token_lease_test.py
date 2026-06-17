"""Unit tests for the token leasing allocation model."""

import math
import pytest
from token_lease import LeaseTerms, calculate_lease


# ---------------------------------------------------------------------------
# Core fee calculation
# ---------------------------------------------------------------------------

def test_total_fee_basic():
    lease = calculate_lease(principal=1000.0, duration_units=7, time_unit="day", micro_tax_rate=0.001)
    # expected: 1000 * 0.001 * 7 = 7.0
    assert lease.total_fee == pytest.approx(7.0, rel=1e-6)


def test_total_fee_hourly():
    lease = calculate_lease(principal=500.0, duration_units=24, time_unit="hour", micro_tax_rate=0.0005)
    # expected: 500 * 0.0005 * 24 = 6.0
    assert lease.total_fee == pytest.approx(6.0, rel=1e-6)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def test_duration_seconds_day():
    lease = calculate_lease(principal=100.0, duration_units=3, time_unit="day")
    assert lease.duration_seconds == 3 * 86_400


def test_duration_seconds_week():
    lease = calculate_lease(principal=100.0, duration_units=2, time_unit="week")
    assert lease.duration_seconds == 2 * 604_800


# ---------------------------------------------------------------------------
# APR calculation
# ---------------------------------------------------------------------------

def test_effective_apr_daily_rate():
    lease = calculate_lease(principal=1.0, duration_units=1, time_unit="day", micro_tax_rate=0.001)
    expected_apr = 0.001 * 365
    assert lease.effective_apr == pytest.approx(expected_apr, rel=1e-4)


# ---------------------------------------------------------------------------
# Expiry epoch is in the future
# ---------------------------------------------------------------------------

def test_expiry_is_future():
    import time
    lease = calculate_lease(principal=100.0, duration_units=1, time_unit="hour")
    assert lease.expiry_epoch > int(time.time())


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_lease_terms_immutable():
    lease = calculate_lease(principal=100.0, duration_units=1, time_unit="day")
    with pytest.raises((AttributeError, TypeError)):
        lease.total_fee = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation guards
# ---------------------------------------------------------------------------

def test_rejects_zero_principal():
    with pytest.raises(ValueError, match="principal"):
        LeaseTerms(principal=0, duration_units=1, time_unit="day", micro_tax_rate=0.001)


def test_rejects_negative_duration():
    with pytest.raises(ValueError, match="duration_units"):
        LeaseTerms(principal=100.0, duration_units=-1, time_unit="day", micro_tax_rate=0.001)


def test_rejects_tax_rate_above_one():
    with pytest.raises(ValueError, match="micro_tax_rate"):
        LeaseTerms(principal=100.0, duration_units=1, time_unit="day", micro_tax_rate=1.5)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def test_to_dict_keys():
    lease = calculate_lease(principal=250.0, duration_units=5, time_unit="day")
    d = lease.to_dict()
    expected_keys = {
        "principal", "duration_units", "time_unit", "micro_tax_rate",
        "total_fee", "expiry_epoch", "duration_seconds", "effective_apr",
    }
    assert set(d.keys()) == expected_keys
