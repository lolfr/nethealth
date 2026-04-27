# Third-Party Licenses

NetHealth bundles or depends on the following third-party software.

## speedtest-cli (vendored as `speedtest_vendor.py`)

- Project: https://github.com/sivel/speedtest-cli
- Author: Matt Martz
- Copyright: 2012 Matt Martz
- License: Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0

A standalone copy of `speedtest_vendor.py` is included in this repository
to avoid runtime dependency on an external package. The original copyright
header is preserved at the top of the file. No code modifications were
made beyond renaming the file for vendoring clarity.

## Runtime dependencies (installed via `requirements.txt`)

These are installed at build time, not redistributed in the source:

- `rumps` (BSD 3-Clause)
- `pyobjc-*` (MIT)
- `tplinkrouterc6u` (MIT)
- `keyring` (MIT)
- `requests` (Apache 2.0)
- `pycryptodome` (BSD-2 / Public Domain)

Refer to each package's repository for full license text.
