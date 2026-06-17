"""
Token Leasing Allocation Model
==============================
Sandbox extension — workspace/ only. Self-contained, no protocol layer imports.

Calculates lease terms for tokenized credit lines based on principal,
duration, and a configurable micro-tax rate applied per time unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


TimeUnit = Literal["hour", "day", "week"]

_SECONDS: dict[TimeUnit, int] = {
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
}


@dataclass(frozen=True)
class LeaseTerms:
    principal: float          # token units locked as collateral
    duration_units: int       # number of time-units for the lease
    time_unit: TimeUnit       # granularity
    micro_tax_rate: float     # fractional fee per time-unit (e.g. 0.001 = 0.1%)

    # computed on init
    total_fee: float = field(init=False)
    expiry_epoch: int = field(init=False)

    def __post_init__(self) -> None:
        if self.principal <= 0:
            raise ValueError("principal must be positive")
        if self.duration_units <= 0:
            raise ValueError("duration_units must be positive")
        if not (0 < self.micro_tax_rate < 1):
            raise ValueError("micro_tax_rate must be in (0, 1)")

        fee = self.principal * self.micro_tax_rate * self.duration_units
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        expiry = now_epoch + self.duration_units * _SECONDS[self.time_unit]

        # frozen dataclass — use object.__setattr__ to set computed fields
        object.__setattr__(self, "total_fee", round(fee, 8))
        object.__setattr__(self, "expiry_epoch", expiry)

    @property
    def duration_seconds(self) -> int:
        return self.duration_units * _SECONDS[self.time_unit]

    @property
    def effective_apr(self) -> float:
        """Annualised cost as a fraction of principal."""
        units_per_year = 365 * 86_400 / _SECONDS[self.time_unit]
        return self.micro_tax_rate * units_per_year

    def to_dict(self) -> dict:
        return {
            "principal": self.principal,
            "duration_units": self.duration_units,
            "time_unit": self.time_unit,
            "micro_tax_rate": self.micro_tax_rate,
            "total_fee": self.total_fee,
            "expiry_epoch": self.expiry_epoch,
            "duration_seconds": self.duration_seconds,
            "effective_apr": round(self.effective_apr, 6),
        }


def calculate_lease(
    principal: float,
    duration_units: int,
    time_unit: TimeUnit = "day",
    micro_tax_rate: float = 0.001,
) -> LeaseTerms:
    """Public entry point for lease term calculation."""
    return LeaseTerms(
        principal=principal,
        duration_units=duration_units,
        time_unit=time_unit,
        micro_tax_rate=micro_tax_rate,
    )
