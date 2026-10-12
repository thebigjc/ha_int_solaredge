"""Data update coordinator for one SolarEdge site."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DeviceType,
    SiteData,
    SolarEdgeAuthenticationError,
    SolarEdgeClient,
    SolarEdgeCreditLimitError,
    SolarEdgeError,
    SolarEdgeForbiddenError,
    SolarEdgeRateLimitError,
)
from .const import (
    CONF_SITE_ID,
    DOMAIN,
    INVENTORY_UPDATE_INTERVAL,
    ISSUE_CREDIT_LIMIT,
    ISSUE_SCOPE_MISSING,
    LOGGER,
)
from .polling import PollingPlan, resolve_plan

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry

INVENTORY_TYPES = (DeviceType.INVERTER, DeviceType.METER, DeviceType.BATTERY)
CREDIT_LIMIT_RETRY = timedelta(hours=6)
POWER_WINDOW = timedelta(hours=1)


class SolarEdgeCoordinator(DataUpdateCoordinator[SiteData]):
    """Polls one site, spending as few monthly credits as the options allow."""

    config_entry: SolarEdgeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SolarEdgeConfigEntry, client: SolarEdgeClient
    ) -> None:
        """Read the polling options from the entry."""
        plan = resolve_plan(entry.options)
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_SITE_ID]}",
            update_interval=plan.tick,
        )
        self.client = client
        self.site_id = int(entry.data[CONF_SITE_ID])
        self.plan: PollingPlan = plan
        self._last_site: datetime | None = None
        self._last_telemetry: dict[DeviceType, datetime] = {}
        self._last_slow: datetime | None = None
        self._last_inventory: datetime | None = None
        self.scope_denied = False

    async def _async_setup(self) -> None:
        """Load site metadata and inventory once so entities can be created."""
        async with self._translate_errors():
            metadata = await self.client.async_get_site(self.site_id)
            devices = await self.client.async_get_devices(self.site_id, INVENTORY_TYPES)
        self.data = SiteData(metadata=metadata, devices=devices)
        self._last_inventory = dt_util.utcnow()

    async def _async_update_data(self) -> SiteData:
        """Fetch each data group whose own interval has elapsed."""
        data = self.data
        now = dt_util.utcnow()
        async with self._translate_errors():
            if self._due(self._last_inventory, INVENTORY_UPDATE_INTERVAL, now):
                data.devices = await self.client.async_get_devices(
                    self.site_id, INVENTORY_TYPES
                )
                self._last_inventory = now
                self._remove_stale_devices()
            if self._due(self._last_slow, timedelta(minutes=self.plan.slow_minutes), now):
                data.metadata = await self.client.async_get_site(self.site_id)
                if data.metadata.installation_date is not None:
                    data.lifetime_energy = await self.client.async_get_lifetime_energy(
                        self.site_id,
                        self._as_site_time(data.metadata.installation_date),
                        self._site_now(),
                    )
                data.open_alerts = await self.client.async_get_open_alerts(self.site_id)
                self._last_slow = now
            if self._due(self._last_site, timedelta(minutes=self.plan.site_minutes), now):
                data.overview = await self.client.async_get_overview(self.site_id)
                site_now = self._site_now()
                data.power = await self.client.async_get_power(
                    self.site_id, site_now - POWER_WINDOW, site_now
                )
                self._last_site = now
            if self.plan.any_telemetry and not self.scope_denied:
                await self._async_update_device_telemetry(data, now)
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_CREDIT_LIMIT))
        return data

    def _telemetry_due(self, kind: DeviceType, now: datetime) -> bool:
        """Whether this device family is enabled, present, and past its own interval."""
        plan = self.plan
        enabled, minutes = {
            DeviceType.INVERTER: (plan.inverters, plan.telemetry_minutes),
            DeviceType.METER: (plan.meters, plan.telemetry_minutes),
            DeviceType.BATTERY: (plan.storage, plan.storage_minutes),
        }[kind]
        return (
            enabled
            and any(d.type == kind and d.active for d in self.data.devices)
            and self._due(self._last_telemetry.get(kind), timedelta(minutes=minutes), now)
        )

    async def _async_update_device_telemetry(self, data: SiteData, now: datetime) -> None:
        end = self._site_now()
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            if self._telemetry_due(DeviceType.INVERTER, now):
                data.inverters = await self.client.async_get_inverter_telemetry(
                    self.site_id, start, end
                )
                self._last_telemetry[DeviceType.INVERTER] = now
            if self._telemetry_due(DeviceType.METER, now):
                data.meters = await self.client.async_get_meter_telemetry(
                    self.site_id, start, end
                )
                self._last_telemetry[DeviceType.METER] = now
            if self._telemetry_due(DeviceType.BATTERY, now):
                data.storage = await self.client.async_get_storage_telemetry(self.site_id)
                self._last_telemetry[DeviceType.BATTERY] = now
        except SolarEdgeForbiddenError as err:
            self.scope_denied = True
            LOGGER.warning("Device telemetry refused for site %s: %s", self.site_id, err)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id(ISSUE_SCOPE_MISSING),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SCOPE_MISSING,
                translation_placeholders={"site_id": str(self.site_id)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_SCOPE_MISSING))

    def _site_tz(self) -> tzinfo:
        zone = self.data.metadata.timezone if self.data else None
        return (dt_util.get_time_zone(zone) if zone else None) or dt_util.get_default_time_zone()

    def _site_now(self) -> datetime:
        """Current time at the site, offset-aware, as the API's from/to expect."""
        return dt_util.now(self._site_tz()).replace(microsecond=0)

    def _as_site_time(self, value: datetime) -> datetime:
        """Return value offset-aware, reading a naive one as site-local time."""
        return value if value.tzinfo else value.replace(tzinfo=self._site_tz())

    def _translate_errors(self) -> _ErrorTranslator:
        return _ErrorTranslator(self)

    def _issue_id(self, key: str) -> str:
        return f"{key}_{self.config_entry.entry_id}"

    def raise_credit_issue(self) -> None:
        """Tell the user the monthly credit quota is spent and what to change."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(ISSUE_CREDIT_LIMIT),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CREDIT_LIMIT,
            translation_placeholders={"site_id": str(self.site_id)},
        )

    def _remove_stale_devices(self) -> None:
        registry = dr.async_get(self.hass)
        current = {(DOMAIN, str(self.site_id))} | {
            (DOMAIN, d.serial_number) for d in self.data.devices
        }
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            if not device.identifiers & current:
                LOGGER.info("Removing device %s, no longer in the site inventory", device.name)
                registry.async_remove_device(device.id)

    @staticmethod
    def _due(last: datetime | None, interval: timedelta, now: datetime) -> bool:
        return last is None or now - last >= interval


class _ErrorTranslator:
    """Maps client and OAuth errors to the exceptions the coordinator contract expects."""

    def __init__(self, coordinator: SolarEdgeCoordinator) -> None:
        self._coordinator = coordinator

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> bool:
        if exc is None:
            return False
        if isinstance(exc, SolarEdgeAuthenticationError | OAuth2TokenRequestReauthError):
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from exc
        if isinstance(exc, SolarEdgeCreditLimitError):
            self._coordinator.raise_credit_issue()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="credit_limit",
                retry_after=CREDIT_LIMIT_RETRY.total_seconds(),
            ) from exc
        if isinstance(exc, SolarEdgeRateLimitError):
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                retry_after=float(exc.retry_after) if exc.retry_after else None,
            ) from exc
        if isinstance(exc, SolarEdgeError | OAuth2TokenRequestError | ClientError):
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(exc)},
            ) from exc
        return False
