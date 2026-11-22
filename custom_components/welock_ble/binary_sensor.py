"""Binary sensors for the WeLock BLE integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_ADDRESS, CONF_DEVICE_NUMBER, DOMAIN
from .coordinator import WeLockCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the lock's binary sensors."""
    coordinator: WeLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeLockReachableSensor(coordinator, entry)])


class WeLockReachableSensor(CoordinatorEntity[WeLockCoordinator], BinarySensorEntity):
    """Result of the last Bluetooth exchange with the lock.

    The integration connects on demand, so this reflects the most recent open
    attempt: ``unknown`` until the first command has been sent.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_reachable"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.data[CONF_DEVICE_NUMBER])},
            name=self.coordinator.device_name,
            manufacturer="WeLock",
            model=self.coordinator.state.model or "Bluetooth lock",
            sw_version=self.coordinator.state.version,
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.state.link_ok

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"ble_address": self._entry.data.get(CONF_BLE_ADDRESS)}
