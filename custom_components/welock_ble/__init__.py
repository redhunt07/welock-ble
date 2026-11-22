"""WeLock BLE — control WeLock locks through a generic Bluetooth adapter."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import CONF_REFRESH_TOKEN, CONF_TOKEN, DOMAIN
from .coordinator import WeLockCoordinator
from .welock_cloud import WeLockAuthError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]


def _store_token(hass: HomeAssistant, entry: ConfigEntry, coordinator: WeLockCoordinator) -> None:
    """Persist a freshly issued access token in the config entry."""
    token = coordinator.cloud.token
    refresh = coordinator.cloud.refresh_token
    if token == entry.data.get(CONF_TOKEN) and refresh == entry.data.get(CONF_REFRESH_TOKEN):
        return
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_TOKEN: token, CONF_REFRESH_TOKEN: refresh},
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a WeLock lock from a config entry."""
    coordinator = WeLockCoordinator(hass, entry)
    # The token obtained during the config flow is stored in the entry: reusing
    # it avoids a password round-trip at every restart (and a fresh WhatsApp
    # handshake when the code login method was chosen).
    coordinator.cloud.set_token(
        entry.data.get(CONF_TOKEN), entry.data.get(CONF_REFRESH_TOKEN)
    )
    try:
        if not coordinator.cloud.token:
            await coordinator.cloud.login()
            _store_token(hass, entry, coordinator)
        await coordinator.async_refresh_lock_info()
        await coordinator.async_config_entry_first_refresh()
    except WeLockAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001 - surfaced as ConfigEntryNotReady
        raise ConfigEntryNotReady(f"cannot reach the WeLock cloud: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Options (polling interval, auto-lock window) are applied by reloading the
    # entry, so the coordinator and the entities are rebuilt with the new values.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry after its options or data changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
