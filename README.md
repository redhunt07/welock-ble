# WeLock BLE — Home Assistant integration

Control **WeLock** smart locks from Home Assistant over a **generic Bluetooth
adapter** (a local USB dongle, an onboard radio, or an ESPHome Bluetooth proxy)
instead of the vendor WIFIBOX gateway.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

> **Why a cloud login is required.** The lock does not accept commands built by
> the client: every operational frame is an AES-protected string produced by the
> WeLock cloud. The app only forwards those bytes over Bluetooth, and the AES key
> never leaves the server and the lock. This integration does exactly the same:
> it logs into your own WeLock account, asks the cloud for the frame matching the
> live challenge sent by the lock, and replays it over your local Bluetooth
> adapter. No WIFIBOX, no gateway — but an internet connection is needed to open
> the lock, exactly like the phone app.

The full reverse-engineered protocol (UUIDs, framing, cloud API, enums, evidence)
is documented in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## What you get

| Platform | Entities |
| --- | --- |
| `lock` | The lock itself (`unlock` opens it; WeLock locks are spring-latch and relock themselves) |
| `sensor` | Battery, lock status code, model, firmware, Bluetooth address, device number, last token, last reply |
| `binary_sensor` | Reachable — result of the last Bluetooth exchange |
| `button` | Open, Refresh data |

The integration is fully configurable from the UI (no YAML): account login,
WhatsApp-code login, lock selection, Bluetooth address, and options for the cloud
polling interval and the "unlocked" window.

## Requirements

* Home Assistant **2024.11** or newer, with the `bluetooth` integration working
  (a local adapter or an ESPHome Bluetooth proxy in range of the lock).
* A WeLock account (the same one used in the phone app) with at least one lock
  paired to it.
* The lock must be in Bluetooth range of the adapter/proxy. The phone app and
  Home Assistant cannot hold the link at the same time — that is a protocol
  limitation, not an integration one.

## Installation

See [`docs/INSTALL.md`](docs/INSTALL.md) for HACS and manual installation, plus
adapter/proxy notes.

Short version (HACS custom repository):

1. HACS → ⋮ → *Custom repositories* → add this repository URL as **Integration**.
2. Install **WeLock BLE**, restart Home Assistant.
3. *Settings → Devices & Services → Add integration → WeLock BLE*.

## Configuration — and how to get the values it asks for

The config flow asks for the WeLock account first, then for the lock and its
Bluetooth address. Everything below is what you need and how to obtain it.

### 1. WeLock account

The email or phone number you use in the WeLock app. For a phone number use the
international format with digits only (e.g. an Italian number
`+39 000 000 0000` becomes `390000000000`). The password is the app password.

### 2. `deviceNumber`, `deviceName` and the Bluetooth MAC address

These three values identify your lock. The integration ships a small probe that
prints them straight from your account:

```bash
python -m venv .venv && .venv/bin/pip install aiohttp bleak
.venv/bin/python tools/welock_probe.py devices --account <your-account> --password '<your-password>'
```

Output for each lock looks like this (values anonymised):

```
============================================================
deviceNumber : 1234567890
deviceName   : WeLockA1B2C3
model        : TOUCHEBL63
qrStr        : https://service.welock.com/?code=...
lockModel    : TOUCHEBL63
bleName      : WeLockA1B2C3
BLE radios (use the entry whose name matches bleName):
  name=WeLockA1B2C3 mac=AA:BB:CC:DD:EE:FF power=47 version={...} <-- lock
  name=WeLockG0W1B0X mac=11:22:33:44:55:66 power=0 version={...}
```

* `deviceNumber` → the **Device number** the config flow shows in its lock list.
* `deviceName` (equal to `bleName`/`deviceID`) → the name the lock advertises over
  Bluetooth. The integration sends it in the unlock payload; the cloud rejects
  any other value.
* The **Bluetooth MAC address** is the `mac=` of the radio marked `<-- lock`
  (the one whose name matches `bleName`). The other radio is the WIFIBOX gateway
  and is not used by this integration.

