"""Tests for the in-tree V2 client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_v2.api import (
    BASE_URL,
    ApiKeyAuth,
    DeviceType,
    Series,
    SiteOverview,
    SolarEdgeAuthenticationError,
    SolarEdgeClient,
    SolarEdgeConnectionError,
    SolarEdgeCreditLimitError,
    SolarEdgeForbiddenError,
    SolarEdgeRateLimitError,
    SolarEdgeResponseError,
)
from custom_components.solaredge_v2.api.models import (
    Alert,
    parse_datetime,
    parse_telemetry,
    to_watt_hours,
    to_watts,
)

from .conftest import SITE, SITE_ID, MockApi

TZ = timezone(timedelta(hours=-5))
START = datetime(2026, 9, 29, tzinfo=TZ)
END = datetime(2026, 9, 29, 13, 5, tzinfo=TZ)


def _client(hass: HomeAssistant) -> SolarEdgeClient:
    return SolarEdgeClient(async_get_clientsession(hass), ApiKeyAuth("test-key"))


async def test_reads_every_endpoint(
    hass: HomeAssistant, mock_api: MockApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each call parses its fixture, sends the key as a header, and never in the URL."""
    client = _client(hass)
    sites = await client.async_get_sites()
    assert [s.site_id for s in sites] == [SITE_ID, 7654321]

    site = await client.async_get_site(SITE_ID)
    assert site.name == "Example Home"
    assert site.peak_power_kw == 7.6
    assert site.timezone == "America/Chicago"

    overview = await client.async_get_overview(SITE_ID)
    assert overview.production == pytest.approx(18010)
    assert overview.consumption_from_grid == 420

    power = await client.async_get_power(SITE_ID, START, END)
    assert power.latest_watts == pytest.approx(4310.7)

    lifetime = await client.async_get_lifetime_energy(SITE_ID, datetime(2021, 4, 1, tzinfo=TZ), END)
    assert lifetime == pytest.approx(41_500_000)

    devices = await client.async_get_devices(
        SITE_ID, [DeviceType.INVERTER, DeviceType.METER, DeviceType.BATTERY]
    )
    assert {d.type for d in devices} == {"INVERTER", "METER", "BATTERY"}

    alerts = await client.async_get_open_alerts(SITE_ID)
    assert alerts[0].component_serial == "7E1234B5-67"

    inverters = await client.async_get_inverter_telemetry(SITE_ID, START, END)
    assert inverters["7E1234B5-67"]["power"].latest_watts == pytest.approx(4310)
    meters = await client.async_get_meter_telemetry(SITE_ID, START, END)
    assert meters["M-PROD-001"]["productionEnergy"].total_watt_hours == pytest.approx(3120.5)
    storage = await client.async_get_storage_telemetry(SITE_ID)
    assert storage["BAT-1"]["remainingEnergy"].latest_watt_hours == pytest.approx(1614.2666)
    storage_call = next(
        c for c in aioclient_mock.mock_calls if str(c[1]).startswith(f"{SITE}/storage/telemetry")
    )
    assert dict(storage_call[1].query) == {}

    for _method, url, _data, headers in aioclient_mock.mock_calls:
        assert headers["X-API-Key"] == "test-key"
        assert "test-key" not in str(url)
    device_call = next(c for c in aioclient_mock.mock_calls if str(c[1]).startswith(f"{SITE}/devices"))
    assert device_call[1].query.getall("types") == ["INVERTER", "METER", "BATTERY"]
    energy_call = next(c for c in aioclient_mock.mock_calls if str(c[1]).startswith(f"{SITE}/energy"))
    assert energy_call[1].query["from"] == "2021-04-01T00:00:00-05:00"
    assert energy_call[1].query["resolution"] == "YEAR"
    power_call = next(c for c in aioclient_mock.mock_calls if str(c[1]).startswith(f"{SITE}/power"))
    assert dict(power_call[1].query) == {
        "resolution": "QUARTER_HOUR",
        "unit": "W",
        "from": "2026-09-29T00:00:00-05:00",
        "to": "2026-09-29T13:05:00-05:00",
    }
    inverter_call = next(c for c in aioclient_mock.mock_calls if "/inverters/" in str(c[1]))
    assert inverter_call[1].query["resolution"] == "HOUR"


