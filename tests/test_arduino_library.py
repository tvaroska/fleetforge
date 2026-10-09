"""The Arduino library (R3-fw-3) — the `fleetforge` component plus library.json and a wrapper.

The component directory IS the library: PlatformIO compiles every `src/*.c` (the same set
the CMake SRCS pins) plus `src/Fleetforge.cpp`, the component's second consumer after
`agent_main.c`. What this file holds, on every `just test`, because none of it can be fixed
by OTA once a board carries it (DECISIONS 2026-10-08, R3-fw-3):

* the example's `partitions.csv` IS `ab-4m-arduino-v1`: the ADR's rows, the spec's
  fingerprint, the catalog's entry. A wrong row is a recall;
* the wrapper's rollback posture: a strong `verifyRollbackLater()` returning true in the
  same file as `begin()` (or Arduino confirms every OTA'd image before setup()), the confirm
  timer armed before anything that can fail, a build that refuses a config with rollback
  off, and no call that confirms, marks or reboots;
* the wrapper reaches the component through `include/` plus exactly one private setter, and
  only it calls that setter (R1-fw-2: the version is what booted);
* the boot sequence is `agent_main.c`'s;
* the ARDUINO guards stay in `ff_identity` and the wrapper, so the IDF agent is unchanged;
* one library version, three files: agent/version.txt, library.json, ff_lib_version.h;
* `agent/tools/lib_bundle.py` refuses every table that would boot wrong.

Comments are stripped before any grep (`_code`), the idiom of the other agent tripwires.
"""

from __future__ import annotations

import configparser
import hashlib
import importlib.util
import json
import re
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fleetforge.firmware.manifest import ARDUINO_PARTITION_LAYOUT, SUPPORTED_LAYOUTS
from tests.agent_src import (
    AGENT_DIR,
    AGENT_MAIN_C,
    ARDUINO_WRAPPER_CPP,
    ARDUINO_WRAPPER_H,
    COMPONENT_CMAKE,
    COMPONENT_DIR,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    EXAMPLE_INO,
    EXAMPLE_PARTITIONS,
    EXAMPLE_PLATFORMIO_INI,
    LIB_QEMU_PLATFORMIO_INI,
    LIB_VERSION_H,
    LIBRARY_JSON,
)
from tests.test_agent_partitions import (
    _FINGERPRINT_DATA_SUBTYPES,
    _FINGERPRINT_TYPES,
    OTA_SLOT_SIZE,
    _rows,
)
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code
from tests.test_ota_component import HAZARDS, _declared_functions, _public_headers

REPO_ROOT = AGENT_DIR.parent
VERSION_TXT = AGENT_DIR / "version.txt"
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"
TOOLS_DIR = AGENT_DIR / "tools"
LIB_BUNDLE_PY = TOOLS_DIR / "lib_bundle.py"
FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_IDENTITY_INTERNAL_H = COMPONENT_SRC / "ff_identity_internal.h"

# design/decisions/arduino-gets-its-own-layout-id.md, retyped. (name, type, subtype, offset,
# size). A different map is a new layout id, never an edit to this list.
ADR_ROWS = [
    ("nvs", "data", "nvs", 0x9000, 0x5000),
    ("otadata", "data", "ota", 0xE000, 0x2000),
    ("ota_0", "app", "ota_0", 0x10000, 0x1E0000),
    ("ota_1", "app", "ota_1", 0x1F0000, 0x1E0000),
    ("ff_cfg", "data", "0x40", 0x3D0000, 0x1000),
    ("coredump", "data", "coredump", 0x3F0000, 0x10000),
]
# spec/device-protocol.md -> Partition layouts, retyped.
ARDUINO_LAYOUT_SHA256 = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"

# The pinned toolchain: pioarduino 55.03.312-1 = Arduino-ESP32 3.3.12 on ESP-IDF v5.5.5.
PINNED_PLATFORM = (
    "https://github.com/pioarduino/platform-espressif32/releases/download/"
    "55.03.312-1/platform-espressif32.zip"
)