Without Python you can still get the MAC with any BLE scanner app (e.g. nRF
Connect) by looking for a device advertising the `WeLock…` name of your lock; the
`deviceNumber` and `deviceName` are shown in the WeLock app's lock details.

### 3. Login method

Two methods are offered, matching the two login paths of the Android app:

* **Password** — the integration posts the account password to
  `POST /api/Account/token/welock`.
* **WhatsApp code** — WeLock does not send emails or SMS for the app login: it
  uses a WhatsApp handshake. The integration asks the cloud for a code
  (`WL_XXXXXXXX`), shows it to you, and you send it to the WeLock WhatsApp bot
  (the app opens `wa.me/<bot>/?text=<code>` for you). When the bot answers `OK`,
  confirm the dialog and the code is exchanged for a token.

The resulting token is stored in the config entry so that restarts do not need
the password nor a new WhatsApp code. If the token ever expires, Home Assistant
starts a re-authentication flow.

### 4. Pair / bond

Leave it off unless your adapter requires bonding (some ESPHome proxies and a few
dongles do). WeLock locks accept unpaired writes to the Nordic UART Service.

## Options

*Settings → Devices & Services → WeLock BLE → Configure*:

| Option | Meaning |
| --- | --- |
| Cloud polling interval (seconds) | How often the cloud-side data (battery, status, model) is refreshed. |
| Unlocked window (seconds) | How long the lock is reported as unlocked after an open. WeLock locks relock mechanically, so this only mirrors the app's behaviour. |

## Example automation

```yaml
automation:
  - alias: Open the front door for me
    triggers:
      - trigger: state
        entity_id: person.me
        to: home
    actions:
      - action: lock.unlock
        target:
          entity_id: lock.we_lock_a1b2c3
```

Because the integration connects on demand, opening takes a few seconds (the
cloud round-trip plus the BLE exchange). The `binary_sensor` *Reachable* tells you
whether the last exchange succeeded.

## How it works

1. The lock advertises the Nordic UART Service. The integration connects on
   demand and subscribes to notifications.
2. It sends the ASCII frame `U0` + an auth flag + a timestamp + the token.
3. The lock answers with a 4-byte challenge `55 30 <b2> <b3>`.
4. The integration asks the cloud for `/api/Command/UnLock` with
   `randomFactor = b2 | (b3 << 8)` (little-endian uint16) and `randomFactorData`
   omitted.
5. The returned `U1` frame is written verbatim; `55 31 01 …` means opened,
   `55 31 00 …` means refused.

Every step, including the two independent HCI captures that proved the
`randomFactor` derivation, is documented in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md). Architecture notes for contributors are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Troubleshooting

Common failures (HTTP 403 from the WAF, `UNLIKELY_ERROR: 14`, DNS problems,
"the lock refused to open", the one-auth-per-connection rule) are collected in
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

## Limitations

* Opening needs internet access (cloud-generated frame).
* Only opening is implemented: the lock is spring-latch, so `lock` is a no-op and
  the lock relocks by itself.
* One Bluetooth connection at a time: the phone app must not hold the link.
* The cloud rate-limits `Command/UnLock` (HTTP 403 after ~30 rapid calls); the
  integration paces its requests.

## Privacy

Only your own WeLock account credentials and the lock identifiers are stored, in
the Home Assistant config entry. Credentials are redacted in diagnostics. All
examples in this repository use anonymised identifiers.

## Development

```bash
python -m venv .venv
.venv/bin/pip install aiohttp bleak
.venv/bin/python tests/test_cloud.py     # cloud client against a local mock
.venv/bin/python tools/welock_probe.py --help
```

`docs/PROTOCOL.md` is the reference for the wire format; the probe tool is the
fastest way to inspect your own lock.

## License

MIT — see [`LICENSE`](LICENSE).

WeLock is a trademark of its owner. This project is unofficial and not affiliated
with WeLock.
