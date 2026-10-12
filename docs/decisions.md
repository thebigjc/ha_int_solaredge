# Decisions

## 2026-10-08: Minimum Home Assistant is 2026.10.0, schemas use probatio

Core 2026.10 types flow, service and websocket schemas as probatio, so the
voluptuous schemas failed mypy (developer blog 2026-09-30, "Probatio is our
validation engine"). The integration now imports `probatio` directly, as core
does; runtime behavior is unchanged because core has validated with probatio
since 2026.9. The suite runs on core 2026.10.0 and `hacs.json` follows the
tested core. Setup also stops translating OAuth2 implementation errors itself:
since 2026.10 core raises `ConfigEntryNotReady` when the implementation is
temporarily unavailable and `ConfigEntryAuthFailed` when it is gone (developer
blog 2026-09-07), where the old `except ValueError` turned both into a reauth.
Rejected: aliasing `probatio as vol`, which core's lint config bans.

## 2026-09-29: New domain `solaredge_v2` instead of overriding `solaredge`

Rejected: shipping the custom integration under the core domain `solaredge`.
A custom integration with a core domain replaces the core one entirely, which
would also remove core's web-login module statistics, and the old entries
(V1 key, site ID) cannot be migrated anyway because V2 needs new credentials.
A separate domain lets both run side by side until V1 stops on
November 3, 2026.

## 2026-09-29: Own the V2 client in-tree

Rejected: depending on `aiosolaredge` (V1 only) or waiting for a V2 package.
No V2 client is on PyPI. An in-tree asyncio client that takes the shared
session closes `async-dependency`, `inject-websession`, `strict-typing`, and
`dependency-transparency` together, following the ha_int_elkm1 precedent.

## 2026-09-29: Support both Site Access and Fleet Access

Homeowners can only use Site Access (OAuth); SolarEdge says Fleet Access needs
an installer profile. Installers who already hold an account key would lose
their path if only OAuth were offered, and the API key path costs little
extra code. Rejected: OAuth only.

## 2026-10-12: Own OAuth callback without `state`

Verified live: SolarEdge does not return `state`, and ignores the request's
`redirect_uri` in favour of the registered one, so Home Assistant's
`/auth/external/callback` and my.home-assistant.io cannot match a callback to
its flow (upstream issues 6, 8, 15). The integration now serves
`/api/solaredge_v2/oauth/callback`, which resolves the callback to the single
waiting flow (rejecting zero, several, or expired ones, 10 minute window) and
passes `code` and `site_id` to the flow. PKCE S256 with a per-flow verifier
binds the code to that flow in place of `state` (RFC 9700 2.1, 4.7.1). A
`state` is still sent and preferred if ever echoed. When the callback carries
`site_id` the manual Site ID question is skipped; it stays as a fallback, and
the site is still proved with `GET /sites/{id}`. Supersedes the 2026-09-29
decision to ask for the Site ID and not to add a callback view.

## 2026-09-29: Poll for the Free tier by default

Default interval 60 minutes and device telemetry off, so a single site fits
in 2,000 credits a month (1,860 used). Rejected: the core integration's
cadence, which polls overview, power flow, and energy details every 15 minutes
(its const.py); at one credit per call those three alone would be 8,640
credits in 30 days and need a paid tier.

## 2026-09-29: Explicit windows, HOUR for device telemetry, YEAR for lifetime

The API rejects more than 12 hours of 15-minute data, and every time-series
endpoint defaults to midnight-to-now, so relying on defaults fails every call
made after noon. Device telemetry uses `HOUR` from midnight to keep "today"
totals correct in one call; the alternative (two calls, one for power at 15
minutes and one for totals) doubles the telemetry cost. Lifetime energy sums
`YEAR` buckets because `TOTAL` has no documented span limit and `YEAR` is the
only resolution documented as unlimited.

## 2026-09-29: No optimizer data

The V2 Monitoring API exposes no optimizer telemetry. The web portal endpoints
that core's `solaredge_web` and the solaredgeoptimizers integration scrape are
not part of the V1 shutdown, so panel-level data stays with those. Rejected:
scraping the portal here, which would bring back username and password
storage and an unversioned, unstable surface.

## 2026-09-29: Scanner review findings that do not apply

- `oauth-clientresponseerror` in `application_credentials.py`: that code is
  the token implementation that raises `OAuth2TokenRequestReauthError` and
  `OAuth2TokenRequestTransientError`; it catches `ClientResponseError` from
  `raise_for_status()` only to translate it, exactly as the core helper does.
- `update-listener-plus-reload` in `config_flow.py`: the entry registers no
  update listener. Option changes reload through `OptionsFlowWithReload`, so
  `async_update_reload_and_abort` in reauth and reconfigure is the supported
  path.

## 2026-10-09: Core V1 key-leak remediation applied as tests, not code

The core `solaredge` integration logged its V1 site API key on a 503 (key in
the query string, `ClientResponseError` printing the URL, generic coordinator
logging `str(err)`, coordinator named by object repr, no backoff). Checked
each remediation item against this integration:

- Never interpolate an error that carries the URL: V2 credentials are header
  only, and the client builds its own error text from status and problem
  detail, so there is nothing to change.
- Stable coordinator name: already `solaredge_v2 <site id>`.
- Log once per outage: core's coordinator logs only on the success-to-failure
  transition, and 429 responses already carry `retry_after`. Rejected: adding
  a second backoff layer for 5xx, which would hide recovery and save no
  credits that a 503 is known to cost.
- Negative test: added `tests/test_secrets.py`, which fails the site call
  and the token refresh with a 503 and a transport error for both access
  types and asserts no credential reaches a log record, the coordinator's
  `last_exception`, or a request URL. The test was verified against a
  deliberate header leak in the client before the leak was reverted.

The property is now enforced by test rather than by review; see
[security.md](security.md).

## 2026-10-12: Per-group polling budget, defaults unchanged

Users who want one data group often (battery state of charge every 30
minutes) could not without spending about 7,200 credits a month. Polling is now
configured per group: site (overview and power), inverter and meter telemetry
(one shared interval), storage telemetry (own interval), each family
switchable. The coordinator ticks at the GCD of the enabled intervals and calls
only due groups; other data keeps its last value.

- Defaults and legacy options are unchanged: no per-family keys means
  `device_telemetry` decides all three, every interval follows the site
  interval, so upgraded installs make the same calls at the same times. The
  entry is not migrated in place; `polling.resolve_plan` interprets old options
  and the options form writes the new keys on save.
- Interval 0 means "follow the update interval". Rejected: a separate interval
  per family (more fields for little gain) and a single telemetry interval
  (cannot say "storage often, inverters rarely").
- Slow data (metadata, lifetime, alerts) is derived as max(6 h, site interval),
  not another option. Inventory stays at 12 hours.
- Site maximum raised from 360 to 1440 minutes so site data can be made rare
  (a daily snapshot is 60 credits a month); longer leaves "today" totals stale.
- The estimator warns above `FREE_TIER_MONTHLY_CREDITS` (2,000) but does not
  block, since other tiers differ. The estimate assumes every enabled family is
  present, and the form shows the estimate of the saved settings.

