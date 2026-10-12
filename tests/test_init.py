"""Tests for setup, unload, device removal, and diagnostics."""

from __future__ import annotations

import time

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.solaredge_v2.const import DOMAIN, OAUTH2_TOKEN
from custom_components.solaredge_v2.diagnostics import async_get_config_entry_diagnostics

from .conftest import SITE, SITE_ID, MockApi


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_unload(hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry) -> None:
    """The site and its three devices register, and unload is clean."""
    await _setup(hass, api_key_entry)
    assert api_key_entry.state is ConfigEntryState.LOADED
    registry = dr.async_get(hass)
    site = registry.async_get_device_by_identifier((DOMAIN, str(SITE_ID)), api_key_entry.entry_id)
    assert site is not None
    assert site.name == "Example Home"
    assert site.entry_type is dr.DeviceEntryType.SERVICE
    inverter = registry.async_get_device_by_identifier((DOMAIN, "7E1234B5-67"), api_key_entry.entry_id)
    assert inverter is not None
    assert inverter.via_device_id == site.id
    assert inverter.model == "SE7600H-US"
    assert inverter.sw_version == "4.21.30"
    assert await hass.config_entries.async_unload(api_key_entry.entry_id)
    assert api_key_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(("override", "state"), [({"site": 500}, ConfigEntryState.SETUP_RETRY), ({"site": 401}, ConfigEntryState.SETUP_ERROR)])
async def test_setup_failures(
    hass: HomeAssistant,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
    override: dict[str, int],
    state: ConfigEntryState,
) -> None:
    """Transient errors retry; rejected credentials start reauth."""
    mock_api(**override)
    await _setup(hass, api_key_entry)
    assert api_key_entry.state is state
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows) is (state is ConfigEntryState.SETUP_ERROR)


async def test_oauth_setup_refreshes_token(
    hass: HomeAssistant,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    oauth_entry: MockConfigEntry,
) -> None:
    """An expired token is refreshed with a JSON body before the first call."""
    hass.config_entries.async_update_entry(
        oauth_entry, data={**oauth_entry.data, "token": {**oauth_entry.data["token"], "expires_at": time.time() - 10}}
    )
    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={"access_token": "fresh", "refresh_token": "r2", "token_type": "Bearer", "expires_in": 7200},
    )
    await _setup(hass, oauth_entry)
    assert oauth_entry.state is ConfigEntryState.LOADED
    refresh = next(c for c in aioclient_mock.mock_calls if c[0] == "POST")
    assert refresh[2] == {
        "grant_type": "refresh_token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh",
    }
    assert oauth_entry.data["token"]["access_token"] == "fresh"
    assert oauth_entry.data["token"]["refresh_token"] == "r2"
    site_call = next(c for c in aioclient_mock.mock_calls if str(c[1]) == SITE)
    assert site_call[3]["Authorization"] == "Bearer fresh"


@pytest.mark.parametrize(("status", "state"), [(400, ConfigEntryState.SETUP_ERROR), (503, ConfigEntryState.SETUP_RETRY)])
async def test_oauth_refresh_failure(
    hass: HomeAssistant,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    oauth_entry: MockConfigEntry,
    status: int,
    state: ConfigEntryState,
) -> None:
    """A rejected refresh token needs reauth; a server error retries."""
    hass.config_entries.async_update_entry(
        oauth_entry, data={**oauth_entry.data, "token": {**oauth_entry.data["token"], "expires_at": time.time() - 10}}
    )
    aioclient_mock.post(OAUTH2_TOKEN, status=status, json={"error": "invalid_grant"})
    await _setup(hass, oauth_entry)
    assert oauth_entry.state is state


async def test_oauth_implementation_missing(hass: HomeAssistant, mock_api: MockApi) -> None:
    """An entry whose application credential was deleted asks for reauth."""
    assert await async_setup_component(hass, "application_credentials", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(SITE_ID),
        data={"auth_type": "oauth", "site_id": SITE_ID, "auth_implementation": "gone", "token": {}},
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_remove_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
) -> None:
    """Only devices absent from the inventory can be removed by the user."""
    assert await async_setup_component(hass, "config", {})
    await _setup(hass, api_key_entry)
    registry = dr.async_get(hass)
    inverter = registry.async_get_device_by_identifier((DOMAIN, "7E1234B5-67"), api_key_entry.entry_id)
    old = registry.async_get_or_create(
        config_entry_id=api_key_entry.entry_id, identifiers={(DOMAIN, "OLD-SERIAL")}
    )
    client = await hass_ws_client(hass)
    for device, allowed in ((inverter, False), (old, True)):
        assert device is not None
        response = await client.remove_device(device.id)
        assert response["success"] is allowed


async def test_diagnostics(hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry) -> None:
    """Diagnostics redact the key and include the parsed data."""
    await _setup(hass, api_key_entry)
    diag = await async_get_config_entry_diagnostics(hass, api_key_entry)
    assert diag["entry"]["data"]["api_key"] == REDACTED
    assert diag["data"]["metadata"]["name"] == "Example Home"
    assert diag["device_telemetry_denied"] is False
