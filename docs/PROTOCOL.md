# WeLock BLE protocol — reverse-engineering report

Target: **WeLock Android app 4.5.8** (`cn.sixpower.wlock`, versionCode 9988, APKPure build)
APK SHA-256: `7b33794fc795c6c82491e564f43c4ef3f413d87dcf5af6baa17e8a966717a11d`
Dart AOT payload analysed: `lib/arm64-v8a/libapp.so`, SHA-256 `32f26edb675bd0c7e70d5cdd4d5f4513d97a67e92cf1b26b471cf62e9bf3a106`
Dart 3.7.2, snapshot hash `d91c0e6f35f0eb2e44124e8f42aa44a7`, target `android arm64`.

> **Note on identifiers.** Every account/lock identifier in this document is an
> anonymised example: `390000000000` is the account, `1234567890` the device
> number, `WeLockA1B2C3` the lock name, `AA:BB:CC:DD:EE:FF` its Bluetooth
> address. The frame bytes, tokens and suffixes are real captured values.

The app is Flutter. **All BLE logic lives in Dart AOT code** (`libapp.so`), so JADX/DEX
shows only the Java shell. Everything below comes from `blutter` disassembly of the AOT
snapshot (`/tmp/welock/blutter_out/asm/...`) cross-checked against the Dart object pool
(`objs.txt`, `pp.txt`) and the raw string table.

---

## 0. TL;DR — the single most important finding

**The WeLock lock does not accept commands built by the app.**
Every operational BLE frame is a hex string produced by the WeLock cloud
(`https://clientapi.welock.com.cn/api/Command/...`), which the app forwards verbatim to the
lock. There is **no AES / no payload crypto anywhere in the Dart code** — the only hashes in
the whole app are `MD5` (used for the HTTP `sign` header and cache keys) and `SHA-256` (used
for push aliases). The AES key lives on the server and in the lock, provisioned at
`/api/Command/Activation`.

Consequences for a Home Assistant plugin:

| Capability | Local (dongle only) | Needs WeLock cloud |
|---|---|---|
| Connect / discover / notify | ✅ | — |
| Challenge/authentication (`U0`) | ✅ (frames are built locally) | — |
| **Unlock (`U1`)** | ❌ | ✅ (`/api/Command/UnLock` returns the frame) |
| Battery poll / status poll | ✅ (legacy `AA 60` / `AA 88` frames) | — |
| Add/delete card, PIN, fingerprint, reset, OTA, sync time… | ❌ | ✅ |

So the plugin architecture is: **WeLock cloud account → command bytes → generic BLE dongle
(local `bleak` / HA `bluetooth` stack) → lock.** This replaces the vendor WIFIBOX gateway while
keeping the vendor's command generation, which is the only way to speak to the lock without
re-implementing the (undocumented, server-side) AES layer.

### 0.1 The token is derived from the live challenge — CRACKED AND VERIFIED

The cloud call is not free-form: `/api/Command/UnLock` must be given
`randomFactor` = the challenge the lock just sent, read as a **little-endian uint16**
(payload bytes `XX YY` of the `55 30 XX YY` notification → `XX | YY << 8`).

Verified against two independent HCI captures of the official app *and* reproduced live
against the lock from a generic USB dongle:

| Lock challenge | `randomFactor` | Cloud frame | Lock answer |
|---|---|---|---|
| `55 30 5a 2f` | 12122 (`0x2f5a`) | `U1wDAwtdaHAAAAp` | `55 31 01` ✅ (app) |
| `55 30 a7 44` | 17575 (`0x44a7`) | `U1Q9AQTkn6AAAA8` | `55 31 01` ✅ (app) |
| `55 30 f6 31` | 12790 (`0x31f6`) | `U1z4Az7u2xAAAAZ` | `55 31 01` ✅ (HA dongle) |

Rules learned the hard way:

- `randomFactorData` must be **omitted/null**: any value makes the server answer with the null
  token `AAAAAAAA` (and the null suffix `AAAAA`).
