# Architecture

This document is for contributors: how the integration is put together and which
protocol invariants the code depends on. The wire format itself is documented in
[`PROTOCOL.md`](PROTOCOL.md).

## Modules

| File | Responsibility |
| --- | --- |
| `__init__.py` | Entry setup/unload, token persistence, reload on option change. |
| `config_flow.py` | Account login (password or WhatsApp code), lock selection, reauth, options. |
| `coordinator.py` | Owns the cloud session and the on-demand BLE exchange; exposes state to the entities. |
| `welock_cloud.py` | HTTP client for the WeLock private API (`https://clientapi.welock.com.cn/api`), envelope parsing, token handling, error mapping. |
| `ble_transport.py` | BLE transport: connect, discover the Nordic UART Service, challenge/auth/write/reply with retries. |
| `lock.py`, `sensor.py`, `binary_sensor.py`, `button.py` | Entities. |
| `diagnostics.py` | Redacted dump of the entry and the last state. |
| `const.py` | Constants: endpoints, route segment, UUIDs, frame markers, defaults. |

## Data flow of an open command

```
lock.unlock
  └─ coordinator.async_open()
       ├─ cloud login (token from the entry, refresh or password login)
       ├─ ble.async_unlock(command_provider=_frame_for)
       │    ├─ connect (address from the entry; resolved through HA bluetooth)
       │    ├─ subscribe to the NUS TX characteristic
       │    ├─ write  U0 <auth flag> <ts> <token>
       │    ├─ wait for the 4-byte challenge 55 30 b2 b3
       │    └─ command_provider(challenge)
       │         └─ cloud POST /Command/UnLock  {deviceNumber, deviceName,
       │            randomFactor: b2 | b3<<8, battery, randomFactorData: null}
       │              → {"command": "5531<token><suffix>"}
       ├─ write the returned frame verbatim
       └─ parse 55 31 <status>; status 01 = opened
```

The cloud call happens *inside* the BLE exchange because the challenge is only
known after the connection is established.

## Invariants the code relies on

* **The token is derived from the live challenge.** `randomFactor` is the
  challenge payload read as a little-endian uint16 (bytes 2 and 3 of
  `55 30 b2 b3`). Bytes 0–1 are the header and must not be used.
* **`randomFactorData` must be omitted.** Any non-null value makes the server
  answer with a null token (`AAAAAAAA` + `AAAAA` suffix); short values return
  HTTP 500.
* **`deviceName` must match the lock's advertised name exactly** (`deviceID` from
  `device/GetLockDevice`). The server rejects other values.
* **Writes must be acknowledged.** `write_gatt_char(..., response=False)` is
  silently dropped by the lock; only write-with-response works. Frames are
  chunked at the 20-byte ATT payload limit.
* **One authentication per connection.** After the first auth + command, a second
  auth on the same link is rejected with the 5-byte form `55 30 00 <chal>`. The
  transport therefore treats a `len(ack) >= 5` reply as a rejection and
  reconnects.
* **The link is not held.** Every command opens a connection and closes it; this
  keeps the lock available to the phone app and avoids BlueZ stale handles.

## Error handling

* `WeLockAuthError` → `ConfigEntryAuthFailed` → Home Assistant starts reauth.
* `WeLockApiError` (network/HTTP/envelope) → `UpdateFailed` in the coordinator, or
  `HomeAssistantError` for user actions.
* `WeLockBLEError` → `HomeAssistantError` with the failing step.
* The WAF returns HTTP 403 when `Command/UnLock` is called too fast; requests are
  paced (~1.2 s) and retried.

## Security and privacy

* Credentials live in the config entry; the token and refresh token are persisted
  so restarts do not require a new login.
* Diagnostics redact password, token and refresh token, keeping their length.
* No personal identifiers are hard-coded: every example in the docs is
  anonymised.

## Testing

`tests/test_cloud.py` runs the cloud client against an inline `aiohttp` mock
server (`127.0.0.1:8765`) and exercises the password login, the WhatsApp code
login, device listing and the unlock payload. It prints `OK` on success and needs
no network access.

`tools/welock_probe.py` is the manual counterpart: it talks to the real cloud and
(optionally) to a real lock, printing the challenge, the derived `randomFactor`,
the frame and the reply.
