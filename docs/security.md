# Security

## Trust boundaries

| Boundary | What crosses it | Control | Enforced or not |
| --- | --- | --- | --- |
| Home Assistant to `monitoringapi.solaredge.com` | API key or bearer token, site ID | HTTPS through Home Assistant's shared aiohttp session with certificate verification; credentials only in headers | Enforced by the client: no code path puts a credential in a URL, and tests assert that neither credential appears in any request URL, log line, or error message (see Logs below) |
| Browser to `connect.solaredge.com` | The user's mySolarEdge sign-in | SolarEdge's consent screen; Home Assistant never sees the password | Enforced by SolarEdge |
| OAuth callback to Home Assistant | Authorization code and signed `state` | Home Assistant verifies the JWT `state` before accepting a code | Enforced by core |
| Config entry storage | API key, access and refresh tokens, client secret (in application credentials) | `.storage` files readable by anything running as the Home Assistant user | Not protected against other code in the same process or on the same host |

## Credential scope

- A Fleet Access key carries every scope for every site associated with the
  account; SolarEdge applies no scope filter to API keys (documented). Treat
  it as account-wide read access.
- A Site Access token is limited to the approved site and the scopes chosen
  on the consent screen. Requesting only `SITE_DATA` narrows it further at the
  cost of device telemetry.
- Neither credential can change anything: the Basic Monitoring API is read
  only.

## Logs

On 2026-10-09 the core `solaredge` integration (V1 API) wrote its site API key
to the Home Assistant log: V1 carries the key in the query string, aiohttp's
`ClientResponseError` prints the full request URL, and the generic coordinator
logs `str(err)` when an update fails. This integration stays clear of that
path by construction, and `tests/test_secrets.py` pins each property:

- Credentials travel only in headers (`X-API-Key` or `Authorization`), so a
  URL in any error text carries no secret.
- The client never calls `raise_for_status()` on API responses. It builds its
  own error text from the HTTP status and the problem detail the API returns,
  so header values are never part of an exception string.
- The coordinator is named `solaredge_v2 <site id>`, so a failure log line
  identifies the site instead of printing an object address.
- The coordinator logs one error when an outage starts and nothing further
  until the next success (core behavior); a 429 carries SolarEdge's
  `Retry-After` into the next attempt and a spent monthly quota waits six hours.
- A failed token refresh raises an aiohttp error whose text names only the
  token URL; the refresh token and client secret stay in the JSON body.

The tests set the capture level to DEBUG, fail the site call with a 503 and
with a transport error, fail the token refresh the same two ways, and assert
that the API key, access token, refresh token, and client secret appear in no
captured record, in no `last_exception`, and in no request URL. They were
checked against a deliberate leak (interpolating the request headers into the
client's error text), which they catch.

Debug logging (`custom_components.solaredge_v2`) records each API path and status,
token and callback traffic, and error bodies for diagnosis. `api/redact.py` masks
secret fields and long opaque values first, `tests/test_debug_logging.py` pins this,
and successful API bodies are never logged.

## Diagnostics

Diagnostics redact `api_key`, `token`, `access_token`, and `refresh_token`.
They keep the site ID, site name, device serial numbers, and alert details,
which identify the installation; review a diagnostics file before sharing it
publicly.

## Revocation

Rotating or deleting the application in the SolarEdge developer console
invalidates its credentials. A single Site Access token can be revoked with
`POST /v2/oauth2/revoke-token`. The integration does not call revocation on
removal; that is listed in [backlog.md](backlog.md).
