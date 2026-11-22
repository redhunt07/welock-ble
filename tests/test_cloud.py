#!/usr/bin/env python3
"""Functional test for the WeLock cloud client against a local mock server.

Run with:  python3 tests/test_cloud.py
Requires aiohttp (no Home Assistant needed).

The mock reproduces the *observed* live API: the ``welock`` route segment, the
server-issued WhatsApp code (``data.code`` + ``data.link``), the access/refresh
token pair, and the device/ble shapes returned by the real server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "welock_ble"


def load_protocol_modules() -> types.ModuleType:
    package = types.ModuleType("welock_ble")
    package.__path__ = [str(PKG_DIR)]
    sys.modules["welock_ble"] = package
    for name in ("const", "welock_cloud"):
        spec = importlib.util.spec_from_file_location(
            f"welock_ble.{name}", PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"welock_ble.{name}"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return package


load_protocol_modules()

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from welock_ble import const  # noqa: E402
from welock_ble.welock_cloud import WeLockCloud, compute_sign  # noqa: E402

UNLOCK_FRAME = "aa1e00" + "11" * 25  # arbitrary 28-byte "encrypted" frame
ISSUED_CODE = "WL_TESTCODE"  # what the server hands out, never built client-side
BOT = "8613302487016"
seen: list[dict] = []


def expected_sign(timestamp: str, identifier: str) -> str:
    """Independent re-implementation of the APK algorithm."""
    reversed_payload = (identifier + timestamp)[::-1]
    b64 = base64.b64encode(reversed_payload.encode()).decode()
    return hashlib.md5(hashlib.md5(b64.encode()).hexdigest().encode()).hexdigest()


async def handle(request: web.Request) -> web.Response:
    body = await request.text()
    record = {
        "path": request.path,
        "headers": dict(request.headers),
        "body": json.loads(body) if body else {},
    }
    seen.append(record)

    if "/Account/token/" in request.path:
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "access_token": "TOKEN-123",
                    "refresh_token": "REFRESH-456",
                    "account": "390000000000",
                    "email": "me@example.com",
                    "nickName": "Lock#0001",
                },
            }
        )
    if request.path.endswith(f"/Account/refresh/{const.ROUTE_SEGMENT}"):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "access_token": "TOKEN-REFRESHED",
                    "refresh_token": "REFRESH-789",
                },
            }
        )
    if request.path.endswith(f"/Account/MessageCode/{const.ROUTE_SEGMENT}"):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "code": ISSUED_CODE,
                    "link": f"http://wa.me/{BOT}/?text={ISSUED_CODE}",
                },
            }
        )
    if request.path.endswith(f"/Account/VerificationCodeLogin/{const.ROUTE_SEGMENT}"):
        return web.json_response({"code": 0, "data": {"access_token": "CODE-TOKEN"}})
    if request.path.endswith(f"/Account/MessageLogin/{const.ROUTE_SEGMENT}"):
        return web.json_response({"code": 0, "data": {"access_token": "MSG-TOKEN"}})
    if request.path.endswith("/Device/GetDevices"):
        return web.json_response(
            {
                "code": 0,
                "totalCount": 1,
                "data": [
                    {
                        "deviceNumber": "999",
                        "deviceName": "Front door",
                        "deviceMac": "AA:BB:CC:DD:EE:FF",
                    }
                ],
            }
        )
    if request.path.endswith("/device/GetLockDevice"):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "lockNumber": "999",
                    "lockModel": "TOUCHEBL63",
                    "deviceID": "WeLockA1B2C3",
                },
            }
        )
    if request.path.endswith("/device/GetDeivceInfo"):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "deviceNumber": "999",
                    "ble": [
                        {
                            "position": 2,
                            "deviceName": "WeLockG0W1B0X",
                            "deviceMac": "112233445566",
                            "power": 0,
                        },
                        {
                            "position": 1,
                            "deviceName": "WeLockA1B2C3",
                            "deviceMac": "AABBCCDDEEFF",
                            "power": 45,
                            "devicePassword": "60a1bSiJEwAxrV/nal63TA==",
                        },
                    ],
                },
            }
        )
    if request.path.endswith("/Command/UnLock"):
        return web.json_response(
            {
                "code": 0,
                "data": {"serverID": "srv-1", "command": UNLOCK_FRAME},
            }
        )
    return web.json_response({"code": 1002, "msg": "unknown path"}, status=200)


async def main() -> int:
    app = web.Application()
    app.router.add_route("POST", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8765)
    await site.start()

    failures: list[str] = []
    try:
        # Point the client at the mock server.
        const.API_BASE = "http://127.0.0.1:8765/api"
        import welock_ble.welock_cloud as cloud_module

        cloud_module.API_BASE = const.API_BASE

        if const.ROUTE_SEGMENT != "welock":
            failures.append(f"route segment: {const.ROUTE_SEGMENT!r}")
        for path in (
            const.PATH_LOGIN,
            const.PATH_MESSAGE_CODE,
            const.PATH_MESSAGE_LOGIN,
            const.PATH_REFRESH,
        ):
            if "/android" in path or not path.endswith("/welock"):
                failures.append(f"path segment: {path!r}")

        async with aiohttp.ClientSession() as session:
            client = WeLockCloud(session, "me@example.com", "secret", "install-id-1")
            token = await client.login()
            if token != "TOKEN-123":
                failures.append(f"login token: {token!r}")
            if client.refresh_token != "REFRESH-456":
                failures.append(f"refresh token: {client.refresh_token!r}")
            if client.account_info.get("account") != "390000000000":
                failures.append(f"account info: {client.account_info!r}")

            devices = await client.async_get_devices()
            if not devices or devices[0]["deviceNumber"] != "999":
                failures.append(f"devices: {devices!r}")

            lock = await client.async_get_lock_info("999")
            if lock.get("deviceID") != "WeLockA1B2C3":
                failures.append(f"lock info: {lock!r}")

            info = await client.async_get_device_info("999")
            ble = {entry["deviceName"]: entry for entry in info.get("ble", [])}
            if ble.get("WeLockA1B2C3", {}).get("deviceMac") != "AABBCCDDEEFF":
                failures.append(f"device info: {info!r}")

            await client.refresh()
            if client.token != "TOKEN-REFRESHED":
                failures.append(f"refreshed token: {client.token!r}")
            if client.refresh_token != "REFRESH-789":
                failures.append(f"refreshed refresh token: {client.refresh_token!r}")

            response = await client.async_get_unlock_command("999", "Front door", battery=45)
            if response.get("command") != UNLOCK_FRAME:
                failures.append(f"unlock command: {response!r}")

        # --- assertions on what the mock server received ---------------------
        login_call = next(c for c in seen if "/Account/token/" in c["path"])
        body = login_call["body"]
        if body.get("account") != "me@example.com":
            failures.append(f"login account: {body.get('account')!r}")
        if body.get("password") != "secret":
            failures.append("login password missing")
        if body.get("isIot") != "1":
            failures.append(f"login isIot: {body.get('isIot')!r}")
        if body.get("platform") != "android":
            failures.append(f"login platform: {body.get('platform')!r}")

        headers = login_call["headers"]
        for required in (
            "sign",
            "nonce",
            "requestClientTimestamp",
            "identifierForVendor",
            "appVersion",
            "operatingSystem",
            "isPhysicalDevice",
            "APP_CLIENT",
            "User-Agent",
            "Content-Type",
        ):
            if required not in headers:
                failures.append(f"missing header {required}")

        if headers.get("operatingSystem") != "android":
            failures.append("operatingSystem header")
        if len(headers.get("nonce", "")) != 12 or not headers.get("nonce", "").isdigit():
            failures.append(f"nonce: {headers.get('nonce')!r}")

        expected = expected_sign(
            headers["requestClientTimestamp"], headers["identifierForVendor"]
        )
        if headers.get("sign") != expected:
            failures.append(f"sign mismatch: {headers.get('sign')} != {expected}")
        if headers.get("sign") != compute_sign(
            headers["requestClientTimestamp"], headers["identifierForVendor"]
        ):
            failures.append("compute_sign disagrees with the reference implementation")

        refresh_call = next(c for c in seen if "/Account/refresh/" in c["path"])
        if refresh_call["body"].get("access_token") != "TOKEN-123":
            failures.append(f"refresh access_token: {refresh_call['body']!r}")
        if refresh_call["body"].get("refresh_token") != "REFRESH-456":
            failures.append(f"refresh refresh_token: {refresh_call['body']!r}")

        unlock_call = next(c for c in seen if c["path"].endswith("/Command/UnLock"))
        unlock_body = unlock_call["body"]
        if unlock_body.get("deviceNumber") != "999":
            failures.append(f"unlock deviceNumber: {unlock_body.get('deviceNumber')!r}")
        if unlock_body.get("deviceName") != "Front door":
            failures.append(f"unlock deviceName: {unlock_body.get('deviceName')!r}")
        if unlock_body.get("battery") != "45":
            failures.append(f"unlock battery: {unlock_body.get('battery')!r}")
        if not str(unlock_body.get("randomFactor", "")).isdigit():
            failures.append(f"unlock randomFactor: {unlock_body.get('randomFactor')!r}")
        if "randomFactorData" in unlock_body:
            failures.append("randomFactorData should be omitted when not supplied")
        if unlock_call["headers"].get("Authorization") != "TOKEN-REFRESHED":
            failures.append(
                f"unlock Authorization: {unlock_call['headers'].get('Authorization')!r}"
            )

        # WhatsApp handshake: the server issues the code, the client only relays
        # it and spends it on MessageLogin.  Nothing is generated locally.
        async with aiohttp.ClientSession() as code_session:
            code_client = WeLockCloud(code_session, "390000000000", "", "ID-CODE")
            pending = await code_client.request_code()
            if pending != ISSUED_CODE:
                failures.append(f"issued code: {pending!r}")
            if code_client.pending_link != f"http://wa.me/{BOT}/?text={ISSUED_CODE}":
                failures.append(f"pending link: {code_client.pending_link!r}")
            code_token = await code_client.login_with_code(pending)
        if code_token != "MSG-TOKEN":
            failures.append(f"code login token: {code_token!r}")

        code_call = next(
            c
            for c in seen
            if c["path"].endswith(f"/Account/MessageLogin/{const.ROUTE_SEGMENT}")
        )
        if code_call["body"].get("code") != pending:
            failures.append(f"code login body code: {code_call['body'].get('code')!r}")
        if code_call["body"].get("account") != "390000000000":
            failures.append(
                f"code login body account: {code_call['body'].get('account')!r}"
            )
        if code_call["body"].get("isIot") != "1":
            failures.append(f"code login isIot: {code_call['body'].get('isIot')!r}")

        message_call = next(
            c
            for c in seen
            if c["path"].endswith(f"/Account/MessageCode/{const.ROUTE_SEGMENT}")
        )
        if message_call["body"].get("messageType") != "WhatsApp":
            failures.append(f"messageType: {message_call['body'].get('messageType')!r}")
        if not isinstance(message_call["body"].get("randomCode"), int):
            failures.append(
                f"MessageCode randomCode must be an int: "
                f"{message_call['body'].get('randomCode')!r}"
            )

        # A rejected MessageCode reply must be surfaced, not swallowed.
        async with aiohttp.ClientSession() as bad_session:
            bad_client = WeLockCloud(bad_session, "nobody@example.com", "", "ID-BAD")
            try:
                await bad_client.request_code()
            except Exception as err:  # noqa: BLE001
                failures.append(f"unexpected error for a valid reply: {err}")

        # The 6-digit SMS path must still route to VerificationCodeLogin.
        async with aiohttp.ClientSession() as sms_session:
            sms_client = WeLockCloud(sms_session, "390000000000", "", "ID-SMS")
            sms_token = await sms_client.login_with_code("123456")
        if sms_token != "CODE-TOKEN":
            failures.append(f"sms login token: {sms_token!r}")
        sms_call = next(
            c
            for c in seen
            if c["path"].endswith(f"/Account/VerificationCodeLogin/{const.ROUTE_SEGMENT}")
        )
        if sms_call["body"].get("code") != "123456":
            failures.append(f"sms login body code: {sms_call['body'].get('code')!r}")
    finally:
        await runner.cleanup()

    if failures:
        print("FAILED")
        for failure in failures:
            print(" -", failure)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
