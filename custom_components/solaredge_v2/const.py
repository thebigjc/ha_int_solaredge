"""Constants for the SolarEdge Monitoring API v2 integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "solaredge_v2"
LOGGER = logging.getLogger(__package__)
MANUFACTURER: Final = "SolarEdge"

OAUTH2_AUTHORIZE: Final = "https://connect.solaredge.com/authorize"
OAUTH2_TOKEN: Final = "https://monitoringapi.solaredge.com/v2/oauth2/token"
OAUTH2_SCOPES: Final = "SITE_DATA DEVICE_DATA"
OAUTH2_ACCESS_DURATION_MONTHS: Final = "24"
OAUTH_CALLBACK_PATH: Final = "/api/solaredge_v2/oauth/callback"
OAUTH_PENDING_TTL: Final = timedelta(minutes=10)

CONF_SITE_ID: Final = "site_id"
CONF_AUTH_TYPE: Final = "auth_type"
AUTH_TYPE_API_KEY: Final = "api_key"
AUTH_TYPE_OAUTH: Final = "oauth"

CONF_SCAN_INTERVAL_MINUTES: Final = "scan_interval_minutes"
CONF_DEVICE_TELEMETRY: Final = "device_telemetry"
DEFAULT_SCAN_INTERVAL_MINUTES: Final = 60
MIN_SCAN_INTERVAL_MINUTES: Final = 15
MAX_SCAN_INTERVAL_MINUTES: Final = 360
DEFAULT_DEVICE_TELEMETRY: Final = False

SLOW_UPDATE_INTERVAL: Final = timedelta(hours=6)
INVENTORY_UPDATE_INTERVAL: Final = timedelta(hours=12)

ISSUE_CREDIT_LIMIT: Final = "credit_limit"
ISSUE_SCOPE_MISSING: Final = "device_scope_missing"
