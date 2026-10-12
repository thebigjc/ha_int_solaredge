# SolarEdge Monitoring API v2 facts

Sources, read 2026-09-29:

- Migrating from V1 to V2:
  <https://api-docs.solaredge.com/docs/basic-monitoring-api/m32bx376ka8mb-migrating-from-v1-to-v2>
- Authentication:
  <https://api-docs.solaredge.com/docs/developer-platform/g09jmvp5t2cok-authentication>
- Homeowner and Self-Access Guide:
  <https://api-docs.solaredge.com/docs/developer-platform/df8kx5j4z9vi0-homeowner-and-self-access-guide>
- Aggregation and Polling Strategy:
  <https://api-docs.solaredge.com/docs/basic-monitoring-api/2eb2zn9wi9y2p-aggregation-and-polling-strategy>
- The Basic Monitoring API OpenAPI document, version 2.0.1, exported from the
  same site's "Export" button. It is not committed here because it is
  SolarEdge's document.

"Documented" means the rule is stated in those sources. "Tested" means a unit
test exercises the code path against a fixture built from the documented
shape. Nothing in this table has been verified against the live API yet; see
[backlog.md](backlog.md).

## Lifecycle

| Fact | Status |
| --- | --- |
| V1 is planned to stop responding on November 3, 2026. | Documented |
| V1 keys do not work with V2; V2 issues new credentials in the developer console. | Documented |
| V2 has no Site API Key; homeowners use OAuth (Site Access), installers use a Fleet API Key. | Documented |
| Billing for paid tiers is held until November 1, 2026. | Documented |

## Authentication

| Fact | Status |
| --- | --- |
| Base URL `https://monitoringapi.solaredge.com/v2`. | Documented |
| Fleet Access sends `X-API-Key: <key>`; Site Access sends `Authorization: Bearer <token>`. | Documented, tested |
| Authorize URL `https://connect.solaredge.com/authorize`; scopes `SITE_DATA`, `DEVICE_DATA`; `access_duration` in months, 24 maximum. | Documented |
| Token URL `https://monitoringapi.solaredge.com/v2/oauth2/token` takes a JSON body with `grant_type`, `code` or `refresh_token`, `client_id`, `client_secret`. | Documented, tested |
| Access tokens last 7,200 seconds; each refresh returns a new refresh token and invalidates the old one. | Documented |
| Refresh tokens are valid 30 days. | Documented (homeowner guide) |
| The callback carries `code`, `site_id`, and optionally `external_id`. | Documented |
| SolarEdge Connect echoes the `state` parameter back on the callback. | Verified live 2026-10-12: it does not. |
| SolarEdge Connect ignores the `redirect_uri` of the authorize request and redirects to the registered URL; the token exchange accepts PKCE `code_verifier` and the registered `redirect_uri`. | Verified live 2026-10-12 |
| `GET /sites/{id}/storage/telemetry` with no query returns today's data at QUARTER_HOUR; `stateOfEnergy` is a 0-1 fraction although its unit says PERCENTAGE; the newest bucket is often null. | Verified live 2026-10-12 |
| `GET /sites` and `GET /alerts` accept only an API key, not an OAuth token. | Documented (OpenAPI `security` per operation) |
| Revocation: `POST /v2/oauth2/revoke-token` with `{"token": ...}`. | Documented, not used |

## Endpoints the integration calls

| Call | Parameters sent | Status |
| --- | --- | --- |
| `GET /sites` | `page`, `sites-in-page=1000` (maximum 1,000); a short page ends the loop | Documented, tested |
| `GET /sites/{id}` | none | Documented, tested |
| `GET /sites/{id}/overview` | none; defaults to midnight today site time until now | Documented, tested |
| `GET /sites/{id}/power` | `resolution=QUARTER_HOUR`, `unit=W`, `from`/`to` the last hour | Documented, tested |
| `GET /sites/{id}/energy` | `resolution=YEAR`, `unit=WH`, `from=<installation date>`, `to=now`; the yearly buckets are summed | Documented (`YEAR` is the only resolution with an unlimited span), tested |
| `GET /sites/{id}/devices` | `types=INVERTER&types=METER&types=BATTERY` | The default is `INVERTER` only (documented). The repeated-parameter encoding follows the OpenAPI default for arrays and is unverified against the live API. |
| `GET /sites/{id}/alerts` | `only-open=true`, `alerts-in-page=100` (maximum) | Documented, tested |
| `GET /sites/{id}/inverters/telemetry` | `resolution=HOUR`, `from=<midnight site time>`, `to=now` | Documented, tested |
| `GET /sites/{id}/meters/telemetry` | same | Documented, tested |
| `GET /sites/{id}/storage/telemetry` | same | Documented, tested |

Date parameters are ISO 8601 date-times without an offset, interpreted in site
local time (documented worked example: `from=2026-05-01T00:00:00`).

## Response shapes and units

| Fact | Status |
| --- | --- |
| Measurements envelope: `{period, unit, resolution, values: [{timestamp, value}]}`; the timestamp is the start of the bucket. | Documented, tested |
| Meter and storage telemetry series carry `unit` and `values` without their own `period`. | Documented (OpenAPI examples), tested |
| Telemetry metrics are present only when the requested resolution supports them. | Documented |
| Overview blocks `production` and `consumption` each carry their own `unit` (`WH`, `KWH`, `MWH`, `GWH`). | Documented, tested |
| Power units `W`, `KW`, `MW`; energy units `WH`, `KWH`, `MWH`, `GWH`. | Documented, tested |
| `stateOfEnergy` uses unit `PERCENTAGE` with values on a 0 to 100 scale. | Documented by example (65.2). The enum also lists `PERCENTAGE_100`, whose meaning is unverified; the integration reads either as 0 to 100. |
| Site power is net PV production: battery discharge is subtracted and charge added. | Documented |
| `/power`, `/energy`, and all three telemetry endpoints allow at most 12 hours at `QUARTER_HOUR`, 24 hours at `HOUR`, and one month at `DAY`; `YEAR` is unlimited. Longer spans return 400. The default window (midnight to now) therefore breaks `QUARTER_HOUR` after noon, which is why every call sends an explicit window. | Documented, tested |
| Telemetry voltage and frequency timestamps mark the end of the bucket; every other metric marks the start. Current is derived as power divided by voltage. | Documented |
| `stateOfEnergy` is `remainingEnergy` divided by usable (not nameplate) capacity. | Documented |
| Site `siteId` is an integer. | Documented |

## Errors and quota

| Fact | Status |
| --- | --- |
| API errors use RFC 7807 Problem Details with `detail`. | Documented, tested |
| 401: missing, invalid, expired, or revoked credentials. | Documented, tested |
| 403: no access to the site, missing scope, or tier gate. | Documented, tested |
| 429 with `x-ratelimit-remaining-minute: 0` and `retry-after`: per-minute limit. | Documented, tested |
| 429 with `x-ratelimit-remaining-minute` above 0 and no `retry-after`: monthly credit limit; resets at the billing cycle. | Documented, tested |
| Every call costs one credit. Free tier: 2,000 credits a month, 10 calls a minute, one application. | Documented |
| OAuth token endpoint errors use RFC 6749 `error` and `error_description` at HTTP 400. | Documented |

## V1 data that V2 no longer provides

Inverter: DC voltage, ground-fault resistance, power limit, temperature,
inverter mode, operation mode, apparent and reactive power, cos phi, per-phase
data, and line-to-line voltages. Battery: internal temperature and the numeric
battery state. Per-optimizer telemetry is not in V2 at all. (Documented.)
