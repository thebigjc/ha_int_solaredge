"""Make SolarEdge requests and responses safe to write to a debug log."""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
import json
import re
from typing import Any

MASK = "***"
MAX_BODY = 500
MAX_VALUE = 200

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "client_secret",
        "code",
        "code_verifier",
        "id_token",
        "refresh_token",
        "token",
    }
)
_PUBLIC_QUERY_KEYS = frozenset({"error", "error_description", "error_uri", "site_id"})
_TRACE_MARKERS = ("request-id", "requestid", "trace-id", "traceid", "cf-ray", "amz-cf-id")
# Opaque credentials are long unbroken runs; ordinary words and URLs are not.
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: MASK if k.lower() in _SECRET_KEYS else _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def safe_body(text: str) -> str:
    """Return a request or response body with credentials masked, truncated for a log."""
    with contextlib.suppress(ValueError):
        text = json.dumps(_mask(json.loads(text)))
    return _LONG_TOKEN.sub(MASK, text)[:MAX_BODY]


def safe_query(query: Mapping[str, str]) -> dict[str, str]:
    """Return callback parameters, keeping values only for the non-secret ones."""
    return {k: v[:MAX_VALUE] if k in _PUBLIC_QUERY_KEYS else MASK for k, v in query.items()}


def trace_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return only the response headers a provider can use to find a request in its logs."""
    return {k: v for k, v in headers.items() if any(m in k.lower() for m in _TRACE_MARKERS)}
