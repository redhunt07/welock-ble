"""Diagnostics support for the WeLock BLE integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_REFRESH_TOKEN, CONF_TOKEN, DOMAIN

_REDACTED = ("password", "token", "refresh_token")


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """Hide credentials but keep their presence and length visible."""
    clean = dict(data)
    for key in _REDACTED:
        value = clean.get(key)
        if value:
            clean[key] = f"redacted ({len(str(value))} chars)"
        elif key in clean:
            clean[key] = None
    return clean


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    state: dict[str, Any] = {}
    if coordinator is not None:
        lock = coordinator.state
        state = {
            "battery": lock.battery,
            "lock_status": lock.lock_status,
            "bluetooth_address": lock.mac,
            "model": lock.model,
            "version": lock.version,
            "reachable": lock.link_ok,
            "last_token": lock.last_command,
            "last_reply": lock.last_reply,
            "cloud_device_info": lock.extra,
        }
    return {
        "entry": {
            "title": entry.title,
            "data": _redact(dict(entry.data)),
            "options": dict(entry.options),
        },
        "state": state,
    }