- `battery` is irrelevant to the token; only the low byte of `randomFactor` is used by the
  token function, but the suffix depends on the full value, so pass the exact uint16.
- The token is deterministic per `randomFactor` (same request → same token), so the cloud is a
  pure function here.
- **One authentication per connection**: a second `U0 01 …` on the same link is rejected with
  `55 30 00 <challenge>` and the link then fails. Each unlock needs its own connection.
- Write requests (`response=True`) are mandatory; ATT write commands are silently dropped on a
  weak link. Chunk at 20 bytes.

---

## 1. BLE transport

### 1.1 GATT profile

The app uses the **Nordic UART Service (NUS)** layout. UUIDs are hard-coded string constants in
`package:welock_common/service/ble_lock_face_impl.dart` (function at `0x7b1c24`, size `0x1b4`,
which stores the notify/write characteristic UUIDs into the connection object):

| Role | UUID | Evidence |
|---|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | string used by `discoverServices()` at `0x7b1dd8` |
| Write (app → lock) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | `r16 = "6e400002-…"` stored into field `0x1f` |
| Notify (lock → app) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | `r16 = "6e400003-…"` stored into field `0x1b` |

Additional UUIDs present in the binary but not used by the lock flow:
`0000fee0-0000-1000-8000-00805f9b34fb`, `0000fee1-…` (vendor service), CCCD
`00002902-0000-1000-8000-00805f9b34fb`.

The BLE plugin is the **legacy `flutter_blue`** package
(`package:flutter_blue/src/bluetooth_characteristic.dart`), i.e. `characteristic.write()` +
`characteristic.value().listen(...)`, no `writeWithoutResponse` fast path, no `requestMtu`
in the lock path (only in the Nordic DFU path) → **default ATT MTU, 20-byte writes**.

### 1.2 Write path (chunking)

`BleLockOperateFaceImpl::_sendCmd` (`0x7a5cf0`, `ble_lock_face_impl.dart` L12500) and
`_writeData` (`0x7a5e38`) implement:

```dart
Future<void> _sendCmd(dynamic bytes) async {
  ...guard...
  final data = (bytes as List).cast<int>();
  if (data.length > 20) {
    _writeData(data.sublist(0, 40));   // first slice
    Timer(const Duration(milliseconds: ...), () {
      _sendCmd(data.sublist(20));      // remaining slices, recursively
    });
  } else {
    _writeData(data);                  // single 20-byte-or-less write
  }
}

Future<void> _writeData(List<int> data) async {
  log("write data: $data");
  await <write characteristic>.write(data);
}
```

Notes:
- `_writeData` always logs the literal string `"write data: "` followed by the byte list — this
  is the single best place to hook a debugger/sniffer.
- The chunker is delay-based (a `Timer` between slices), so slices must be written **in order
  with a short pause** (the app uses a timer of a few tens of ms). A real BLE link needs
  ~30–60 ms between writes on a bonded link.
- Because the frame length for encrypted commands is > 20, the app is not doing a single
  `write()`; a generic dongle implementation must chunk at 20 bytes and wait.

### 1.3 Notification path

`BleLockOperateFaceImpl` subscribes with `characteristic.value().listen(...)`
(`0x79a5f8`), storing the `StreamSubscription` in field `0x53`. The handler (`0x79c890`,
size `0x3d0c`) logs `"received data: "`, `" firstSendCmd: "`, `" streamSubscriptionData:"`,
then:

- sets an internal timeout/flag field `0x3f = 65` (0x41),
- clears the "pending buffer" field `0x87`,
- switches on the pending operation id (`FirstSendCmd`, see §3) and maps the response to a
  `LockOperatorStatus` (`failed`, `disconnectedBle`) or a localized UI message
  (`src_add_card_fail_exist`, `src_add_card_fail_full`, `src_use_ble_first_ck_admin_tip`,
  `src_lock_soft_not_support`, `src_connected_send_cmd`, `src_disconnected_success`),
- calls `_responseRequestServerID()`.

### 1.4 Response parsing

