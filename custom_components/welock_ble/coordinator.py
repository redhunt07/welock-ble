"""Coordinator for the WeLock BLE integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble_transport import WeLockBLE, WeLockBLEError
from .const import (
    CONF_ACCOUNT,
    CONF_BLE_ADDRESS,
    CONF_DEVICE_MAC,
    CONF_DEVICE_NAME,
    CONF_DEVICE_NUMBER,
    CONF_IDENTIFIER,
    CONF_TOKEN,
    CONF_PAIR,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .welock_cloud import WeLockApiError, WeLockAuthError, WeLockCloud

_LOGGER = logging.getLogger(__name__)


def entry_password_present(entry: ConfigEntry) -> bool:
    """Return True when the entry can perform a password login."""
    return bool(entry.data.get(CONF_PASSWORD))


@dataclass
class WeLockState:
    """Latest known state of a lock."""

    battery: int | None = None
    lock_status: int | None = None
    name: str = ""
    device_number: str = ""
    mac: str | None = None
    model: str | None = None
    version: str | None = None
    last_command: str | None = None
    last_reply: dict[str, Any] = field(default_factory=dict)
    link_ok: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_battery(data: Any) -> int | None:
    """Search a device payload for a battery-like value."""
    if not isinstance(data, dict):
        return None
    for key in ("power", "battery", "Battery", "electricity", "powerNum", "batteryNum"):
        if key in data:
            parsed = _to_int(data[key])
            if parsed is not None:
                return parsed
    for value in data.values():
        if isinstance(value, dict):
            found = _find_battery(value)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                found = _find_battery(item)
                if found is not None:
                    return found
    return None


def _lock_radio(info: Any, device_name: str) -> dict[str, Any] | None:
    """Return the ``ble`` entry that belongs to the lock itself.

    ``device/GetDeivceInfo`` lists every radio of the account: the lock and,
    when the user owns one, the WIFIBOX gateway.  The gateway reports power 0
    and a different firmware, so the entry must be selected by name and never
    by position (the order is not stable).
    """
    if not isinstance(info, dict):
        return None
    for radio in info.get("ble") or []:
        if not isinstance(radio, dict):
            continue
        if str(radio.get("deviceName") or "") == device_name:
            return radio
    return None


def _compose_version(radio: dict[str, Any] | None) -> str | None:
    """Build a firmware string from a ``ble`` entry of the lock."""
    if not radio:
        return None
    version = radio.get("version")
    if not isinstance(version, dict):
        return None
    parts: list[str] = []
    if version.get("modelName"):
        parts.append(str(version["modelName"]))
    if version.get("modelNumber") is not None:
        parts.append(f"model {version['modelNumber']}")
    if version.get("hardware") is not None:
        parts.append(f"hw {version['hardware']}")
    if version.get("firmware") is not None:
        parts.append(f"fw {version['firmware']}")
    return " / ".join(parts) or None


def _find_status(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in ("lockStatus", "LockStatus", "lockState", "state", "status"):
        if key in data:
            parsed = _to_int(data[key])
            if parsed is not None:
                return parsed
    return None


class WeLockCoordinator(DataUpdateCoordinator[WeLockState]):
    """Owns the cloud session and the on-demand BLE link."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.device_number: str = entry.data[CONF_DEVICE_NUMBER]
        self.device_name: str = entry.data[CONF_DEVICE_NAME]
        address = entry.data.get(CONF_BLE_ADDRESS) or entry.data.get(CONF_DEVICE_MAC)
        self.ble = WeLockBLE(
            hass,
            str(address),
            pair=bool(entry.data.get(CONF_PAIR, False)),
        )
        self.cloud = WeLockCloud(
            async_get_clientsession(hass),
            entry.data[CONF_ACCOUNT],
            entry.data.get(CONF_PASSWORD, ""),
            entry.data[CONF_IDENTIFIER],
            debug=bool(entry.data.get("debug", False)),
        )
        self.cloud.set_token(
            entry.data.get(CONF_TOKEN), entry.data.get(CONF_REFRESH_TOKEN)
        )

        interval = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.device_name}",
            update_interval=timedelta(seconds=interval),
        )
        self.state = WeLockState(
            name=self.device_name,
            device_number=self.device_number,
            mac=str(address) if address else None,
        )

    async def _async_update_data(self) -> WeLockState:
        """Refresh cloud-side information about the lock."""
        try:
            if not self.cloud.token:
                if not entry_password_present(self.entry):
                    raise ConfigEntryAuthFailed(
                        "stored token missing and no password available"
                    )
                await self.cloud.login()
            info = await self.cloud.async_get_device_info(self.device_number)
        except WeLockAuthError as err:
            raise ConfigEntryAuthFailed(f"authentication failed: {err}") from err
        except WeLockApiError as err:
            raise UpdateFailed(f"cloud request failed: {err}") from err

        self.state.extra = info
        radio = _lock_radio(info, self.device_name)
        if radio is not None:
            self.state.battery = _to_int(radio.get("power"))
            self.state.version = _compose_version(radio) or self.state.version
        if self.state.battery is None:
            self.state.battery = _find_battery(info)
        self.state.lock_status = _find_status(info)
        if not self.state.mac:
            self.state.mac = info.get("deviceMac") if isinstance(info, dict) else None
        return self.state

    async def async_refresh_lock_info(self) -> None:
        """Fetch the BLE details (MAC address, firmware, battery) of the lock."""
        try:
            info = await self.cloud.async_get_lock_info(self.device_number)
        except WeLockApiError as err:
            _LOGGER.debug("GetLockDevice failed: %s", err)
            return
        if not isinstance(info, dict):
            return
        self.state.mac = info.get("deviceMac") or self.state.mac
        self.state.model = info.get("lockModel") or self.state.model
        self.state.version = info.get("version") or self.state.version
        battery = _to_int(info.get("power"))
        if battery is not None:
            self.state.battery = battery

    async def async_open(self) -> bool:
        """Open the lock over a direct BLE connection.

        The lock answers ``U0`` with a fresh challenge; the cloud derives the
        token from it, so ``/Command/UnLock`` is called with ``randomFactor``
        set to the challenge read as a little endian uint16. The resulting
        ``U1`` frame is then replayed verbatim over BLE.
        """
        if not self.state.mac and not self.entry.data.get(CONF_BLE_ADDRESS):
            raise WeLockBLEError("no BLE address known for this lock")
        await self._ensure_login()

        battery = self.state.battery if self.state.battery is not None else 0

        async def _frame_for(challenge: bytes) -> bytes:
            """Ask the cloud for the frame matching the live challenge."""
            random_factor = challenge[0] | (challenge[1] << 8)
            try:
                response = await self.cloud.async_get_unlock_command(
                    self.device_number,
                    self.device_name,
                    battery=battery,
                    random_factor=random_factor,
                    random_factor_data=None,
                )
            except WeLockApiError as err:
                raise WeLockBLEError(f"cloud unlock command failed: {err}") from err
            command = response.get("command") if isinstance(response, dict) else None
            if not command:
                raise WeLockBLEError("cloud returned no unlock command")
            return bytes.fromhex(command)

        try:
            result = await self.ble.async_unlock(command_provider=_frame_for)
        except WeLockBLEError:
            self.state.link_ok = False
            self.async_set_updated_data(self.state)
            raise
        self.state.last_command = str(result.get("token"))
        self.state.last_reply = result
        self.state.link_ok = bool(result.get("success"))
        self.async_set_updated_data(self.state)
        _LOGGER.debug("unlock reply: %s", result)
        return bool(result.get("success"))

    async def _ensure_login(self) -> None:
        """Make sure the cloud session is usable before a command call."""
        if self.cloud.token:
            return
        if not entry_password_present(self.entry):
            raise WeLockBLEError("stored token missing and no password available")
        try:
            await self.cloud.login()
        except (WeLockAuthError, WeLockApiError) as err:
            raise WeLockBLEError(f"cloud login failed: {err}") from err