# The test-local extension of the shared rule: `coredump` is ESP-IDF's data subtype 3.
# The shared dict is not edited; an unknown name still raises.
_SUBTYPES = {**_FINGERPRINT_DATA_SUBTYPES, "coredump": 3}


# agent_main.c's boot sequence, as the tokens a consumer's task must contain in this order.
# Shared with tests/test_worked_example.py (the ESP-IDF example's fleetforge_start.c).
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


def _subtype(ptype: str, subtype: str) -> int:
    if subtype.startswith("0x") or subtype.isdigit():
        return int(subtype, 0)
    if ptype == "app":
        match = re.fullmatch(r"ota_(\d+)", subtype)
        assert match is not None, subtype
        return 0x10 + int(match.group(1))
    return _SUBTYPES[subtype]


def _fingerprint(rows: list[tuple[str, str, str, int, int]]) -> str:
    lines = "".join(
        f"{_FINGERPRINT_TYPES[ptype]}:{_subtype(ptype, subtype)}:{offset}:{size}\n"
        for _name, ptype, subtype, offset, size in sorted(rows, key=lambda row: row[3])
    )
    return hashlib.sha256(lines.encode("ascii")).hexdigest()


def _wrapper() -> str:
    return _code(ARDUINO_WRAPPER_CPP)


def _begin_body() -> str:
    source = _wrapper()
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


def _ini(path: Path) -> configparser.ConfigParser:
    # `;` comments only; no interpolation (a URL or a CONFIG_ line is never a template).
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None)
    parser.read_string(path.read_text())
    return parser


def _env(parser: configparser.ConfigParser, name: str) -> dict[str, str]:
    """An env's effective options: [env] defaults, then `extends`, then its own."""
    section = f"env:{name}"
    merged = dict(parser["env"]) if parser.has_section("env") else {}
    own = dict(parser[section])
    parent = own.pop("extends", None)
    if parent is not None:
        merged.update(_env(parser, parent.removeprefix("env:")))
    merged.update(own)
    return merged


class TestTheExampleLayout:
    """`examples/Basic/partitions.csv` is what boards in the field carry for life."""

    def test_rows_are_exactly_the_adr_table(self) -> None:
        assert _rows(EXAMPLE_PARTITIONS) == ADR_ROWS

    def test_fingerprint_is_the_spec_and_the_catalog(self) -> None:
        fingerprint = _fingerprint(_rows(EXAMPLE_PARTITIONS))
        assert fingerprint == ARDUINO_LAYOUT_SHA256
        assert SUPPORTED_LAYOUTS[ARDUINO_PARTITION_LAYOUT].partition_table_sha256 == fingerprint
        row = next(
            line
            for line in DEVICE_PROTOCOL.read_text().splitlines()
            if "`ab-4m-arduino-v1`" in line
        )
        assert f"`{fingerprint}`" in row
        assert f"| {OTA_SLOT_SIZE} |" in row

    def test_the_arduino_recipe_offsets_and_the_slot_contract(self) -> None:
        """The Arduino upload writes boot_app0 at 0xe000 and the app at 0x10000 whatever
        the table says (the ADR's finding): the table must put otadata and ota_0 there."""
        rows = {row[0]: row for row in _rows(EXAMPLE_PARTITIONS)}
        assert rows["otadata"][3] == 0xE000
        assert rows["ota_0"][3] == 0x10000
        assert rows["ota_0"][4] == rows["ota_1"][4] == OTA_SLOT_SIZE
        assert rows["ota_1"][3] == rows["ota_0"][3] + rows["ota_0"][4]
        assert rows["ff_cfg"][1:3] == ("data", "0x40")
        assert not any(row[2] == "factory" for row in rows.values())
        assert SUPPORTED_LAYOUTS[ARDUINO_PARTITION_LAYOUT].ota_slot_size == OTA_SLOT_SIZE

    def test_the_csv_names_its_layout(self) -> None:
        assert 'layout id "ab-4m-arduino-v1"' in EXAMPLE_PARTITIONS.read_text()

    def test_the_announced_layout_is_chosen_by_arduino(self) -> None:
        """ARDUINO selects the library's id; the IDF branch is still ab-4m-v1, verbatim."""
        header = _code(FF_IDENTITY_INTERNAL_H)
        match = re.search(
            r"#if defined\(ARDUINO\)\s*#define FF_PARTITION_LAYOUT \"([a-z0-9.-]+)\"\s*"
            r"#else\s*#define FF_PARTITION_LAYOUT \"ab-4m-v1\"\s*#endif",
            header,
        )
        assert match is not None
        assert match.group(1) == ARDUINO_PARTITION_LAYOUT
        assert match.group(1) in SUPPORTED_LAYOUTS
        assert "#define FF_OTA_SLOT_SIZE 1966080" in header