Responses are parsed as a **byte buffer addressed by fixed offsets**, not as a
length-prefixed structure. The parser uses `ByteData.getUint8(index)` (dynamic dispatch
`GDT[cid_x0 + -0xc44]`), and the extracted indices are:

| Response field | Byte offset read | Stored into (impl field) |
|---|---|---|
| status / result code | **4** | `0x27`, `0x2f`, `0xbf` |
| status / result code | **6** | `0x27`, `0x2f`, `0xbf`, `0x1b` |
| status / result code | **8** | `0x2f`, `0xbf`, `0xff` |

Complete list of `getUint8` sites inside the notify handler (`0x79cb2c` … `0x7a1200`):
`(6→0x27)`, `(8→0x2f)`, `(4→0x27)`, `(6→0x2f)`, `(4→0x2f)`, `(8→0xbf)`, `(4→0xbf)`,
`(8→0x1b)`, `(8→0xff)` — i.e. **the meaningful bytes of every lock reply sit at offsets
4, 6 and 8** of the notification payload, and they are *status/error codes*, not a
length-prefixed TLV.

`_responseRequestServerID` (`0x7a5094`, size `0x2e0`) then does:

```dart
Future<void> _responseRequestServerID({int index = 1}) async {
  if (receivedBuffer == null) return;
  if (index > 3) return;
  final serverId = receivedBuffer.field_7;      // serverID echoed by the lock
  final status   = await updateCmdStatus(this.field_b, serverId);
  if (status == 0) { log("_responseRequestServerID success:"); receivedBuffer = null; }
  else            { log("_responseRequestServerID error:  - code: "); }
}
```

`updateCmdStatus` is `CommandService.updateCmdStatus` (`0x7a5374`) — it reports the executed
command to the cloud (`/api/ufun/CommandStatus` family) so the server can log the operation.

### 1.5 Connection lifecycle

- `_setBleStateListen` (`0x7b3c94`) subscribes to `BluetoothDeviceState`; on disconnect it logs
  `"10 ble disconnected "` and `"断开连接倒计时： "` (disconnect countdown) and calls
  `connectBle()`.
- `_autoDisconnect60s` (`ble_lock_face_impl.dart` L21777) → the app disconnects the link after
  **60 s of inactivity**. A HA plugin should connect on demand and disconnect right after the
  command, never keep the link open.
- `_bleGuard` (L3262) wraps operations with timeouts/retries; `_operationStart()` is the common
  entry point used by every `BleLockOperateFaceImpl` command method.
- The app scans by **name** (`"scan not the same name "`, `"found the connected lockName "`,
  `"startScan"`, `"found the new lockName "`, `", rssi="`) rather than by address, and gates the
  gateway pairing on RSSI (`> -60 dBm`).

---

## 2. Frame format (Nordic UART: ASCII command in, hex reply out)

The lock speaks an ASCII command language over NUS. Captured from the official app
(HCI snoop `btsnoop_hci_261121_135227.log`) and reproduced live from a generic dongle.

### 2.1 Challenge

```
TX  U0
RX  55 30 XX YY      ; XX = random, YY ≈ battery level (47…51 in every capture)
```

### 2.2 Authentication

```
TX  U0 01 <ts:4 LE> <token:8>
RX  55 30 XX YY      ; accepted (4 bytes: echo of the challenge)
RX  55 30 00 XX YY   ; rejected (5 bytes: extra 00 status)
```

`<ts>` is a Unix timestamp as a little-endian uint32; the lock does not validate it strictly
(`int(time.time())` was accepted live, while the app sends `now + 86400`). `<token>` is the 8
ASCII characters the cloud returned for the current challenge (§0.1).

### 2.3 Command

```
TX  U1 <token:8> <suffix:5>
RX  55 31 <status> XX YY
```

`<status>` `01` = success, `00` = rejected; the trailing `XX YY` echoes the challenge.
The complete `U1` frame is returned verbatim by `/api/Command/UnLock` in the `command` field:
the app never builds it. The suffix (`AAAAp`, `AAAA8`, `AAAAZ`…) is generated server-side and
varies with `randomFactor`.

