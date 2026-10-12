"""Config flow for the SolarEdge Monitoring API v2 integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, override

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio

from .api import (
    ApiKeyAuth,
    AuthProvider,
    BearerTokenAuth,
    SiteMetadata,
    SiteSummary,
    SolarEdgeAuthenticationError,
    SolarEdgeClient,
    SolarEdgeConnectionError,
    SolarEdgeCreditLimitError,
    SolarEdgeError,
    SolarEdgeForbiddenError,
)
from .const import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH,
    CONF_AUTH_TYPE,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SITE_ID,
    CONF_STORAGE_INTERVAL_MINUTES,
    CONF_TELEMETRY_INTERVAL_MINUTES,
    CONF_TELEMETRY_INVERTERS,
    CONF_TELEMETRY_METERS,
    CONF_TELEMETRY_STORAGE,
    DOMAIN,
    FOLLOW_SITE_INTERVAL,
    FREE_TIER_MONTHLY_CREDITS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .polling import estimate_monthly_credits, resolve_plan

_LOGGER = logging.getLogger(__name__)

API_KEY_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
SITE_ID_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SITE_ID): NumberSelector(
            NumberSelectorConfig(min=1, step=1, mode=NumberSelectorMode.BOX)
        )
    }
)


class _StaticToken:
    def __init__(self, token: str) -> None:
        self._token = token

    async def __call__(self) -> str:
        return self._token


class SolarEdgeFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Set up a site with Site Access (OAuth) or Fleet Access (API key)."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow state."""
        super().__init__()
        self._oauth_data: dict[str, Any] = {}
        self._api_key: str | None = None
        self._sites: list[SiteSummary] = []

    @property
    @override
    def logger(self) -> logging.Logger:
        """Return the flow logger."""
        return _LOGGER

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> SolarEdgeOptionsFlow:
        """Return the options flow."""
        return SolarEdgeOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick the access type their developer application has."""
        return self.async_show_menu(step_id="user", menu_options=["oauth", "api_key"])

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the Site Access OAuth flow."""
        return await self.async_step_pick_implementation()

    @override
    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Keep the token, then ask for (or re-check) the site it was granted for."""
        self._oauth_data = data
        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = self._existing_entry()
            site_id = int(entry.data[CONF_SITE_ID])
            granted = self.external_data.get(CONF_SITE_ID)
            if granted is not None and int(granted) != site_id:
                return self.async_abort(reason="oauth_site_forbidden")
            errors, _ = await self._async_validate_site(self._bearer(), site_id)
            if errors:
                return self.async_abort(reason=f"oauth_{errors['base']}")
            return self.async_update_reload_and_abort(
                entry, data_updates={**data, CONF_AUTH_TYPE: AUTH_TYPE_OAUTH}
            )
        if (granted := self.external_data.get(CONF_SITE_ID)) is not None:
            # The callback names the approved site, so skip the manual question.
            return await self.async_step_site({CONF_SITE_ID: granted})
        return await self.async_step_site()

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry for a Site ID; asked only when the callback did not carry one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            site_id = int(user_input[CONF_SITE_ID])
            await self.async_set_unique_id(str(site_id))
            self._abort_if_unique_id_configured()
            errors, metadata = await self._async_validate_site(self._bearer(), site_id)
            if metadata is not None:
                return self.async_create_entry(
                    title=metadata.name,
                    data={
                        **self._oauth_data,
                        CONF_AUTH_TYPE: AUTH_TYPE_OAUTH,
                        CONF_SITE_ID: site_id,
                    },
                )
        return self.async_show_form(
            step_id="site",
            data_schema=self.add_suggested_values_to_schema(SITE_ID_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a Fleet Access App API Key and list the sites it can read."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            client = self._client(ApiKeyAuth(api_key))
            try:
                sites = await client.async_get_sites()
            except SolarEdgeError as err:
                errors["base"] = _error_key(err)
            else:
                if not sites:
                    errors["base"] = "no_sites"
                else:
                    self._api_key = api_key
                    self._sites = sites
                    if len(sites) == 1:
                        return await self.async_step_select_site(
                            {CONF_SITE_ID: str(sites[0].site_id)}
                        )
                    return await self.async_step_select_site()
        return self.async_show_form(
            step_id="api_key", data_schema=API_KEY_SCHEMA, errors=errors
        )

    async def async_step_select_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one site from those the key can read."""
        if self._api_key is None:
            return await self.async_step_api_key()
        if user_input is not None:
            site_id = int(user_input[CONF_SITE_ID])
            await self.async_set_unique_id(str(site_id))
            self._abort_if_unique_id_configured()
            name = next((s.name for s in self._sites if s.site_id == site_id), str(site_id))
            return self.async_create_entry(
                title=name,
                data={
                    CONF_AUTH_TYPE: AUTH_TYPE_API_KEY,
                    CONF_API_KEY: self._api_key,
                    CONF_SITE_ID: site_id,
                },
            )
        options = [
            SelectOptionDict(value=str(s.site_id), label=f"{s.name} ({s.site_id})")
            for s in self._sites
        ]
        return self.async_show_form(
            step_id="select_site",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_SITE_ID): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for the entry's access type."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key, or confirm and restart OAuth."""
        return await self._async_step_credentials("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the credentials of an existing site."""
        return await self._async_step_credentials("reconfigure", user_input)

    async def _async_step_credentials(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        entry = self._existing_entry()
        site_id = int(entry.data[CONF_SITE_ID])
        if entry.data.get(CONF_AUTH_TYPE) == AUTH_TYPE_OAUTH:
            if user_input is None:
                return self.async_show_form(
                    step_id=step_id,
                    description_placeholders={"site_id": str(site_id)},
                )
            return await self.async_step_pick_implementation()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            await self.async_set_unique_id(str(site_id))
            self._abort_if_unique_id_mismatch()
            errors, metadata = await self._async_validate_site(ApiKeyAuth(api_key), site_id)
            if metadata is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id=step_id,
            data_schema=API_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"site_id": str(site_id)},
        )

    def _existing_entry(self) -> ConfigEntry:
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        return self._get_reconfigure_entry()

    def _client(self, auth: AuthProvider) -> SolarEdgeClient:
        return SolarEdgeClient(async_get_clientsession(self.hass), auth)

    def _bearer(self) -> BearerTokenAuth:
        return BearerTokenAuth(_StaticToken(str(self._oauth_data[CONF_TOKEN]["access_token"])))

    async def _async_validate_site(
        self, auth: AuthProvider, site_id: int
    ) -> tuple[dict[str, str], SiteMetadata | None]:
        try:
            metadata = await self._client(auth).async_get_site(site_id)
        except SolarEdgeError as err:
            return {"base": _error_key(err)}, None
        return {}, metadata


