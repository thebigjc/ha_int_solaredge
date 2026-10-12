"""Tests for sensor values and metadata."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.solaredge_v2.const import DOMAIN

from .conftest import MockApi


@pytest.fixture(autouse=True)
def enable_all() -> Any:
    """Create disabled-by-default entities too."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.mark.parametrize(
    ("entity_id", "state", "unit"),
    [
        ("sensor.example_home_current_power", "4310.7", "W"),
        ("sensor.example_home_production_today", "18.01", "kWh"),
        ("sensor.example_home_consumption_today", "20.41", "kWh"),
        ("sensor.example_home_exported_to_grid_today", "0.07", "kWh"),
        ("sensor.example_home_imported_from_grid_today", "0.42", "kWh"),
        ("sensor.example_home_self_consumed_today", "9.98", "kWh"),
        ("sensor.example_home_charged_to_storage_today", "7.96", "kWh"),
        ("sensor.example_home_consumed_from_storage_today", "10.01", "kWh"),
        ("sensor.example_home_lifetime_production", "41500.0", "kWh"),
        ("sensor.example_home_peak_power", "7.6", "kW"),
        ("sensor.example_home_open_alerts", "1", None),
        ("sensor.inverter_1_ac_power", "4310.0", "W"),
        ("sensor.inverter_1_energy_today", "2.1403", "kWh"),
        ("sensor.inverter_1_ac_voltage", "241.2", "V"),
        ("sensor.inverter_1_ac_current", "17.9", "A"),
        ("sensor.inverter_1_grid_frequency", "60.01", "Hz"),
        ("sensor.production_meter_production_power", "6480.0", "W"),
        ("sensor.production_meter_production_today", "3.1205", "kWh"),
        ("sensor.production_meter_consumption_power", "unknown", "W"),
        ("sensor.battery_1_charge_power", "666.0", "W"),
        ("sensor.battery_1_discharge_power", "0.0", "W"),
    ],
)
async def test_sensor_values(
    hass: HomeAssistant,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
    entity_id: str,
    state: str,
    unit: str | None,
) -> None:
    """Values are normalized from whatever unit the API returned."""
    await hass.config_entries.async_setup(api_key_entry.entry_id)
    await hass.async_block_till_done()
    current = hass.states.get(entity_id)
    assert current is not None, entity_id
    assert current.state == state
    assert current.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit


async def test_last_update_and_unique_ids(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """Timestamps parse and unique IDs are site or serial scoped."""
    await hass.config_entries.async_setup(api_key_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_home_last_update_from_site").state == "2026-09-29T17:45:00+00:00"
    registry = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, api_key_entry.entry_id)}
    assert "1234567_current_power" in unique_ids
    assert "7E1234B5-67_inverter_power" in unique_ids
    assert all(e.platform == DOMAIN for e in er.async_entries_for_config_entry(registry, api_key_entry.entry_id))


async def test_empty_overview(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """Missing blocks and empty series read as unknown, not zero."""
    mock_api(overview={}, power={"unit": "W", "values": []}, alerts=[], inverters={"inverters": {}})
    await hass.config_entries.async_setup(api_key_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_home_production_today").state == "unknown"
    assert hass.states.get("sensor.example_home_current_power").state == "unknown"
    assert hass.states.get("sensor.example_home_open_alerts").state == "0"
    assert hass.states.get("sensor.inverter_1_ac_power").state == "unknown"


LATEST = datetime(2026, 10, 12, 8, 45, tzinfo=timezone(timedelta(hours=-4)))


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("entity_id", "value", "unit"),
    [
        ("sensor.example_home_battery_state_of_energy", 18.598, "%"),
        ("sensor.example_home_battery_power", 1332.6667, "W"),
        ("sensor.battery_1_state_of_energy", 17.699128, "%"),
        ("sensor.battery_2_state_of_energy", 19.509332, "%"),
        ("sensor.battery_1_remaining_energy", 1.6142666, "kWh"),
        ("sensor.battery_2_remaining_energy", 1.7563666, "kWh"),
        ("sensor.battery_1_power", 666.0, "W"),
        ("sensor.battery_2_power", 666.6667, "W"),
    ],
)
async def test_battery_sensors_from_real_response(
    hass: HomeAssistant,
    mock_api: MockApi,
    api_key_entry: MockConfigEntry,
    entity_id: str,
    value: float,
    unit: str,
) -> None:
    """The in-progress null bucket is skipped, the fraction is shown as percent, and the
    site state of energy is capacity weighted."""
    await _setup(hass, api_key_entry)
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    assert float(state.state) == pytest.approx(value, abs=0.005 if unit != "kWh" else 1e-6)
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == unit


@pytest.mark.parametrize(
    "entity_id",
    [
        "sensor.example_home_battery_state_of_energy",
        "sensor.example_home_battery_power",
        "sensor.battery_1_state_of_energy",
        "sensor.battery_2_remaining_energy",
        "sensor.battery_2_power",
    ],
)
async def test_battery_sensors_expose_data_timestamp(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry, entity_id: str
) -> None:
    """The bucket time of the latest value is exposed as text and as epoch seconds."""
    await _setup(hass, api_key_entry)
    attributes = hass.states.get(entity_id).attributes
    assert attributes["data_timestamp"] == LATEST.isoformat()
    assert attributes["data_timestamp_unix"] == int(LATEST.timestamp())
    assert isinstance(attributes["data_timestamp_unix"], int)


async def test_battery_without_data_has_no_entities(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """BAT-3 reports only an empty series: no device, no entities."""
    await _setup(hass, api_key_entry)
    assert hass.states.get("sensor.battery_3_state_of_energy") is None
    assert hass.states.get("sensor.battery_3_power") is None
    assert dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "BAT-3"), api_key_entry.entry_id) is None
    assert dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "BAT-1"), api_key_entry.entry_id) is not None


async def test_no_battery_data_creates_no_battery_entities(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """With no battery data there are no site battery sensors either, not zeroes."""
    mock_api(storage={"storage": {"BAT-3": {"acGridChargingEnergy": {"unit": "WH", "values": []}}}})
    await _setup(hass, api_key_entry)
    assert hass.states.get("sensor.example_home_battery_state_of_energy") is None
    assert hass.states.get("sensor.example_home_battery_power") is None


async def test_battery_entities_appear_once_data_arrives(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """A battery that starts reporting later gains its entities on the next update."""
    mock_api(storage={"storage": {"BAT-1": {"acGridChargingEnergy": {"unit": "WH", "values": []}}}})
    await _setup(hass, api_key_entry)
    assert hass.states.get("sensor.battery_1_state_of_energy") is None
    mock_api()
    freezer.tick(timedelta(hours=1, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.battery_1_state_of_energy") is not None


async def test_unparseable_bucket_time_omits_attributes(
    hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry
) -> None:
    """A value whose bucket time cannot be parsed is shown without timestamp attributes."""
    mock_api(storage={"storage": {"BAT-1": {"stateOfEnergy": {"unit": "PERCENTAGE", "values": [{"timestamp": "bad", "value": 0.5}]}}}})
    await _setup(hass, api_key_entry)
    state = hass.states.get("sensor.battery_1_state_of_energy")
    assert float(state.state) == 50.0
    assert "data_timestamp" not in state.attributes