class TestPlatformioIni:
    def test_the_platform_is_pinned_exactly(self) -> None:
        for path in (EXAMPLE_PLATFORMIO_INI, LIB_QEMU_PLATFORMIO_INI):
            parser = _ini(path)
            for name in [s.removeprefix("env:") for s in parser.sections() if s.startswith("env:")]:
                assert _env(parser, name)["platform"] == PINNED_PLATFORM, (path, name)

    def test_a_bare_pio_run_builds_both_targets(self) -> None:
        parser = _ini(EXAMPLE_PLATFORMIO_INI)
        default = [e.strip() for e in parser["platformio"]["default_envs"].split(",")]
        assert default == ["esp32", "esp32s3"]

    def test_the_example_has_the_two_persona_targets(self) -> None:
        parser = _ini(EXAMPLE_PLATFORMIO_INI)
        envs = [s.removeprefix("env:") for s in parser.sections() if s.startswith("env:")]
        assert envs == ["esp32", "esp32s3"]
        assert _env(parser, "esp32")["board"] == "esp32dev"
        assert _env(parser, "esp32s3")["board"] == "esp32-s3-devkitc-1"
        for name in envs:
            env = _env(parser, name)
            assert env["framework"] == "arduino"
            assert env["board_build.partitions"] == "partitions.csv"
            assert env["lib_deps"] == "symlink://../.."
            assert "custom_sdkconfig" not in env, "the persona build must stay stock"
        assert (EXAMPLE_PLATFORMIO_INI.parent / "../..").resolve() == COMPONENT_DIR

    def test_the_qemu_env_is_esp32_plus_the_nic_only(self) -> None:
        """lib-qemu/ is env:esp32 with its paths rebased and ONE delta: the emulated
        NIC (the rule test_agent_partitions.py holds for the agent's esp32 fragment)."""
        example = _env(_ini(EXAMPLE_PLATFORMIO_INI), "esp32")
        harness_parser = _ini(LIB_QEMU_PLATFORMIO_INI)
        envs = [s for s in harness_parser.sections() if s.startswith("env:")]
        assert envs == ["env:esp32-qemu"]
        qemu = _env(harness_parser, "esp32-qemu")
        assert qemu.pop("custom_sdkconfig") == "CONFIG_ETH_USE_OPENETH=y"
        harness = LIB_QEMU_PLATFORMIO_INI.parent
        for key in ("board_build.partitions",):
            assert (harness / qemu.pop(key)).resolve() == (
                EXAMPLE_PLATFORMIO_INI.parent / example.pop(key)
            ).resolve()
        assert (harness / qemu.pop("lib_deps").removeprefix("symlink://")).resolve() == (
            COMPONENT_DIR
        )
        example.pop("lib_deps")
        assert qemu == example
        src_dir = harness_parser["platformio"]["src_dir"]
        assert (harness / src_dir).resolve() == EXAMPLE_PLATFORMIO_INI.parent

    def test_only_the_sketch_compiles_from_the_example_folder(self) -> None:
        for path in (EXAMPLE_PLATFORMIO_INI, LIB_QEMU_PLATFORMIO_INI):
            parser = _ini(path)
            for section in parser.sections():
                if section.startswith("env:"):
                    env = _env(parser, section.removeprefix("env:"))
                    assert env["build_src_filter"] == "-<*> +<*.ino.cpp>", (path, section)


