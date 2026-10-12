"""Polling policy: which data groups are read, how often, and what that costs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from math import gcd
from typing import Any

from .const import (
    CONF_DEVICE_TELEMETRY,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_STORAGE_INTERVAL_MINUTES,
    CONF_TELEMETRY_INTERVAL_MINUTES,
    CONF_TELEMETRY_INVERTERS,
    CONF_TELEMETRY_METERS,
    CONF_TELEMETRY_STORAGE,
    DEFAULT_DEVICE_TELEMETRY,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    FOLLOW_SITE_INTERVAL,
    INVENTORY_UPDATE_INTERVAL,
    MINUTES_PER_MONTH,
    SLOW_UPDATE_INTERVAL,
)

SLOW_CALLS = 3  # metadata, lifetime energy, open alerts
SITE_CALLS = 2  # overview, power
INVENTORY_CALLS = 1


@dataclass(frozen=True)
class PollingPlan:
    """The effective polling policy of one entry, resolved from its options."""

    site_minutes: int
    inverters: bool
    meters: bool
    storage: bool
    telemetry_minutes: int
    storage_minutes: int

    @property
    def any_telemetry(self) -> bool:
        """Whether any device family is polled."""
        return self.inverters or self.meters or self.storage

    @property
    def slow_minutes(self) -> int:
        """Metadata, lifetime energy, and alerts: every six hours, or the site interval if longer."""
        return max(int(SLOW_UPDATE_INTERVAL.total_seconds() // 60), self.site_minutes)

    @property
    def tick(self) -> timedelta:
        """The coordinator tick: the GCD of the enabled telemetry and site intervals."""
        tick = self.site_minutes
        if self.inverters or self.meters:
            tick = gcd(tick, self.telemetry_minutes)
        if self.storage:
            tick = gcd(tick, self.storage_minutes)
        return timedelta(minutes=tick)


def resolve_plan(options: Mapping[str, Any]) -> PollingPlan:
    """Read the options, interpreting the older single telemetry switch.

    An entry saved before per-family telemetry has only `device_telemetry`;
    true means all three families, following the site interval.
    """
    site = int(options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES))
    legacy = bool(options.get(CONF_DEVICE_TELEMETRY, DEFAULT_DEVICE_TELEMETRY))
    return PollingPlan(
        site_minutes=site,
        inverters=bool(options.get(CONF_TELEMETRY_INVERTERS, legacy)),
        meters=bool(options.get(CONF_TELEMETRY_METERS, legacy)),
        storage=bool(options.get(CONF_TELEMETRY_STORAGE, legacy)),
        telemetry_minutes=int(options.get(CONF_TELEMETRY_INTERVAL_MINUTES, FOLLOW_SITE_INTERVAL))
        or site,
        storage_minutes=int(options.get(CONF_STORAGE_INTERVAL_MINUTES, FOLLOW_SITE_INTERVAL))
        or site,
    )


def _runs(interval_minutes: int) -> int:
    return round(MINUTES_PER_MONTH / interval_minutes)


def estimate_monthly_credits(plan: PollingPlan) -> int:
    """Credits a 30-day month costs, one per call, counting every enabled group.

    Assumes each enabled device family is present on the site; a family the
    site lacks is never called.
    """
    total = SITE_CALLS * _runs(plan.site_minutes)
    total += (int(plan.inverters) + int(plan.meters)) * _runs(plan.telemetry_minutes)
    total += int(plan.storage) * _runs(plan.storage_minutes)
    total += SLOW_CALLS * _runs(plan.slow_minutes)
    total += INVENTORY_CALLS * _runs(int(INVENTORY_UPDATE_INTERVAL.total_seconds() // 60))
    return total
