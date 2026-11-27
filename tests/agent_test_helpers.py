"""Shared C firmware, Arduino wrapper, partition table, and protocol-spec test helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from tests.agent_src import (
    AGENT_DIR,
    ARDUINO_WRAPPER_CPP,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    read_c_family,
)

REPO_ROOT = AGENT_DIR.parent
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"
FEATURE_TELEMETRY_DOC = REPO_ROOT / "docs" / "features" / "health-telemetry.md"
TELEMETRY_SPEC_HEADING = "### `up/telemetry` — user-defined metrics"
TELEMETRY_PROPOSAL_HEADING = "### up/telemetry proposal (R4-fw-2, proposed)"

VERSION_TXT = AGENT_DIR / "version.txt"
FF_OTA_C = COMPONENT_SRC / "ff_ota.c"
FF_MQTT_C = COMPONENT_SRC / "ff_mqtt.c"
FF_TXN_C = COMPONENT_SRC / "ff_txn.c"
FF_TXN_H = COMPONENT_SRC / "ff_txn.h"
FF_CFG_C = COMPONENT_SRC / "ff_cfg.c"
FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_MARKER_C = COMPONENT_SRC / "ff_marker.c"

OTA_SLOT_SIZE = 1966080
EXPECTED_PARTITION_TABLE_SHA256 = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
ARDUINO_LAYOUT_SHA256 = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"

_FINGERPRINT_TYPES: dict[str, int] = {"app": 0, "data": 1}
_FINGERPRINT_DATA_SUBTYPES: dict[str, int] = {"ota": 0, "phy": 1, "nvs": 2}

MAKER_HEADERS = (
    "ff_log.h",
    "ff_safe_window.h",
    "ff_sec.h",
    "ff_selftest.h",
    "ff_sleep.h",
    "ff_telemetry.h",
)

HAZARDS = (
    "ff_mqtt_publish_status",
    "ff_identity_note_rollback_capable",
    "ff_identity_note_selftest",
    "ff_identity_note_safe_window",
    "ff_store_save_rollback_capable",
    "ff_identity_announce_json",
    "ff_ota_start",
    "ff_txn_save",
    "FF_STATUS_CONFIRMED",
    "FF_PARTITION_LAYOUT",
)

BOOT_SEQUENCE_ORDER = (
    "nvs_ready(",
    "ff_cfg_load(",
    "ff_store_sync_token(",
    "ff_progress_init(",
    "ff_identity_init(",
    "esp_netif_init(",
    "esp_event_loop_create_default(",
    "ff_net_bring_up(",
    "FF_PROGRESS_LINK_UP",
    "ff_time_sync(",
    "FF_PROGRESS_TIME_SYNCED",
    "ff_store_load(",
    "ff_mqtt_run(",
)

TIMER_CONTEXT = (
    "confirm_timeout_cb",
    "rollback_now_cb",
    "ff_mqtt_arm_confirm_timer",
    "start_rollback_gate",
)


def _code(source: Path | str) -> str:
    """Return C/C++ source text with block and line comments removed, keeping string literals."""
    raw = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", raw, flags=re.DOTALL)


def _function_body(source: Path | str, name: str) -> str:
    """Return the brace-matched body of C function definition `name`."""
    code = _code(source) if isinstance(source, Path) else source
    match = re.search(rf"\b{re.escape(name)}\s*\([^;{{)]*\)\s*\{{", code)
    assert match is not None, f"no definition of {name}()"
    depth = 0
    for index in range(match.end() - 1, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[match.end() : index]
    raise AssertionError(f"unbalanced braces in {name}()")


def _member_body(code: str, signature: str) -> str:
    """Return the brace-matched body of a C++ member with nested parentheses in its signature."""
    match = re.search(re.escape(signature) + r"\s*\{", code)
    assert match is not None, signature
    depth = 0
    for index in range(match.end() - 1, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[match.end() : index]
    raise AssertionError(f"unbalanced braces in {signature}")


def _cmake_code(path: Path) -> str:
    """Return CMake text with `#` line comments stripped."""
    return "\n".join(
        line.split("#", 1)[0].rstrip() for line in path.read_text(encoding="utf-8").splitlines()
    )


def _public_headers() -> list[Path]:
    headers = sorted(COMPONENT_INCLUDE.glob("*.h"))
    assert headers, "the component has no public headers"
    return headers


def _declared_functions(header: Path) -> set[str]:
    """Return `ff_*` function prototypes declared in `header` (excluding typedef callbacks)."""
    code = "\n".join(
        line for line in _code(header).splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r"\btypedef\s+\w+\s*\(\s*\*\s*\w+\s*\)\s*\([^;]*\)\s*;", "", code)
    return set(re.findall(r"\b(ff_[a-z0-9_]+)\s*\(", code))


def _begin_body() -> str:
    source = _code(ARDUINO_WRAPPER_CPP)
    match = re.search(r"\bbool\s+FleetforgeClass::begin\s*\([^)]*\)\s*\{", source)
    assert match is not None, "no FleetforgeClass::begin definition in Fleetforge.cpp"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end() : index]
    raise AssertionError("unbalanced braces in begin()")


def _ff_calls(code: str) -> list[str]:
    return re.findall(r"\b(ff_[a-z0-9_]+)\s*\(", code)


def _rows(path: Path) -> list[tuple[str, str, str, int, int]]:
    """Parse an ESP-IDF partition CSV into `(name, type, subtype, offset, size)` tuples."""
    parsed: list[tuple[str, str, str, int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = [field.strip() for field in stripped.split(",")]
        name, ptype, subtype, offset, size = fields[0], fields[1], fields[2], fields[3], fields[4]
        parsed.append((name, ptype, subtype, int(offset, 0), int(size, 0)))
    return parsed


def _fingerprint_subtype(ptype: str, subtype: str) -> int:
    if subtype.startswith("0x") or subtype.isdigit():
        return int(subtype, 0)
    if ptype == "app":
        if subtype == "factory":
            return 0
        match = re.fullmatch(r"ota_(\d+)", subtype)
        if match is None:
            raise KeyError(subtype)
        return 0x10 + int(match.group(1))
    data_subtypes = {**_FINGERPRINT_DATA_SUBTYPES, "coredump": 3}
    return data_subtypes[subtype]


def _fingerprint(rows: list[tuple[str, str, str, int, int]]) -> str:
    """Compute the geometry-only SHA-256 partition fingerprint from parsed CSV rows."""
    lines = [
        f"{_FINGERPRINT_TYPES[ptype]}:{_fingerprint_subtype(ptype, subtype)}:{offset}:{size}\n"
        for _name, ptype, subtype, offset, size in sorted(rows, key=lambda row: row[3])
    ]
    return hashlib.sha256("".join(lines).encode("ascii")).hexdigest()


def _csv_fingerprint(path: Path) -> str:
    """Compute the partition-table SHA-256 directly from a partition CSV file."""
    types = {"app": 0, "data": 1}
    subtypes: dict[str, dict[str, int]] = {
        "app": {"factory": 0, "test": 0x20, **{f"ota_{i}": 0x10 + i for i in range(16)}},
        "data": {
            "ota": 0,
            "phy": 1,
            "nvs": 2,
            "coredump": 3,
            "nvs_keys": 4,
            "efuse": 5,
            "undefined": 6,
            "esphttpd": 0x80,
            "fat": 0x81,
            "spiffs": 0x82,
            "littlefs": 0x83,
        },
    }
    rows: list[tuple[int, int, int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(",")]
        subtype = (
            int(fields[2], 0) if fields[2].startswith("0x") else subtypes[fields[1]][fields[2]]
        )
        rows.append((types[fields[1]], subtype, int(fields[3], 0), int(fields[4], 0)))
    serial = "".join(f"{t}:{s}:{o}:{z}\n" for t, s, o, z in sorted(rows, key=lambda r: r[2]))
    return hashlib.sha256(serial.encode("ascii")).hexdigest()


def _spec_magic() -> bytes:
    text = DEVICE_PROTOCOL.read_text(encoding="utf-8")
    section = text[text.index("## Library marker") : text.index("## Evolution rules")]
    match = re.search(r"hex `([0-9a-f]{32})`", section)
    assert match is not None, "the spec's Library marker table has no magic"
    return bytes.fromhex(match.group(1))


def _firmware_magic() -> bytes:
    match = re.search(r"\.magic\s*=\s*\{([^}]*)\}", _code(FF_MARKER_C))
    assert match is not None, "ff_marker.c has no .magic initializer"
    values = re.findall(r"0x([0-9a-fA-F]{2})\b", match.group(1))
    return bytes(int(value, 16) for value in values)


def spec_hb_example() -> dict[str, object]:
    """Return the parsed `up/hb` JSON example from `spec/device-protocol.md`."""
    text = DEVICE_PROTOCOL.read_text(encoding="utf-8")
    start = text.index("### `up/hb` — heartbeat")
    fence = text.index("```json\n", start) + len("```json\n")
    raw = text[fence : text.index("```", fence)].strip()
    example = json.loads(raw)
    assert isinstance(example, dict) and example, "the spec's up/hb example could not be parsed"
    return example


def spec_hb_keys() -> list[str]:
    return list(spec_hb_example())


def _json_fence_after(text: str, heading: str) -> str:
    """Return the first ```json fenced block after `heading`, de-indented."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    opening = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "```json")
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i].strip() == "```")
    return "\n".join(line.strip() for line in lines[opening + 1 : closing])


def _telemetry_example() -> str:
    """Return the `up/telemetry` JSON example from spec or feature doc."""
    spec_text = DEVICE_PROTOCOL.read_text(encoding="utf-8")
    if TELEMETRY_SPEC_HEADING in spec_text.splitlines():
        return _json_fence_after(spec_text, TELEMETRY_SPEC_HEADING)
    return _json_fence_after(
        FEATURE_TELEMETRY_DOC.read_text(encoding="utf-8"),
        TELEMETRY_PROPOSAL_HEADING,
    )


def _published(mqtt_code: str | None = None) -> str:
    """Return the `case MQTT_EVENT_PUBLISHED:` arm from the `ff_mqtt` C family."""
    code = _code(read_c_family("ff_mqtt")) if mqtt_code is None else mqtt_code
    match = re.search(r"case MQTT_EVENT_PUBLISHED:(.*?)\bcase\b", code, re.S)
    assert match is not None
    return match.group(1)