class TestLibraryJson:
    def _manifest(self) -> dict[str, Any]:
        loaded: Any = json.loads(LIBRARY_JSON.read_text())
        assert isinstance(loaded, dict)
        return loaded

    def test_one_version_in_three_files(self) -> None:
        txt = VERSION_TXT.read_text().strip()
        header = re.search(r'#define FF_LIB_VERSION "([^"]+)"', LIB_VERSION_H.read_text())
        assert header is not None
        assert txt == self._manifest()["version"] == header.group(1), (
            "the library version lives in three files that must be bumped together: "
            "agent/version.txt, agent/components/fleetforge/library.json (version) and "
            "agent/components/fleetforge/src/ff_lib_version.h (FF_LIB_VERSION); the Arduino "
            "IDE package's library.properties is generated from library.json (R3-fw-8)"
        )

    def test_werror_like_the_cmake_component(self) -> None:
        flags = self._manifest()["build"]["flags"]
        assert {"-Wall", "-Wextra", "-Werror"} <= set(flags)

    def test_every_c_file_compiles(self) -> None:
        """No srcDir/srcFilter that could drop a src/*.c: PlatformIO's defaults (`src`,
        `include`) are the component's, so the Arduino build compiles the CMake SRCS set."""
        manifest = self._manifest()
        build = manifest.get("build", {})
        assert "srcDir" not in build and "srcFilter" not in build
        assert "includeDir" not in build
        listed = set(re.findall(r'"src/([a-z0-9_]+\.c)"', COMPONENT_CMAKE.read_text()))
        assert listed == {path.name for path in COMPONENT_SRC.glob("*.c")}

    def test_it_is_an_arduino_library(self) -> None:
        manifest = self._manifest()
        assert manifest["name"] == "Fleetforge"
        assert manifest["frameworks"] == ["arduino"]
        assert manifest["headers"] == "Fleetforge.h"

    def test_no_library_properties_at_the_component_root(self) -> None:
        """The Arduino IDE compiles src/ only and cannot see include/, so a library.properties
        here would make it accept this unflattened directory and fail. The IDE package is
        generated by scripts/arduino_package.py (R3-fw-8), library.properties included."""
        assert not (COMPONENT_DIR / "library.properties").exists()


