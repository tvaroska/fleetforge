"""Encode and decode the `ff_cfg` flash-time configuration blob. **A cross-language contract.**

One 4 KB `data`/`0x40` partition at `0x12000` (`agent/partitions.csv`, frozen at R0) holds
everything that differs between two boards flashed from the same bundle: which server they
talk to, how they get on the network, and the single-use enrollment token they exchange for
a broker credential. `agent/main/ff_cfg.c` is the reader; this is one of two writers. The
other is the browser flasher (`R0-fe-3`), which will re-implement `encode()` in TypeScript —
so the format has to be describable in a paragraph and implementable with `TextEncoder` and
a CRC32 table.

Layout — little-endian, a 16-byte header then compact JSON, `0xff` to the end:

    0x000  4  magic       b"FFCF"
    0x004  2  version     u16 = 1
    0x006  2  reserved    u16 = 0
    0x008  4  payload_len u32, 1 … 4080
    0x00c  4  crc32       u32, IEEE (zlib.crc32) over exactly payload_len payload bytes
    0x010  N  payload     compact UTF-8 JSON object

**Why a header at all, when the payload is self-describing JSON:** erased flash is all
`0xff`, and so is the tail of a half-written blob. Without magic + length + CRC there is no
way for the firmware to tell "never configured" (a board waiting for the flasher) from
"configured, then a write was interrupted" (a board that must refuse to run) — and the
second one silently connecting somewhere with half a URL is the failure this prevents.

**Why JSON and not a packed struct:** cJSON already ships with ESP-IDF, and unknown keys are
ignored on both sides, so the format evolves additively exactly like the wire protocol
(`spec/device-protocol.md` → *Evolution rules*). A packed struct would need a version bump —
and a re-flash of every board — to carry one new field.

Standard library only, and no import from `fleetforge.*`: this runs inside the pinned ESP-IDF
image, which has no uv and no project venv (same rule as `make_manifest.py` and
`verify_bundle.py`).

**No secret is printed.** `--token`, `--psk` and their values never reach stdout; `describe()`
renders the *keys* a blob carries and the length of each secret, never its bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

MAGIC = b"FFCF"
VERSION = 1

# `<4sHHII`: magic, version, reserved, payload_len, crc32. 16 bytes.
HEADER_STRUCT = struct.Struct("<4sHHII")
HEADER_SIZE = HEADER_STRUCT.size

# agent/partitions.csv → `ff_cfg, data, 0x40, 0x12000, 0x1000`. Retyped, not imported:
# `agent/main/ff_cfg.h` retypes it too and `tests/test_ff_cfg.py` is what keeps the three
# equal. A blob that is not exactly one partition long cannot be flashed at an offset.
PARTITION_SIZE = 4096
MAX_PAYLOAD = PARTITION_SIZE - HEADER_SIZE

# Erased flash. The fill is `0xff` rather than `0x00` so that a blob written short still
# looks like erased flash after its payload, instead of like a payload of NUL bytes.
FILL = 0xFF

# Every key `agent/main/ff_cfg.c` reads. Unknown keys are *ignored* by both sides (that is
# the whole point of JSON here), so this list is documentation and CLI surface — never a
# validation whitelist. Wi-Fi credentials are `ssid`/`psk`, deliberately NOT
# `wifi_ssid`/`wifi_password`: `tests/test_agent_partitions.py::test_agent_holds_no_credential`
# greps every file under `agent/` for those spellings, and a C string literal naming one
# would fail the build for the whole fleet's benefit. Do not rename them.
KNOWN_KEYS = (
    "api_base",
    "mqtt_uri",
    "token",
    "ssid",
    "psk",
    "link",
    "ntp",
    "hb_s",
    "power",
    "wake_s",
)

# The two the firmware cannot invent a default for: it has nowhere to enroll and nowhere
# to connect. `ff_cfg.c` refuses to boot without them rather than falling back.
REQUIRED_KEYS = ("api_base", "mqtt_uri")

# Which of the keys above are secrets, for `describe()` and for the CLI's echo.
SECRET_KEYS = ("token", "psk")

LINKS = ("wifi", "ethernet")
POWER_CLASSES = ("always_on", "sleepy")

# 0600 / 0700, the `.sim/` posture: a written blob holds a live enrollment token.
FILE_MODE = 0o600


class ConfigError(ValueError):
    """The blob (or the fields offered for one) is not usable. Always fatal."""


def encode(fields: dict[str, Any]) -> bytes:
    """Serialise `fields` into exactly `PARTITION_SIZE` bytes.

    Compact separators, `sort_keys=False` — the payload is read by a JSON parser, so key
    order carries no meaning, but a stable order makes two blobs diffable by eye.
    """
    if not isinstance(fields, dict):
        raise ConfigError("ff_cfg payload must be a JSON object")
    missing = [key for key in REQUIRED_KEYS if not fields.get(key)]
    if missing:
        raise ConfigError(
            f"ff_cfg needs {', '.join(missing)}: the agent has no compiled-in default for "
            "either, and a board that guesses one connects somewhere unexpected"
        )

    payload = json.dumps(fields, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_PAYLOAD:
        raise ConfigError(
            f"ff_cfg payload is {len(payload)} bytes, {len(payload) - MAX_PAYLOAD} over the "
            f"{MAX_PAYLOAD}-byte limit ({PARTITION_SIZE}-byte partition minus a "
            f"{HEADER_SIZE}-byte header). A long URL plus a long Wi-Fi password really does "
            "reach this; shorten one or grow the partition, which is a re-flash of the fleet."
        )

    header = HEADER_STRUCT.pack(MAGIC, VERSION, 0, len(payload), zlib.crc32(payload))
    blob = header + payload
    return blob + bytes([FILL]) * (PARTITION_SIZE - len(blob))


def decode(blob: bytes) -> dict[str, Any]:
    """Parse a blob back into its fields, or raise `ConfigError` naming what was wrong.

    Every rejection here has a matching branch in `agent/main/ff_cfg.c`, and both say the
    same thing for the same reason: a board with an unreadable config must idle loudly, not
    connect approximately.
    """
    if len(blob) < HEADER_SIZE:
        raise ConfigError(
            f"ff_cfg blob is {len(blob)} bytes, shorter than its {HEADER_SIZE}-byte header"
        )

    magic, version, reserved, payload_len, crc = HEADER_STRUCT.unpack(blob[:HEADER_SIZE])
    if magic != MAGIC:
        if magic == bytes([FILL]) * 4:
            raise ConfigError("ff_cfg is erased (all 0xff): this board has never been configured")
        raise ConfigError(f"ff_cfg has no {MAGIC.decode()} magic (got {magic!r})")
    if version != VERSION:
        raise ConfigError(
            f"ff_cfg version {version} is not {VERSION}; this agent cannot read it "
            "(the flasher and the firmware are from different releases)"
        )
    if reserved != 0:
        raise ConfigError(f"ff_cfg reserved field is {reserved}, not 0")
    if not 1 <= payload_len <= MAX_PAYLOAD:
        raise ConfigError(f"ff_cfg payload_len {payload_len} is outside 1…{MAX_PAYLOAD}")
    if len(blob) < HEADER_SIZE + payload_len:
        raise ConfigError(
            f"ff_cfg claims a {payload_len}-byte payload but the blob holds "
            f"{len(blob) - HEADER_SIZE} bytes after the header — a short write"
        )

    payload = blob[HEADER_SIZE : HEADER_SIZE + payload_len]
    actual = zlib.crc32(payload)
    if actual != crc:
        raise ConfigError(
            f"ff_cfg crc32 mismatch (header 0x{crc:08x}, payload 0x{actual:08x}): "
            "the blob was written partially or corrupted in flash"
        )

    try:
        fields = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"ff_cfg payload is not JSON: {exc}") from exc
    if not isinstance(fields, dict):
        raise ConfigError("ff_cfg payload is JSON but not an object")
    return fields


def describe(fields: dict[str, Any]) -> str:
    """A one-line rendering of a decoded blob with every secret reduced to its length."""
    parts = []
    for key, value in fields.items():
        if key in SECRET_KEYS:
            parts.append(f"{key}=<{len(str(value))} chars, not shown>")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def fields_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """The CLI flags that were actually given, as blob fields.

    Absent flags are absent *keys*, not nulls: `ff_cfg.c` applies its own defaults (which
    come from `spec/prd.md` → Timing), and writing them here would freeze today's values
    into every board flashed today.
    """
    fields: dict[str, Any] = {"api_base": args.api_base.rstrip("/"), "mqtt_uri": args.mqtt_uri}
    if args.token:
        fields["token"] = args.token
    if args.ssid:
        fields["ssid"] = args.ssid
    if args.psk:
        fields["psk"] = args.psk
    if args.link:
        fields["link"] = args.link
    if args.no_ntp:
        # An explicit empty string, not an absent key: the firmware's default is a
        # pool server, and "no NTP" has to be sayable to prove the TLS-needs-a-clock
        # rule (R0-fw-1 AC5). It cannot be said as `--ntp ''` through `just`, which
        # drops empty arguments when it splices *args into the recipe.
        fields["ntp"] = ""
    elif args.ntp is not None:
        fields["ntp"] = args.ntp
    if args.hb is not None:
        fields["hb_s"] = args.hb
    if args.power:
        fields["power"] = args.power
    if args.wake is not None:
        fields["wake_s"] = args.wake
    return fields


def validate(fields: dict[str, Any]) -> None:
    """Refuse a config the *server* or the firmware would refuse, before it is flashed.

    The same ordering rule `POST /v1/enroll` and the simulator both follow: a `sleepy`
    board with no wake interval that reaches the HTTP call spends a single-use token to
    earn a 422. Here it costs nothing to catch.
    """
    link = fields.get("link", "wifi")
    if link not in LINKS:
        raise ConfigError(f"link must be one of {list(LINKS)}, not {link!r}")
    if link == "wifi" and not fields.get("ssid"):
        raise ConfigError(
            "link=wifi needs --ssid (and normally --psk): the board cannot join a network without one"
        )
    power = fields.get("power", "always_on")
    if power not in POWER_CLASSES:
        raise ConfigError(f"power must be one of {list(POWER_CLASSES)}, not {power!r}")
    if power == "sleepy" and not int(fields.get("wake_s") or 0) > 0:
        raise ConfigError(
            "power=sleepy needs a positive --wake, or POST /v1/enroll refuses the identity "
            "(422) and presence has no 2.5x window to compute from"
        )
    if int(fields.get("hb_s") or 1) <= 0:
        raise ConfigError("--hb must be positive")
    for scheme, key in (("http", "api_base"), ("mqtt", "mqtt_uri")):
        value = str(fields[key])
        if not value.startswith(f"{scheme}://") and not value.startswith(f"{scheme}s://"):
            raise ConfigError(
                f"{key} must start with {scheme}:// or {scheme}s:// — the scheme is what selects "
                f"TLS on the device (got {value!r})"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write an ff_cfg blob for one board (4096 bytes, flashed at the ff_cfg offset).",
        epilog="The output holds a live enrollment token: write it into a 0700 directory "
        "that is gitignored (.qemu/ is what `just agent-cfg` uses).",
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write the 4096-byte blob")
    parser.add_argument("--api-base", required=True, help="origin serving /v1, no trailing slash")
    parser.add_argument("--mqtt-uri", required=True, help="mqtt://host:port or mqtts://host:port")
    parser.add_argument("--token", default="", help="the single-use ffe_ enrollment token")
    parser.add_argument("--ssid", default="", help="Wi-Fi SSID (link=wifi)")
    parser.add_argument("--psk", default="", help="Wi-Fi passphrase (link=wifi)")
    parser.add_argument("--link", choices=LINKS, default="", help="default: wifi")
    parser.add_argument(
        "--ntp", default=None, help="SNTP server; default pool.ntp.org on the device"
    )
    parser.add_argument(
        "--no-ntp",
        action="store_true",
        help="disable SNTP entirely — the board keeps a 1970 clock and every TLS "
        "handshake fails on 'certificate not yet valid'. A test knob, not a setting.",
    )
    parser.add_argument("--hb", type=int, default=None, help="heartbeat seconds; default 60")
    parser.add_argument("--power", choices=POWER_CLASSES, default="", help="default: always_on")
    parser.add_argument(
        "--wake", type=int, default=None, help="expected_wake_interval_s (power=sleepy)"
    )
    parser.add_argument(
        "--print", dest="show", action="store_true", help="decode a written blob back"
    )
    args = parser.parse_args(argv)

    try:
        fields = fields_from_args(args)
        validate(fields)
        blob = encode(fields)
    except ConfigError as exc:
        print(f"ff_cfg: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT's mode applies only on creation, so chmod as well — an existing 0644 blob
    # from an earlier run must not keep a live token world-readable (state.py's rule).
    handle = os.open(args.out, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    try:
        os.write(handle, blob)
    finally:
        os.close(handle)
    os.chmod(args.out, FILE_MODE)

    print(f"ff_cfg: wrote {len(blob)} bytes to {args.out} (0{FILE_MODE:o})")
    if args.show:
        print(f"  {describe(decode(blob))}")
    else:
        shown = [key for key in fields if key not in SECRET_KEYS]
        secret = [key for key in fields if key in SECRET_KEYS]
        print(
            f"  keys: {', '.join(shown)}"
            + (f" (+ {', '.join(secret)}, not shown)" if secret else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
