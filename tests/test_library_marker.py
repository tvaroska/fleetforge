"""R3-spec-3 / R3-fw-6 — the library marker is in the component and the announce reports it.

Firmware tripwires, as text, in the idiom of `test_layout_detection.py`: no host test can run
`agent/components/fleetforge/src/*.c`, so what is held here, on every `just test`, is the
shape that makes a scan of a built `.bin` (`docs/features/ota-library.md` -> *R3-fw-6*) and
the QEMU announce come out right:

* `ff_marker.c`'s magic IS the spec's (spec/device-protocol.md -> *Library marker*), byte for
  byte, and ends in ASCII `FFOTALIB`;
* the layout is the spec table, asserted at compile time, and the object is 4-byte aligned;
* `lib_version` is `FF_LIB_VERSION` in every build, never `PROJECT_VER`;
* nothing force-keeps the marker: a sketch that has the library installed but never calls it
  must not be marked, so only a read from another TU (`ff_identity.c`) keeps it linked;
* it is defined once, declared in a PRIVATE header, and out of the public surface;
* the announce's `lib_marker` is the last key, and the boot line that prints the version
  comes before `device_id` with no hex run the diagnostics bundle would read as a device id;
* the server never refuses a board for the key, whatever its value.
"""

from __future__ import annotations

import json
import re

from fleetforge.ingestor.protocol import AnnouncePayload, decode
from tests.agent_src import (
    AGENT_DIR,
    AGENT_MAIN_C,
    ARDUINO_WRAPPER_CPP,
    COMPONENT_CMAKE,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    LIBRARY_JSON,
)
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code

REPO_ROOT = AGENT_DIR.parent
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"
ARDUINO_PACKAGE_PY = REPO_ROOT / "scripts" / "arduino_package.py"
FF_MARKER_C = COMPONENT_SRC / "ff_marker.c"
FF_MARKER_H = COMPONENT_SRC / "ff_marker.h"
FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"

# Spellings that would keep the marker in an image that never references it. Not bare
# `retain`: ff_mqtt.c publishes the announce with MQTT retain.
FORCE_KEEP_SPELLINGS = (
    "__attribute__((used",
    "__attribute__((retain",
    "KEEP(",
    "-Wl,-u",
    "-u ff_lib_marker",
)

TOPIC = "ff/v1/d/a4cf12b3de90/up/announce"
# A minimal valid announce, the shape of tests/test_ingestor.py::ANNOUNCE.
ANNOUNCE = {
    "proto": 1,
    "device_id": "a4cf12b3de90",
    "platform_type": "esp32",
    "fw_version": "1.0.0",
    "agent_version": "0.4.7",
    "link_type": "ethernet",
    "power_class": "always_on",
    "partition_layout": "ab-4m-arduino-v1",
    "ota_slot_size": 1966080,
    "capabilities": ["ota"],
}


def _spec_magic() -> bytes:
    text = DEVICE_PROTOCOL.read_text()
    section = text[text.index("## Library marker") : text.index("## Evolution rules")]
    match = re.search(r"hex `([0-9a-f]{32})`", section)
    assert match is not None, "the spec's Library marker table has no magic"
    return bytes.fromhex(match.group(1))


def _firmware_magic() -> bytes:
    match = re.search(r"\.magic\s*=\s*\{([^}]*)\}", _code(FF_MARKER_C))
    assert match is not None, "ff_marker.c has no .magic initializer"
    values = re.findall(r"0x([0-9a-fA-F]{2})\b", match.group(1))
    return bytes(int(value, 16) for value in values)


class TestTheMarkerIsTheSpec:
    def test_the_magic_is_the_spec_magic(self) -> None:
        magic = _firmware_magic()
        assert len(magic) == 16
        assert magic == _spec_magic()
        assert magic[8:] == b"FFOTALIB"

    def test_the_layout_is_asserted_at_compile_time(self) -> None:
        code = _code(FF_MARKER_C)
        assert "sizeof(ff_lib_marker_t) == 64" in code
        assert "offsetof(ff_lib_marker_t, format) == 16" in code
        assert "offsetof(ff_lib_marker_t, lib_version) == 20" in code
        assert "offsetof(ff_lib_marker_t, reserved1) == 52" in code
        assert "sizeof(FF_LIB_VERSION) > 1 && sizeof(FF_LIB_VERSION) <= 32" in code

    def test_the_fields_are_the_spec_table_in_order(self) -> None:
        body = re.search(r"typedef struct \{([^}]*)\} ff_lib_marker_t;", _code(FF_MARKER_H))
        assert body is not None
        fields = re.findall(r"(\w+)\s+(\w+)(?:\[(\d+)\])?;", body.group(1))
        assert fields == [
            ("uint8_t", "magic", "16"),
            ("uint8_t", "format", ""),
            ("uint8_t", "reserved0", "3"),
            ("char", "lib_version", "32"),
            ("uint8_t", "reserved1", "12"),
        ]

    def test_it_is_four_byte_aligned(self) -> None:
        assert re.search(
            r"const ff_lib_marker_t ff_lib_marker __attribute__\(\(aligned\(4\)\)\) =",
            _code(FF_MARKER_C),
        )

    def test_format_one_and_the_library_version(self) -> None:
        assert re.search(r"#define FF_LIB_MARKER_FORMAT 1\b", _code(FF_MARKER_H))
        code = _code(FF_MARKER_C)
        assert ".format = FF_LIB_MARKER_FORMAT," in code
        assert ".lib_version = FF_LIB_VERSION," in code
        assert '#include "ff_lib_version.h"' in code
        # PROJECT_VER is the maker's version in their own ESP-IDF project.
        assert "PROJECT_VER" not in code
        assert "esp_app_get_description" not in code