class TestTheRollbackPosture:
    """CRITICAL.md: device-side confirm timer / rollback path, as the library build has it."""

    def test_verify_rollback_later_is_overridden_true_next_to_begin(self) -> None:
        source = _wrapper()
        assert re.search(
            r'extern "C" bool verifyRollbackLater\(void\)\s*\{\s*return true;\s*\}', source
        )
        assert "FleetforgeClass::begin" in source
        assert "__attribute__((weak))" not in source

    def test_the_confirm_timer_is_the_first_ff_call_and_nothing_returns_before_it(self) -> None:
        body = _begin_body()
        calls = _ff_calls(body)
        assert calls and calls[0] == "ff_mqtt_arm_confirm_timer", calls
        before = body[: body.index("ff_mqtt_arm_confirm_timer")]
        assert not re.search(r"\b(return|if|while|goto|switch)\b", before), before

    def test_a_config_without_rollback_does_not_compile(self) -> None:
        source = _wrapper()
        guard = re.search(
            r'#include "sdkconfig\.h"\s*#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE\s*#error "([^"]+)"',
            source,
        )
        assert guard is not None
        assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in guard.group(1)

    def test_linking_the_library_does_no_work(self) -> None:
        """No static-init work: the marker writer rule's spirit (DECISIONS R3-spec-3)."""
        source = _wrapper() + _code(ARDUINO_WRAPPER_H)
        for spelling in ("constructor", "__attribute__((used", "retain", "KEEP("):
            assert spelling not in source, spelling
        assert "constexpr FleetforgeClass() = default;" in _code(ARDUINO_WRAPPER_H)

    def test_nothing_here_confirms_marks_or_reboots(self) -> None:
        source = _wrapper()
        for name in (
            *HAZARDS,
            "esp_ota_mark_app_valid_cancel_rollback",
            "esp_ota_mark_app_invalid",
            "esp_ota_set_boot_partition",
            "esp_restart",
            "ESP_ERROR_CHECK",
            "abort",
        ):
            assert not re.search(rf"\b{name}\b", source), name

    def test_it_reaches_the_component_through_include_plus_one_setter(self) -> None:
        public = set().union(*(_declared_functions(header) for header in _public_headers()))
        called = set(_ff_calls(_wrapper()))
        assert called - public == {"ff_identity_set_app_versions"}
        includes = set(
            re.findall(r'#include "(ff_[a-z0-9_]+\.h)"', ARDUINO_WRAPPER_CPP.read_text())
        )
        public_headers = {path.name for path in COMPONENT_INCLUDE.glob("*.h")}
        assert includes - public_headers == {"ff_identity_internal.h", "ff_lib_version.h"}

    def test_only_the_wrapper_sets_the_versions(self) -> None:
        """R1-fw-2: the version is a constant of the running image, handed in once."""
        callers = []
        for path in sorted([*COMPONENT_SRC.iterdir(), AGENT_MAIN_C]):
            if path.suffix not in (".c", ".cpp", ".h"):
                continue
            code = _code(path)
            if re.search(r"\bff_identity_set_app_versions\s*\(", code) and path.name not in (
                "ff_identity.c",
                "ff_identity_internal.h",
            ):
                callers.append(path.name)
        assert callers == ["Fleetforge.cpp"]
        assert len(re.findall(r"\bff_identity_set_app_versions\s*\(", _begin_body())) == 1
        assert "ff_identity" not in _code(COMPONENT_SRC / "ff_ota.c")

    def test_the_example_calls_begin_first(self) -> None:
        sketch = _code(EXAMPLE_INO)
        setup = re.search(r"void setup\(\)\s*\{\s*([^;]+);", sketch)
        assert setup is not None
        assert setup.group(1).strip() == "Fleetforge.begin(FW_VERSION)"
        for forbidden in ("verifyRollbackLater", "verifyOta", "WiFi.begin", "configTime"):
            assert forbidden not in sketch


class TestParityWithAgentMain:
    def test_every_ff_call_of_agent_main_is_made(self) -> None:
        main_calls = set(_ff_calls(_code(AGENT_MAIN_C)))
        missing = main_calls - set(_ff_calls(_wrapper()))
        assert missing == set(), f"agent_main.c calls these, Fleetforge.cpp does not: {missing}"

    def test_the_boot_sequence_order(self) -> None:
        task = _function_body(_wrapper(), "fleetforge_task")
        positions = [task.index(token) for token in BOOT_SEQUENCE_ORDER]
        assert positions == sorted(positions)
        assert _ff_calls(task)[-1] == "ff_mqtt_run"

    def test_enroll_only_when_absent_and_save_right_after(self) -> None:
        source = _wrapper()
        task = _function_body(source, "fleetforge_task")
        assert re.search(
            r"if \(stored == ESP_ERR_NVS_NOT_FOUND\) \{\s*enroll_until_credentialed\(", task
        )
        assert task.count("enroll_until_credentialed(") == 1
        assert source.count("ff_enroll(") == 1
        ladder = _function_body(source, "enroll_until_credentialed")
        after = ladder[ladder.index("ff_enroll(") :]
        assert _ff_calls(after)[:2] == ["ff_enroll", "ff_store_save"]

    def test_netif_and_event_loop_tolerate_an_existing_one(self) -> None:
        source = _wrapper()
        assert not re.search(r"ESP_ERROR_CHECK\(\s*esp_(netif_init|event_loop_create)", source)
        assert "ESP_ERR_INVALID_STATE" in _function_body(source, "created_or_existing")

    def test_the_task_matches_the_agent_main_task(self) -> None:
        source = ARDUINO_WRAPPER_CPP.read_text()
        assert "#define FF_TASK_STACK 8192" in source
        sdkconfig = (AGENT_DIR / "sdkconfig.defaults").read_text()
        assert "CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192" in sdkconfig


