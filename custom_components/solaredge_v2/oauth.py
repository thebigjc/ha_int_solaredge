"""OAuth callback that works without the `state` parameter.

SolarEdge's authorize endpoint does not echo `state` and always redirects to the
redirect URL registered in the developer console, so Home Assistant's own
`/auth/external/callback` cannot match a callback to its config flow. This module owns
the callback instead: it tracks the authorize attempts in flight, and resolves a callback
to the single flow that is waiting. PKCE ties the code to that flow's verifier, which is
what protects against a forged callback in place of `state` (RFC 9700 sections 2.1, 4.7.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import secrets
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, OAUTH_CALLBACK_PATH, OAUTH_PENDING_TTL

_LOGGER = logging.getLogger(__name__)

PLACEHOLDER_HOST = "https://<your-home-assistant-host>"


def callback_url(hass: HomeAssistant) -> str:
    """The URL to register in the SolarEdge console; prefers the external URL.

    Raises NoURLAvailableError when Home Assistant has neither URL configured.
    """
    return f"{get_url(hass, allow_internal=True, prefer_external=True)}{OAUTH_CALLBACK_PATH}"


def callback_url_hint(hass: HomeAssistant) -> str:
    """Like `callback_url`, but with a placeholder host when no URL is known."""
    try:
        return callback_url(hass)
    except NoURLAvailableError:
        return f"{PLACEHOLDER_HOST}{OAUTH_CALLBACK_PATH}"


@dataclass(frozen=True, slots=True)
class PendingAuth:
    """One authorize attempt waiting for its callback."""

    flow_id: str
    state: str
    code_verifier: str
    redirect_uri: str
    expires: datetime


class PendingAuths:
    """The authorize attempts in flight, each bound to a config flow."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start empty."""
        self._hass = hass
        self._items: dict[str, PendingAuth] = {}

    def register(self, flow_id: str, code_verifier: str, redirect_uri: str) -> PendingAuth:
        """Remember an attempt; a new one for the same flow replaces the old."""
        pending = PendingAuth(
            flow_id=flow_id,
            state=secrets.token_urlsafe(32),
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            expires=dt_util.utcnow() + OAUTH_PENDING_TTL,
        )
        self._items[flow_id] = pending
        return pending

    def active(self) -> list[PendingAuth]:
        """Attempts that have not expired and whose flow still exists."""
        now = dt_util.utcnow()
        for flow_id, item in list(self._items.items()):
            if item.expires <= now or not self._flow_alive(flow_id):
                del self._items[flow_id]
        return list(self._items.values())

    def pop(self, flow_id: str) -> None:
        """Forget an attempt once its callback has been delivered."""
        self._items.pop(flow_id, None)

    def _flow_alive(self, flow_id: str) -> bool:
        try:
            self._hass.config_entries.flow.async_get(flow_id)
        except UnknownFlow:
            return False
        return True


PENDING_KEY: HassKey[PendingAuths] = HassKey(f"{DOMAIN}_pending_auths")


def async_pending(hass: HomeAssistant) -> PendingAuths:
    """The pending registry, created with the callback view on first use."""
    if PENDING_KEY not in hass.data:
        hass.data[PENDING_KEY] = PendingAuths(hass)
        hass.http.register_view(SolarEdgeOAuthCallbackView())
    return hass.data[PENDING_KEY]


def _page(message: str, *, status: int = 200, close: bool = False) -> web.Response:
    script = "<script>window.close()</script>" if close else ""
    return web.Response(
        status=status,
        content_type="text/html",
        text=f"<!doctype html><title>SolarEdge</title><p>{message}</p>{script}",
    )


class SolarEdgeOAuthCallbackView(HomeAssistantView):
    """Receives SolarEdge's redirect and hands the code to the waiting config flow."""

    requires_auth = False
    url = OAUTH_CALLBACK_PATH
    name = "api:solaredge_v2:oauth:callback"

    async def get(self, request: web.Request) -> web.Response:
        """Resolve the callback to one pending flow and resume it."""
        hass: HomeAssistant = request.app[KEY_HASS]
        query = request.query
        if "code" not in query and "error" not in query:
            return _page("The callback has no authorization code.", status=400)

        pending = async_pending(hass)
        active = pending.active()
        if (state := query.get("state")) is not None:
            matches = [p for p in active if secrets.compare_digest(p.state, state)]
        else:
            matches = active
        if not matches:
            return _page(
                "No SolarEdge sign-in is waiting in Home Assistant, or it expired. "
                "Start the setup again.",
                status=400,
            )
        if len(matches) > 1:
            return _page(
                "More than one SolarEdge sign-in is waiting in Home Assistant. "
                "Remove the stale setup or reauthentication flow, then start again.",
                status=409,
            )

        item = matches[0]
        user_input: dict[str, Any] = {
            "state": {
                "flow_id": item.flow_id,
                "redirect_uri": item.redirect_uri,
                "code_verifier": item.code_verifier,
            }
        }
        if "code" in query:
            user_input["code"] = query["code"]
            if query.get("site_id", "").isdecimal():
                user_input["site_id"] = int(query["site_id"])
        else:
            user_input["error"] = query["error"]
        pending.pop(item.flow_id)
        try:
            await hass.config_entries.flow.async_configure(item.flow_id, user_input)
        except UnknownFlow:
            return _page("The setup flow no longer exists. Start again.", status=400)
        _LOGGER.debug("Resumed OAuth configuration flow")
        return _page("SolarEdge is connected. Return to Home Assistant.", close=True)
