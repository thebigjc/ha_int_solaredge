"""Async client for the SolarEdge Monitoring API v2 (Basic Monitoring)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from http import HTTPStatus
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .auth import AuthProvider
from .exceptions import (
    SolarEdgeAuthenticationError,
    SolarEdgeConnectionError,
    SolarEdgeCreditLimitError,
    SolarEdgeForbiddenError,
    SolarEdgeRateLimitError,
    SolarEdgeResponseError,
)
from .models import (
    Alert,
    Device,
    DeviceType,
    Series,
    SiteMetadata,
    SiteOverview,
    SiteSummary,
    Telemetry,
    parse_telemetry,
)

BASE_URL = "https://monitoringapi.solaredge.com/v2"
DEFAULT_TIMEOUT = 30.0
SITES_PAGE_SIZE = 1000


class SolarEdgeClient:
    """One authenticated view of the V2 API; every call costs one credit."""

    def __init__(
        self,
        session: ClientSession,
        auth: AuthProvider,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Use the caller's session; the client never creates or closes one."""
        self._session = session
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _request(
        self, path: str, params: Sequence[tuple[str, str]] | None = None
    ) -> Any:
        headers = {"Accept": "application/json", **await self._auth.async_get_headers()}
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._session.get(
                    f"{self._base_url}{path}", params=params or (), headers=headers
                )
                async with response:
                    if response.status >= 400:
                        raise await _error_for(response)
                    return await response.json(content_type=None)
        except TimeoutError as err:
            raise SolarEdgeConnectionError(f"Timeout calling {path}") from err
        except ClientError as err:
            raise SolarEdgeConnectionError(f"Error calling {path}: {err}") from err
        except ValueError as err:
            raise SolarEdgeResponseError(200, f"Invalid JSON from {path}") from err

    async def async_get_sites(self) -> list[SiteSummary]:
        """GET /sites, every page. Fleet Access only; OAuth tokens are rejected."""
        sites: list[SiteSummary] = []
        page = 1
        while True:
            body = await self._request(
                "/sites", [("page", str(page)), ("sites-in-page", str(SITES_PAGE_SIZE))]
            )
            block = body.get("sites") if isinstance(body, dict) else None
            items = block.get("site") if isinstance(block, dict) else None
            if not isinstance(items, list):
                return sites
            sites.extend(SiteSummary.from_api(i) for i in items if isinstance(i, dict))
            if len(items) < SITES_PAGE_SIZE:
                return sites
            page += 1

    async def async_get_site(self, site_id: int) -> SiteMetadata:
        """GET /sites/{id}."""
        body = await self._request(f"/sites/{site_id}")
        if not isinstance(body, dict) or "siteId" not in body:
            raise SolarEdgeResponseError(200, "Site metadata without siteId")
        return SiteMetadata.from_api(body)

    async def async_get_overview(self, site_id: int) -> SiteOverview:
        """GET /sites/{id}/overview for midnight today (site time) until now."""
        body = await self._request(f"/sites/{site_id}/overview")
        return SiteOverview.from_api(body if isinstance(body, dict) else {})

    async def async_get_power(self, site_id: int, start: datetime, end: datetime) -> Series:
        """GET /sites/{id}/power at QUARTER_HOUR; the API rejects spans over 12 hours."""
        body = await self._request(
            f"/sites/{site_id}/power",
            [("resolution", "QUARTER_HOUR"), ("unit", "W"), *_window(start, end)],
        )
        return Series.from_api(body)

    async def async_get_lifetime_energy(
        self, site_id: int, start: datetime, end: datetime
    ) -> float | None:
        """Sum GET /sites/{id}/energy YEAR buckets, the only unlimited span, in Wh."""
        body = await self._request(
            f"/sites/{site_id}/energy",
            [("resolution", "YEAR"), ("unit", "WH"), *_window(start, end)],
        )
        return Series.from_api(body).total_watt_hours

    async def async_get_devices(
        self, site_id: int, types: Sequence[DeviceType]
    ) -> list[Device]:
        """GET /sites/{id}/devices; the API returns only inverters unless types is sent."""
        body = await self._request(
            f"/sites/{site_id}/devices", [("types", t.value) for t in types]
        )
        return [
            Device.from_api(i)
            for i in (body if isinstance(body, list) else [])
            if isinstance(i, dict) and i.get("serialNumber")
        ]

    async def async_get_open_alerts(self, site_id: int) -> list[Alert]:
        """GET /sites/{id}/alerts, open alerts only, first page of 100."""
        body = await self._request(
            f"/sites/{site_id}/alerts", [("only-open", "true"), ("alerts-in-page", "100")]
        )
        return [
            Alert.from_api(i)
            for i in (body if isinstance(body, list) else [])
            if isinstance(i, dict) and "alertId" in i
        ]

    async def async_get_inverter_telemetry(
        self, site_id: int, start: datetime, end: datetime
    ) -> Telemetry:
        """GET /sites/{id}/inverters/telemetry at HOUR, which allows up to 24 hours."""
        return parse_telemetry(
            await self._telemetry(f"/sites/{site_id}/inverters/telemetry", start, end),
            "inverters",
        )

    async def async_get_meter_telemetry(
        self, site_id: int, start: datetime, end: datetime
    ) -> Telemetry:
        """GET /sites/{id}/meters/telemetry at HOUR."""
        return parse_telemetry(
            await self._telemetry(f"/sites/{site_id}/meters/telemetry", start, end), "meters"
        )

    async def async_get_storage_telemetry(self, site_id: int) -> Telemetry:
        """GET /sites/{id}/storage/telemetry for today at the API's default QUARTER_HOUR.

        No from/to or resolution is sent: windowed requests were rejected with
        "Failed to convert [...] to required type [Instant]", while the bare call returns
        today's data.
        """
        return parse_telemetry(
            await self._request(f"/sites/{site_id}/storage/telemetry"), "storage"
        )

    async def _telemetry(self, path: str, start: datetime, end: datetime) -> Any:
        return await self._request(path, [("resolution", "HOUR"), *_window(start, end)])


def _window(start: datetime, end: datetime) -> list[tuple[str, str]]:
    """Format a window as ISO 8601 instants with an explicit UTC offset.

    The API converts from/to to an Instant and answers HTTP 400 when the offset is
    missing. Naive datetimes are refused rather than guessed at.
    """
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("window start and end must be timezone-aware")
    return [
        ("from", start.isoformat(timespec="seconds")),
        ("to", end.isoformat(timespec="seconds")),
    ]


async def _error_for(response: ClientResponse) -> SolarEdgeResponseError:
    status = response.status
    try:
        body = await response.json(content_type=None)
    except ClientError, ValueError:
        body = None
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("message") or body.get("title") or "")
    detail = detail or _phrase(status)
    if status == 401:
        return SolarEdgeAuthenticationError(status, detail)
    if status == 403:
        return SolarEdgeForbiddenError(status, detail)
    if status == 429:
        remaining = response.headers.get("x-ratelimit-remaining-minute")
        if remaining is not None and remaining.strip() not in ("", "0"):
            return SolarEdgeCreditLimitError(status, detail)
        retry_after = response.headers.get("retry-after")
        return SolarEdgeRateLimitError(
            status, detail, int(retry_after) if retry_after and retry_after.isdigit() else None
        )
    return SolarEdgeResponseError(status, detail)


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "error"
