# Changelog

## Unreleased

- Polling is configurable per data group. New options: inverter, meter, and
  storage telemetry switches, an inverter and meter interval, a storage
  interval (0 follows the update interval), and an update interval of up to
  1440 minutes. The options form shows the estimated credits a month and warns
  above the Free tier's 2,000. Defaults and the old `device_telemetry` option
  behave exactly as before (true means all three families). Slow data follows
  the update interval when that is longer than six hours.

- Windowed requests (power, energy, inverter and meter telemetry) send `from`/`to`
  with a UTC offset, fixing the HTTP 400 "Failed to convert ... [Instant]" setup
  failure (upstream issue 7).
- Site Access sign-in no longer depends on `state`, which SolarEdge does not
  return (upstream issues 6, 8, 15). The integration serves its own callback at
  `/api/solaredge_v2/oauth/callback`, uses PKCE S256, and takes `site_id` from
  the redirect. Register that URL (on your HA external URL) as the application's
  Redirect URI. Reauthentication uses the same path.
- Battery state of energy is now percent (the API sends a 0-1 fraction). Added
  site battery state of energy (capacity-weighted) and battery power sensors,
  per-battery power, `data_timestamp` and `data_timestamp_unix` attributes, and
  entities only for batteries that report data.
- Storage telemetry is requested without `from`/`to`, which avoids the HTTP 400
  "Failed to convert ... [Instant]" error.

- Added tests proving that the API key, access token, refresh token, and
  client secret never reach a log line, an error message, or a request URL
  when the site call or the token refresh fails. Prompted by the core V1
  `solaredge` integration writing its API key to the log on a 503; see
  docs/security.md (Logs) and docs/decisions.md.
- Test fixture `mock_api` accepts an exception to fail a route at the
  transport level.

## 2026.09.29.2

- First release. Reads a SolarEdge site through the Monitoring API v2 with
  Site Access (OAuth 2.0) or Fleet Access (App API Key), replacing the V1 API
  that SolarEdge switches off on November 3, 2026.
