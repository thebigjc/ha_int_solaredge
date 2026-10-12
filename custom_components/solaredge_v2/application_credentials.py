"""Application credentials for SolarEdge Site Access (OAuth 2.0)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, cast, override

from aiohttp import ClientResponseError
from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import LocalOAuth2ImplementationWithPkce
from yarl import URL

from .const import OAUTH2_ACCESS_DURATION_MONTHS, OAUTH2_AUTHORIZE, OAUTH2_SCOPES, OAUTH2_TOKEN
from .oauth import async_pending, callback_url, callback_url_hint


class SolarEdgeOAuth2Implementation(AuthImplementation):
    """PKCE authorization to the registered callback, with a JSON token endpoint.

    SolarEdge does not return `state`, so the callback view finds the flow by itself
    (see `oauth`); the PKCE verifier kept per flow binds the code to that flow.
    """

    @property
    @override
    def redirect_uri(self) -> str:
        """The integration's own callback, which must match the registered URL."""
        return callback_url(self.hass)

    @override
    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Start a PKCE attempt for this flow and remember it for the callback."""
        verifier = LocalOAuth2ImplementationWithPkce.generate_code_verifier()
        pending = async_pending(self.hass).register(flow_id, verifier, self.redirect_uri)
        challenge = LocalOAuth2ImplementationWithPkce.compute_code_challenge(verifier)
        return str(
            URL(self.authorize_url)
            .with_query(
                {
                    "response_type": "code",
                    "client_id": self.client_id,
                    "redirect_uri": pending.redirect_uri,
                    "state": pending.state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
            .update_query(self.extra_authorize_data)
        )

    @override
    async def async_resolve_external_data(self, external_data: Any) -> dict[str, Any]:
        """Exchange the code, proving possession of this flow's PKCE verifier."""
        state = external_data["state"]
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": external_data["code"],
                "redirect_uri": state["redirect_uri"],
                "code_verifier": state["code_verifier"],
            }
        )

    @property
    @override
    def extra_authorize_data(self) -> dict[str, Any]:
        """Request both scopes and the longest grant SolarEdge Connect allows."""
        return {"scope": OAUTH2_SCOPES, "access_duration": OAUTH2_ACCESS_DURATION_MONTHS}

    @override
    async def _token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """Post the token request as JSON and map failures like the core helper."""
        payload = {**data, "client_id": self.client_id, "client_secret": self.client_secret}
        session = async_get_clientsession(self.hass)
        resp = await session.post(self.token_url, json=payload)
        try:
            resp.raise_for_status()
        except ClientResponseError as err:
            kwargs: dict[str, Any] = {
                "request_info": err.request_info,
                "history": err.history,
                "status": err.status,
                "message": err.message,
                "headers": err.headers,
                "domain": self.domain,
            }
            if err.status == HTTPStatus.TOO_MANY_REQUESTS or err.status >= 500:
                raise OAuth2TokenRequestTransientError(**kwargs) from err
            raise OAuth2TokenRequestReauthError(**kwargs) from err
        return cast(dict[str, Any], await resp.json(content_type=None))


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> SolarEdgeOAuth2Implementation:
    """Return the SolarEdge implementation for a stored credential."""
    return SolarEdgeOAuth2Implementation(
        hass,
        auth_domain,
        credential,
        AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN),
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Links shown in the Add Application Credentials dialog."""
    return {
        "developer_console": "https://developer.solaredge.com",
        "self_access_guide": (
            "https://api-docs.solaredge.com/docs/developer-platform/"
            "df8kx5j4z9vi0-homeowner-and-self-access-guide"
        ),
        "redirect_url": callback_url_hint(hass),
    }
