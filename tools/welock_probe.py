#!/usr/bin/env python3
"""Standalone probe for the WeLock lock (cloud + Bluetooth).

This tool needs neither Home Assistant nor the integration to be installed; it
imports the protocol modules directly so you can debug the account, inspect the
cloud answer and try a raw BLE write.

Examples
--------
    # 1. check the account and list locks
    python3 tools/welock_probe.py login --account me@example.com --password secret
    python3 tools/welock_probe.py devices --account me@example.com --password secret

    # 2. see the frame the cloud returns for an unlock (nothing is sent)
    python3 tools/welock_probe.py unlock --account me@example.com --password secret \
        --device-number 1234567890

    # 3. scan for Bluetooth locks and poll their status
    python3 tools/welock_probe.py scan --seconds 10
    python3 tools/welock_probe.py probe --address AA:BB:CC:DD:EE:FF

    # 4. full open: cloud frame + BLE write
    python3 tools/welock_probe.py unlock --account me@example.com --password secret \
        --device-number 1234567890 --address AA:BB:CC:DD:EE:FF

The cloud password is only sent over HTTPS to clientapi.welock.com.cn.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "welock_ble"


def _load_package() -> types.ModuleType:
    """Import the protocol modules without triggering the HA package __init__."""
    package = types.ModuleType("welock_ble")
    package.__path__ = [str(PKG_DIR)]
    sys.modules["welock_ble"] = package
    for name in ("const", "welock_cloud", "ble_transport"):
        spec = importlib.util.spec_from_file_location(
            f"welock_ble.{name}", PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"welock_ble.{name}"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return package


_load_package()

from welock_ble import const  # noqa: E402
from welock_ble.welock_cloud import WeLockCloud, new_identifier  # noqa: E402

try:
    from welock_ble.ble_transport import WeLockBLE, WeLockBLEError  # noqa: E402
except ImportError:  # pragma: no cover - bleak missing
    WeLockBLE = None  # type: ignore[assignment]
    WeLockBLEError = Exception  # type: ignore[assignment,misc]

try:
    import aiohttp
except ImportError:  # pragma: no cover
    sys.exit("aiohttp is required: pip install aiohttp")

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    BleakClient = None  # type: ignore[assignment]
    BleakScanner = None  # type: ignore[assignment]


def _hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


async def _client(args: argparse.Namespace) -> WeLockCloud:
    session = aiohttp.ClientSession()
    client = WeLockCloud(
        session,
        args.account,
        args.password,
        args.identifier or new_identifier(),
        debug=args.debug,
    )
    if args.code:
        await client.login_with_code(args.code)
    else:
        await client.login()
    print(f"token: {client.token}")
    print(f"refresh_token: {client.refresh_token}")
    return client


async def cmd_request_code(args: argparse.Namespace) -> None:
    session = aiohttp.ClientSession()
    client = WeLockCloud(
        session,
        args.account,
        "",
        args.identifier or new_identifier(),
        debug=args.debug,
    )
    code = await client.request_code()
    print(f"Send this code to the WeLock bot on WhatsApp: {code}")
    if client.pending_link:
        print(f"or open: {client.pending_link}")
    await session.close()


async def cmd_login(args: argparse.Namespace) -> None:
    client = await _client(args)
    await client._session.close()  # noqa: SLF001 - CLI lifetime


async def cmd_devices(args: argparse.Namespace) -> None:
    """List the account's locks with everything the integration needs."""
    client = await _client(args)
    devices = await client.async_get_devices()
    if args.raw:
        print(json.dumps(devices, indent=2, ensure_ascii=False))
        await client._session.close()  # noqa: SLF001
        return

    if not devices:
        print("no locks on this account")
    for device in devices:
        number = str(device.get("deviceNumber") or "")
        print("=" * 60)
        print(f"deviceNumber : {number}")
        print(f"deviceName   : {device.get('deviceName') or device.get('name')}")
        print(f"model        : {device.get('model')}")
        print(f"qrStr        : {device.get('qrStr')}")
        try:
            lock = await client.async_get_lock_info(number)
            info = await client.async_get_device_info(number)
        except Exception as err:  # noqa: BLE001 - best effort enrichment
            print(f"  (BLE details unavailable: {err})")
            continue
        lock_name = lock.get("deviceID")
        print(f"lockModel    : {lock.get('lockModel')}")
        print(f"bleName      : {lock_name}")
        print("BLE radios (use the entry whose name matches bleName):")
        for radio in info.get("ble") or []:
            if not isinstance(radio, dict):
                continue
            mac = str(radio.get("deviceMac") or "")
            mac = ":".join(mac[i : i + 2] for i in range(0, len(mac), 2)) or "?"
            mark = " <-- lock" if lock_name and radio.get("deviceName") == lock_name else ""
            print(
                f"  name={radio.get('deviceName')} mac={mac} "
                f"power={radio.get('power')} version={radio.get('version')}{mark}"
            )
    await client._session.close()  # noqa: SLF001


async def cmd_lockinfo(args: argparse.Namespace) -> None:
    client = await _client(args)
    info = await client.async_get_lock_info(args.device_number)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    await client._session.close()  # noqa: SLF001


