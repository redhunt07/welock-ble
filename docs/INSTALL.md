# Installation

## Before you start

You need:

* Home Assistant **2024.11** or newer with the `bluetooth` integration working.
* A Bluetooth path to the lock: a local adapter supported by BlueZ, or an
  **ESPHome Bluetooth proxy** in range. The integration uses Home Assistant's
  Bluetooth API to resolve the device, so proxies work exactly like a local
  adapter.
* Your WeLock account credentials, plus `deviceNumber`, `deviceName` and the
  lock's Bluetooth MAC — see the "Configuration" section of the
  [README](../README.md#configuration--and-how-to-get-the-values-it-asks-for).

## HACS (custom repository)

This integration is not in the default HACS store yet; add it as a custom
repository:

1. HACS → three-dot menu → **Custom repositories**.
2. Repository: the URL of this repository. Category: **Integration**.
3. Install **WeLock BLE**.
4. Restart Home Assistant.
5. *Settings → Devices & Services → Add integration → WeLock BLE*.

## Manual installation

1. Copy the `custom_components/welock_ble` directory into your Home Assistant
   `config/custom_components/` directory, so that the final path is
   `config/custom_components/welock_ble/manifest.json`.
2. Restart Home Assistant.
3. *Settings → Devices & Services → Add integration → WeLock BLE*.

Via SSH on Home Assistant OS:

```bash
scp -r custom_components/welock_ble root@homeassistant.local:/config/custom_components/
```

## Bluetooth adapter / proxy notes

* The integration connects **on demand**: it does not keep a link open, so the
  lock stays reachable for the phone app the rest of the time.
* Writes must be acknowledged (write-with-response). Adapters that only support
  write-without-response will fail with `UNLIKELY_ERROR: 14`; retry or use
  another adapter/proxy.
* The lock advertises the Nordic UART Service. Any BLE 4.0+ adapter works; no
  bonding is required.
* If you use an ESPHome proxy, make sure it is in range and that active
  connections are allowed (`bluetooth_proxy` with `active: true` on recent
  ESPHome versions).
* Only one central can hold the connection at a time: if the WeLock app is open
  on the lock screen, the open command may fail.

## Updating

* HACS: update from HACS and restart.
* Manual: replace the directory and restart.

Configuration entries and their tokens survive updates.
