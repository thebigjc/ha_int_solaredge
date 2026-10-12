"""Tests for battery aggregation from the real storage telemetry response."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from custom_components.solaredge_v2.api import Series
from custom_components.solaredge_v2.api.models import Telemetry, parse_telemetry
from custom_components.solaredge_v2.battery import (
    battery_power,
    battery_remaining_energy,
    battery_state_of_energy,
    has_data,
    site_power,
    site_state_of_energy,
)

from .conftest import load

EDT = timezone(timedelta(hours=-4))
LATEST = datetime(2026, 10, 12, 8, 45, tzinfo=EDT)


@pytest.fixture
def storage() -> Telemetry:
    """The real two-battery response; every series ends in an in-progress null bucket."""
    return parse_telemetry(load("storage_telemetry.json"), "storage")


def _series(unit: str, *values: tuple[int, float | None]) -> Series:
    return Series.from_api(
        {
            "unit": unit,
            "values": [
                {"timestamp": datetime(2026, 1, 1, 0, m, tzinfo=UTC).isoformat(), "value": v}
                for m, v in values
            ],
        }
    )


def test_fixture_ends_in_null_buckets(storage: Telemetry) -> None:
    """The latest bucket is null, so the readings come from the one before it."""
    assert storage["BAT-1"]["stateOfEnergy"].samples[-1].value is None
    assert storage["BAT-1"]["stateOfEnergy"].latest == pytest.approx(0.17699128)


def test_battery_state_of_energy_is_percent(storage: Telemetry) -> None:
    """The API sends a 0-1 fraction labelled PERCENTAGE; the sensor reports percent."""
    first = battery_state_of_energy(storage["BAT-1"])
    second = battery_state_of_energy(storage["BAT-2"])
    assert first is not None
    assert second is not None
    assert first.value == pytest.approx(17.699128)
    assert second.value == pytest.approx(19.509332)
    assert first.timestamp == LATEST


def test_battery_remaining_energy_in_wh(storage: Telemetry) -> None:
    """Remaining energy is the latest non-null value in Wh with its bucket time."""
    reading = battery_remaining_energy(storage["BAT-1"])
    assert reading is not None
    assert reading.value == pytest.approx(1614.2666)
    assert reading.timestamp == LATEST


def test_battery_power_is_charge_minus_discharge(storage: Telemetry) -> None:
    """Positive means charging."""
    reading = battery_power(storage["BAT-2"])
    assert reading is not None
    assert reading.value == pytest.approx(666.6667)
    discharging = {
        "chargePower": _series("W", (0, 0.0), (15, None)),
        "dischargePower": _series("W", (0, 1200.0), (15, None)),
    }
    reading = battery_power(discharging)
    assert reading is not None
    assert reading.value == -1200.0


def test_battery_power_converts_units() -> None:
    """kW inputs are normalised to watts."""
    reading = battery_power({"chargePower": _series("KW", (0, 1.5)), "dischargePower": _series("KW", (0, 0.5))})
    assert reading is not None
    assert reading.value == pytest.approx(1000.0)


def test_battery_power_with_one_direction_only() -> None:
    """A missing discharge series counts as no discharge, not as no data."""
    reading = battery_power({"chargePower": _series("W", (0, 300.0))})
    assert reading is not None
    assert reading.value == 300.0


def test_site_state_of_energy_is_capacity_weighted(storage: Telemetry) -> None:
    """Sum(remaining) / Sum(remaining / fraction), not the mean of the two."""
    reading = site_state_of_energy(storage)
    assert reading is not None
    remaining = 1614.2666 + 1756.3666
    capacity = 1614.2666 / 0.17699128 + 1756.3666 / 0.19509332
    assert reading.value == pytest.approx(remaining / capacity * 100)
    assert reading.value == pytest.approx(18.6, abs=0.05)
    assert reading.timestamp == LATEST


def test_site_state_of_energy_ignores_batteries_without_data(storage: Telemetry) -> None:
    """BAT-3 has only an empty series; it neither counts as 0 nor drags the result."""
    assert "BAT-3" in storage
    only_first = site_state_of_energy({"BAT-1": storage["BAT-1"], "BAT-3": storage["BAT-3"]})
    assert only_first is not None
    assert only_first.value == pytest.approx(17.699128)


def test_site_state_of_energy_falls_back_to_mean_fraction() -> None:
    """Without remaining energy the mean fraction times 100 is the best estimate."""
    reading = site_state_of_energy(
        {
            "A": {"stateOfEnergy": _series("PERCENTAGE", (0, 0.2))},
            "B": {"stateOfEnergy": _series("PERCENTAGE", (0, 0.4))},
        }
    )
    assert reading is not None
    assert reading.value == pytest.approx(30.0)


def test_site_state_of_energy_without_every_capacity_uses_mean() -> None:
    """An empty battery gives no capacity, so weighting would hide it; use the mean fraction."""
    reading = site_state_of_energy(
        {
            "A": {"stateOfEnergy": _series("PERCENTAGE", (0, 0.0)), "remainingEnergy": _series("WH", (0, 0.0))},
            "B": {"stateOfEnergy": _series("PERCENTAGE", (0, 0.5)), "remainingEnergy": _series("WH", (0, 5000.0))},
        }
    )
    assert reading is not None
    assert reading.value == pytest.approx(25.0)


def test_site_power_sums_batteries(storage: Telemetry) -> None:
    """Site power is the sum of each battery's charge minus discharge."""
    reading = site_power(storage)
    assert reading is not None
    assert reading.value == pytest.approx(666.0 + 666.6667)
    assert reading.timestamp == LATEST


def test_no_data_gives_none(storage: Telemetry) -> None:
    """With only empty series every reading is None, never 0."""
    empty = {"BAT-3": storage["BAT-3"]}
    assert site_state_of_energy(empty) is None
    assert site_power(empty) is None
    assert site_state_of_energy({}) is None
    assert battery_state_of_energy(storage["BAT-3"]) is None
    assert battery_remaining_energy(storage["BAT-3"]) is None
    assert battery_power(storage["BAT-3"]) is None


def test_has_data(storage: Telemetry) -> None:
    """A battery has data when a metric we read has a non-null sample."""
    assert has_data(storage["BAT-1"])
    assert not has_data(storage["BAT-3"])
    assert not has_data({"stateOfEnergy": _series("PERCENTAGE", (0, None))})


def test_battery_power_ignores_unknown_units() -> None:
    """A value in a unit that is not a power unit is no reading."""
    assert battery_power({"chargePower": _series("WH", (0, 5.0))}) is None
