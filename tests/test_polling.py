"""Tests for the polling plan and the monthly credit estimator."""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.solaredge_v2.const import (
    CONF_DEVICE_TELEMETRY,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_STORAGE_INTERVAL_MINUTES,
    CONF_TELEMETRY_INTERVAL_MINUTES,
    CONF_TELEMETRY_INVERTERS,
    CONF_TELEMETRY_METERS,
    CONF_TELEMETRY_STORAGE,
    FREE_TIER_MONTHLY_CREDITS,
)
from custom_components.solaredge_v2.polling import estimate_monthly_credits, resolve_plan

BATTERY_ONLY = {
    CONF_SCAN_INTERVAL_MINUTES: 720,
    CONF_TELEMETRY_INVERTERS: False,
    CONF_TELEMETRY_METERS: False,
    CONF_TELEMETRY_STORAGE: True,
    CONF_STORAGE_INTERVAL_MINUTES: 30,
}


def test_upstream_defaults_unchanged() -> None:
    """No options: 60 minute site polling, no telemetry, about 1,860 credits."""
    plan = resolve_plan({})
    assert plan.site_minutes == 60
    assert not plan.any_telemetry
    assert plan.slow_minutes == 360
    assert plan.tick == timedelta(minutes=60)
    assert estimate_monthly_credits(plan) == 1860


@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_switch_maps_to_all_families(legacy: bool) -> None:
    """Old device_telemetry true enables all three families at the site interval."""
    plan = resolve_plan({CONF_SCAN_INTERVAL_MINUTES: 45, CONF_DEVICE_TELEMETRY: legacy})
    assert (plan.inverters, plan.meters, plan.storage) == (legacy,) * 3
    assert plan.telemetry_minutes == plan.storage_minutes == 45
    assert plan.tick == timedelta(minutes=45)


def test_new_keys_override_legacy_switch() -> None:
    """Per-family keys win over the old switch."""
    plan = resolve_plan({CONF_DEVICE_TELEMETRY: True, CONF_TELEMETRY_METERS: False})
    assert (plan.inverters, plan.meters, plan.storage) == (True, False, True)


def test_battery_only_plan() -> None:
    """Storage every 30 minutes, site every 12 hours."""
    plan = resolve_plan(BATTERY_ONLY)
    assert plan.tick == timedelta(minutes=30)
    assert plan.slow_minutes == 720
    # site 2*60, storage 1440, slow 3*60, inventory 60
    assert estimate_monthly_credits(plan) == 120 + 1440 + 180 + 60


def test_everything_at_30_minutes() -> None:
    """All families at 30 minutes."""
    plan = resolve_plan({CONF_SCAN_INTERVAL_MINUTES: 30, CONF_DEVICE_TELEMETRY: True})
    # site 2*1440, telemetry 3*1440, slow 3*120, inventory 60
    assert estimate_monthly_credits(plan) == 2880 + 4320 + 360 + 60
    assert estimate_monthly_credits(plan) > FREE_TIER_MONTHLY_CREDITS


def test_tick_is_gcd_of_enabled_intervals() -> None:
    """Disabled families do not shorten the tick."""
    plan = resolve_plan(
        {
            CONF_SCAN_INTERVAL_MINUTES: 90,
            CONF_TELEMETRY_INVERTERS: True,
            CONF_TELEMETRY_INTERVAL_MINUTES: 60,
            CONF_TELEMETRY_STORAGE: True,
            CONF_STORAGE_INTERVAL_MINUTES: 45,
        }
    )
    assert plan.tick == timedelta(minutes=15)
    assert resolve_plan({**BATTERY_ONLY, CONF_TELEMETRY_INTERVAL_MINUTES: 7}).tick == timedelta(minutes=30)


def test_slow_follows_longer_site_interval() -> None:
    """A 24 hour site interval makes slow data daily too."""
    assert resolve_plan({CONF_SCAN_INTERVAL_MINUTES: 1440}).slow_minutes == 1440
