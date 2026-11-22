"""Buttons for the WeLock BLE integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ble_transport import WeLockBLEError
from .const import CONF_DEVICE_NUMBER, DOMAIN
from .coordinator import WeLockCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the action buttons."""
    coordinator: WeLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WeLockOpenButton(coordinator, entry),
            WeLockRefreshButton(coordinator, entry),
        ]
    )


class _WeLockButton(CoordinatorEntity[WeLockCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.data[CONF_DEVICE_NUMBER])},
            name=self.coordinator.device_name,
            manufacturer="WeLock",
            model=self.coordinator.state.model or "Bluetooth lock",
            sw_version=self.coordinator.state.version,
        )


class WeLockOpenButton(_WeLockButton):
    """Explicit "open" action (same command as the lock entity)."""

    _attr_translation_key = "open"
    _attr_icon = "mdi:lock-open-variant"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_open"

    async def async_press(self) -> None:
        try:
            opened = await self.coordinator.async_open()
        except WeLockBLEError as err:
            raise HomeAssistantError(f"cannot open: {err}") from err
        if not opened:
            raise HomeAssistantError("the lock refused to open")


class WeLockRefreshButton(_WeLockButton):
    """Refresh the cloud-side details (battery, model, firmware, MAC)."""

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = None

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_refresh"

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_refresh_lock_info()
        except Exception as err:  # noqa: BLE001 - surfaced to the user
            raise HomeAssistantError(f"cannot refresh lock data: {err}") from err
        await self.coordinator.async_request_refresh()
