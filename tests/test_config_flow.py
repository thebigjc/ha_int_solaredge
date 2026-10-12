"""Tests for the config, reauth, reconfigure, and options flows."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from aiohttp import ClientResponse
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers.config_entry_oauth2_flow import LocalOAuth2ImplementationWithPkce
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

from custom_components.solaredge_v2.api import BASE_URL
from custom_components.solaredge_v2.config_flow import CREDIT_WARNING
from custom_components.solaredge_v2.const import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH,
    CONF_AUTH_TYPE,
    CONF_DEVICE_TELEMETRY,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SITE_ID,
    CONF_STORAGE_INTERVAL_MINUTES,
    CONF_TELEMETRY_INTERVAL_MINUTES,
    CONF_TELEMETRY_INVERTERS,
    CONF_TELEMETRY_METERS,
    CONF_TELEMETRY_STORAGE,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_TOKEN,
    OAUTH_CALLBACK_PATH,
    OAUTH_PENDING_TTL,
)
from custom_components.solaredge_v2.polling import resolve_plan

from .conftest import CLIENT_ID, CLIENT_SECRET, SITE, SITE_ID, MockApi

TOKEN = {"access_token": "new-access", "refresh_token": "new-refresh", "token_type": "Bearer", "expires_in": 7200}


@pytest.fixture(autouse=True)
def no_setup() -> Any:
    """Keep entry creation from setting the integration up."""
    with patch("custom_components.solaredge_v2.async_setup_entry", return_value=True) as mock:
        yield mock


async def _start(hass: HomeAssistant, choice: str) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": choice})


async def test_api_key_multiple_sites(hass: HomeAssistant, mock_api: MockApi) -> None:
    """A key that reads two sites asks which one."""
    result = await _start(hass, "api_key")
    assert result["step_id"] == "api_key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: " test-key "})
    assert result["step_id"] == "select_site"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SITE_ID: str(SITE_ID)})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Home"
    assert result["data"] == {CONF_AUTH_TYPE: AUTH_TYPE_API_KEY, CONF_API_KEY: "test-key", CONF_SITE_ID: SITE_ID}
    assert result["result"].unique_id == str(SITE_ID)


async def test_api_key_single_site(hass: HomeAssistant, mock_api: MockApi) -> None:
    """A key that reads one site creates the entry directly."""
    mock_api(sites={"sites": {"count": 1, "site": [{"siteId": SITE_ID, "name": "Only"}]}})
    result = await _start(hass, "api_key")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "test-key"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Only"


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"sites": 401}, "invalid_auth"),
        ({"sites": 403}, "site_forbidden"),
        ({"sites": 500}, "unknown"),
        ({"sites": {"sites": {"count": 0, "site": []}}}, "no_sites"),
    ],
)
async def test_api_key_errors_recover(
    hass: HomeAssistant, mock_api: MockApi, override: dict[str, Any], error: str
) -> None:
    """Each failure shows an error and the flow still completes afterwards."""
    mock_api(**override)
    result = await _start(hass, "api_key")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "bad"})
    assert result["errors"] == {"base": error}
    mock_api()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "test-key"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SITE_ID: str(SITE_ID)})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_api_key_credit_and_connection_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Credit exhaustion and transport failures have their own messages."""
    aioclient_mock.get(f"{BASE_URL}/sites", status=429, headers={"x-ratelimit-remaining-minute": "5"}, json={})
    result = await _start(hass, "api_key")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "k"})
    assert result["errors"] == {"base": "credit_limit"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/sites", exc=TimeoutError)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "k"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_api_key_duplicate(hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry) -> None:
    """The same site cannot be added twice."""
    result = await _start(hass, "api_key")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "test-key"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SITE_ID: str(SITE_ID)})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_select_site_without_key(hass: HomeAssistant) -> None:
    """Reaching the site picker without a validated key goes back to the key form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    result = await flow.async_step_select_site()
    assert result["step_id"] == "api_key"


EXTERNAL_URL = "https://ha.example.com:8443"
CALLBACK = f"{EXTERNAL_URL}{OAUTH_CALLBACK_PATH}"


@pytest.fixture(autouse=True)
def external_url(hass: HomeAssistant, current_request_with_host: None) -> None:
    """Give Home Assistant the external URL the callback is derived from."""
    hass.config.external_url = EXTERNAL_URL


def _authorize_params(result: dict[str, Any]) -> dict[str, str]:
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(f"{OAUTH2_AUTHORIZE}?")
    return dict(URL(result["url"]).query)


def _waiting(hass: HomeAssistant) -> set[str]:
    return {f["flow_id"] for f in hass.config_entries.flow.async_progress()}


async def _callback(
    hass_client_no_auth: ClientSessionGenerator, query: str = "code=abcd&site_id=1234567"
) -> ClientResponse:
    client = await hass_client_no_auth()
    return await client.get(f"{OAUTH_CALLBACK_PATH}?{query}")


async def _start_authorize(
    hass: HomeAssistant, result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Continue a flow into the external step and return it with the authorize query."""
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    return result, _authorize_params(result)