### 2.4 Legacy `AA` frames

`CommandService::getDeviceStatusBytes` (`0x7a9830`) and `getBatteryBytes` (`0x7b8c14`) build
`AA 88 …` / `AA 60 …` (30 bytes, zero padded); a 6-byte variant lives in
`ble_gateway_set_wifi_page.dart` (`_getBatteryBytes` `0x7c618c`). These belong to the older
non-NUS protocol / the WiFi gateway and were not observed in the direct-BLE unlock path.

---

## 3. Operation ids (`FirstSendCmd`)

Enum extracted from the Dart object pool (`objs.txt`), class `FirstSendCmd`. This is the
operation id the app stores before sending and matches in the notification handler:

| Name | Value |
|---|---|
| `SEND_OPEN_LOCK` | 0 |
| `SEND_SET_TIME` | 1 |
| `SEND_ADD_CARD` | 3 |
| `SEND_DEL_CARD` | 4 |
| `SEND_DEL_MANAGER_CARD` | 5 |
| `SEND_ACTIVE_LOCK` | 6 |
| `SEND_RESET_LOCK` | 7 |
| `SEND_LOAD_LOCK_LOG` | 8 |
| `SEND_ADD_PWD` | 9 |
| `SEND_DEL_PWD` | 10 |
| `SEND_SEND_OTA` | 11 |
| `SEND_MODIFY_BLE_PWD` | 12 |
| `SEND_DEL_FP` | 14 |
| `SEND_SET_TIME_AFTER_OPERATION` | 15 |
| `ONLY_CONNECT_LOCK` | 16 |
| `SEND_SEARCH_LOCK_NAME` | 17 |
| `SEND_SET_LOCKED_DELAY_TIME` | 19 |
| `SEND_ADD_MANAGER_CARD` | 20 |
| `SEND_DEL_ALL_CARD` | 21 |
| `SEND_GET_DEVICE_STATUS` | 22 |
| `SEND_LOCAL_LOAD_IDS` | 24 |
| `SEND_LOCAL_ADD_CARD` | 25 |
| `SEND_LOCAL_ADD_FP` | 26 |
| `SEND_LOCAL_ADD_PWD` | 27 |
| `SEND_LOCAL_DEL_ID` | 28 |
| `SEND_LOCAL_CLEAR_USERS` | 29 |

Related enums (same source):

- `CommandStatus`: `unKnow_1=0, waiting0=1, success1=2, fail2=3, cmd3=4, cmd4=5, cmd5=6, cmd6=7 … cmd13=14`
- `CommandExeErrorStatus`: `unKnow_1=0, unKnowCmd4=1, notAllow5=2, cantConnect6=3, cmdTimeout7=4, notExist8=5, alreadFull9=6, hasExist10=7, unKnowCmd11=8`
- `LockStatus`: `unlocking=0, locked=2`
- `LockType`: `FRONT1=0, BACK2=1, OTHER0=2`
- `LockLocalRole`: `admin0=0, user1=1, superAdmin2=2`
- `DeviceType`: `WeLock1=0, FeiBiGateWay2=1, WeLockBleGateWay3=2, NfcLock4=3, TuYaGateWay5=4, AD6=5, DoorSensor7=6`
- `OpenType` (unlock method reported in logs): `BLE_APP6=5, WELOCK_TEM_PWD11=10, BLE_TEM_PWD14=13, BLE_GW_OPEN17=16, BLE_GW_SWITCH210=20, BLE_DOOR_SENSOR31=21, auto_locked=29, FP_ERROR_LOG99=19, LOCK_RESTART211=40, panel_ble_tem_pwd=36`
- `ConnectType`: `mb_ble=0, ble_gw=1`
- `FunctionName`: `resetLock=9, modifyBlePassword=11, activeLock=12, lockLog=13, errorTime=28, disableLockRecord=30`