async def _ble_write(address: str, payload: bytes, pair: bool) -> list[bytes]:
    if BleakClient is None:
        sys.exit("bleak is required: pip install bleak")
    notifications: list[bytes] = []

    def _on_notify(_sender, data: bytearray) -> None:
        notifications.append(bytes(data))
        print(f"notification: {_hex(data)}")

    client = BleakClient(address, pair=pair)
    await client.connect()
    try:
        service = client.services.get_service(const.BLE_SERVICE_UUID)
        print(f"service found: {service is not None}")
        write_char = client.services.get_characteristic(const.BLE_WRITE_CHAR_UUID)
        notify_char = client.services.get_characteristic(const.BLE_NOTIFY_CHAR_UUID)
        print(f"write char: {write_char}, notify char: {notify_char}")
        await client.start_notify(notify_char, _on_notify)
        for offset in range(0, len(payload), const.BLE_CHUNK_SIZE):
            chunk = payload[offset : offset + const.BLE_CHUNK_SIZE]
            print(f"> {_hex(chunk)}")
            await client.write_gatt_char(write_char, chunk, response=True)
            if offset + const.BLE_CHUNK_SIZE < len(payload):
                await asyncio.sleep(const.BLE_CHUNK_DELAY)
        await asyncio.sleep(2)
        await client.stop_notify(notify_char)
    finally:
        await client.disconnect()
    return notifications


async def cmd_unlock(args: argparse.Namespace) -> None:
    client = await _client(args)
    battery = args.battery
    device_name = args.device_name or args.device_number

    if not args.address:
        response = await client.async_get_unlock_command(
            args.device_number, device_name, battery=battery
        )
        print("cloud response:", json.dumps(response, indent=2, ensure_ascii=False))
        command = response.get("command") or response.get("cmd")
        payload = bytes.fromhex(str(command))
        print(f"frame ({len(payload)} bytes): {_hex(payload)}")
        print(
            "note: without --address the frame is built for randomFactor=0; "
            "the lock only accepts the frame derived from its live challenge."
        )
        await client._session.close()  # noqa: SLF001
        return

    if WeLockBLE is None:
        sys.exit("bleak is required: pip install bleak")

    async def _frame_for(challenge: bytes) -> bytes:
        random_factor = challenge[0] | (challenge[1] << 8)
        print(f"challenge {_hex(challenge)} -> randomFactor {random_factor}")
        response = await client.async_get_unlock_command(
            args.device_number,
            device_name,
            battery=battery,
            random_factor=random_factor,
            random_factor_data=None,
        )
        command = response.get("command") or response.get("cmd")
        payload = bytes.fromhex(str(command))
        print(f"cloud frame ({len(payload)} bytes): {_hex(payload)}")
        return payload

    print(f"opening {args.address} …")
    link = WeLockBLE(None, args.address, pair=args.pair)
    try:
        result = await link.async_unlock(command_provider=_frame_for)
    except WeLockBLEError as err:
        sys.exit(f"unlock failed: {err}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    await client._session.close()  # noqa: SLF001


async def cmd_scan(args: argparse.Namespace) -> None:
    if BleakScanner is None:
        sys.exit("bleak is required: pip install bleak")
    print(f"scanning for {args.seconds}s …")
    devices = await BleakScanner.discover(timeout=args.seconds)
    for device in devices:
        print(f"{device.address}  rssi={device.rssi}  name={device.name}")
    print("--- NUS advertisers ---")
    for device in devices:
        uuids = [str(uuid).lower() for uuid in (device.metadata or {}).get("uuids", [])]
        if const.BLE_SERVICE_UUID in uuids:
            print(f"{device.address}  name={device.name}  uuids={uuids}")


async def cmd_probe(args: argparse.Namespace) -> None:
    print(f"status poll ({len(const.FRAME_STATUS)} bytes)")
    await _ble_write(args.address, const.FRAME_STATUS, args.pair)
    await asyncio.sleep(1)
    print(f"battery poll ({len(const.FRAME_BATTERY)} bytes)")
    await _ble_write(args.address, const.FRAME_BATTERY, args.pair)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WeLock BLE probe")
    parser.add_argument("--debug", action="store_true", help="log HTTP bodies")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_account(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--account", required=True)
        sp.add_argument(
            "--password",
            help="account password (omit when using --code)",
            default="",
        )
        sp.add_argument(
            "--code",
            help="WhatsApp handshake code (WL_XXXXXXXX) obtained from the bot",
        )
        sp.add_argument("--identifier", help="stable install id (default: random)")

    p = sub.add_parser("request-code", help="send a WhatsApp login code")
    p.add_argument("--account", required=True)
    p.add_argument("--identifier", help="stable install id (default: random)")
    p.set_defaults(func=cmd_request_code)

    p = sub.add_parser("login", help="authenticate and print the token")
    add_account(p)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("devices", help="list locks with their BLE details")
    add_account(p)
    p.add_argument("--raw", action="store_true", help="print the raw cloud JSON")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("lockinfo", help="BLE details of one lock")
    add_account(p)
    p.add_argument("--device-number", required=True)
    p.set_defaults(func=cmd_lockinfo)

    p = sub.add_parser("unlock", help="ask the cloud for an unlock frame")
    add_account(p)
    p.add_argument("--device-number", required=True)
    p.add_argument("--device-name")
    p.add_argument("--battery", type=int, default=100)
    p.add_argument("--address", help="BLE MAC: also write the frame")
    p.add_argument("--pair", action="store_true")
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("scan", help="scan Bluetooth devices")
    p.add_argument("--seconds", type=float, default=10)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("probe", help="poll status/battery on a lock")
    p.add_argument("--address", required=True)
    p.add_argument("--pair", action="store_true")
    p.set_defaults(func=cmd_probe)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
