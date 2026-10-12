"""Battery readings derived from storage telemetry.

The API labels `stateOfEnergy` as PERCENTAGE but sends a 0-1 fraction, so every value
here is converted to percent. The newest bucket is usually still null, so readings come
from the latest non-null sample and carry that sample's bucket timestamp. Batteries with
no data are left out of aggregates instead of counting as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .api import Sample, Series, Telemetry
from .api.models import to_watt_hours, to_watts

type Metrics = dict[str, Series]

READ_METRICS = ("chargePower", "dischargePower", "remainingEnergy", "stateOfEnergy")


@dataclass(frozen=True, slots=True)
class Reading:
    """A value and the start of the bucket it was measured in."""

    value: float
    timestamp: datetime | None


def has_data(metrics: Metrics) -> bool:
    """Whether any metric the integration reads has a non-null sample."""
    return any(
        name in metrics and metrics[name].latest_sample is not None for name in READ_METRICS
    )


def _oldest(timestamps: list[datetime | None]) -> datetime | None:
    known = [t for t in timestamps if t is not None]
    return min(known) if known else None


def _fraction(metrics: Metrics) -> Sample | None:
    series = metrics.get("stateOfEnergy")
    return series.latest_sample if series else None


def _remaining_wh(metrics: Metrics) -> Reading | None:
    series = metrics.get("remainingEnergy")
    sample = series.latest_sample if series else None
    if series is None or sample is None:
        return None
    value = to_watt_hours(sample.value, series.unit)
    return Reading(value, sample.timestamp) if value is not None else None


def battery_state_of_energy(metrics: Metrics) -> Reading | None:
    """One battery's state of energy in percent."""
    sample = _fraction(metrics)
    if sample is None or sample.value is None:
        return None
    return Reading(sample.value * 100, sample.timestamp)


def battery_remaining_energy(metrics: Metrics) -> Reading | None:
    """One battery's remaining energy in Wh."""
    return _remaining_wh(metrics)


def battery_power(metrics: Metrics) -> Reading | None:
    """Charge minus discharge in W, positive while charging."""
    parts: list[tuple[float, datetime | None]] = []
    sign = {"chargePower": 1.0, "dischargePower": -1.0}
    for name, factor in sign.items():
        series = metrics.get(name)
        sample = series.latest_sample if series else None
        if series is None or sample is None:
            continue
        if (watts := to_watts(sample.value, series.unit)) is not None:
            parts.append((factor * watts, sample.timestamp))
    if not parts:
        return None
    return Reading(sum(v for v, _ in parts), _oldest([t for _, t in parts]))


def site_state_of_energy(storage: Telemetry) -> Reading | None:
    """Capacity-weighted state of energy in percent across batteries with data.

    Capacity is remaining energy divided by the fraction. When any battery lacks a
    usable capacity, weighting would silently drop it, so the mean fraction is used.
    """
    fractions: list[tuple[float, Reading | None, datetime | None]] = []
    for metrics in storage.values():
        sample = _fraction(metrics)
        if sample is None or sample.value is None:
            continue
        fractions.append((sample.value, _remaining_wh(metrics), sample.timestamp))
    if not fractions:
        return None
    timestamp = _oldest([t for _, _, t in fractions])
    if all(f > 0 and r is not None for f, r, _ in fractions):
        remaining = sum(r.value for _, r, _ in fractions if r is not None)
        capacity = sum(r.value / f for f, r, _ in fractions if r is not None)
        return Reading(remaining / capacity * 100, timestamp)
    return Reading(sum(f for f, _, _ in fractions) / len(fractions) * 100, timestamp)


def site_power(storage: Telemetry) -> Reading | None:
    """Total battery power in W, positive while charging, over batteries with data."""
    readings = [r for m in storage.values() if (r := battery_power(m)) is not None]
    if not readings:
        return None
    return Reading(sum(r.value for r in readings), _oldest([r.timestamp for r in readings]))