The command builders exposed by `CommandService` (all cloud-backed, all returning
`Future<RequestBleCmdModel?>`) are: `getDeviceStatusBytes`, `getBatteryBytes`, `getOpenBytes`,
`getSyncTimeBytes`, `getSyncTimeBytes0x39`, `getUFunGwSetWifiBytes`, `updateCmdStatus`, plus the
card/PIN/fingerprint/reset/OTA variants.

---

## 4. Cloud API

Base: `https://clientapi.welock.com.cn/api` (string `"https://clientapi.welock.com.cn/api"` in
`package:welock_common/data/api/apis.dart`).

### 4.1 Endpoint inventory (relevant subset)

```
/api/Account/token/{seg}       login (account + password)
/api/Account/refresh/{seg}     token refresh (both tokens in the body)
/api/Account/Registered/       sign-up
/api/Account/ChangePassword
/api/Device/GetDevices         device list
/api/device/GetLockDevice      lock/BLE details (BleLockInfo)
/api/device/GetDeivceInfo      device state (battery, lock status)
/api/device/LocalUnLock        local unlock bookkeeping
/api/device/GetUnlockRecord    unlock history
/api/device/AlterDevName
/api/device/DevicePassword     BLE password management
/api/Command/UnLock            → {"serverID":…, "command":"<hex>"}   ← unlock frame
/api/Command/Activation        activate/provision a lock (installs the AES key)
/api/Command/Reset
/api/Command/DeviceSyncTime, /DeviceSyncTimeV2
/api/Command/AddCard, /SetCard, /DeleteID
/api/Command/AddPassword, /SetPassword, /DelPassword
/api/Command/AddFingerprint, /DeleteFingerprint
/api/Command/LockDelay, /SetBleGateway, /CommunicationPassword, /SyncID, /OTA,
/api/Command/ReadUnlockRecord, /Report, /ClearOrdinaryUsers
/api/ufun/*                    gateway family (WIFIBOX): LockStatus, UnLock, CommandStatus,
                               GatewaysStatus, AddGateways, SetLockTime, SetPassword, Upgrade…
/api/feibi/*                   FeiBi gateway family
```

### 4.2 Login

`UserServices::login` (`0x54ecb8`, `user_services.dart` L10) POSTs to
`RequestApi.LOGIN` (`/api/Account/token/`) with the JSON body:

```json
{
  "account":  "<email or phone>",
  "password": "<password>",
  "timeOffset": <int>,
  "platform": "android",
  "model": "<device model>",
  "language": "<locale>",
  "Jpushid": "<push id>",
  "isIot": "1"
}
```

#### 4.2.1 The route segment is `welock`

Every `RequestApi` path ends with a runtime segment. It is **not** free-form and
**not** `android`: `app_init.dart` sets it once at startup
(`0xb93280: r0 = "welock"` -> `0xb93298: StoreStaticField(0x10fc, r0)`) and every
request builder interpolates that field. Calling any `/Account/...` route with
another segment (`android`, `app`, `ios`, `web`, `h5`, `client`, `api`, `v2`,
`9988`, `4.5.8`) answers `{"code":1002}` or an ASP.NET 404. With `welock` the
same request succeeds, which is what unblocked the whole cloud path.

#### 4.2.2 Password login (verified live)

`POST /api/Account/token/welock` with the body above (password sent verbatim,
never hashed) answers:

```json
{"code":0,"data":{"access_token":"661F8F87...","refresh_token":"8e7da46f...",
 "account":"390000000000","nickName":"Lock#0001","areacode":"39",
 "tel":"390000000000","email":"user@example.com","push":true,
 "marketing":false}}
```

One call therefore returns both tokens and the account identity, and the server
resolves an e-mail login to the canonical phone account (`account` + `areacode`).

Headers actually required: `User-Agent: APP_CLIENT` and
`Content-Type: application/json`. Without the user agent Cloudflare answers HTTP
403 before the request reaches ASP.NET. `sign`, `nonce` and
`requestClientTimestamp` are accepted but not enforced (a request carrying none
of them still logs in).

