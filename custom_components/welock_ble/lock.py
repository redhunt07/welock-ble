"""Lock platform for the WeLock BLE integration."""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ble_transport import WeLockBLEError
from .const import (
    CONF_AUTO_LOCK_SECONDS,
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NUMBER,
    DEFAULT_AUTO_LOCK_SECONDS,
    DOMAIN,
)
from .coordinator import WeLockCoordinator

_LOGGER = logging.getLogger(__name__)


def _auto_lock_seconds(entry: ConfigEntry) -> int:
    """Auto-lock window: WeLock locks are spring-latch and relock themselves."""
    try:
        return int(
            entry.options.get(CONF_AUTO_LOCK_SECONDS, DEFAULT_AUTO_LOCK_SECONDS)
        )
    except (TypeError, ValueError):
        return DEFAULT_AUTO_LOCK_SECONDS


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the lock entity."""
    coordinator: WeLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeLockLockEntity(coordinator, entry)])


class WeLockLockEntity(CoordinatorEntity[WeLockCoordinator], LockEntity):
    """A WeLock lock driven over a generic Bluetooth adapter."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_lock"
        self._attr_is_locked = True
        self._attr_is_unlocking = False
        self._cancel_auto_lock = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.data[CONF_DEVICE_NUMBER])},
            name=self.coordinator.device_name,
            manufacturer="WeLock",
            model="Bluetooth lock",
            sw_version=self.coordinator.state.version,
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        return {
            "ble_address": self._entry.data.get(CONF_BLE_ADDRESS),
            "device_number": self._entry.data.get(CONF_DEVICE_NUMBER),
            "lock_status": self.coordinator.state.lock_status,
            "last_command": self.coordinator.state.last_command,
            "last_reply": self.coordinator.state.last_reply.get("raw"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        status = self.coordinator.state.lock_status
        if status is not None and not self._attr_is_unlocking:
            self._attr_is_locked = status != 0
        super()._handle_coordinator_update()

    async def async_unlock(self, **kwargs) -> None:
        """Open the lock: the cloud derives the token from the live challenge."""
        try:
            opened = await self.coordinator.async_open()
        except WeLockBLEError as err:
            raise HomeAssistantError(f"cannot open {self.coordinator.device_name}: {err}") from err
        if not opened:
            raise HomeAssistantError(f"{self.coordinator.device_name} refused to open")
        self._attr_is_locked = False
        self._attr_is_unlocking = True
        self.async_write_ha_state()
        if self._cancel_auto_lock is not None:
            self._cancel_auto_lock()
        self._cancel_auto_lock = async_call_later(
            self.hass, _auto_lock_seconds(self._entry), self._finish_unlock
        )

    async def _finish_unlock(self, _now) -> None:
        self._attr_is_unlocking = False
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_lock(self, **kwargs) -> None:
        """No-op: WeLock locks relock themselves."""
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending auto-lock timer when the entity is removed."""
        if self._cancel_auto_lock is not None:
            self._cancel_auto_lock()
            self._cancel_auto_lock = None