def _error_key(err: SolarEdgeError) -> str:
    if isinstance(err, SolarEdgeAuthenticationError):
        return "invalid_auth"
    if isinstance(err, SolarEdgeForbiddenError):
        return "site_forbidden"
    if isinstance(err, SolarEdgeCreditLimitError):
        return "credit_limit"
    if isinstance(err, SolarEdgeConnectionError):
        return "cannot_connect"
    _LOGGER.error("Unexpected response from the SolarEdge API: %s", err)
    return "unknown"


CREDIT_WARNING = (
    f"**Warning:** this exceeds the {FREE_TIER_MONTHLY_CREDITS:,} credits a month of the "
    "Free tier. Updates stop when the quota is spent."
)


def _minutes_selector(minimum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=MAX_SCAN_INTERVAL_MINUTES,
            step=1,
            unit_of_measurement="min",
            mode=NumberSelectorMode.BOX,
        )
    )


class SolarEdgeOptionsFlow(OptionsFlowWithReload):
    """Tune polling against the monthly credit budget."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the polling options."""
        errors: dict[str, str] = {}
        # Defaults come from the resolved plan so an entry saved with the older
        # single telemetry switch shows, and keeps, the behaviour it had.
        current = resolve_plan(self.config_entry.options)
        shown: dict[str, Any] = {
            CONF_SCAN_INTERVAL_MINUTES: current.site_minutes,
            CONF_TELEMETRY_INVERTERS: current.inverters,
            CONF_TELEMETRY_METERS: current.meters,
            CONF_TELEMETRY_STORAGE: current.storage,
            CONF_TELEMETRY_INTERVAL_MINUTES: self.config_entry.options.get(
                CONF_TELEMETRY_INTERVAL_MINUTES, FOLLOW_SITE_INTERVAL
            ),
            CONF_STORAGE_INTERVAL_MINUTES: self.config_entry.options.get(
                CONF_STORAGE_INTERVAL_MINUTES, FOLLOW_SITE_INTERVAL
            ),
        }
        if user_input is not None:
            shown = {
                key: int(user_input.get(key, value))
                if isinstance(value, int) and not isinstance(value, bool)
                else user_input.get(key, value)
                for key, value in shown.items()
            }
            for key in (CONF_TELEMETRY_INTERVAL_MINUTES, CONF_STORAGE_INTERVAL_MINUTES):
                if 0 < shown[key] < MIN_SCAN_INTERVAL_MINUTES:
                    errors[key] = "interval_too_short"
            if not errors:
                return self.async_create_entry(data=shown)
        credits = estimate_monthly_credits(resolve_plan(shown))
        schema = probatio.Schema(
            {
                probatio.Required(CONF_SCAN_INTERVAL_MINUTES, default=shown[CONF_SCAN_INTERVAL_MINUTES]): _minutes_selector(
                    MIN_SCAN_INTERVAL_MINUTES
                ),
                probatio.Required(CONF_TELEMETRY_INVERTERS, default=shown[CONF_TELEMETRY_INVERTERS]): BooleanSelector(),
                probatio.Required(CONF_TELEMETRY_METERS, default=shown[CONF_TELEMETRY_METERS]): BooleanSelector(),
                probatio.Required(CONF_TELEMETRY_STORAGE, default=shown[CONF_TELEMETRY_STORAGE]): BooleanSelector(),
                probatio.Required(
                    CONF_TELEMETRY_INTERVAL_MINUTES, default=shown[CONF_TELEMETRY_INTERVAL_MINUTES]
                ): _minutes_selector(FOLLOW_SITE_INTERVAL),
                probatio.Required(
                    CONF_STORAGE_INTERVAL_MINUTES, default=shown[CONF_STORAGE_INTERVAL_MINUTES]
                ): _minutes_selector(FOLLOW_SITE_INTERVAL),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "credits": f"{credits:,}",
                "warning": CREDIT_WARNING if credits > FREE_TIER_MONTHLY_CREDITS else "",
            },
        )