async def test_site_list_pages(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full page asks for the next one."""
    monkeypatch.setattr("custom_components.solaredge_v2.api.client.SITES_PAGE_SIZE", 1)
    aioclient_mock.get(
        f"{BASE_URL}/sites?page=1",
        json={"sites": {"count": 2, "site": [{"siteId": 1, "name": "A"}]}},
    )
    aioclient_mock.get(
        f"{BASE_URL}/sites?page=2",
        json={"sites": {"count": 2, "site": [{"siteId": 2}]}},
    )
    aioclient_mock.get(f"{BASE_URL}/sites?page=3", json={"sites": {"count": 2, "site": []}})
    sites = await _client(hass).async_get_sites()
    assert [(s.site_id, s.name) for s in sites] == [(1, "A"), (2, "2")]


async def test_site_list_unexpected_shape(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A body without the sites block is an empty list."""
    aioclient_mock.get(f"{BASE_URL}/sites", json={"unexpected": True})
    assert await _client(hass).async_get_sites() == []


@pytest.mark.parametrize(
    ("status", "headers", "exc"),
    [
        (401, {}, SolarEdgeAuthenticationError),
        (403, {}, SolarEdgeForbiddenError),
        (429, {"x-ratelimit-remaining-minute": "0", "retry-after": "30"}, SolarEdgeRateLimitError),
        (429, {"x-ratelimit-remaining-minute": "7"}, SolarEdgeCreditLimitError),
        (429, {}, SolarEdgeRateLimitError),
        (500, {}, SolarEdgeResponseError),
    ],
)
async def test_error_mapping(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    headers: dict[str, str],
    exc: type[Exception],
) -> None:
    """Status codes and rate-limit headers map to distinct exceptions."""
    aioclient_mock.get(SITE, status=status, headers=headers, json={"detail": "nope"})
    with pytest.raises(exc) as info:
        await _client(hass).async_get_site(SITE_ID)
    assert isinstance(info.value, SolarEdgeResponseError)
    assert info.value.detail == "nope"
    if isinstance(info.value, SolarEdgeRateLimitError):
        assert info.value.retry_after == (30 if "retry-after" in headers else None)


async def test_error_without_json_body(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A non-JSON error body falls back to the reason phrase."""
    aioclient_mock.get(SITE, status=502, text="<html>bad gateway</html>")
    with pytest.raises(SolarEdgeResponseError) as info:
        await _client(hass).async_get_site(SITE_ID)
    assert info.value.status == 502


@pytest.mark.parametrize("exc", [TimeoutError, ClientError])
async def test_transport_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: type[Exception]) -> None:
    """Timeouts and aiohttp errors become connection errors."""
    aioclient_mock.get(SITE, exc=exc)
    with pytest.raises(SolarEdgeConnectionError):
        await _client(hass).async_get_site(SITE_ID)


async def test_invalid_json(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A 200 with a body that is not JSON is a response error."""
    aioclient_mock.get(SITE, text="not json")
    with pytest.raises(SolarEdgeResponseError):
        await _client(hass).async_get_site(SITE_ID)


async def test_site_without_id(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Metadata must carry siteId."""
    aioclient_mock.get(SITE, json={"name": "x"})
    with pytest.raises(SolarEdgeResponseError):
        await _client(hass).async_get_site(SITE_ID)


async def test_unexpected_shapes_are_empty(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Lists and maps of the wrong type produce empty results rather than errors."""
    for path in ("overview", "devices", "alerts", "inverters/telemetry", "power"):
        aioclient_mock.get(f"{SITE}/{path}", json="unexpected")
    client = _client(hass)
    assert await client.async_get_overview(SITE_ID) == SiteOverview()
    assert await client.async_get_devices(SITE_ID, [DeviceType.INVERTER]) == []
    assert await client.async_get_open_alerts(SITE_ID) == []
    assert await client.async_get_inverter_telemetry(SITE_ID, START, END) == {}
    assert (await client.async_get_power(SITE_ID, START, END)).latest is None


def test_units_and_parsing() -> None:
    """Unit conversion, timestamp parsing, and defensive parsing of odd values."""
    assert to_watts(2, "kw") == 2000
    assert to_watts(2, "WH") is None
    assert to_watts(None, "W") is None
    assert to_watt_hours(1, "GWH") == 1e9
    assert to_watt_hours(1, None) is None
    assert parse_datetime("2026-01-01T00:00:00Z") == datetime.fromisoformat("2026-01-01T00:00:00+00:00")
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None
    series = Series.from_api({"unit": "W", "values": [{"value": True}, {"value": "x"}, "bad"]})
    assert series.latest is None
    assert series.total is None
    assert Series.from_api(None, unit="W").unit == "W"
    assert parse_telemetry({"inverters": {"A": "bad", "B": {}}}, "inverters") == {"B": {}}
    alert = Alert.from_api({"alertId": 1, "impact": True, "component": "x"})
    assert alert.impact is None
    assert alert.component_serial is None


async def test_nonstandard_status(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A status code outside the HTTP registry still produces a detail."""
    aioclient_mock.get(SITE, status=599, text="")
    with pytest.raises(SolarEdgeResponseError) as info:
        await _client(hass).async_get_site(SITE_ID)
    assert info.value.detail == "error"


async def test_positive_offset_is_percent_encoded(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A '+' in a positive offset reaches the server as %2B, never as a space."""
    aioclient_mock.get(re.compile(rf"{re.escape(SITE)}/power"), json={})
    plus2 = timezone(timedelta(hours=2))
    await _client(hass).async_get_power(
        SITE_ID, datetime(2026, 9, 29, 9, tzinfo=plus2), datetime(2026, 9, 29, 10, tzinfo=plus2)
    )
    url = aioclient_mock.mock_calls[0][1]
    assert url.query["from"] == "2026-09-29T09:00:00+02:00"
    assert "%2B02:00" in url.raw_query_string or "%2B02%3A00" in url.raw_query_string
    assert "+02" not in url.raw_query_string.replace("%2B", "")


async def test_naive_window_is_rejected(hass: HomeAssistant) -> None:
    """Naive datetimes would be sent without an offset, which the API rejects."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await _client(hass).async_get_power(SITE_ID, datetime(2026, 9, 29), END)
    with pytest.raises(ValueError, match="timezone-aware"):
        await _client(hass).async_get_lifetime_energy(SITE_ID, START, datetime(2026, 9, 29))
