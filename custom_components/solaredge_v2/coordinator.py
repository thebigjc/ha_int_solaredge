"""Data update coordinator for one SolarEdge site."""

from __future__ import annotations

from datetime import datetime, timedelta
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
    CONF_DEVICE_TELEMETRY,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SITE_ID,
    DEFAULT_DEVICE_TELEMETRY,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    INVENTORY_UPDATE_INTERVAL,
    ISSUE_CREDIT_LIMIT,
    ISSUE_SCOPE_MISSING,
    LOGGER,
    SLOW_UPDATE_INTERVAL,
)

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
        minutes = entry.options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES)
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_SITE_ID]}",
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.site_id = int(entry.data[CONF_SITE_ID])
        self.device_telemetry: bool = entry.options.get(
            CONF_DEVICE_TELEMETRY, DEFAULT_DEVICE_TELEMETRY
        )
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
        """Fetch the fast data every interval and the slow data when it is due."""
        data = self.data
        now = dt_util.utcnow()
        async with self._translate_errors():
            if self._due(self._last_inventory, INVENTORY_UPDATE_INTERVAL, now):
                data.devices = await self.client.async_get_devices(
                    self.site_id, INVENTORY_TYPES
                )
                self._last_inventory = now
                self._remove_stale_devices()
            if self._due(self._last_slow, SLOW_UPDATE_INTERVAL, now):
                data.metadata = await self.client.async_get_site(self.site_id)
                if data.metadata.installation_date is not None:
                    data.lifetime_energy = await self.client.async_get_lifetime_energy(
                        self.site_id,
                        data.metadata.installation_date.replace(tzinfo=None),
                        self._site_now(),
                    )
                data.open_alerts = await self.client.async_get_open_alerts(self.site_id)
                self._last_slow = now
            data.overview = await self.client.async_get_overview(self.site_id)
            site_now = self._site_now()
            data.power = await self.client.async_get_power(
                self.site_id, site_now - POWER_WINDOW, site_now
            )
            if self.device_telemetry and not self.scope_denied:
                await self._async_update_device_telemetry(data)
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_CREDIT_LIMIT))
        return data

    async def _async_update_device_telemetry(self, data: SiteData) -> None:
        types = {d.type for d in data.devices if d.active}
        end = self._site_now()
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            if DeviceType.INVERTER in types:
                data.inverters = await self.client.async_get_inverter_telemetry(
                    self.site_id, start, end
                )
            if DeviceType.METER in types:
                data.meters = await self.client.async_get_meter_telemetry(
                    self.site_id, start, end
                )
            if DeviceType.BATTERY in types:
                data.storage = await self.client.async_get_storage_telemetry(self.site_id)
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

    def _site_now(self) -> datetime:
        """Wall-clock time at the site, without an offset, as the API's from/to expect."""
        zone = self.data.metadata.timezone if self.data else None
        tz = (dt_util.get_time_zone(zone) if zone else None) or dt_util.get_default_time_zone()
        return dt_util.now(tz).replace(tzinfo=None, microsecond=0)

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