#### 4.2.3 WhatsApp-code login (server-issued code)

| Route (after `/api`) | Builder | Payload keys |
|---|---|---|
| `/Account/MessageCode/welock` | `_getSocialMediaCode` (`0x832594`) | `account`, `messageType` (`"WhatsApp"`), `platform`, `model`, `language`, `randomCode`, `timeOffset` |
| `/Account/MessageLogin/welock` | `LOGIN_BY_SOCIAL_CODE` (`0x568710`) | `account`, `code` (`WL_` + 8 chars), `platform`, `model`, `language`, `isIot` |
| `/Account/VerificationCodeLogin/welock` | `loginSms` (`0x566a74`) | `account`, `code` (6 digits), `platform`, `model`, `language`, `isIot` |
| `/Account/GetVerificationCode/welock` | `GET_V_CODE` | `account`, `language`, `type` (1-11), `areacode`, `Ticket`, `Randstr` |

`MessageCode` answers:

```json
{"code":0,"data":{"code":"WL_QRSTUVWX",
 "link":"http://wa.me/8613302487016/?text=WL_QRSTUVWX"}}
```

The code is **issued by the server** and is `WL_` followed by eight uppercase
alphanumerics. The client never builds it: the literal `"WL_"` does not appear in
`libapp.so` at all; the login page reads `data.code` (literal `"code"` at
`0x8323c0`) and displays it verbatim. `randomCode` is a client-side widget value
(`_BodyState.field_7b`) that the server ignores. The bot's WhatsApp number also
arrives in the reply (`data.link`), so it is not hard-coded either.

The user sends the code to that bot; the bot confirms it server-side and only
then `MessageLogin` succeeds and returns the token pair. Spending a code the bot
has not confirmed answers `{"code":3003}`. `VerificationCodeLogin` is the
separate 6-digit SMS route (`code:Length(6-6)`), not this handshake.

#### 4.2.4 Token refresh

`POST /api/Account/refresh/welock` needs **both** tokens in the JSON body
(`{"access_token":...,"refresh_token":...}`) and no `Authorization` header.
Sending only one of them answers `{"code":1002,"msg":"access_token"}` (or
`"refresh_token"`): `msg` names the missing field. A successful refresh rotates
both tokens.

#### 4.2.5 Authenticated endpoints (verified live)

With `Authorization: <access_token>`:

| Route | Body | Live answer |
|---|---|---|
| `/Device/GetDevices` | `{}` | `{"code":0,"totalCount":1,"data":[{"deviceName":"1234567890","deviceNumber":"1234567890","model":"TOUCHEBL63","ble":[{"position":2,"deviceName":"WeLockG0W1B0X","deviceMac":"112233445566","power":0},{"position":1,"deviceName":"WeLockA1B2C3","deviceMac":"AABBCCDDEEFF","power":45}]}]}` |
| `/device/GetLockDevice` | `{"deviceNumber":"1234567890"}` | `{"code":0,"data":{"lockNumber":"1234567890","lockModel":"TOUCHEBL63","deviceID":"WeLockA1B2C3","deviceModel":26,"qrStr":"https://service.welock.com/?code=..."}}` |
| `/device/GetDeivceInfo` (sic) | `{"deviceNumber":"1234567890"}` | the same device plus a per-radio `devicePassword` (`"60a1bSiJEwAxrV/nal63TA=="` for the lock) and `version{modelName:"Touch",modelNumber:26,hardware:5,firmware:4}` |
| `/Command/UnLock` | `{"lockNumber":"1234567890"}` or `{"deviceNumber":"1234567890"}` | `{"code":1002}`: the cloud/gateway unlock path is not usable for this lock, so the direct-BLE frame is the path that works |

`GetLockDevice.deviceID` names the lock's own radio inside the `ble` array, which
is how the integration pairs `deviceNumber` -> BLE MAC (`AA:BB:CC:DD:EE:FF` here,
battery 45). The second radio (`WeLockG0W1B0X`, power 0) is the WIFIBOX gateway.

