"""Sensors for the WeLock BLE integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_ADDRESS, CONF_DEVICE_NUMBER, DOMAIN
from .coordinator import WeLockCoordinator


def _firmware(state_extra: Any) -> str | None:
    """Compose a firmware string from ``device/GetDeivceInfo``'s ``ble`` list."""
    if not isinstance(state_extra, dict):
        return None
    for radio in state_extra.get("ble") or []:
        if not isinstance(radio, dict):
            continue
        version = radio.get("version")
        if not isinstance(version, dict):
            continue
        parts: list[str] = []
        if version.get("modelName"):
            parts.append(str(version["modelName"]))
        if version.get("modelNumber") is not None:
            parts.append(f"model {version['modelNumber']}")
        if version.get("hardware") is not None:
            parts.append(f"hw {version['hardware']}")
        if version.get("firmware") is not None:
            parts.append(f"fw {version['firmware']}")
        if parts:
            return " / ".join(parts)
    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the lock's sensors."""
    coordinator: WeLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WeLockBatterySensor(coordinator, entry),
            WeLockStatusSensor(coordinator, entry),
            WeLockModelSensor(coordinator, entry),
            WeLockFirmwareSensor(coordinator, entry),
            WeLockMacSensor(coordinator, entry),
            WeLockDeviceNumberSensor(coordinator, entry),
            WeLockLastTokenSensor(coordinator, entry),
            WeLockLastReplySensor(coordinator, entry),
        ]
    )


class _WeLockSensor(CoordinatorEntity[WeLockCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"ble_address": self._entry.data.get(CONF_BLE_ADDRESS)}


class WeLockBatterySensor(_WeLockSensor):
    """Battery level reported by the WeLock cloud."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = None

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_battery"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.state.battery


class WeLockStatusSensor(_WeLockSensor):
    """Raw lock status code reported by the cloud."""

    _attr_translation_key = "status_code"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_status"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.state.lock_status


class WeLockModelSensor(_WeLockSensor):
    """Lock model reported by ``device/GetLockDevice``."""

    _attr_translation_key = "model"
    _attr_icon = "mdi:lock-outline"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_model"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.model


class WeLockFirmwareSensor(_WeLockSensor):
    """Hardware/firmware revision reported for the lock's radio."""

    _attr_translation_key = "firmware"
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_firmware"

    @property
    def native_value(self) -> str | None:
        return _firmware(self.coordinator.state.extra)


class WeLockMacSensor(_WeLockSensor):
    """Bluetooth address used to reach the lock."""

    _attr_translation_key = "mac"
    _attr_icon = "mdi:bluetooth"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_mac"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.mac or self._entry.data.get(CONF_BLE_ADDRESS)


class WeLockDeviceNumberSensor(_WeLockSensor):
    """WeLock device number of the lock."""

    _attr_translation_key = "device_number"
    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_number"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.device_number or None


class WeLockLastTokenSensor(_WeLockSensor):
    """Token used by the most recent open command."""

    _attr_translation_key = "last_token"
    _attr_icon = "mdi:key-variant"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_last_token"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.last_command


class WeLockLastReplySensor(_WeLockSensor):
    """Raw reply returned by the lock for the most recent command."""

    _attr_translation_key = "last_reply"
    _attr_icon = "mdi:message-text-outline"

    def __init__(self, coordinator: WeLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_NUMBER]}_last_reply"

    @property
    def native_value(self) -> str | None:
        reply = self.coordinator.state.last_reply or {}
        raw = reply.get("raw")
        return str(raw) if raw else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = dict(super().extra_state_attributes)
        reply = self.coordinator.state.last_reply or {}
        attrs.update(
            {
                "status": reply.get("status"),
                "success": reply.get("success"),
                "attempts": reply.get("attempts"),
                "token": reply.get("token"),
            }
        )
        return attrs