class TestNothingForceKeepsIt:
    def test_no_force_keep_anywhere_in_the_component_or_its_package(self) -> None:
        paths = [
            *sorted(COMPONENT_SRC.iterdir()),
            *sorted(COMPONENT_INCLUDE.iterdir()),
            COMPONENT_CMAKE,
            LIBRARY_JSON,
            ARDUINO_PACKAGE_PY,
        ]
        for path in paths:
            text = path.read_text()
            for spelling in FORCE_KEEP_SPELLINGS:
                assert spelling not in text, f"{path.name} contains {spelling!r}"

    def test_it_is_compiled(self) -> None:
        assert '"src/ff_marker.c"' in COMPONENT_CMAKE.read_text()


class TestOneDefinitionPrivate:
    def test_defined_only_in_ff_marker_c(self) -> None:
        definition = re.compile(r"^const ff_lib_marker_t ff_lib_marker\b", re.MULTILINE)
        defined = [
            path.name
            for path in sorted(COMPONENT_SRC.iterdir())
            if path.suffix in (".c", ".cpp") and definition.search(_code(path))
        ]
        assert defined == ["ff_marker.c"]

    def test_declared_only_in_the_private_header(self) -> None:
        declaration = "extern const ff_lib_marker_t ff_lib_marker;"
        declared = [
            path.name
            for directory in (COMPONENT_SRC, COMPONENT_INCLUDE)
            for path in sorted(directory.iterdir())
            if declaration in _code(path)
        ]
        assert declared == ["ff_marker.h"]
        assert (COMPONENT_SRC / "ff_marker.h").is_file()
        assert not (COMPONENT_INCLUDE / "ff_marker.h").exists()

    def test_ff_identity_is_the_reader(self) -> None:
        code = _code(FF_IDENTITY_C)
        assert '#include "ff_marker.h"' in code
        assert "ff_lib_marker." in code

    def test_out_of_the_public_surface(self) -> None:
        for path in [*sorted(COMPONENT_INCLUDE.glob("*.h")), AGENT_MAIN_C, ARDUINO_WRAPPER_CPP]:
            text = path.read_text()
            assert "ff_lib_marker" not in text, path.name
            assert "ff_marker.h" not in text, path.name


class TestTheAnnounceReportsIt:
    def test_lib_marker_is_the_last_key(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        line = 'cJSON_AddNumberToObject(root, "lib_marker", ff_lib_marker.format);'
        assert body.count(line) == 1
        assert body.index('"capabilities"') < body.index(line)
        tail = body[body.index(line) + len(line) :]
        assert "cJSON_Add" not in tail
        assert tail.strip() == "return root;"

    def test_the_spec_example_ends_with_lib_marker(self) -> None:
        text = DEVICE_PROTOCOL.read_text()
        start = text.index("### `up/announce` — identity")
        block_start = text.index("```json", start) + len("```json")
        example = json.loads(text[block_start : text.index("```", block_start)])
        assert list(example)[-1] == "lib_marker"
        assert example["lib_marker"] == 1
        assert example["proto"] == 1

    def test_the_boot_line_precedes_device_id_and_carries_no_hex_run(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "ff_identity_init")
        image = re.search(
            r'ESP_LOGI\(TAG, "(image: fleetforge library %s, lib_marker %u)",([^;]*)\);', body
        )
        assert image is not None
        # frontend/src/diagnostics.ts takes the device id from the LAST ff-id line's first
        # 12-hex run, so this line goes before device_id and has none.
        assert not re.search(r"[0-9a-f]{12}", image.group(1))
        assert "ff_lib_marker.lib_version" in image.group(2)
        assert image.start() < body.index('"device_id %s"')


class TestTheServerNeverRefusesForIt:
    def _decoded(self, payload: dict[str, object]) -> AnnouncePayload:
        announce = decode(AnnouncePayload, TOPIC, json.dumps(payload).encode())
        assert announce is not None
        return announce

    def test_lib_marker_one(self) -> None:
        assert self._decoded({**ANNOUNCE, "lib_marker": 1}).fw_version == "1.0.0"

    def test_lib_marker_malformed(self) -> None:
        assert self._decoded({**ANNOUNCE, "lib_marker": "x"}).fw_version == "1.0.0"

    def test_lib_marker_absent(self) -> None:
        """Every pre-R3 board: agents up to 0.4.7 as released (tag v0.4.3)."""
        assert self._decoded(ANNOUNCE).fw_version == "1.0.0"
