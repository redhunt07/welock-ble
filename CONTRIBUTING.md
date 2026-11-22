# Contributing

Thanks for helping! A few ground rules keep this project honest and maintainable.

## What this project is

A Home Assistant integration for WeLock locks, plus the reverse-engineered
protocol it is built on. The protocol document
([`docs/PROTOCOL.md`](docs/PROTOCOL.md)) is evidence-based: every claim must be
traceable to a disassembly snippet, an HCI capture or a live exchange.

## Ground rules

* **No invented values.** If a byte, a parameter or a behaviour is not proven,
  write it as unknown in the docs and keep the code conservative.
* **No personal data.** Never hard-code an account, a device number, a lock name
  or a MAC address. Examples in docs and tests use anonymised values
  (`390000000000`, `1234567890`, `WeLockA1B2C3`, `AA:BB:CC:DD:EE:FF`).
* **Keep it generic.** Everything that differs between users must be configurable
  from the UI (config flow or options), never patched in `const.py`.

## Adding support for another lock model

1. Capture the exchange with the app (Android *Enable Bluetooth HCI snoop log*,
   then `adb bugreport` and extract
   `FS/data/misc/bluetooth/logs/btsnoop_hci_*.log`; analyse with `tshark -x`).
2. Note the challenge, the frame sent and the reply.
3. Check whether the existing `U0`/`U1` flow and the `randomFactor` derivation
   still apply.
4. If the model needs a new command (e.g. setting the clock, managing cards),
   add it as a separate code path rather than changing the open flow.
5. Document the evidence in `docs/PROTOCOL.md` and mention the model.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install aiohttp bleak
.venv/bin/python tests/test_cloud.py
```

`tests/test_cloud.py` spins up an inline `aiohttp` mock on `127.0.0.1:8765`; no
network or real lock is needed. It prints `OK` when all checks pass.

For manual checks against the real cloud and a real lock use
`tools/welock_probe.py` (see `--help`).

## Pull requests

* Keep changes focused; describe what was tested and how (mock, probe, real
  lock).
* Update `docs/PROTOCOL.md`, `docs/CONFIGURATION.md` and `README.md` when the
  behaviour changes.
* Run `python -m py_compile` on the touched modules and the test suite before
  opening the PR.

## License

By contributing you agree that your work is released under the MIT license of
this repository.