class TestArduinoGuardsAreConfined:
    def test_only_identity_and_the_wrapper_mention_arduino(self) -> None:
        mentions = sorted(
            path.name
            for directory in (COMPONENT_SRC, COMPONENT_INCLUDE)
            for path in directory.iterdir()
            if path.suffix in (".c", ".cpp", ".h") and re.search(r"\bARDUINO\b", _code(path))
        )
        assert mentions == ["Fleetforge.cpp", "ff_identity.c", "ff_identity_internal.h"]
        assert "ARDUINO" not in _code(AGENT_MAIN_C)

    def test_the_idf_lines_are_still_there_verbatim(self) -> None:
        source = _code(FF_IDENTITY_C)
        assert "esp_app_get_description()->version" in source
        assert 'cJSON_AddStringToObject(root, "agent_version", app->version);' in source

    def test_the_wrapper_body_is_guarded(self) -> None:
        code = _wrapper().strip()
        assert code.startswith("#if defined(ARDUINO)")
        assert code.endswith("#endif")

    def test_log_tags_cover_every_component_tag(self) -> None:
        """The core starts at ERROR; begin() raises exactly the library's tags to INFO."""
        tags = {
            match
            for path in COMPONENT_SRC.glob("*.c")
            for match in re.findall(r'static const char \*TAG = "([^"]+)";', path.read_text())
        }
        source = _wrapper()
        own = re.search(r'static const char \*TAG = "([^"]+)";', source)
        assert own is not None
        listed = re.search(r"FF_LOG_TAGS\[\] = \{([^}]*)\}", source)
        assert listed is not None
        assert set(re.findall(r'"([^"]+)"', listed.group(1))) == tags | {own.group(1)}
        assert "-DLOG_LOCAL_LEVEL=ESP_LOG_INFO" in LIBRARY_JSON.read_text()


# ── agent/tools/lib_bundle.py ──────────────────────────────────────────────────────────


