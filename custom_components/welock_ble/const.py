"""Constants for the WeLock BLE integration.

All values below are reverse-engineered from WeLock Android app 4.5.8
(see ``docs/PROTOCOL.md`` for the full evidence trail).
"""

from __future__ import annotations

DOMAIN = "welock_ble"

# --- Cloud API ---------------------------------------------------------------
API_BASE = "https://clientapi.welock.com.cn/api"
APP_VERSION = "4.5.8-9988"
APP_CLIENT = "APP_CLIENT"

# The app appends a *fixed* runtime route segment to every RequestApi path. It is
# the static field set at ``app_init.dart`` 0xb93298 (``StoreStaticField(0x10fc,
# "welock")``), so the real routes are ``/Account/token/welock`` etc. Any other
# segment (e.g. "android") is accepted by the router but answers ``code:1002``.
ROUTE_SEGMENT = "welock"
PATH_LOGIN = f"/Account/token/{ROUTE_SEGMENT}"
PATH_MESSAGE_CODE = f"/Account/MessageCode/{ROUTE_SEGMENT}"
PATH_CODE_LOGIN = f"/Account/VerificationCodeLogin/{ROUTE_SEGMENT}"
# WhatsApp handshake: the code the server issues (``WL_`` + 8 chars) is spent here.
PATH_MESSAGE_LOGIN = f"/Account/MessageLogin/{ROUTE_SEGMENT}"
# Format of the handshake code: "WL_" followed by eight uppercase alphanumerics
# (live example: "WL_ABCDEFGH"). The server issues it verbatim in the
# ``MessageCode`` reply as ``data.code``; the client never builds it.
CODE_PREFIX = "WL_"
CODE_LENGTH = 8
# ``refresh`` needs both tokens in the *body* (msg "access_token"/"refresh_token"
# names the missing field); the ``Authorization`` header is not used.
PATH_REFRESH = f"/Account/refresh/{ROUTE_SEGMENT}"
PATH_DEVICES = "/Device/GetDevices"
PATH_LOCK_INFO = "/device/GetLockDevice"
PATH_DEVICE_INFO = "/device/GetDeivceInfo"
PATH_UNLOCK = "/Command/UnLock"
PATH_COMMAND_STATUS = "/ufun/CommandStatus"

# --- BLE (Nordic UART Service) ----------------------------------------------
BLE_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
BLE_WRITE_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
BLE_NOTIFY_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# The app writes 20-byte slices separated by a short delay (default ATT MTU).
BLE_CHUNK_SIZE = 20
BLE_CHUNK_DELAY = 0.06

# --- Wire protocol (reverse-engineered, verified on a real lock) -------------
# U0                      -> 55 30 <challenge:2>          challenge request
# U0 01 <ts:4 LE> <tok:8> -> 55 30 <challenge:2>          authentication
# U1 <tok:8> <cmd:5>      -> 55 31 <status> <challenge:2> command
# <cmd> = "AAAA" + one character: "p" = open, "8" = load log.
BLE_AUTH_FLAG = b"\x01"
BLE_CMD_OPEN = b"AAAAp"
BLE_CMD_LOAD_LOG = b"AAAA8"
BLE_REPLY_CHALLENGE = b"\x55\x30"
BLE_REPLY_COMMAND = b"\x55\x31"
BLE_STATUS_OK = 0x01
BLE_TOKEN_LEN = 8
BLE_REPLY_TIMEOUT = 2.5
BLE_RETRIES = 5

# --- Defaults ----------------------------------------------------------------
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_AUTO_LOCK_SECONDS = 8
MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 3600
MIN_AUTO_LOCK_SECONDS = 1
MAX_AUTO_LOCK_SECONDS = 120

CONF_ACCOUNT = "account"
CONF_AUTH_MODE = "auth_mode"
CONF_TOKEN = "token"
CONF_REFRESH_TOKEN = "refresh_token"

AUTH_MODE_PASSWORD = "password"
AUTH_MODE_CODE = "code"
CONF_PASSWORD = "password"
CONF_DEVICE_NUMBER = "device_number"
CONF_DEVICE_NAME = "device_name"
CONF_BLE_ADDRESS = "ble_address"
CONF_DEVICE_MAC = "device_mac"
CONF_IDENTIFIER = "identifier"
CONF_PAIR = "pair"
CONF_SEND_STATUS_FIRST = "send_status_first"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AUTO_LOCK_SECONDS = "auto_lock_seconds"
