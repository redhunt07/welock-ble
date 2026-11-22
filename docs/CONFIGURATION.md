# Configuration reference

Everything is configured from the Home Assistant UI. No YAML is required.

## Config flow

### Step 1 — account

| Field | Notes |
| --- | --- |
| `account` | WeLock account: email, or phone in international format with digits only (`390000000000`). |
| `auth_mode` | `password` or `code` (WhatsApp handshake). |

### Step 2a — password

The password is sent as-is to `POST /api/Account/token/welock`. It is stored in
the config entry (redacted in diagnostics) so that restarts do not need it again.

### Step 2b — WhatsApp code

1. The integration calls `POST /api/Account/MessageCode/welock`; the cloud issues
   a code of the form `WL_XXXXXXXX` (the `WL_` prefix plus eight alphanumerics).
2. The dialog shows the code and, when the cloud returns one, a `wa.me` link that
   opens the WeLock bot with the code pre-filled.
3. Send the code to the bot, wait for `OK`, then confirm the dialog.
4. `POST /api/Account/MessageLogin/welock` exchanges the code for a token.

A code can be used once; a new one is requested for every attempt. If the app
already showed you a code, you can type that one instead of the generated one.

### Step 3 — lock

The integration lists the locks of the account and shows, for each one, the
`deviceNumber`, the advertised `deviceName` and the Bluetooth address of the
lock radio (the WIFIBOX gateway radio is filtered out).

| Field | Notes |
| --- | --- |
| `device_number` | The lock to control. |
| `ble_address` | MAC address of the lock radio, as seen by your adapter/proxy. Defaults to the value reported by the cloud; change it if your setup sees a different address. |
| `pair` | Bond before writing. Off by default. |

### Re-authentication

If the token expires (or the refresh token is rejected), Home Assistant starts a
reauth flow with the same two login methods. The entry keeps its lock and
options.

## Options

*Settings → Devices & Services → WeLock BLE → Configure* (one entry per lock).

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `scan_interval` | 300 s | 60–3600 s | Cloud polling interval for battery/status/model. |
| `auto_lock_seconds` | 8 s | 1–120 s | How long the lock is reported as unlocked after an open. WeLock locks relock mechanically; this only mirrors the app's UI behaviour. |

Options are applied by reloading the entry.

## Entities

| Entity | Type | Notes |
| --- | --- | --- |
| Lock | `lock` | `unlock` sends the cloud frame over Bluetooth. `lock` is a no-op (spring latch). |
| Open | `button` | Same command as `lock.unlock`, for dashboards/scripts. |
| Refresh data | `button` | Refreshes the cloud-side details (battery, model, firmware, MAC). |
| Battery | `sensor` | Battery percentage from the cloud. |
| Lock status code | `sensor` (diagnostic) | Raw status value reported by the cloud. |
| Model | `sensor` (diagnostic) | `lockModel` from `device/GetLockDevice`. |
| Firmware | `sensor` (diagnostic) | Radio model/hardware/firmware revision. |
| Bluetooth address | `sensor` (diagnostic) | Address used to reach the lock. |
| Device number | `sensor` (diagnostic) | WeLock device number. |
| Last token | `sensor` (diagnostic) | Token of the last open command. |
| Last reply | `sensor` (diagnostic) | Raw reply of the lock, with status/success/attempts attributes. |
| Reachable | `binary_sensor` (diagnostic) | Result of the last Bluetooth exchange; unknown until the first command. |

## Services and events

The integration uses the standard `lock.unlock` service. Failures are raised as
`HomeAssistantError` so that automations can catch them:

```yaml
- action: lock.unlock
  target:
    entity_id: lock.we_lock_a1b2c3
  continue_on_error: true
```

## Diagnostics

*Settings → Devices & Services → WeLock BLE → Download diagnostics* returns the
entry data (password, access token and refresh token redacted), the options, and
the last known state including the raw cloud payload.

## Enabling debug logs

```yaml
logger:
  default: warning
  logs:
    custom_components.welock_ble: debug
```

Debug logs include the challenge, the derived `randomFactor`, the frame sent and
the lock's reply — useful when a command is refused.
