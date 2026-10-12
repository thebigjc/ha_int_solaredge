"""The SolarEdge Monitoring API v2 integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_entry_oauth2_flow,
    config_validation as cv,
    device_registry as dr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ApiKeyAuth, AuthProvider, BearerTokenAuth, SolarEdgeClient
from .const import AUTH_TYPE_OAUTH, CONF_AUTH_TYPE, CONF_SITE_ID, DOMAIN, MANUFACTURER
from .coordinator import SolarEdgeCoordinator
from .oauth import async_pending

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SolarEdgeConfigEntry = ConfigEntry[SolarEdgeCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the OAuth callback so a late or stray redirect gets a clear answer."""
    async_pending(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Set up one SolarEdge site."""
    auth = await _async_auth_provider(hass, entry)
    client = SolarEdgeClient(async_get_clientsession(hass), auth)
    coordinator = SolarEdgeCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    metadata = coordinator.data.metadata
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(entry.data[CONF_SITE_ID]))},
        manufacturer=MANUFACTURER,
        name=metadata.name,
        model="Monitoring site",
        entry_type=dr.DeviceEntryType.SERVICE,
    )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a device only when the site inventory no longer lists it."""
    known = {(DOMAIN, str(entry.data[CONF_SITE_ID]))} | {
        (DOMAIN, d.serial_number) for d in entry.runtime_data.data.devices
    }
    return not device.identifiers & known


async def _async_auth_provider(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry
) -> AuthProvider:
    if entry.data.get(CONF_AUTH_TYPE) != AUTH_TYPE_OAUTH:
        return ApiKeyAuth(entry.data[CONF_API_KEY])
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def token() -> str:
        await session.async_ensure_token_valid()
        return str(session.token["access_token"])

    return BearerTokenAuth(token)