**Error `1002`.** Generic non-success over HTTP 200. Its `msg` is sometimes a
field-level validation message (`account:Length(...)`, `password:Length(6-20)`,
`code:Length(6-6)`, `type:Value(1~11)`, `refresh_token`), which *is*
diagnostic; an empty `msg` means the request was well-formed but rejected.
Success is `{"code":0}` (`200`/`2000` also occur on older routes).

Both API hosts are live and serve the same routes:
`https://clientapi.welock.com.cn/api` (used by the mobile app) and
`https://clientapi.we-lock.com/api`. `https://clientapi.we-lock.com/file/video/usb_video/`
is a media-only host. `/api/Basis/VersionCheck2` answers `1000` on both hosts,
which shows the request headers, `sign` and `nonce` are accepted.

The token obtained from either login path is stored in the config entry; the
integration never needs the password again. When the token expires the entry
raises `ConfigEntryAuthFailed`, which starts Home Assistant's reauth flow (and,
for code accounts, requests a fresh WhatsApp code).

### 4.3 Request signing (`sign` header)

`StringUtil::sign` (`0x5646e8`, `package:welock_common/common/string_util.dart` L286) is:

```dart
static String sign(dynamic a, dynamic b) {
  final s = "$a$b";                 // interpolate
  final r = reverseString(s);       // StringUtil::reverseString
  final b64 = base64Encode(utf8.encode(r));
  final h1 = md5(utf8.encode(b64)).toString();   // hex
  final h2 = md5(utf8.encode(h1)).toString();    // hex
  return h2;
}
```

Called from `HttpUtils` (`0x563d48`, `http_utils.dart`) with
`a = requestClientTimestamp` (ms, as string) and `b = identifierForVendor`
(`PhoneInfo.identifierForVendor`, nullable). The headers assembled by the same function are:

```
appVersion, deviceModel, requestClientTimestamp, identifierForVendor,
sign, nonce, operatingSystem ("android"), isPhysicalDevice,
APP_CLIENT, User-Agent, Authorization
```

`nonce` = `randomDigits12()` (12 random digits). `Authorization` carries the token returned by
`/api/Account/token/{seg}` (raw token, no `Bearer` prefix). The HTTP client also handles a `1000` response code as "expired token"
and refreshes it (`/api/Account/refresh/{seg}`).

### 4.4 Unlock request

`CommandService::getOpenBytes` (`0x7aff10`, size `0x1fc`, `cmd_service.dart`) builds a
`Map<String,String?>` and POSTs it to the literal URL
`https://clientapi.welock.com.cn/api/Command/UnLock`:

| key | value |
|---|---|
| `deviceNumber` | lock identifier from the device model (`1234567890`) |
| `deviceName` | lock name (`WeLockA1B2C3`) — the server rejects any other value |
| `randomFactor` | **the lock challenge read as little-endian uint16** (`XX \| YY << 8`) |
| `battery` | battery level; does not influence the token |
| `randomFactorData` | must be **omitted**; any value yields the null token `AAAAAAAA` |

Response model: `RequestBleCmdModel.fromJson` (`0x7a767c`) reads the JSON keys
**`serverID`** and **`command`**. `RequestBleCmdModel.cmd` (`0x7a71fc`) returns
`StringFormat.hexStr2Bytes(command)` — i.e. `command` is a **hex string** that must be decoded
to bytes and written to the lock as-is. It decodes to the complete `U1` frame, token included.

The call must be made **after** the challenge has been read from the lock: the token is a
deterministic function of `randomFactor` (§0.1), so a frame fetched before connecting is only
valid for the challenge it was built from.

### 4.5 Reporting

After the lock replies, the app calls `CommandService.updateCmdStatus` (`0x7a5374`) with the
`serverID` from the reply, so the cloud records the executed command. This is optional for a
third-party client (the lock works either way) but keeps the cloud history consistent.

---

## 5. Lock identity / BLE password

