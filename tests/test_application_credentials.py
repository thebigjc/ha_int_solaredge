"""Tests for the application credentials platform."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.solaredge_v2.application_credentials import (
    async_get_description_placeholders,
)


async def test_description_placeholders(hass: HomeAssistant) -> None:
    """The credentials dialog links the developer console and the redirect URL."""
    placeholders = await async_get_description_placeholders(hass)
    assert placeholders["developer_console"] == "https://developer.solaredge.com"
    assert placeholders["redirect_url"] == (
        "https://<your-home-assistant-host>/api/solaredge_v2/oauth/callback"
    )


async def test_description_placeholders_with_external_url(hass: HomeAssistant) -> None:
    """The dialog shows the exact URL to register in the SolarEdge console."""
    hass.config.external_url = "https://ha.example.com:8443"
    placeholders = await async_get_description_placeholders(hass)
    assert placeholders["redirect_url"] == "https://ha.example.com:8443/api/solaredge_v2/oauth/callback"
