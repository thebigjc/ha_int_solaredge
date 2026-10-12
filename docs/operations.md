# Operations

## Configuration keys

Stored in `ConfigEntry.data` (connection):

| Key | Access type | Meaning |
| --- | --- | --- |
| `auth_type` | both | `oauth` or `api_key`; selects the header provider at setup. |
| `site_id` | both | Integer SolarEdge site ID; also the entry's unique ID (as a string). |
| `api_key` | Fleet Access | App API Key sent as `X-API-Key`. |
| `auth_implementation`, `token` | Site Access | Written by Home Assistant's OAuth helper; `token` holds the access and refresh tokens and `expires_at`. |

Stored in `ConfigEntry.options` (behavior); changing them reloads the entry:

| Key | Default | Bounds | Consequence |
| --- | --- | --- | --- |
| `scan_interval_minutes` | 60 | 15 to 1440 | Site interval (overview and power). Lower than 15 spends credits on an unchanged bucket, so the form does not allow it. 1440 (once a day) is the maximum: a slower site poll leaves "today" totals stale for more than a day. |
| `telemetry_inverters`, `telemetry_meters`, `telemetry_storage` | false | | Create that family's sensors and call its endpoint; one credit per call. |
| `telemetry_interval_minutes` | 0 | 0 or 15 to 1440 | Inverter and meter interval. 0 follows `scan_interval_minutes`. |
| `storage_interval_minutes` | 0 | 0 or 15 to 1440 | Storage interval. 0 follows `scan_interval_minutes`. |
| `device_telemetry` | false | | Legacy switch, read only when the three per-family keys are absent: true means all three on. The options form rewrites the entry with the new keys. |

The coordinator ticks at the greatest common divisor of the site interval and
the enabled telemetry intervals and calls only the groups that are due. Slow
data (metadata, lifetime energy, alerts) is read every six hours, or at the
site interval when that is longer. Inventory stays at 12 hours. Data of a group
that is not due keeps its last value, so entities do not flap. The options form
shows the estimated monthly cost (`polling.estimate_monthly_credits`) and warns
above 2,000.

## Credit budget

Every call costs one credit (documented). A 30-day month has 720 hours.

| Cadence | Calls per run | Runs in 30 days | Credits |
| --- | --- | --- | --- |
| Fast, 60-minute interval | 2 | 720 | 1,440 |
| Slow, every 6 hours | 3 | 120 | 360 |
| Inventory, every 12 hours | 1 | 60 | 60 |
| **Defaults total** | | | **1,860** |

Adding per-device telemetry costs one more credit per update for each device
family present. For a site with one inverter family and nothing else, that is
720 more at 60 minutes (2,580 total, over the Free tier's 2,000) or 480 more
at 90 minutes (fast 3 x 480 = 1,440, plus 420, for 1,860 total). A site with
inverter, meter, and battery at 120 minutes uses 5 x 360 + 420 = 2,220.

Other combinations (30-day month, every enabled family present):

| Settings | Credits |
| --- | --- |
| Defaults (site 60 minutes, no telemetry) | 1,860 |
| Battery only every 30 minutes, site every 12 hours | 1,800 (site 120, storage 1,440, slow 180, inventory 60) |
| Everything every 30 minutes | 7,620 |

Setup and every reload spend two credits (metadata and inventory) plus one
full first refresh (five credits, more with telemetry), so repeated reloads
while troubleshooting count against the month.

When the quota runs out, the API answers 429 with no `retry-after`; the
integration raises the `credit_limit` repair issue and retries every six hours
until the billing cycle resets.

## Local gate

The Home Assistant test harness imports `fcntl`, so the suite runs under WSL
on Windows. The venv is created once:

```bash
python3.14 -m venv ~/solaredgevenv
~/solaredgevenv/bin/pip install -r requirements-dev.txt
~/solaredgevenv/bin/pip install -r requirements-core.txt
```

The harness pins the beta core it was cut from, so the stable core is pinned
in its own file and installed second; CI asserts the installed version. Then,
from the repository root:

```bash
~/solaredgevenv/bin/ruff check .
~/solaredgevenv/bin/mypy --config-file mypy.ini
~/solaredgevenv/bin/pytest --cov tests/
python3 scripts/build_release_artifacts.py --validate-only
```

Coverage must be 100 percent, lines and branches (`pyproject.toml`).

## Releases

A merge to `main` is the only release path. `release.yml` reruns the test and
validation workflows, reads the version from `manifest.json` through
`.release.json`, and publishes `v<version>` with `solaredge_v2.zip`, an SPDX
SBOM, `SHA256SUMS`, and provenance and SBOM attestations. `prepare-release.yml`
opens a version-bump pull request when release paths changed since the last
tag; it needs the release GitHub App's client ID variable and private key
secret. Versions are CalVer `YYYY.MM.DD.N` in America/Chicago time.