`BleLockInfo` (`package:welock_common/models/ble_lock_model.dart`, class id 328, size `0x1c`)
fields, from its JSON parser:

```
deviceMac, deviceName, devicePassword, position, power, version
```

- `deviceMac` — the lock's BLE address (what a generic dongle needs to connect).
- `devicePassword` — the lock's BLE password (provisioned; used by
  `/api/Command/CommunicationPassword` and by the lock's own auth).
- `power` — battery level.
- `version` — firmware.

`WeLockDevice` (`device_model.dart`) exposes `deviceNumber`, `deviceName`, `devicePassword`,
`ble`, `features`, `gatewayModel`, `unlockNumber`, `roleLevel`, `type`, `model`, `lease`,
`expireDate`, `groupID/groupName`, `packageCode/packageMain`. `deviceNumber` is the identifier
used in every `/api/Command/*` call.

---

## 6. What this means for a generic-dongle HA integration

1. **Transport is standard NUS.** Any Linux host with a BLE adapter (or an ESPHome Bluetooth
   proxy) can act as the "phone": connect, discover `6e400001`, enable notifications on
   `6e400003`, write to `6e400002` in 20-byte chunks.
2. **Command generation must stay in the cloud.** The plugin logs into the WeLock account,
   asks `/api/Command/UnLock` for the frame, and writes it. No AES, no key handling.
3. **Local-only telemetry is possible** with `AA 88` (status) and `AA 60` (battery) frames,
   parsing reply bytes at offsets 4/6/8. Everything else needs the cloud.
4. **Do not keep the link open** — the app itself drops it after 60 s; connect on demand.
5. **A lock can only be opened** (`SEND_OPEN_LOCK`); there is no "close" command — WeLock locks
   are spring-latch / auto-lock. A HA `lock` entity must therefore treat `lock()` as a no-op
   and `unlock()` as "send open".

---

## 7. Open questions / how to close them

| Question | How to verify |
|---|---|
| Exact meaning of reply bytes 4/6/8 per operation | Log raw notifications while exercising the app (the app prints `received data: `); correlate with the `LockOperatorStatus` branch taken |
| Is `randomFactorData` required on newer locks? | Call `/api/Command/UnLock` without it; if the lock rejects, capture the `AA 88` reply and send its challenge bytes |
| Is a pre-flight `AA 88` needed before an unlock? | Try unlocking without it; if the lock ignores the command, send `AA 88` first and retry |
| Does the lock require a bonded/encrypted link? | `flutter_blue` flags `notifyEncryptionRequired`; try without bonding, then with `pair=True` |
| Exact `Authorization` format / header casing | Inspect a live request (mitmproxy) or iterate against the API |
| Whether `/api/device/LocalUnLock` returns a usable frame for offline opens | Call it and compare with `/api/Command/UnLock` |

---

## 8. Evidence trail

- APK identity / manifest: REA `inspect_android_package` evidence
  `ev_19b3359b57e567b6de687e3cab114a876e8697dced90274391702ba71596bb3e`
  (parent `ev_4db946f346b16eaa0fe409875f9a9678beda76c1b725f7686537ab24d2697a19`).
- Dart AOT snapshot: REA `inspect_dart_aot` evidence
  `ev_536860ccfd4f56342d1abcd29c4b108f51be2cc9dc58d098b81d47e8fee193d8`
  (parent `ev_217a585c1919307595660b1ca44117b5baa104ff1aee279ccb0315b9f773ffaa`).
- Disassembly: `blutter` on `libapp.so` with flags
  `product no-code_comments no-dwarf_stack_traces_mode dedup_instructions no-tsan no-msan arm64 android compressed-pointers`
  (`libapp` loaded at `0x7faab305f000`, Dart heap `0x7fa900000000`).
- Per-function addresses quoted above are the AOT addresses printed by blutter and are stable
  for this exact `libapp.so` SHA-256.

*Observed* = read directly from the disassembly / object pool. *Inferred* = deduced from
call structure and naming (e.g. reply byte semantics, AES being server-side). *Unknown* =
listed in §7.
