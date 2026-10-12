# SolarEdge Monitoring API v2 for Home Assistant

A Home Assistant custom integration that reads a SolarEdge site through the
SolarEdge Monitoring API **v2** (`https://monitoringapi.solaredge.com/v2`).
SolarEdge has announced that the V1 API (the `?api_key=` query-string API that
the Home Assistant core `solaredge` integration uses) stops responding on
**November 3, 2026**. V1 site API keys do not carry over: V2 issues new
credentials, and homeowners authorize through OAuth 2.0.

[SolarEdge](https://www.solaredge.com) makes inverters, power optimizers,
batteries, and meters for residential and commercial solar systems. Its cloud
monitoring platform collects telemetry from each site; this integration reads
that platform.

Design notes, protocol facts, and operational detail live under
[docs/](docs/README.md).

## What it provides

One config entry per site. Each entry creates a site device and, when
per-device telemetry is on, one device per inverter, meter, and battery in the
site inventory.

| Sensor | Device | Source (V2 endpoint) | Enabled by default |
| --- | --- | --- | --- |
| Current power (W) | Site | `GET /sites/{id}/power`, latest 15-minute bucket | Yes |
| Production today, Consumption today | Site | `GET /sites/{id}/overview` | Yes |
| Exported to grid today, Imported from grid today | Site | `GET /sites/{id}/overview` | Yes |
| Self-consumed, Charged to storage, Consumed from storage today | Site | `GET /sites/{id}/overview` | No |
| Lifetime production | Site | `GET /sites/{id}/energy`, yearly buckets since installation | Yes |
| Peak power, Last update from site, Open alerts | Site (diagnostic) | `GET /sites/{id}`, `GET /sites/{id}/alerts` | Yes |
| AC power (hourly average), Energy today | Inverter | `GET /sites/{id}/inverters/telemetry` | Yes |
| AC voltage, AC current, Grid frequency | Inverter | same | No |
| Production, consumption, import, export power | Meter | `GET /sites/{id}/meters/telemetry` | Yes |
| Production, consumption, import, export today | Meter | same | No |
| Battery state of energy (capacity-weighted, %), Battery power (W, positive = charging) | Site | `GET /sites/{id}/storage/telemetry` | Yes (only when a battery reports data) |
| Power (W, positive = charging), Charge power, Discharge power, State of energy (%), Remaining energy | Battery (one device per battery that reports data) | same | Yes |
| Charged today, Discharged today | Battery | same | No |

The "today" sensors reset at midnight site time and use the `total_increasing`
state class, so they work in the Energy dashboard.

Battery sensors carry `data_timestamp` (ISO 8601) and `data_timestamp_unix`
(epoch seconds) attributes: the start of the 15-minute bucket the value was
measured in. SolarEdge reports it 15 to 30 minutes late, so this is the true
measurement time. The newest bucket is often still empty, so the latest filled
bucket is used. The site state of energy weights each battery by its capacity
(remaining energy divided by its state of energy).

## Setting up Site Access

1. Register the Redirect URI shown in the Add Application Credentials dialog
   (`<HA URL>/api/solaredge_v2/oauth/callback`) in your SolarEdge application.
2. Add the Client ID and Client Secret under Application credentials.
3. Add the integration, choose Site Access, and approve the site on the
   SolarEdge page. The window then returns to Home Assistant and the entry is
   created. Keep only one setup or reauthentication open at a time.

## Use cases

- Keep solar production and grid import/export in the Energy dashboard after
  the V1 API is switched off.
- Get a notification when SolarEdge opens an alert on an inverter, meter, or
  battery (see the blueprint below).
- Watch battery state of energy alongside other home automations.

## Supported and unsupported devices

Supported: any SolarEdge site visible in the SolarEdge monitoring platform,
with the inverters, meters, and batteries that its inventory reports.

Not supported: per-optimizer (panel-level) data. The V2 Monitoring API lists
optimizers in the inventory but exposes no optimizer telemetry. Panel-level
data remains available only through the monitoring web portal, which the
[solaredgeoptimizers](https://github.com/AndrewTapp/solaredgeoptimizers)
custom integration reads; that integration does not use the V1 API and is not
affected by the V1 shutdown. Live power flow, per-destination energy, Weather
Guard, and backup reserve are Advanced Monitoring endpoints limited to the
Business Pro and Enterprise tiers and are not implemented.

## Prerequisites

- Home Assistant 2026.9.0 or newer.
- A SolarEdge developer account at <https://developer.solaredge.com>. The Free
  tier (2,000 credits a month) is enough for one site with the default
  options.
- One of:
  - **Site Access (homeowners).** A Site Access application with the
    `SITE_DATA` and `DEVICE_DATA` scopes and the Redirect URI
    `https://<your-ha-host>/api/solaredge_v2/oauth/callback`, where the host is
    your Home Assistant **external URL** (or internal URL if no external one is
    set), exactly as configured under **Settings > System > Network**.
    SolarEdge ignores any redirect sent with the sign-in request and always uses
    the registered one, and it does not return `state`, so the standard
    `my.home-assistant.io` redirect cannot work. Your browser must be able to
    reach that URL. Sign in to the developer
    console with your mySolarEdge account. SolarEdge's
    [Homeowner and Self-Access Guide](https://api-docs.solaredge.com/docs/developer-platform/df8kx5j4z9vi0-homeowner-and-self-access-guide)
    shows each screen.
  - **Fleet Access (installers and fleet owners).** A Fleet Access
    application's App API Key.

## Installation

1. In HACS, add this repository as a custom repository of type Integration,
   then download **SolarEdge Monitoring API v2**.
2. Restart Home Assistant.
3. For Site Access only: go to **Settings > Devices & services > three-dot
   menu > Application credentials**, add credentials for **SolarEdge
   Monitoring API v2**, and enter the Client ID and Client Secret of your
   Site Access application.
4. Go to **Settings > Devices & services > Add integration** and choose
   **SolarEdge Monitoring API v2**.

## Configuration parameters

Asked during setup:

| Parameter | Access type | Meaning |
| --- | --- | --- |
| Access type | both | Site Access (OAuth) or Fleet Access (App API Key). |
| Site ID | Site Access | Taken from the `site_id` in SolarEdge's redirect and checked with `GET /sites/{id}`. Asked for only if the redirect did not carry it. |
| App API Key | Fleet Access | Sent in the `X-API-Key` header. The flow lists every site the key can read. |
| Site | Fleet Access | Chosen from that list when there is more than one. |

Options (**Configure** on the entry):

| Option | Default | Meaning |
| --- | --- | --- |
| Update interval | 60 minutes (15 to 1440) | How often production, consumption, and power are read (2 credits each time). |
| Inverter, meter, storage telemetry | Off | Each family adds its sensors and one credit per read. Requires the `DEVICE_DATA` scope for Site Access. |
| Inverter and meter interval | 0 (follow the update interval; else 15 to 1440) | Minutes between inverter and meter reads. |
| Storage interval | 0 (follow the update interval; else 15 to 1440) | Minutes between battery reads. |

The form shows the estimated credits a month for the settings and warns above
2,000, the Free tier figure. Entries saved with the older single
"per-device telemetry" switch keep their behaviour: on means all three
families, at the update interval.

## How data is updated

Every API call costs one credit. The integration polls on three cadences:

- Every update interval: `overview` and `power` (2 credits).
- Every telemetry interval: one call per enabled device family present
  (inverters and meters share an interval, storage has its own; both follow the
  update interval by default).
- Every 6 hours, or the update interval if longer: site metadata, lifetime
  energy, and open alerts (3 credits).
- Every 12 hours: the device inventory (1 credit).

With the defaults, one site uses about 1,860 credits in a 30-day month. The
arithmetic and other combinations are in
[docs/operations.md](docs/operations.md). The API's finest resolution is 15
minutes, so an interval shorter than that returns the same bucket.

Worked example: you read PV and grid power locally over Modbus and want only
the battery state of charge. Turn on storage telemetry alone, set the storage
interval to 30 and the update interval to 720. That is storage 1,440, site 120,
site details 180, and inventory 60: about 1,800 credits a month. Turning on
all three families at 30 minutes would be about 7,620. Data of a group that is
not due keeps its last value between reads.

## Automation example

The blueprint
[`solaredge_open_alert_notify.yaml`](blueprints/automation/solaredge_open_alert_notify.yaml)
sends a notification when the Open alerts sensor rises above zero.

## Known limitations

- The V1 API is not used at all; existing core `solaredge` entities are not
  migrated. Statistics from the core integration stay under their old entity
  IDs.
- No optimizer (panel-level) data, as described above.
- V2 dropped several V1 inverter and battery fields (inverter temperature,
  inverter mode, per-phase data, battery internal temperature, battery
  state code); they cannot be offered.
- Site current power is the average of the latest 15-minute bucket, not an
  instantaneous reading. Inverter, meter, and battery power are hourly
  averages, because the API allows at most 12 hours of 15-minute data and the
  same call must cover the whole day for the "today" totals.
- Right after midnight site time the day's first bucket may not exist yet, so
  device values read unknown until it does.
- SolarEdge Site Access grants last at most 24 months; the integration
  requests the maximum. Refresh tokens expire after 30 days without use, so an
  instance that is off for longer than that needs reauthentication.

## Troubleshooting

| Symptom | Cause | Resolution |
| --- | --- | --- |
| "Add application credentials first" when choosing Site Access | No Client ID and Secret stored | Add them under Application credentials (Installation step 3). |
| Redirect error after approving | The application's Redirect URI does not match | Set it to exactly `<your HA URL>/api/solaredge_v2/oauth/callback` in the developer console. |
| "Invalid parameters given" on my.home-assistant.io | An old setup registered the my.home-assistant.io redirect, which SolarEdge cannot complete | Change the application's Redirect URI as above and start the setup again. |
| Callback page says no sign-in is waiting, or more than one | The flow expired (10 minutes), was removed, or two sign-ins are pending | Remove stale setup or reauthentication flows, then start again. |
| "These credentials cannot read this site" | Wrong Site ID, or the grant covers another site | Enter the Site ID shown on the consent screen, or approve the correct site. |
| Repair issue "monthly API credits used up" | The developer account's credits are spent | Raise the interval or turn off per-device telemetry; updates resume at the next billing cycle. |
| Repair issue "device telemetry not permitted" | The grant lacks `DEVICE_DATA` | Reconfigure and approve both scopes, or turn telemetry off. |
| Entities unavailable and a reauthentication prompt | Token or key rejected (HTTP 401) | Follow the prompt; Site Access reruns sign-in, Fleet Access asks for a new key. |

Diagnostics (**three-dot menu > Download diagnostics**) include the last parsed
data with the key and tokens redacted.

## Removal

1. **Settings > Devices & services > SolarEdge Monitoring API v2 > three-dot
   menu > Delete** for each site.
2. For Site Access, optionally remove the application credential and revoke
   the grant: in the SolarEdge developer console, delete or rotate the Site
   Access application, or revoke the token with
   `POST https://monitoringapi.solaredge.com/v2/oauth2/revoke-token`.
3. Remove the repository from HACS and restart Home Assistant.

## Quality scale

The integration targets the Platinum tier; the rule-by-rule status is in
[`quality_scale.yaml`](custom_components/solaredge_v2/quality_scale.yaml).