def _load_lib_bundle() -> ModuleType:
    """By path, like the other tool tests. lib_bundle imports its sibling make_manifest by
    name (it runs from agent/tools/), so that module is registered first."""
    if "make_manifest" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "make_manifest", TOOLS_DIR / "make_manifest.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["make_manifest"] = module
        spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location("lib_bundle_tool", LIB_BUNDLE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lib_bundle = _load_lib_bundle()

_ENTRY = struct.Struct("<2sBBLL16sL")
_TYPES = {"app": 0, "data": 1}


def _table(rows: list[tuple[str, str, str, int, int]]) -> bytes:
    blob = b"".join(
        _ENTRY.pack(
            b"\xaa\x50",
            _TYPES[ptype],
            _subtype(ptype, subtype),
            offset,
            size,
            name.encode().ljust(16, b"\x00"),
            0,
        )
        for name, ptype, subtype, offset, size in rows
    )
    md5 = b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(blob, usedforsecurity=False).digest()
    return (blob + md5).ljust(0xC00, b"\xff")


def _build(
    tmp_path: Path,
    rows: list[tuple[str, str, str, int, int]] = ADR_ROWS,
    app_at: int = 0x10000,
    boot_app0_at: int = 0xE000,
    declare_app_offset: bool = False,
) -> Path:
    build = tmp_path / "build"
    build.mkdir()
    bootloader = b"\xe9" + b"B" * 0x1FF
    table = _table(rows)
    boot_app0 = b"\xff" * 0x1000 + b"\x00" * 0x1000
    app = b"\xe9\x05\x02\x20" + b"\x00" * 8 + struct.pack("<H", 0) + b"A" * 0x3F2
    files = {"bootloader.bin": bootloader, "partitions.bin": table, "firmware.bin": app}
    for name, data in files.items():
        (build / name).write_bytes(data)
    (tmp_path / "boot_app0.bin").write_bytes(boot_app0)
    factory = bytearray(b"\xff" * (app_at + len(app)))
    for offset, data in (
        (0x1000, bootloader),
        (0x8000, table),
        (boot_app0_at, boot_app0),
        (app_at, app),
    ):
        factory[offset : offset + len(data)] = data
    (build / "firmware.factory.bin").write_bytes(bytes(factory))
    extra: dict[str, Any] = {
        "flash_images": [
            {"offset": "0x1000", "path": str(build / "bootloader.bin")},
            {"offset": "0x8000", "path": str(build / "partitions.bin")},
            {"offset": hex(boot_app0_at), "path": str(tmp_path / "boot_app0.bin")},
        ]
    }
    if declare_app_offset:
        extra["application_offset"] = hex(app_at)
    (build / "idedata.json").write_text(json.dumps({"extra": extra}))
    return build


class TestLibBundle:
    def test_the_fingerprint_constant_is_the_spec(self) -> None:
        assert lib_bundle.ARDUINO_LAYOUT_SHA256 == ARDUINO_LAYOUT_SHA256
        assert lib_bundle.ARDUINO_LAYOUT_ID == ARDUINO_PARTITION_LAYOUT

    @pytest.mark.parametrize("declare", [False, True])
    def test_happy_path(self, tmp_path: Path, declare: bool) -> None:
        build = _build(tmp_path, declare_app_offset=declare)
        out = tmp_path / "bundle"
        manifest = lib_bundle.build_bundle(build, out)
        assert manifest["target"] == "esp32"
        assert manifest["partition_layout"] == "ab-4m-arduino-v1"
        assert manifest["partition_table_sha256"] == ARDUINO_LAYOUT_SHA256
        assert manifest["ota_slot_size"] == OTA_SLOT_SIZE
        assert manifest["config_partition"] == {
            "label": "ff_cfg",
            "offset": 0x3D0000,
            "size": 0x1000,
        }
        offsets = {part["name"]: part["offset"] for part in manifest["parts"]}
        assert offsets == {
            "bootloader": 0x1000,
            "partition-table": 0x8000,
            "ota-data": 0xE000,
            "app": 0x10000,
        }
        for part in manifest["parts"]:
            data = (out / part["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == part["sha256"]
            assert len(data) == part["size"]

    def test_it_refuses_a_table_without_ff_cfg(self, tmp_path: Path) -> None:
        rows = [row for row in ADR_ROWS if row[0] != "ff_cfg"]
        with pytest.raises(lib_bundle.BuildError, match="ff_cfg"):
            lib_bundle.build_bundle(_build(tmp_path, rows=rows), tmp_path / "bundle")

    @pytest.mark.parametrize("declare", [False, True])
    def test_it_refuses_an_app_written_outside_ota_0(self, tmp_path: Path, declare: bool) -> None:
        build = _build(tmp_path, app_at=0x20000, declare_app_offset=declare)
        with pytest.raises(lib_bundle.BuildError, match="ota_0 starts at 0x10000"):
            lib_bundle.build_bundle(build, tmp_path / "bundle")

    def test_it_refuses_boot_app0_outside_otadata(self, tmp_path: Path) -> None:
        build = _build(tmp_path, boot_app0_at=0xC000)
        with pytest.raises(lib_bundle.BuildError, match="otadata"):
            lib_bundle.build_bundle(build, tmp_path / "bundle")

    def test_it_refuses_a_different_map(self, tmp_path: Path) -> None:
        rows = [*ADR_ROWS[:-1], ("coredump", "data", "coredump", 0x3F0000, 0x8000)]
        with pytest.raises(lib_bundle.BuildError, match="fingerprint"):
            lib_bundle.build_bundle(_build(tmp_path, rows=rows), tmp_path / "bundle")

    def test_it_refuses_a_stale_factory_image(self, tmp_path: Path) -> None:
        build = _build(tmp_path)
        (build / "partitions.bin").write_bytes(_table(ADR_ROWS[::-1]))
        with pytest.raises(lib_bundle.BuildError, match="does not hold partitions.bin"):
            lib_bundle.build_bundle(build, tmp_path / "bundle")

    def test_it_never_writes_into_agent_dist(self) -> None:
        source = LIB_BUNDLE_PY.read_text()
        assert "agent/dist" not in re.sub(r'""".*?"""', "", source, flags=re.S)
