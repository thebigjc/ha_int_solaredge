"""Debug logs show what SolarEdge said, and never a credential."""

from __future__ import annotations

from datetime import timedelta
import logging
import time

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_v2.api.redact import safe_body, safe_query, trace_headers
from custom_components.solaredge_v2.const import OAUTH2_TOKEN

from .conftest import CLIENT_SECRET, MockApi
from .test_secrets import OAUTH_SECRETS, REFRESH_TOKEN, _assert_absent, _oauth_entry

NEW_ACCESS = "rotated-access-token-7c1e9b"
NEW_REFRESH = "rotated-refresh-token-2d8f4a"


async def _expire_and_tick(hass, entry, freezer: FrozenDateTimeFactory) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "token": {**entry.data["token"], "expires_at": time.time() - 10}}
    )
    freezer.tick(timedelta(minutes=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup_oauth(hass: HomeAssistant):
    entry = _oauth_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def test_safe_body_masks_secret_fields_at_any_depth() -> None:
    body = '{"access_token": "aaa", "nested": {"refresh_token": "bbb"}, "expires_in": 7200}'
    out = safe_body(body)
    assert "aaa" not in out and "bbb" not in out
    assert "7200" in out


def test_safe_body_masks_long_tokens_in_plain_text() -> None:
    out = safe_body("upstream said abcdefghijklmnopqrstuvwxyz012345 failed")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "upstream said" in out


def test_safe_body_truncates() -> None:
    assert len(safe_body("x " * 5000)) <= 520


def test_safe_query_keeps_only_public_values() -> None:
    out = safe_query({"error": "400.9", "code": "secret-code", "state": "secret-state"})
    assert out == {"error": "400.9", "code": "***", "state": "***"}


def test_trace_headers_keeps_only_request_ids() -> None:
    out = trace_headers({"X-Request-Id": "req-1", "Set-Cookie": "session=abc", "CF-Ray": "ray-2"})
    assert out == {"X-Request-Id": "req-1", "CF-Ray": "ray-2"}


async def test_failed_refresh_logs_status_body_and_request_id(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    credentials: None,
) -> None:
    """The 500 body and request ID reach the debug log; the credentials sent do not."""
    caplog.set_level(logging.DEBUG)
    entry = await _setup_oauth(hass)
    aioclient_mock.post(
        OAUTH2_TOKEN,
        status=500,
        json={"error": "internal_error", "error_description": "grant store down"},
        headers={"X-Request-Id": "req-42"},
    )
    await _expire_and_tick(hass, entry, freezer)

    assert any(c[2]["refresh_token"] == REFRESH_TOKEN for c in aioclient_mock.mock_calls if c[0] == "POST")
    assert "refresh_token" in caplog.text  # the field is named, its value masked
    assert "HTTP 500" in caplog.text
    assert "grant store down" in caplog.text
    assert "req-42" in caplog.text
    _assert_absent((*OAUTH_SECRETS, CLIENT_SECRET), caplog.text)


async def test_successful_refresh_logs_no_new_tokens(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    mock_api: MockApi,
    aioclient_mock: AiohttpClientMocker,
    credentials: None,
) -> None:
    """The rotated pair is the most sensitive thing this log could ever see."""
    caplog.set_level(logging.DEBUG)
    entry = await _setup_oauth(hass)
    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 7200},
    )
    await _expire_and_tick(hass, entry, freezer)

    assert entry.data["token"]["access_token"] == NEW_ACCESS
    assert "HTTP 200" in caplog.text
    assert "7200" in caplog.text
    _assert_absent((*OAUTH_SECRETS, NEW_ACCESS, NEW_REFRESH), caplog.text)


async def test_api_call_logs_path_and_status(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    mock_api: MockApi,
    credentials: None,
) -> None:
    """Each API call shows its path and status, without the bearer token."""
    caplog.set_level(logging.DEBUG)
    await _setup_oauth(hass)
    mock_api(overview=503)
    freezer.tick(timedelta(minutes=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert "/overview" in caplog.text
    assert "HTTP 503" in caplog.text
    _assert_absent(OAUTH_SECRETS, caplog.text)
