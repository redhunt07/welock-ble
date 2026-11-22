"""WeLock cloud API client.

Reverse-engineered from WeLock Android app 4.5.8 (see ``docs/PROTOCOL.md``).

The lock does not accept commands built by the client: the cloud returns a hex
frame which is written to the lock verbatim.  This module is therefore the
"command generator" half of the integration; :mod:`ble_transport` is the
transport half.

API shape (observed in the APK):

* login            POST /api/Account/token/
* device list      POST /api/Device/GetDevices
* lock details     POST /api/device/GetLockDevice
* unlock frame     POST /api/Command/UnLock  -> {"serverID": .., "command": "<hex>"}

Headers: appVersion, deviceModel, requestClientTimestamp, identifierForVendor,
sign, nonce, operatingSystem, isPhysicalDevice, APP_CLIENT, User-Agent,
Authorization.

``sign`` is ``md5(md5(base64(reverse(identifierForVendor +
requestClientTimestamp))))`` (hex digest at every step), see
``StringUtil::sign`` at AOT address 0x5646e8 and its only call site at
``http_utils.dart`` 0x563d48, where arg1 is ``requestClientTimestamp`` and
arg2 is ``identifierForVendor``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import time
from datetime import datetime
from typing import Any

import aiohttp

from .const import (
    API_BASE,
    APP_CLIENT,
    APP_VERSION,
    CODE_PREFIX,
    PATH_DEVICE_INFO,
    PATH_DEVICES,
    PATH_LOCK_INFO,
    PATH_CODE_LOGIN,
    PATH_MESSAGE_LOGIN,
    PATH_LOGIN,
    PATH_MESSAGE_CODE,
    PATH_REFRESH,
    PATH_UNLOCK,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)


class WeLockError(Exception):
    """Base error for the WeLock cloud client."""


class WeLockAuthError(WeLockError):
    """Raised when the credentials are rejected."""


class WeLockApiError(WeLockError):
    """Raised when the API answers with an error."""


def compute_sign(timestamp: str, identifier: str) -> str:
    """Return the ``sign`` header value for the given timestamp/identifier."""
    reversed_payload = f"{identifier}{timestamp}"[::-1]
    b64 = base64.b64encode(reversed_payload.encode("utf-8")).decode("ascii")
    first = hashlib.md5(b64.encode("utf-8")).hexdigest()
    return hashlib.md5(first.encode("utf-8")).hexdigest()


def _time_offset_minutes() -> int:
    """Return the local UTC offset in minutes (as ``timeOffset``)."""
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 60) if offset else 0


def _random_digits(count: int = 12) -> str:
    return "".join(random.choice("0123456789") for _ in range(count))


def _unwrap(payload: Any) -> Any:
    """Return the useful part of a WeLock API envelope."""
    if isinstance(payload, dict):
        for key in ("data", "result", "Result", "Data"):
            if key in payload and payload[key] not in (None, "", [], {}):
                return payload[key]
    return payload


def _looks_like_error(payload: Any) -> str | None:
    """Return an error message when the envelope reports a failure."""
    if not isinstance(payload, dict):
        return None
    code = payload.get("code", payload.get("Code", payload.get("status")))
    success = payload.get("success", payload.get("Success"))
    if success is False:
        return str(payload.get("msg") or payload.get("message") or "request failed")
    if code is None:
        return None
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return None
    if code_int in (0, 200, 2000):
        return None
    return str(payload.get("msg") or payload.get("message") or f"error code {code_int}")


AUTH_ERROR_CODES = frozenset({1000, 1003})
"""Body-level codes that mean the session is gone (see ``_authorized``)."""


def _error_code(payload: Any) -> int | None:
    """Return the numeric code of a failed envelope, when present."""
    if not isinstance(payload, dict):
        return None
    code = payload.get("code", payload.get("Code", payload.get("status")))
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


class WeLockCloud:
    """Minimal asynchronous client for the WeLock mobile API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account: str,
        password: str,
        identifier: str,
        *,
        debug: bool = False,
    ) -> None:
        self._session = session
        self._account = account
        self._password = password
        self._identifier = identifier
        self._debug = debug
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._pending_link: str | None = None
        self.account_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ auth
    @property
    def token(self) -> str | None:
        return self._token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def pending_link(self) -> str | None:
        """The ``wa.me`` deep link that came with the last requested code."""
        return self._pending_link

    def set_token(self, token: str | None, refresh_token: str | None = None) -> None:
        """Restore a previously obtained access token."""
        self._token = token or None
        self._refresh_token = refresh_token or None

    def _headers(self, *, authorized: bool) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "appVersion": APP_VERSION,
            "deviceModel": "HomeAssistant",
            "operatingSystem": "android",
            "isPhysicalDevice": "true",
            "APP_CLIENT": APP_CLIENT,
            "User-Agent": APP_CLIENT,
            "requestClientTimestamp": timestamp,
            "identifierForVendor": self._identifier,
            "nonce": _random_digits(12),
            "sign": compute_sign(timestamp, self._identifier),
        }
        if authorized and self._token:
            headers["Authorization"] = self._token
        return headers

    async def _raw_request(
        self, path: str, payload: dict[str, Any], *, authorized: bool
    ) -> tuple[int, Any]:
        url = f"{API_BASE}{path}"
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._headers(authorized=authorized)
        if self._debug:
            logged = dict(payload)
            if "password" in logged:
                logged["password"] = "***"
            _LOGGER.debug("POST %s body=%s", url, json.dumps(logged, ensure_ascii=False))
        try:
            async with self._session.post(
                url, data=body, headers=headers, timeout=TIMEOUT
            ) as response:
                text = await response.text()
                status = response.status
        except aiohttp.ClientError as err:
            raise WeLockApiError(f"{path}: {err}") from err
        if self._debug:
            _LOGGER.debug("POST %s -> %s %s", url, status, text[:2000])
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"raw": text}
        return status, parsed

    async def login(self) -> str:
        """Authenticate and store the access token."""
        payload = {
            "account": self._account,
            "password": self._password,
            "timeOffset": _time_offset_minutes(),
            "platform": "android",
            "model": "HomeAssistant",
            "language": "en",
            "Jpushid": "",
            "isIot": "1",
        }
        status, parsed = await self._raw_request(PATH_LOGIN, payload, authorized=False)
        if status in (401, 403):
            raise WeLockAuthError(f"login rejected (HTTP {status})")
        error = _looks_like_error(parsed)
        if error:
            raise WeLockAuthError(f"login failed: {error}")

        token = self._find_token(parsed)
        if not token:
            raise WeLockAuthError(f"no token in login response: {parsed!r}")
        self._token = token
        data = _unwrap(parsed)
        if isinstance(data, dict):
            refresh = data.get("refresh_token")
            if isinstance(refresh, str) and refresh:
                self._refresh_token = refresh
            # The server resolves the login to a canonical account (phone number
            # plus areacode) even when an e-mail address was supplied.
            self.account_info = {
                key: data.get(key)
                for key in ("account", "areacode", "tel", "email", "nickName")
                if data.get(key) is not None
            }
        _LOGGER.debug("WeLock login ok (account=%s)", self.account_info.get("account"))
        return token

    async def request_code(self) -> str:
        """Ask the server for a WhatsApp handshake code and return it.

        The WeLock login is a handshake, not an OTP delivery: the server issues a
        code, the user sends that code to the WeLock WhatsApp bot, the bot
        confirms it and only then ``MessageLogin`` succeeds.

        The code is *server-issued*: the live reply to ``MessageCode`` is
        ``{"code":0,"data":{"code":"WL_QRSTUVWX","link":"http://wa.me/<bot>/?text=WL_QRSTUVWX"}}``.
        The app merely displays ``data.code`` (it reads the literal ``"code"``
        from the reply) and never builds the string itself -- ``WL_`` does not
        exist anywhere in ``libapp.so``.  ``randomCode`` is a client-side widget
        value (``_BodyState.field_7b``) and is ignored by the server.
        """
        payload = {
            "account": self._account,
            "messageType": "WhatsApp",
            "platform": "android",
            "model": "HomeAssistant",
            "language": "en",
            "randomCode": random.randint(100000, 999999),
            "timeOffset": _time_offset_minutes(),
        }
        status, parsed = await self._raw_request(
            PATH_MESSAGE_CODE, payload, authorized=False
        )
        if status in (401, 403):
            raise WeLockAuthError(f"code request rejected (HTTP {status})")
        error = _looks_like_error(parsed)
        if error:
            raise WeLockAuthError(f"code request failed: {error}")
        data = _unwrap(parsed)
        if not isinstance(data, dict):
            raise WeLockAuthError(f"unexpected MessageCode reply: {parsed!r}")
        code = data.get("code")
        if not isinstance(code, str) or not code:
            raise WeLockAuthError(f"no code in MessageCode reply: {parsed!r}")
        link = data.get("link")
        self._pending_link = link if isinstance(link, str) else None
        _LOGGER.debug("WeLock issued code %s (link=%s)", code, self._pending_link)
        return code

    async def login_with_code(self, code: str) -> str:
        """Exchange a WhatsApp login code for an access token."""
        payload = {
            "account": self._account,
            "code": code,
            "timeOffset": _time_offset_minutes(),
            "platform": "android",
            "model": "HomeAssistant",
            "language": "en",
            "Jpushid": "",
            "isIot": "1",
        }
        # ``VerificationCodeLogin`` only accepts a 6-digit code; the WhatsApp
        # handshake code (``WL_`` + 9 characters) is spent on ``MessageLogin``.
        path = PATH_MESSAGE_LOGIN if code.startswith(CODE_PREFIX) else PATH_CODE_LOGIN
        status, parsed = await self._raw_request(path, payload, authorized=False)
        if status in (401, 403):
            raise WeLockAuthError(f"code login rejected (HTTP {status})")
        error = _looks_like_error(parsed)
        if error:
            raise WeLockAuthError(f"code login failed: {error}")

        token = self._find_token(parsed)
        if not token:
            raise WeLockAuthError(f"no token in code login response: {parsed!r}")
        self._token = token
        _LOGGER.debug("WeLock code login ok")
        return token

    @staticmethod
    def _find_token(parsed: Any) -> str | None:
        if isinstance(parsed, str) and parsed:
            return parsed
        if not isinstance(parsed, dict):
            return None
        for key in ("token", "Token", "access_token", "accessToken"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
        for value in parsed.values():
            if isinstance(value, dict):
                found = WeLockCloud._find_token(value)
                if found:
                    return found
        return None

    async def refresh(self) -> None:
        """Refresh the access token using the stored refresh token.

        The endpoint wants both tokens in the *body* and does not read the
        ``Authorization`` header: asking with only one of them answers
        ``{"code":1002,"msg":"access_token"}`` (or ``"refresh_token"``), i.e. the
        ``msg`` names the field that is missing.
        """
        if not self._token or not self._refresh_token:
            return
        try:
            status, parsed = await self._raw_request(
                PATH_REFRESH,
                {"access_token": self._token, "refresh_token": self._refresh_token},
                authorized=False,
            )
        except WeLockApiError:
            return
        if status != 200 or _looks_like_error(parsed):
            return
        data = _unwrap(parsed)
        if not isinstance(data, dict):
            return
        token = self._find_token(data)
        if token:
            self._token = token
        new_refresh = data.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh:
            self._refresh_token = new_refresh
        _LOGGER.debug("WeLock token refreshed")

    async def _authorized(self, path: str, payload: dict[str, Any]) -> Any:
        if not self._token:
            await self.login()
        status, parsed = await self._raw_request(path, payload, authorized=True)
        if status == 401:
            await self.refresh()
            if self._token:
                status, parsed = await self._raw_request(
                    path, payload, authorized=True
                )
        if status == 401:
            await self.login()
            status, parsed = await self._raw_request(path, payload, authorized=True)

        error = _looks_like_error(parsed)
        if error and status < 400:
            # A revoked session is reported in the body, not with HTTP 401:
            # ``code 1000`` means the access token is no longer valid and
            # ``code 1003`` means the refresh token is gone as well (another
            # login with a different identifier invalidates the previous one).
            # Try the refresh token, then fall back to a full login.
            await self.refresh()
            status, parsed = await self._raw_request(path, payload, authorized=True)
            error = _looks_like_error(parsed)
            if error and status < 400 and self._password:
                await self.login()
                status, parsed = await self._raw_request(path, payload, authorized=True)
                error = _looks_like_error(parsed)

        if status >= 400:
            raise WeLockApiError(f"{path}: HTTP {status}")
        if error:
            # Without a password the session cannot be rebuilt locally: report
            # an authentication failure so Home Assistant starts a reauth flow
            # instead of retrying the setup forever.
            if not self._password and _error_code(parsed) in AUTH_ERROR_CODES:
                raise WeLockAuthError(f"{path}: {error}")
            raise WeLockApiError(f"{path}: {error}")
        return _unwrap(parsed)

    # --------------------------------------------------------------- devices
    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return the account's devices."""
        data = await self._authorized(PATH_DEVICES, {})
        return _as_device_list(data)

    async def async_get_lock_info(self, device_number: str) -> dict[str, Any]:
        """Return the BLE details of one lock (mac address, battery, version)."""
        data = await self._authorized(
            PATH_LOCK_INFO, {"deviceNumber": device_number}
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    async def async_get_device_info(self, device_number: str) -> dict[str, Any]:
        """Return the current state of one device."""
        data = await self._authorized(
            PATH_DEVICE_INFO, {"deviceNumber": device_number}
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- unlock
    async def async_get_unlock_command(
        self,
        device_number: str,
        device_name: str,
        *,
        battery: int = 100,
        random_factor: int | None = None,
        random_factor_data: str | None = None,
    ) -> dict[str, Any]:
        """Ask the cloud for the unlock frame.

        Returns the raw mapping; ``command`` is a hex string that must be
        written to the lock unchanged.
        """
        payload: dict[str, Any] = {
            "deviceNumber": device_number,
            "deviceName": device_name,
            "randomFactor": str(
                random_factor if random_factor is not None else random.randint(1000, 9999)
            ),
            "battery": str(battery),
        }
        if random_factor_data is not None:
            payload["randomFactorData"] = random_factor_data
        data = await self._authorized(PATH_UNLOCK, payload)
        if not isinstance(data, dict):
            raise WeLockApiError(f"unexpected unlock response: {data!r}")
        command = data.get("command") or data.get("cmd")
        if not command:
            raise WeLockApiError(f"unlock response has no command: {data!r}")
        return data


def _as_device_list(data: Any) -> list[dict[str, Any]]:
    """Normalise the many shapes a WeLock device list can take."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("devices", "deviceList", "list", "items", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # A single device returned as a mapping.
        if any(k in data for k in ("deviceNumber", "deviceName")):
            return [data]
    return []


async def async_login(
    session: aiohttp.ClientSession,
    account: str,
    password: str,
    identifier: str,
    *,
    debug: bool = False,
) -> WeLockCloud:
    """Create a client and authenticate it."""
    client = WeLockCloud(session, account, password, identifier, debug=debug)
    await client.login()
    return client


def new_identifier() -> str:
    """Return a stable random install identifier for the ``sign`` header."""
    return hashlib.md5(str(random.random()).encode()).hexdigest()


__all__ = [
    "WeLockCloud",
    "WeLockError",
    "WeLockAuthError",
    "WeLockApiError",
    "async_login",
    "compute_sign",
    "new_identifier",
]
