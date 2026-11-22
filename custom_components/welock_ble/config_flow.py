"""Config flow for the WeLock BLE integration.

Two authentication modes are supported, matching the two login paths found in
the Android app (see ``docs/PROTOCOL.md``):

* ``password`` - ``POST /api/Account/token/welock`` with account/password.
* ``code``     - ``POST /api/Account/MessageCode/welock`` makes the server issue
  a ``WL_`` handshake code (``data.code``); the user sends it to the WeLock
  WhatsApp bot and ``POST /api/Account/MessageLogin/welock`` exchanges it for a
  token.  ``VerificationCodeLogin`` is the 6-digit SMS path, not this one.

The obtained access token is stored in the config entry so that Home Assistant
does not need the password (nor a fresh WhatsApp code) at every restart.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTH_MODE_CODE,
    AUTH_MODE_PASSWORD,
    CONF_ACCOUNT,
    CONF_AUTH_MODE,
    CONF_AUTO_LOCK_SECONDS,
    CONF_BLE_ADDRESS,
    CONF_DEVICE_MAC,
    CONF_DEVICE_NAME,
    CONF_DEVICE_NUMBER,
    CONF_IDENTIFIER,
    CONF_PAIR,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_AUTO_LOCK_SECONDS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_AUTO_LOCK_SECONDS,
    MAX_SCAN_INTERVAL,
    MIN_AUTO_LOCK_SECONDS,
    MIN_SCAN_INTERVAL,
)
from .welock_cloud import WeLockAuthError, WeLockCloud, WeLockError, new_identifier

_LOGGER = logging.getLogger(__name__)

METHODS = {
    AUTH_MODE_PASSWORD: "Password",
    AUTH_MODE_CODE: "WhatsApp code",
}

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): str,
        vol.Required(CONF_AUTH_MODE, default=AUTH_MODE_PASSWORD): vol.In(METHODS),
    }
)

PASSWORD_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})
CODE_SCHEMA = vol.Schema({vol.Required("code"): str})


class WeLockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the WeLock BLE configuration."""

    VERSION = 1

    def __init__(self) -> None:
        self._account = ""
        self._password = ""
        self._auth_mode = AUTH_MODE_PASSWORD
        self._identifier = new_identifier()
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._pending_code: str = ""
        self._pending_link: str = ""
        self._devices: list[dict[str, Any]] = []

    # -- helpers ---------------------------------------------------------
    def _client(self) -> WeLockCloud:
        return WeLockCloud(
            async_get_clientsession(self.hass),
            self._account,
            self._password,
            self._identifier,
        )

    async def _async_finish_login(self, client: WeLockCloud) -> dict[str, str]:
        """Load the device list after a successful authentication."""
        errors: dict[str, str] = {}
        try:
            self._devices = await client.async_get_devices()
            await self._async_enrich_devices(client)
        except WeLockError as err:
            _LOGGER.warning("WeLock device list error: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("unexpected error while loading WeLock devices")
            errors["base"] = "unknown"
        else:
            if not self._devices:
                errors["base"] = "no_devices"
        return errors

    def _async_finish_reauth(self) -> ConfigFlowResult | None:
        """Update the existing entry instead of creating a new one on reauth."""
        if self.source != "reauth":
            return None
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return None
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_PASSWORD: self._password,
                CONF_TOKEN: self._token,
                CONF_REFRESH_TOKEN: self._refresh_token,
            },
        )
        return self.async_abort(reason="reauth_successful")

    async def _async_enrich_devices(self, client: WeLockCloud) -> None:
        """Attach the lock's BLE address and battery to each device.

        ``Device/GetDevices`` only returns the account's devices; the Bluetooth
        details live in ``device/GetDeivceInfo`` (sic), whose ``ble`` list holds
        one entry per radio.  ``device/GetLockDevice`` names the lock's own radio
        (``deviceID``), which is the entry to use.
        """
        for device in self._devices:
            number = device.get("deviceNumber")
            if not number:
                continue
            try:
                lock = await client.async_get_lock_info(str(number))
                info = await client.async_get_device_info(str(number))
            except WeLockError as err:
                _LOGGER.debug("BLE details for %s unavailable: %s", number, err)
                continue
            ble_id = str(lock.get("deviceID") or "")
            device["lockModel"] = lock.get("lockModel")
            device["deviceID"] = ble_id
            for entry in info.get("ble") or []:
                if not isinstance(entry, dict):
                    continue
                if ble_id and entry.get("deviceName") != ble_id:
                    continue
                device["deviceMac"] = entry.get("deviceMac")
                device["battery"] = entry.get("power")
                device["blePassword"] = entry.get("devicePassword")
                break

    # -- steps -----------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account and the preferred login method."""
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT]
            self._auth_mode = user_input[CONF_AUTH_MODE]
            if self._auth_mode == AUTH_MODE_CODE:
                # The cloud answers 1002 for several non-fatal situations (code
                # just sent, rate limit, unknown account).  Do not block the
                # flow: the user can still type the code they received.
                client = self._client()
                try:
                    self._pending_code = await client.request_code()
                    self._pending_link = client.pending_link or ""
                except WeLockError as err:
                    _LOGGER.warning("WeLock code request failed: %s", err)
                return await self.async_step_code()
            return await self.async_step_password()

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the account password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            client = self._client()
            try:
                self._token = await client.login()
                self._refresh_token = client.refresh_token
            except WeLockAuthError:
                errors["base"] = "invalid_auth"
            except WeLockError as err:
                _LOGGER.warning("WeLock login error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error during WeLock login")
                errors["base"] = "unknown"
            else:
                errors = await self._async_finish_login(client)
                if not errors:
                    finished = self._async_finish_reauth()
                    if finished is not None:
                        return finished
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="password", data_schema=PASSWORD_SCHEMA, errors=errors
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the WhatsApp code delivered to the account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._client()
            try:
                self._token = await client.login_with_code(str(user_input["code"]))
                self._refresh_token = client.refresh_token
            except WeLockAuthError:
                errors["base"] = "invalid_code"
            except WeLockError as err:
                _LOGGER.warning("WeLock code login error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error during WeLock code login")
                errors["base"] = "unknown"
            else:
                errors = await self._async_finish_login(client)
                if not errors:
                    finished = self._async_finish_reauth()
                    if finished is not None:
                        return finished
                    return await self.async_step_device()

        schema = CODE_SCHEMA
        if self._pending_code and not errors:
            schema = vol.Schema(
                {vol.Required("code", default=self._pending_code): str}
            )
        return self.async_show_form(
            step_id="code",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "account": self._account,
                "code": self._pending_code or "the code shown by the app",
                "link": self._pending_link or "https://wa.me/",
            },
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the lock and confirm its Bluetooth address."""
        if user_input is not None:
            device_number = user_input[CONF_DEVICE_NUMBER]
            device = next(
                (
                    item
                    for item in self._devices
                    if str(item.get("deviceNumber")) == str(device_number)
                ),
                {},
            )
            name = str(device.get("deviceName") or device_number)
            await self.async_set_unique_id(str(device_number))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ACCOUNT: self._account,
                    CONF_AUTH_MODE: self._auth_mode,
                    CONF_PASSWORD: self._password,
                    CONF_TOKEN: self._token,
                    CONF_REFRESH_TOKEN: self._refresh_token,
                    CONF_IDENTIFIER: self._identifier,
                    CONF_DEVICE_NUMBER: str(device_number),
                    CONF_DEVICE_NAME: name,
                    CONF_DEVICE_MAC: device.get("deviceMac"),
                    CONF_BLE_ADDRESS: user_input[CONF_BLE_ADDRESS],
                    CONF_PAIR: user_input.get(CONF_PAIR, False),
                },
            )

        choices = {
            str(item.get("deviceNumber")): (
                f"{item.get('deviceName') or item.get('deviceNumber')} "
                f"({item.get('deviceNumber')})"
            )
            for item in self._devices
            if item.get("deviceNumber")
        }
        suggested_mac = ""
        for item in self._devices:
            if item.get("deviceMac"):
                suggested_mac = str(item["deviceMac"]).upper()
                break

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_NUMBER): vol.In(choices),
                vol.Required(CONF_BLE_ADDRESS, default=suggested_mac): str,
                vol.Optional(CONF_PAIR, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            description_placeholders={
                "count": str(len(self._devices)),
                "hint": (
                    "MAC address of the lock as seen by your Bluetooth adapter "
                    "(usually the same value reported by the cloud)."
                ),
            },
        )

    # -- reauth ----------------------------------------------------------
    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-authenticate when the stored token is no longer valid."""
        entry: ConfigEntry | None = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if entry is not None:
            self._account = entry.data.get(CONF_ACCOUNT, "")
            self._identifier = entry.data.get(CONF_IDENTIFIER) or new_identifier()
            self._auth_mode = entry.data.get(CONF_AUTH_MODE, AUTH_MODE_PASSWORD)
            self._password = entry.data.get(CONF_PASSWORD, "")
        if self._auth_mode == AUTH_MODE_CODE:
            client = self._client()
            try:
                self._pending_code = await client.request_code()
                self._pending_link = client.pending_link or ""
            except WeLockError as err:
                _LOGGER.debug("reauth code request failed: %s", err)
            return await self.async_step_code()
        return await self.async_step_password()

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> WeLockOptionsFlow:
        """Return the options flow for this entry."""
        return WeLockOptionsFlow()


class WeLockOptionsFlow(OptionsFlow):
    """Tunables for polling and the auto-lock window."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user tune the polling interval and the auto-lock delay."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        interval = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        auto_lock = int(
            entry.options.get(CONF_AUTO_LOCK_SECONDS, DEFAULT_AUTO_LOCK_SECONDS)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=interval): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
                vol.Required(CONF_AUTO_LOCK_SECONDS, default=auto_lock): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_AUTO_LOCK_SECONDS, max=MAX_AUTO_LOCK_SECONDS
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
