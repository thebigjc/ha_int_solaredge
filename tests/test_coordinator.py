"""Tests for polling cadence, error handling, repair issues, and stale devices."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_v2.const import (
    CONF_DEVICE_TELEMETRY,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_STORAGE_INTERVAL_MINUTES,
    CONF_TELEMETRY_INTERVAL_MINUTES,
    CONF_TELEMETRY_INVERTERS,
    CONF_TELEMETRY_METERS,
    CONF_TELEMETRY_STORAGE,
    DOMAIN,
    ISSUE_CREDIT_LIMIT,
    ISSUE_SCOPE_MISSING,
)

from .conftest import SITE, MockApi, load

POWER = "sensor.example_home_current_power"


def _calls(aioclient_mock: AiohttpClientMocker, suffix: str) -> int:
    return sum(1 for c in aioclient_mock.mock_calls if str(c[1]).split("?")[0] == f"{SITE}{suffix}")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_credit_budget_cadence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    api_key_entry: MockConfigEntry,
) -> None:
    """Fast data every interval, slow data every six hours, inventory every twelve."""
    await _setup(hass, api_key_entry)
    assert _calls(aioclient_mock, "") == 2
    assert _calls(aioclient_mock, "/devices") == 1
    assert _calls(aioclient_mock, "/overview") == 1
    for _ in range(5):
        await _tick(hass, freezer, 60)
    assert _calls(aioclient_mock, "/overview") == 6
    assert _calls(aioclient_mock, "/alerts") == 1
    await _tick(hass, freezer, 60)
    assert _calls(aioclient_mock, "/alerts") == 2
    assert _calls(aioclient_mock, "/energy") == 2
    assert _calls(aioclient_mock, "/devices") == 1
    for _ in range(6):
        await _tick(hass, freezer, 60)
    assert _calls(aioclient_mock, "/devices") == 2


async def test_telemetry_off_skips_device_calls(
    hass: HomeAssistant, mock_api: MockApi, aioclient_mock: AiohttpClientMocker, api_key_entry: MockConfigEntry
) -> None:
    """With telemetry off, only site endpoints are called and no device sensors exist."""
    hass.config_entries.async_update_entry(
        api_key_entry, options={CONF_DEVICE_TELEMETRY: False, CONF_SCAN_INTERVAL_MINUTES: 30}
    )
    await _setup(hass, api_key_entry)
    assert _calls(aioclient_mock, "/inverters/telemetry") == 0
    assert hass.states.get("sensor.inverter_1_ac_power") is None
    assert api_key_entry.runtime_data.update_interval == timedelta(minutes=30)


async def test_credit_limit_raises_issue(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
) -> None:
    """Exhausted credits create a repair issue that clears after the next success."""
    await _setup(hass, api_key_entry)
    issue_id = f"{ISSUE_CREDIT_LIMIT}_{api_key_entry.entry_id}"
    mock_api(headers={"x-ratelimit-remaining-minute": "3"}, overview=429)
    await _tick(hass, freezer, 60)
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    mock_api()
    await _tick(hass, freezer, 6 * 60)
    assert hass.states.get(POWER).state == "4310.7"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_rate_limit_and_transport_errors(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
) -> None:
    """Per-minute limits and server errors make entities unavailable until recovery."""
    await _setup(hass, api_key_entry)
    mock_api(headers={"x-ratelimit-remaining-minute": "0", "retry-after": "60"}, overview=429)
    await _tick(hass, freezer, 60)
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    mock_api(headers={"x-ratelimit-remaining-minute": "0"}, overview=429)
    await _tick(hass, freezer, 60)
    mock_api(power=500)
    await _tick(hass, freezer, 60)
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    mock_api()
    await _tick(hass, freezer, 60)
    assert hass.states.get(POWER).state == "4310.7"


async def test_auth_failure_during_update_starts_reauth(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """A 401 after setup starts a reauth flow."""
    await _setup(hass, api_key_entry)
    mock_api(overview=401)
    await _tick(hass, freezer, 60)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_missing_scope_raises_issue(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    api_key_entry: MockConfigEntry,
) -> None:
    """A 403 on telemetry stops device calls and raises a scope issue; site data continues."""
    mock_api(inverters=403)
    await _setup(hass, api_key_entry)
    assert api_key_entry.state is ConfigEntryState.LOADED
    issue_id = f"{ISSUE_SCOPE_MISSING}_{api_key_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await _tick(hass, freezer, 60)
    assert _calls(aioclient_mock, "/inverters/telemetry") == 1
    assert hass.states.get(POWER).state == "4310.7"


async def test_scope_issue_clears(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """A successful telemetry read deletes a stale scope issue."""
    issue_id = f"{ISSUE_SCOPE_MISSING}_{api_key_entry.entry_id}"
    ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_SCOPE_MISSING)
    await _setup(hass, api_key_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_dynamic_and_stale_devices(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
) -> None:
    """A new inverter gains entities; a removed battery leaves the registry."""
    await _setup(hass, api_key_entry)
    devices = load("devices.json")
    devices = [d for d in devices if d["type"] != "BATTERY"] + [
        {**devices[0], "serialNumber": "7E9999B9-99", "name": "Inverter 2"}
    ]
    mock_api(devices=devices)
    await _tick(hass, freezer, 12 * 60)
    registry = dr.async_get(hass)
    assert registry.async_get_device_by_identifier((DOMAIN, "BAT-1"), api_key_entry.entry_id) is None
    assert registry.async_get_device_by_identifier((DOMAIN, "7E9999B9-99"), api_key_entry.entry_id) is not None
    assert hass.states.get("sensor.inverter_2_ac_power") is not None


async def test_site_without_installation_date(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """Without an installation date the lifetime call is skipped."""
    site = load("site.json")
    del site["installationDate"]
    mock_api(site=site)
    await _setup(hass, api_key_entry)
    assert hass.states.get("sensor.example_home_lifetime_production").state == "unknown"


async def test_inverter_only_site(
    hass: HomeAssistant, mock_api: MockApi, aioclient_mock: AiohttpClientMocker, api_key_entry: MockConfigEntry
) -> None:
    """Meter and storage telemetry are not requested when the site has neither."""
    mock_api(devices=[load("devices.json")[0]])
    await _setup(hass, api_key_entry)
    assert _calls(aioclient_mock, "/inverters/telemetry") == 1
    assert _calls(aioclient_mock, "/meters/telemetry") == 0
    assert _calls(aioclient_mock, "/storage/telemetry") == 0


async def test_unrelated_errors_propagate(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """The error translator leaves exceptions it does not own alone."""
    await _setup(hass, api_key_entry)
    with pytest.raises(KeyError):
        async with api_key_entry.runtime_data._translate_errors():
            raise KeyError("x")


async def test_battery_only_site(
    hass: HomeAssistant, mock_api: MockApi, aioclient_mock: AiohttpClientMocker, api_key_entry: MockConfigEntry
) -> None:
    """A site whose inventory lists only a battery reads only storage telemetry."""
    mock_api(devices=[load("devices.json")[2]])
    await _setup(hass, api_key_entry)
    assert _calls(aioclient_mock, "/inverters/telemetry") == 0
    assert _calls(aioclient_mock, "/storage/telemetry") == 1


async def test_windows_use_site_local_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    api_key_entry: MockConfigEntry,
) -> None:
    """Windows are sent as site wall-clock time and stay inside the API span limits."""
    freezer.move_to("2026-09-29T18:05:00+00:00")
    await _setup(hass, api_key_entry)

    def query(suffix: str) -> dict[str, str]:
        call = next(c for c in aioclient_mock.mock_calls if str(c[1]).split("?")[0] == f"{SITE}{suffix}")
        return dict(call[1].query)

    assert query("/power")["from"] == "2026-09-29T12:05:00-05:00"
    assert query("/power")["to"] == "2026-09-29T13:05:00-05:00"
    assert query("/inverters/telemetry")["from"] == "2026-09-29T00:00:00-05:00"
    assert query("/inverters/telemetry")["to"] == "2026-09-29T13:05:00-05:00"
    assert query("/energy")["from"] == "2021-04-01T00:00:00-05:00"


BATTERY_ONLY = {
    CONF_SCAN_INTERVAL_MINUTES: 720,
    CONF_TELEMETRY_INVERTERS: False,
    CONF_TELEMETRY_METERS: False,
    CONF_TELEMETRY_STORAGE: True,
    CONF_STORAGE_INTERVAL_MINUTES: 30,
}


async def test_battery_only_polls_only_due_groups(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    api_key_entry: MockConfigEntry,
) -> None:
    """Storage every 30 minutes, site every 12 hours, inverters and meters never."""
    hass.config_entries.async_update_entry(api_key_entry, options=BATTERY_ONLY)
    await _setup(hass, api_key_entry)
    assert api_key_entry.runtime_data.update_interval == timedelta(minutes=30)
    soe = "sensor.example_home_battery_state_of_energy"
    assert hass.states.get(soe).state != STATE_UNAVAILABLE
    assert hass.states.get("sensor.inverter_1_ac_power") is None
    for _ in range(24):
        await _tick(hass, freezer, 30)
    assert _calls(aioclient_mock, "/storage/telemetry") == 25  # setup plus 24 ticks
    assert _calls(aioclient_mock, "/overview") == 2
    assert _calls(aioclient_mock, "/power") == 2
    assert _calls(aioclient_mock, "/alerts") == 2
    assert _calls(aioclient_mock, "/devices") == 2
    assert _calls(aioclient_mock, "/inverters/telemetry") == 0
    assert _calls(aioclient_mock, "/meters/telemetry") == 0


async def test_values_persist_between_group_polls(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
) -> None:
    """Site sensors keep their value while only storage is polled, and stay available."""
    hass.config_entries.async_update_entry(api_key_entry, options=BATTERY_ONLY)
    await _setup(hass, api_key_entry)
    assert hass.states.get(POWER).state == "4310.7"
    mock_api(overview=500, power=500)  # would fail if the site group were called
    await _tick(hass, freezer, 30)
    await _tick(hass, freezer, 30)
    assert hass.states.get(POWER).state == "4310.7"


async def test_telemetry_family_with_own_interval(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    api_key_entry: MockConfigEntry,
) -> None:
    """Meters follow a 60 minute telemetry-interval while storage runs every 30."""
    hass.config_entries.async_update_entry(
        api_key_entry,
        options={
            CONF_SCAN_INTERVAL_MINUTES: 120,
            CONF_TELEMETRY_METERS: True,
            CONF_TELEMETRY_INTERVAL_MINUTES: 60,
            CONF_TELEMETRY_STORAGE: True,
            CONF_STORAGE_INTERVAL_MINUTES: 30,
        },
    )
    await _setup(hass, api_key_entry)
    for _ in range(4):
        await _tick(hass, freezer, 30)
    assert _calls(aioclient_mock, "/meters/telemetry") == 3
    assert _calls(aioclient_mock, "/storage/telemetry") == 5
    assert _calls(aioclient_mock, "/overview") == 2


async def test_credit_limit_with_multiple_groups(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
) -> None:
    """A credit refusal on the storage group raises the issue and recovers."""
    hass.config_entries.async_update_entry(api_key_entry, options=BATTERY_ONLY)
    await _setup(hass, api_key_entry)
    issue_id = f"{ISSUE_CREDIT_LIMIT}_{api_key_entry.entry_id}"
    mock_api(headers={"x-ratelimit-remaining-minute": "3"}, storage=429)
    await _tick(hass, freezer, 30)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    mock_api()
    await _tick(hass, freezer, 6 * 60)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get("sensor.example_home_battery_state_of_energy").state != STATE_UNAVAILABLE


async def test_legacy_options_behave_as_before(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    api_key_entry: MockConfigEntry,
) -> None:
    """Old device_telemetry true polls all three families on every site tick."""
    await _setup(hass, api_key_entry)
    for _ in range(2):
        await _tick(hass, freezer, 60)
    for suffix in ("/overview", "/inverters/telemetry", "/meters/telemetry", "/storage/telemetry"):
        assert _calls(aioclient_mock, suffix) == 3, suffix