@pytest.mark.usefixtures("credentials")
async def test_authorize_url(hass: HomeAssistant, mock_api: MockApi) -> None:
    """The authorize URL asks for PKCE S256 and the integration's own callback."""
    result = await _start(hass, "oauth")
    query = _authorize_params(result)
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["scope"] == "SITE_DATA DEVICE_DATA"
    assert query["access_duration"] == "24"
    assert query["redirect_uri"] == CALLBACK
    assert query["code_challenge_method"] == "S256"
    assert query["state"]
    assert 43 <= len(query["code_challenge"]) <= 128


@pytest.mark.usefixtures("credentials")
async def test_oauth_full_flow_with_site_id(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
) -> None:
    """A callback without state but with site_id creates the entry with no manual step."""
    result = await _start(hass, "oauth")
    query = _authorize_params(result)
    resp = await _callback(hass_client_no_auth)
    assert resp.status == 200
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_TYPE] == AUTH_TYPE_OAUTH
    assert result["data"][CONF_SITE_ID] == SITE_ID
    assert result["data"]["token"]["access_token"] == "new-access"
    assert result["result"].unique_id == str(SITE_ID)

    token_call = next(c for c in aioclient_mock.mock_calls if c[0] == "POST")
    body = token_call[2]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "abcd"
    assert body["client_secret"] == CLIENT_SECRET
    assert body["redirect_uri"] == CALLBACK
    assert (
        LocalOAuth2ImplementationWithPkce.compute_code_challenge(body["code_verifier"])
        == query["code_challenge"]
    )
    site_call = [c for c in aioclient_mock.mock_calls if str(c[1]) == SITE][-1]
    assert site_call[3]["Authorization"] == "Bearer new-access"


@pytest.mark.usefixtures("credentials")
async def test_each_flow_gets_its_own_verifier(hass: HomeAssistant, mock_api: MockApi) -> None:
    """Two authorize attempts never share a challenge or a state."""
    first = _authorize_params(await _start(hass, "oauth"))
    second = _authorize_params(await _start(hass, "oauth"))
    assert first["code_challenge"] != second["code_challenge"]
    assert first["state"] != second["state"]


@pytest.mark.usefixtures("credentials")
async def test_oauth_without_site_id_asks_for_it(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
) -> None:
    """With no site_id in the callback the manual Site ID step is the fallback."""
    result = await _start(hass, "oauth")
    assert (await _callback(hass_client_no_auth, "code=abcd")).status == 200
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "site"
    mock_api(site=403)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SITE_ID: SITE_ID})
    assert result["errors"] == {"base": "site_forbidden"}
    mock_api()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SITE_ID: SITE_ID})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SITE_ID] == SITE_ID


@pytest.mark.usefixtures("credentials")
async def test_oauth_site_id_still_validated(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
) -> None:
    """A site_id the token cannot read falls back to the form with the error."""
    result = await _start(hass, "oauth")
    await _callback(hass_client_no_auth)
    mock_api(site=403)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "site_forbidden"}


@pytest.mark.usefixtures("credentials")
async def test_callback_site_id_must_be_numeric(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
) -> None:
    """A non-numeric site_id is ignored rather than trusted."""
    result = await _start(hass, "oauth")
    await _callback(hass_client_no_auth, "code=abcd&site_id=abc")
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "site"


@pytest.mark.usefixtures("credentials")
async def test_callback_without_pending_flow(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator, mock_api: MockApi
) -> None:
    """A callback while nothing waits is rejected with an explanation."""
    assert await async_setup_component(hass, DOMAIN, {})
    resp = await _callback(hass_client_no_auth)
    assert resp.status == 400
    assert "No SolarEdge sign-in is waiting" in await resp.text()
    resp = await _callback(hass_client_no_auth, "code=abcd&state=nope")
    assert resp.status == 400


