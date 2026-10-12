"""Typed models for the SolarEdge Monitoring API v2 responses the integration reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

_POWER_TO_W = {"W": 1.0, "KW": 1_000.0, "MW": 1_000_000.0}
_ENERGY_TO_WH = {"WH": 1.0, "KWH": 1_000.0, "MWH": 1_000_000.0, "GWH": 1_000_000_000.0}


class DeviceType(StrEnum):
    """Device types reported by GET /sites/{id}/devices."""

    INVERTER = "INVERTER"
    BATTERY = "BATTERY"
    BATTERY_MODULE = "BATTERY_MODULE"
    OPTIMIZER = "OPTIMIZER"
    METER = "METER"
    EV_CHARGER = "EV_CHARGER"
    BUI = "BUI"
    GATEWAY = "GATEWAY"


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None for anything unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _block(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def to_watts(value: float | None, unit: str | None) -> float | None:
    """Convert a power value to watts; None when the unit is not a power unit."""
    if value is None or unit is None or (factor := _POWER_TO_W.get(unit.upper())) is None:
        return None
    return value * factor


def to_watt_hours(value: float | None, unit: str | None) -> float | None:
    """Convert an energy value to watt-hours; None when the unit is not an energy unit."""
    if value is None or unit is None or (factor := _ENERGY_TO_WH.get(unit.upper())) is None:
        return None
    return value * factor


@dataclass(frozen=True, slots=True)
class Sample:
    """One bucketed value; the timestamp marks the start of the bucket."""

    timestamp: datetime | None
    value: float | None


@dataclass(frozen=True, slots=True)
class Series:
    """A Measurements envelope: a unit and its samples in time order."""

    unit: str | None
    samples: tuple[Sample, ...] = ()

    @classmethod
    def from_api(cls, data: Any, unit: str | None = None) -> Series:
        """Build from a Measurements object; the meter and storage shapes carry no period."""
        if not isinstance(data, dict):
            return cls(unit=unit)
        raw = data.get("values")
        samples = tuple(
            Sample(parse_datetime(item.get("timestamp")), _float(item.get("value")))
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
        )
        return cls(unit=data.get("unit", unit), samples=samples)

    @property
    def latest_sample(self) -> Sample | None:
        """The most recent sample with a value; the newest bucket is often still null."""
        for sample in reversed(self.samples):
            if sample.value is not None:
                return sample
        return None

    @property
    def latest(self) -> float | None:
        """The most recent non-null value."""
        sample = self.latest_sample
        return sample.value if sample else None

    @property
    def total(self) -> float | None:
        """The sum of all non-null values, or None when there are none."""
        values = [s.value for s in self.samples if s.value is not None]
        return sum(values) if values else None

    @property
    def latest_watts(self) -> float | None:
        """The latest value converted to watts."""
        return to_watts(self.latest, self.unit)

    @property
    def latest_watt_hours(self) -> float | None:
        """The latest value converted to watt-hours."""
        return to_watt_hours(self.latest, self.unit)

    @property
    def total_watt_hours(self) -> float | None:
        """The summed value converted to watt-hours."""
        return to_watt_hours(self.total, self.unit)


@dataclass(frozen=True, slots=True)
class SiteSummary:
    """One entry of GET /sites (Fleet Access only)."""

    site_id: int
    name: str
    activation_status: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SiteSummary:
        """Build from a site list item."""
        return cls(
            site_id=int(data["siteId"]),
            name=str(data.get("name") or data["siteId"]),
            activation_status=data.get("activationStatus"),
        )


@dataclass(frozen=True, slots=True)
class SiteMetadata:
    """GET /sites/{id}."""

    site_id: int
    name: str
    peak_power_kw: float | None
    installation_date: datetime | None
    last_update_time: datetime | None
    activation_status: str | None
    timezone: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SiteMetadata:
        """Build from the site metadata body."""
        location = data.get("location")
        return cls(
            site_id=int(data["siteId"]),
            name=str(data.get("name") or data["siteId"]),
            peak_power_kw=_float(data.get("peakPower")),
            installation_date=parse_datetime(data.get("installationDate")),
            last_update_time=parse_datetime(data.get("lastUpdateTime")),
            activation_status=data.get("activationStatus"),
            timezone=location.get("timezone") if isinstance(location, dict) else None,
        )


@dataclass(frozen=True, slots=True)
class SiteOverview:
    """GET /sites/{id}/overview for a window, with every energy value in Wh."""

    production: float | None = None
    production_to_self_consumption: float | None = None
    production_to_storage: float | None = None
    production_to_grid: float | None = None
    consumption: float | None = None
    consumption_from_pv: float | None = None
    consumption_from_storage: float | None = None
    consumption_from_grid: float | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SiteOverview:
        """Build from the overview body, normalizing each block by its own unit."""
        prod = _block(data, "production")
        cons = _block(data, "consumption")

        def wh(block: dict[str, Any], key: str) -> float | None:
            return to_watt_hours(_float(block.get(key)), block.get("unit"))

        return cls(
            production=wh(prod, "total"),
            production_to_self_consumption=wh(prod, "toSelfConsumption"),
            production_to_storage=wh(prod, "toStorage"),
            production_to_grid=wh(prod, "toGrid"),
            consumption=wh(cons, "total"),
            consumption_from_pv=wh(cons, "fromPv"),
            consumption_from_storage=wh(cons, "fromStorage"),
            consumption_from_grid=wh(cons, "fromGrid"),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """One entry of GET /sites/{id}/devices."""

    type: str
    serial_number: str
    name: str | None
    manufacturer: str | None
    model: str | None
    part_number: str | None
    firmware_version: str | None
    connected_to: str | None
    active: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        """Build from an inventory item."""
        return cls(
            type=str(data.get("type", "")),
            serial_number=str(data["serialNumber"]),
            name=data.get("name"),
            manufacturer=data.get("manufacturer"),
            model=data.get("model"),
            part_number=data.get("partNumber"),
            firmware_version=data.get("firmwareVersion"),
            connected_to=data.get("connectedTo"),
            active=data.get("active") is not False,
        )


@dataclass(frozen=True, slots=True)
class Alert:
    """One entry of GET /sites/{id}/alerts."""

    alert_id: int
    type: str | None
    category: str | None
    impact: int | None
    status: str | None
    component_serial: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Alert:
        """Build from an alert item."""
        component = data.get("component")
        impact = data.get("impact")
        return cls(
            alert_id=int(data["alertId"]),
            type=data.get("type"),
            category=data.get("category"),
            impact=impact if isinstance(impact, int) and not isinstance(impact, bool) else None,
            status=data.get("status"),
            component_serial=(
                component.get("serialNumber") if isinstance(component, dict) else None
            ),
        )


type Telemetry = dict[str, dict[str, Series]]
"""Serial number to metric name (as the API spells it) to series."""


def parse_telemetry(data: Any, key: str) -> Telemetry:
    """Parse a bulk telemetry body keyed by `inverters`, `meters`, or `storage`."""
    devices = data.get(key) if isinstance(data, dict) else None
    result: Telemetry = {}
    if not isinstance(devices, dict):
        return result
    for serial, metrics in devices.items():
        if isinstance(metrics, dict):
            result[str(serial)] = {
                name: Series.from_api(series) for name, series in metrics.items()
            }
    return result


@dataclass(slots=True)
class SiteData:
    """Everything one coordinator refresh knows about a site."""

    metadata: SiteMetadata
    devices: list[Device] = field(default_factory=list)
    overview: SiteOverview | None = None
    power: Series | None = None
    lifetime_energy: float | None = None
    open_alerts: list[Alert] | None = None
    inverters: Telemetry = field(default_factory=dict)
    meters: Telemetry = field(default_factory=dict)
    storage: Telemetry = field(default_factory=dict)