@pytest.mark.usefixtures("credentials")
async def test_callback_with_two_pending_flows(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
) -> None:
    """Two waiting flows are ambiguous: reject and leave both usable."""
    first = await _start(hass, "oauth")
    second = await _start(hass, "oauth")
    resp = await _callback(hass_client_no_auth)
    assert resp.status == 409
    assert "More than one" in await resp.text()
    for flow in (first, second):
        assert flow["flow_id"] in _waiting(hass)
    # Removing one flow makes the other unambiguous again.
    hass.config_entries.flow.async_abort(second["flow_id"])
    assert (await _callback(hass_client_no_auth)).status == 200
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(first["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("credentials")
async def test_echoed_state_selects_the_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
) -> None:
    """If SolarEdge starts returning state, it picks the flow even with two waiting."""
    first = await _start(hass, "oauth")
    query = _authorize_params(first)
    await _start(hass, "oauth")
    resp = await _callback(hass_client_no_auth, f"code=abcd&site_id={SITE_ID}&state={query['state']}")
    assert resp.status == 200
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(first["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("credentials")
async def test_expired_flow_is_rejected(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    mock_api: MockApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A callback after the pending window is rejected and the code is not used."""
    result = await _start(hass, "oauth")
    freezer.tick(OAUTH_PENDING_TTL + timedelta(seconds=1))
    resp = await _callback(hass_client_no_auth)
    assert resp.status == 400
    assert result["flow_id"] in _waiting(hass)


@pytest.mark.usefixtures("credentials")
async def test_callback_inside_window_is_accepted(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    mock_api: MockApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Just inside the window still works."""
    await _start(hass, "oauth")
    freezer.tick(OAUTH_PENDING_TTL - timedelta(seconds=1))
    assert (await _callback(hass_client_no_auth)).status == 200


@pytest.mark.usefixtures("credentials")
async def test_callback_without_code(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator, mock_api: MockApi
) -> None:
    """A callback with neither code nor error is rejected and keeps the flow waiting."""
    result = await _start(hass, "oauth")
    resp = await _callback(hass_client_no_auth, "site_id=1234567")
    assert resp.status == 400
    assert result["flow_id"] in _waiting(hass)
    assert (await _callback(hass_client_no_auth)).status == 200


@pytest.mark.usefixtures("credentials")
async def test_callback_rejected_by_user(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator, mock_api: MockApi
) -> None:
    """An error callback aborts the waiting flow."""
    result = await _start(hass, "oauth")
    assert (await _callback(hass_client_no_auth, "error=access_denied")).status == 200
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "user_rejected_authorize"


@pytest.mark.usefixtures("credentials")
async def test_callback_flow_vanishes(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator, mock_api: MockApi
) -> None:
    """A flow that disappears between matching and resuming gives an error page."""
    await _start(hass, "oauth")
    with patch.object(hass.config_entries.flow, "async_configure", side_effect=UnknownFlow):
        resp = await _callback(hass_client_no_auth)
    assert resp.status == 400


@pytest.mark.usefixtures("credentials")
async def test_dismissed_flow_is_forgotten(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator, mock_api: MockApi
) -> None:
    """A flow the user dismissed no longer counts as pending."""
    result = await _start(hass, "oauth")
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert (await _callback(hass_client_no_auth)).status == 400


async def test_authorize_without_any_url(hass: HomeAssistant, credentials: None, mock_api: MockApi) -> None:
    """With no usable Home Assistant URL the flow aborts with guidance."""
    with patch("custom_components.solaredge_v2.oauth.get_url", side_effect=NoURLAvailableError):
        result = await _start(hass, "oauth")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_url_available"


async def test_oauth_reauth(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
    oauth_entry: MockConfigEntry,
) -> None:
    """Reauth uses the same callback and replaces the token after checking the site."""
    result = await oauth_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result, query = await _start_authorize(hass, result)
    assert query["redirect_uri"] == CALLBACK
    assert (await _callback(hass_client_no_auth)).status == 200
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert oauth_entry.data["token"]["access_token"] == "new-access"
    assert oauth_entry.data[CONF_SITE_ID] == SITE_ID


async def test_oauth_reauth_other_site_in_callback(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
    oauth_entry: MockConfigEntry,
) -> None:
    """Approving a different site than the entry's aborts without saving."""
    result = await oauth_entry.start_reauth_flow(hass)
    result, _ = await _start_authorize(hass, result)
    await _callback(hass_client_no_auth, "code=abcd&site_id=999")
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "oauth_site_forbidden"
    assert oauth_entry.data["token"]["access_token"] == "access"


async def test_oauth_reconfigure_wrong_site(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_api: MockApi,
    oauth_entry: MockConfigEntry,
) -> None:
    """A new grant that cannot read the configured site aborts without saving."""
    result = await oauth_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result, _ = await _start_authorize(hass, result)
    await _callback(hass_client_no_auth, "code=abcd")
    mock_api(site=403)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "oauth_site_forbidden"
    assert oauth_entry.data["token"]["access_token"] == "access"


async def test_api_key_reauth(hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry) -> None:
    """Reauth for a Fleet Access entry validates and stores the new key."""
    result = await api_key_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_api(site=401)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    mock_api()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "new-key"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert api_key_entry.data[CONF_API_KEY] == "new-key"


async def test_api_key_reconfigure(hass: HomeAssistant, mock_api: MockApi, api_key_entry: MockConfigEntry) -> None:
    """Reconfigure replaces the key of a Fleet Access entry."""
    result = await api_key_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "other-key"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert api_key_entry.data[CONF_API_KEY] == "other-key"


async def test_options(hass: HomeAssistant, api_key_entry: MockConfigEntry) -> None:
    """Options store the site interval, per-family switches, and telemetry intervals."""
    result = await hass.config_entries.options.async_init(api_key_entry.entry_id)
    assert result["step_id"] == "init"
    # The fixture entry has the old device_telemetry switch on: all families, 60 minutes.
    assert result["description_placeholders"] == {"credits": "4,020", "warning": CREDIT_WARNING}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_MINUTES: 720.0,
            CONF_TELEMETRY_INVERTERS: False,
            CONF_TELEMETRY_METERS: False,
            CONF_TELEMETRY_STORAGE: True,
            CONF_TELEMETRY_INTERVAL_MINUTES: 0.0,
            CONF_STORAGE_INTERVAL_MINUTES: 30.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert api_key_entry.options == {
        CONF_SCAN_INTERVAL_MINUTES: 720,
        CONF_TELEMETRY_INVERTERS: False,
        CONF_TELEMETRY_METERS: False,
        CONF_TELEMETRY_STORAGE: True,
        CONF_TELEMETRY_INTERVAL_MINUTES: 0,
        CONF_STORAGE_INTERVAL_MINUTES: 30,
    }


async def test_options_form_defaults_from_old_entry(hass: HomeAssistant, api_key_entry: MockConfigEntry) -> None:
    """Resubmitting an untouched form keeps an old entry's behaviour and drops the old key."""
    hass.config_entries.async_update_entry(
        api_key_entry, options={CONF_SCAN_INTERVAL_MINUTES: 45, CONF_DEVICE_TELEMETRY: True}
    )
    result = await hass.config_entries.options.async_init(api_key_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert api_key_entry.options == {
        CONF_SCAN_INTERVAL_MINUTES: 45,
        CONF_TELEMETRY_INVERTERS: True,
        CONF_TELEMETRY_METERS: True,
        CONF_TELEMETRY_STORAGE: True,
        CONF_TELEMETRY_INTERVAL_MINUTES: 0,
        CONF_STORAGE_INTERVAL_MINUTES: 0,
    }
    assert resolve_plan(api_key_entry.options) == resolve_plan(
        {CONF_SCAN_INTERVAL_MINUTES: 45, CONF_DEVICE_TELEMETRY: True}
    )


async def test_options_default_entry_shows_estimate_without_warning(
    hass: HomeAssistant, api_key_entry: MockConfigEntry
) -> None:
    """A default entry shows 1,860 credits and no warning."""
    hass.config_entries.async_update_entry(api_key_entry, options={})
    result = await hass.config_entries.options.async_init(api_key_entry.entry_id)
    assert result["description_placeholders"] == {"credits": "1,860", "warning": ""}


async def test_options_reject_interval_under_15(hass: HomeAssistant, api_key_entry: MockConfigEntry) -> None:
    """A telemetry interval of 1 to 14 minutes is refused; 0 means follow the site."""
    result = await hass.config_entries.options.async_init(api_key_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STORAGE_INTERVAL_MINUTES: 5.0}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_STORAGE_INTERVAL_MINUTES: "interval_too_short"}
    assert result["description_placeholders"]["credits"]


async def test_missing_credentials(hass: HomeAssistant) -> None:
    """Choosing OAuth with no application credentials aborts with guidance."""
    result = await _start(hass, "oauth")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"
