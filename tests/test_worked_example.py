"""The worked example (R3-fw-4): the Morse blinker, in Arduino and ESP-IDF, and its README.

`examples/Basic/` (Arduino / PlatformIO, the persona) and `examples/basic_idf/` (ESP-IDF) are
the library's documentation: a maker copies them. So what a copy carries is held here on
every `just test`, the way the agent's own immutables are (DECISIONS 2026-10-08, R3-fw-4):

* the sketch stays one screen, its two build-B lines are the README's `sed` targets and
  occur exactly once, and it still calls `Fleetforge.begin()` first (that test lives in
  test_arduino_library.py);
* the IDF example's `partitions.csv` IS `ab-4m-v1` (an IDF build of the component announces
  that id): `agent/partitions.csv`'s rows, the spec's fingerprint. A wrong row is a recall;
* its `sdkconfig.defaults` turns rollback on and burns no eFuse;
* `fleetforge_start.c` is the IDF twin of `Fleetforge.cpp`'s `begin()`: the confirm timer
  first, an `#error` without rollback, the public include/ only, `agent_main.c`'s order;
* `main.c` is one screen and calls `fleetforge_start()` first;
* every `# quickstart:` block `scripts/lib_quickstart.py` runs is in the right README,
  once, and a README without one fails the scripted run by name.

Comments are stripped before any grep of C (`_code`), the idiom of the other agent tripwires.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from fleetforge.firmware.manifest import BUILTIN_LAYOUTS, EXPECTED_PARTITION_LAYOUT
from tests.agent_src import (
    AGENT_DIR,
    AGENT_MAIN_C,
    COMPONENT_DIR,
    COMPONENT_INCLUDE,
    EXAMPLE_INO,
    EXAMPLE_PLATFORMIO_INI,
    EXAMPLE_README,
    IDF_EXAMPLE_CMAKE,
    IDF_EXAMPLE_DIR,
    IDF_EXAMPLE_MAIN_C,
    IDF_EXAMPLE_MAIN_CMAKE,
    IDF_EXAMPLE_PARTITIONS,
    IDF_EXAMPLE_README,
    IDF_EXAMPLE_SDKCONFIG,
    IDF_EXAMPLE_START_C,
    IDF_EXAMPLE_START_H,
)
from tests.test_agent_fault_injection import _cmake_code
from tests.test_agent_partitions import EXPECTED_PARTITION_TABLE_SHA256, _rows
from tests.test_agent_txn import _function_body
from tests.test_arduino_library import BOOT_SEQUENCE_ORDER, _ff_calls, _fingerprint
from tests.test_ff_cfg import _code
from tests.test_ota_component import HAZARDS, _declared_functions, _public_headers

REPO_ROOT = AGENT_DIR.parent
QUICKSTART_PY = REPO_ROOT / "scripts" / "lib_quickstart.py"
JUSTFILE = REPO_ROOT / "justfile"
ONE_SCREEN = 70  # lines

# Retyped, not imported: a tripwire must not read the constant it guards.
IDF_SDKCONFIG_REQUIRED = (
    "CONFIG_PARTITION_TABLE_CUSTOM=y",
    'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"',
    'CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"',
    "CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y",
    "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
    "CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y",
    "CONFIG_MBEDTLS_HAVE_TIME_DATE=y",
)
# Each an irreversible eFuse burn, or (the last) a version that is not PROJECT_VER.
IDF_SDKCONFIG_FORBIDDEN_PREFIXES = (
    "CONFIG_SECURE_BOOT",
    "CONFIG_SECURE_FLASH_ENC_ENABLED",
    "CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK",
    "CONFIG_APP_PROJECT_VER_FROM_CONFIG",
)
NEVER_CALLED = (
    *HAZARDS,
    "esp_ota_mark_app_valid_cancel_rollback",
    "esp_ota_mark_app_invalid",
    "esp_ota_set_boot_partition",
    "esp_restart",
    "ESP_ERROR_CHECK",
    "abort",
    "ff_identity_set_app_versions",  # private, and ARDUINO-only
)


def _load_quickstart() -> ModuleType:
    """By path, like the other script tests; registered first because it uses dataclasses."""
    spec = importlib.util.spec_from_file_location("lib_quickstart", QUICKSTART_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lib_quickstart"] = module
    spec.loader.exec_module(module)
    return module


quickstart = _load_quickstart()


def _start() -> str:
    return _code(IDF_EXAMPLE_START_C)


def _main() -> str:
    return _code(IDF_EXAMPLE_MAIN_C)


def _sdkconfig_lines() -> list[str]:
    return [
        line.strip()
        for line in IDF_EXAMPLE_SDKCONFIG.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class TestTheSketch:
    def test_it_is_one_screen(self) -> None:
        assert len(EXAMPLE_INO.read_text().splitlines()) <= ONE_SCREEN

    def test_version_and_message_are_overridable(self) -> None:
        sketch = _code(EXAMPLE_INO)
        for name, value in (("FW_VERSION", "1.0.0"), ("MORSE_MESSAGE", "SOS")):
            assert re.search(
                rf'#ifndef {name}\s*#define {name} "{re.escape(value)}"\s*#endif', sketch
            )

    def test_the_readme_edit_hits_exactly_the_two_defines(self) -> None:
        """The README's `sed` replaces the first match per line; each literal is unique."""
        raw = EXAMPLE_INO.read_text()
        assert raw.count('"SOS"') == 1
        assert raw.count('"1.0.0"') == 1
        edited = raw.replace('"SOS"', '"HELLO"').replace('"1.0.0"', '"1.1.0"')
        assert '#define MORSE_MESSAGE "HELLO"' in edited
        assert '#define FW_VERSION "1.1.0"' in edited

    def test_it_prints_the_line_the_scripted_run_waits_for(self) -> None:
        sketch = _code(EXAMPLE_INO)
        assert 'Serial.printf("morse: %s (firmware %s)\\n", MORSE_MESSAGE, FW_VERSION);' in sketch

    def test_it_never_touches_what_the_library_owns(self) -> None:
        sketch = _code(EXAMPLE_INO)
        for forbidden in (
            "verifyRollbackLater",
            "verifyOta",
            "WiFi.begin",
            "ETH.begin",
            "configTime",
            "esp_ota_",
            "esp_restart",
        ):
            assert forbidden not in sketch, forbidden


class TestTheIdfExampleLayout:
    """`examples/basic_idf/partitions.csv` is what every board flashed from it carries."""

    def test_rows_are_exactly_the_agent_table(self) -> None:
        rows = _rows(IDF_EXAMPLE_PARTITIONS)
        assert rows == _rows(AGENT_DIR / "partitions.csv")
        assert [row[0] for row in rows] == [
            "nvs",
            "otadata",
            "phy_init",
            "ff_cfg",
            "ota_0",
            "ota_1",
        ]
        assert ("ff_cfg", "data", "0x40", 0x12000, 0x1000) in rows

    def test_fingerprint_is_the_spec_and_the_catalog(self) -> None:
        profile = BUILTIN_LAYOUTS[EXPECTED_PARTITION_LAYOUT]
        assert EXPECTED_PARTITION_LAYOUT == "ab-4m-v1"
        fingerprint = _fingerprint(_rows(IDF_EXAMPLE_PARTITIONS))
        assert fingerprint == profile.partition_table_sha256 == EXPECTED_PARTITION_TABLE_SHA256

    def test_the_csv_names_its_layout(self) -> None:
        header = IDF_EXAMPLE_PARTITIONS.read_text()
        assert '"ab-4m-v1"' in header
        assert "FLASH-TIME IMMUTABLE" in header


class TestTheIdfSdkconfig:
    def test_the_flash_time_options_are_on(self) -> None:
        lines = _sdkconfig_lines()
        for required in IDF_SDKCONFIG_REQUIRED:
            assert required in lines, required

    def test_no_efuse_burn_and_no_config_version(self) -> None:
        for line in _sdkconfig_lines():
            if line.startswith(IDF_SDKCONFIG_FORBIDDEN_PREFIXES):
                assert not line.endswith("=y"), line


class TestTheIdfProject:
    def test_the_component_is_two_levels_up(self) -> None:
        cmake = _cmake_code(IDF_EXAMPLE_CMAKE)
        match = re.search(
            r'set\(EXTRA_COMPONENT_DIRS "\$\{CMAKE_CURRENT_LIST_DIR\}/([^"]+)"\)', cmake
        )
        assert match is not None
        assert (IDF_EXAMPLE_DIR / match.group(1)).resolve() == COMPONENT_DIR.resolve()

    def test_the_version_is_project_ver_set_before_project_cmake(self) -> None:
        cmake = _cmake_code(IDF_EXAMPLE_CMAKE)
        version = cmake.index('set(PROJECT_VER "1.0.0")')
        assert version < cmake.index("include($ENV{IDF_PATH}/tools/cmake/project.cmake)")
        assert version < cmake.index("project(fleetforge_basic)")

    def test_main_requires_the_component_and_compiles_two_files(self) -> None:
        cmake = _cmake_code(IDF_EXAMPLE_MAIN_CMAKE)
        requires = re.search(r"\bREQUIRES\b([^)]*)", cmake)
        assert requires is not None
        assert "fleetforge" in requires.group(1).split()
        srcs = re.search(r"\bSRCS\b(.*?)(?=\bINCLUDE_DIRS\b|\bREQUIRES\b)", cmake, re.S)
        assert srcs is not None
        assert re.findall(r'"([^"]+)"', srcs.group(1)) == ["main.c", "fleetforge_start.c"]
        assert "-Wall -Wextra" in cmake


class TestTheIdfRollbackPosture:
    """CRITICAL.md: device-side confirm timer / rollback path, as the IDF example has it."""

    def test_the_confirm_timer_is_the_first_ff_call_and_nothing_returns_before_it(self) -> None:
        body = _function_body(_start(), "fleetforge_start")
        calls = _ff_calls(body)
        assert calls and calls[0] == "ff_mqtt_arm_confirm_timer", calls
        before = body[: body.index("ff_mqtt_arm_confirm_timer")]
        assert not re.search(r"\b(return|if|while|goto|switch)\b", before), before
        assert before.strip() == ""

    def test_a_config_without_rollback_does_not_compile(self) -> None:
        guard = re.search(
            r'#include "sdkconfig\.h"\s*#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE\s*#error "([^"]+)"',
            _start(),
        )
        assert guard is not None
        assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in guard.group(1)
        assert _start().lstrip().startswith('#include "sdkconfig.h"')

    def test_nothing_here_confirms_marks_or_reboots(self) -> None:
        for source in (_start(), _main()):
            for name in NEVER_CALLED:
                assert not re.search(rf"\b{name}\b", source), name

    def test_it_reaches_the_component_through_include_only(self) -> None:
        public_headers = {path.name for path in COMPONENT_INCLUDE.glob("*.h")}
        public = set().union(*(_declared_functions(header) for header in _public_headers()))
        for path in (IDF_EXAMPLE_START_C, IDF_EXAMPLE_MAIN_C):
            includes = set(re.findall(r'#include "(ff_[a-z0-9_]+\.h)"', path.read_text()))
            assert includes, path.name
            assert includes <= public_headers, (path.name, includes - public_headers)
            called = set(_ff_calls(_code(path)))
            assert called <= public, (path.name, called - public)

    def test_the_header_is_the_one_entry_point(self) -> None:
        header = _code(IDF_EXAMPLE_START_H)
        assert re.findall(r"\b\w+\s+(\w+)\s*\([^)]*\)\s*;", header) == ["fleetforge_start"]
        assert "bool fleetforge_start(void);" in header


class TestTheIdfParity:
    """fleetforge_start.c is Fleetforge.cpp's begin() + task, and agent_main.c's app_main."""

    def test_every_ff_call_of_agent_main_is_made(self) -> None:
        main_calls = set(_ff_calls(_code(AGENT_MAIN_C)))
        made = set(_ff_calls(_start())) | set(_ff_calls(_main()))
        assert main_calls - made == set()

    def test_the_boot_sequence_order(self) -> None:
        task = _function_body(_start(), "fleetforge_task")
        positions = [task.index(token) for token in BOOT_SEQUENCE_ORDER]
        assert positions == sorted(positions)
        assert _ff_calls(task)[-1] == "ff_mqtt_run"

    def test_enroll_only_when_absent_and_save_right_after(self) -> None:
        source = _start()
        task = _function_body(source, "fleetforge_task")
        assert re.search(
            r"if \(stored == ESP_ERR_NVS_NOT_FOUND\) \{\s*enroll_until_credentialed\(", task
        )
        assert task.count("enroll_until_credentialed(") == 1
        assert source.count("ff_enroll(") == 1
        ladder = _function_body(source, "enroll_until_credentialed")
        after = ladder[ladder.index("ff_enroll(") :]
        assert _ff_calls(after)[:2] == ["ff_enroll", "ff_store_save"]
        assert "ESP_ERR_INVALID_RESPONSE" in ladder and "ESP_ERR_INVALID_ARG" in ladder

    def test_netif_and_event_loop_tolerate_an_existing_one(self) -> None:
        source = _start()
        assert "ESP_ERR_INVALID_STATE" in _function_body(source, "created_or_existing")
        task = _function_body(source, "fleetforge_task")
        assert "created_or_existing(esp_netif_init())" in task
        assert "created_or_existing(esp_event_loop_create_default())" in task

    def test_the_task_matches_the_agent_main_task(self) -> None:
        source = IDF_EXAMPLE_START_C.read_text()
        for define in (
            "#define FF_TASK_STACK 8192",
            "#define FF_TASK_PRIO 1",
            "#define NET_TIMEOUT_MS 30000",
            "#define SNTP_TIMEOUT_MS 15000",
            "#define ENROLL_RETRY_MIN_MS 60000",
            "#define ENROLL_RETRY_MAX_MS 900000",
        ):
            assert define in source, define
        start = _function_body(_start(), "fleetforge_start")
        assert "xTaskCreatePinnedToCore(fleetforge_task" in start
        assert "s_started" in start


class TestMainIsOneScreen:
    def test_it_is_one_screen(self) -> None:
        assert len(IDF_EXAMPLE_MAIN_C.read_text().splitlines()) <= ONE_SCREEN

    def test_app_main_calls_fleetforge_start_first(self) -> None:
        match = re.search(r"void app_main\(void\)\s*\{\s*([^;]+);", _main())
        assert match is not None
        assert match.group(1).strip() == "fleetforge_start()"

    def test_the_message_is_overridable_and_the_version_is_reported(self) -> None:
        main = _main()
        assert re.search(r'#ifndef MORSE_MESSAGE\s*#define MORSE_MESSAGE "SOS"\s*#endif', main)
        assert re.search(
            r'printf\("morse: %s \(firmware %s\)\\n", MORSE_MESSAGE, ff_identity_fw_version\(\)\);',
            main,
        )


class TestTheReadmes:
    @pytest.mark.parametrize(
        ("readme", "offset", "layout"),
        [
            (EXAMPLE_README, "0x3D0000", "ab-4m-arduino-v1"),
            (IDF_EXAMPLE_README, "0x12000", "ab-4m-v1"),
        ],
    )
    def test_each_names_its_offset_and_layout(self, readme: Path, offset: str, layout: str) -> None:
        text = readme.read_text()
        assert offset in text
        assert f"`{layout}`" in text

    def test_every_block_the_script_runs_is_in_its_readme_once(self) -> None:
        readmes = {
            quickstart.ARDUINO_README: EXAMPLE_README,
            quickstart.IDF_README: IDF_EXAMPLE_README,
        }
        assert set(quickstart.REQUIRED_BLOCKS) == set(readmes)
        for key, names in quickstart.REQUIRED_BLOCKS.items():
            text = readmes[key].read_text()
            for name in names:
                assert text.count(f"# quickstart: {name}\n") == 1, (key, name)
            blocks = quickstart.require_blocks(text, names, key)
            example_dir = readmes[key].parent.relative_to(REPO_ROOT).as_posix()
            for name, body in blocks.items():
                if name == "pio-own-project":  # the one block run from the clone root
                    assert body.splitlines()[0] == "mkdir ../my-blinker", body
                else:
                    assert body.splitlines()[0] == f"cd {example_dir}", (name, body)

    def test_the_build_block_is_a_bare_pio_run(self) -> None:
        blocks = quickstart.require_blocks(
            EXAMPLE_README.read_text(), ("arduino-build",), "Basic/README.md"
        )
        assert blocks["arduino-build"].splitlines() == [
            "cd agent/components/fleetforge/examples/Basic",
            "pio run",
        ]

    def test_the_own_project_block_rewrites_the_one_lib_deps_line(self) -> None:
        body = quickstart.require_blocks(
            EXAMPLE_README.read_text(), ("pio-own-project",), "Basic/README.md"
        )["pio-own-project"]
        assert 'sed -i "s|symlink://../..|' in body
        assert EXAMPLE_PLATFORMIO_INI.read_text().count("symlink://../..") == 1
        copy = next(line for line in body.splitlines() if line.startswith("cp "))
        assert "{Basic.ino,partitions.csv,platformio.ini}" in copy
        for name in ("Basic.ino", "partitions.csv", "platformio.ini"):
            assert (EXAMPLE_PLATFORMIO_INI.parent / name).is_file()

    def test_the_arduino_edit_block_is_the_tested_sed(self) -> None:
        blocks = quickstart.require_blocks(
            EXAMPLE_README.read_text(), ("arduino-edit",), "Basic/README.md"
        )
        assert 'sed -i \'s/"SOS"/"HELLO"/; s/"1.0.0"/"1.1.0"/\' Basic.ino' in blocks["arduino-edit"]


PIO_TAIL = """\
Environment    Status    Duration
-------------  --------  ------------
esp32          SUCCESS   00:00:05.440
esp32s3        SUCCESS   00:00:04.893
========================= 2 succeeded in 00:00:10.333 =========================
"""


class TestQuickstartScript:
    def test_pio_summary_reads_the_table(self) -> None:
        assert quickstart.pio_summary(PIO_TAIL) == {"esp32": "SUCCESS", "esp32s3": "SUCCESS"}
        noisy = (
            "[SUCCESS] Took 4.89 seconds\n" + PIO_TAIL + "esp32c3        FAILED    00:00:01.100\n"
        )
        assert quickstart.pio_summary(noisy) == {
            "esp32": "SUCCESS",
            "esp32s3": "SUCCESS",
            "esp32c3": "FAILED",
        }

    def test_check_envs_fails_when_one_is_missing(self) -> None:
        run = quickstart.Run(
            tree=Path("t"), work=Path("w"), logs=Path("l"), base="", guest_base="", mqtt_uri=""
        )
        one = PIO_TAIL.replace("esp32s3        SUCCESS", "esp32s3        FAILED ")
        with pytest.raises(quickstart.QuickstartError):
            quickstart.check_envs(run, "x", one, ("esp32", "esp32s3"))
        with pytest.raises(quickstart.QuickstartError):
            quickstart.check_envs(run, "x", "no table", ("esp32",))
        quickstart.check_envs(run, "x", PIO_TAIL, ("esp32", "esp32s3"))
        assert run.results

    def test_fresh_core_requires_build_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            quickstart.main(["--fresh-pio-core"])
        assert exit_info.value.code == 2
        assert "--build-only" in capsys.readouterr().err

    def test_extraction_returns_the_body_without_the_marker(self) -> None:
        text = "intro\n\n```sh\n# quickstart: one\ncd a\necho 'x'\n```\n\n```sh\nnot named\n```\n"
        assert quickstart.extract_blocks(text) == {"one": "cd a\necho 'x'\n"}

    def test_a_missing_block_fails_by_name(self) -> None:
        text = "```sh\n# quickstart: arduino-built\ncd x\n```\n"
        with pytest.raises(
            quickstart.QuickstartError, match="no `# quickstart: arduino-build` block"
        ):
            quickstart.require_blocks(text, ("arduino-build",), "README.md")

    def test_the_real_readme_with_a_renamed_marker_fails(self) -> None:
        text = EXAMPLE_README.read_text().replace(
            "# quickstart: arduino-build\n", "# quickstart: arduino-compile\n"
        )
        with pytest.raises(quickstart.QuickstartError, match="arduino-build"):
            quickstart.require_blocks(
                text, quickstart.REQUIRED_BLOCKS[quickstart.ARDUINO_README], "Basic/README.md"
            )

    def test_a_block_named_twice_is_refused(self) -> None:
        text = "```sh\n# quickstart: a\nx\n```\n```sh\n# quickstart: a\ny\n```\n"
        with pytest.raises(quickstart.QuickstartError, match="twice"):
            quickstart.extract_blocks(text)

    def test_parse_rows_reads_the_decoder_output(self) -> None:
        decoded = (
            "Parsing binary partition input...\nVerifying table...\n"
            "# ESP-IDF Partition Table\n# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs,data,nvs,0x9000,24K,\notadata,data,ota,0xf000,8K,\nphy_init,data,phy,0x11000,4K,\n"
            "ff_cfg,data,64,0x12000,4K,\nota_0,app,ota_0,0x20000,1920K,\n"
            "ota_1,app,ota_1,0x200000,1920K,\n"
        )
        assert quickstart.parse_rows(decoded) == _rows(AGENT_DIR / "partitions.csv")

    def test_app_version_reads_the_descriptor(self) -> None:
        head = bytes(32) + bytes.fromhex("3254cdab") + bytes(12) + b"1.0.0".ljust(32, b"\0")
        assert quickstart.app_version(head) == "1.0.0"
        with pytest.raises(quickstart.QuickstartError):
            quickstart.app_version(bytes(96))

    def test_efuse_burns_are_exact_names_not_capabilities(self) -> None:
        """verify_bundle.py's rule: CONFIG_SECURE_BOOT_V1_SUPPORTED=y is the chip, not a burn."""
        config = [
            "CONFIG_SECURE_BOOT_V1_SUPPORTED=y",
            "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
            "CONFIG_SECURE_FLASH_ENC_ENABLED=n",
        ]
        assert quickstart.efuse_burns(config) == []
        assert quickstart.efuse_burns([*config, "CONFIG_SECURE_BOOT=y"]) == ["CONFIG_SECURE_BOOT=y"]
        assert quickstart.efuse_burns(["CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK=y"]) != []

    def test_only_our_warnings_count(self) -> None:
        output = "\n".join(
            [
                "/w/agent/components/fleetforge/src/ff_cfg.c:1:2: warning: unused [-Wunused]",
                "Basic.ino.cpp:3:1: warning: x",
                "/opt/esp/idf/components/lwip/x.c:1:1: warning: theirs",
                "all good",
            ]
        )
        assert len(quickstart.our_warnings(output)) == 2

    def test_the_image_is_the_justfile_pin(self) -> None:
        image = quickstart.idf_image()
        assert image.startswith("espressif/idf:v5.5.5@sha256:")
        assert f'idf_image := "{image}"' in JUSTFILE.read_text()

    def test_the_block_is_never_spliced_into_the_container_program(self) -> None:
        assert 'bash -euo pipefail -c "$BLOCK"' in quickstart.IDF_PROGRAM
        assert "{" not in quickstart.IDF_PROGRAM.replace("${", "")

    def test_the_recipe_runs_the_script_with_plain_python(self) -> None:
        recipe = re.search(r"^lib-quickstart \*args:\n((?:    .*\n)+)", JUSTFILE.read_text(), re.M)
        assert recipe is not None
        assert recipe.group(1).strip() == "python3 -u scripts/lib_quickstart.py {{ args }}"

    def test_host_idf_output_is_ignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
        dockerignore = (AGENT_DIR / ".dockerignore").read_text().splitlines()
        for name in (
            "build",
            "sdkconfig",
            "sdkconfig.old",
            "dependencies.lock",
            "managed_components",
        ):
            assert any(
                line.rstrip("/") == f"agent/components/fleetforge/examples/*/{name}"
                for line in gitignore
            ), name
            assert f"components/fleetforge/examples/*/{name}" in dockerignore, name


# R3-test-1: the broken build and its verdict.
CMD_R = "c-rbtest"
VERSION_B = "1.1.0-qs1760000000"
VERSION_R = "1.2.0-qs1760000000-rbtest"
DETAIL_R = "returned to ota_1; ota_0 did not confirm"


def _rollback_row(**overrides: object) -> dict[str, object]:
    """The final row of a good rollback run; `overrides` replace deploy keys or fw_version."""
    steps = ["staging", "staged", "confirming", "rolling_back", "rolled_back"]
    deploy: dict[str, object] = {
        "cmd_id": CMD_R,
        "state": "rolled_back",
        "is_terminal": True,
        "detail": DETAIL_R,
        "steps": [{"state": state} for state in steps],
    }
    row: dict[str, object] = {"device_id": "000000000000", "fw_version": VERSION_B}
    for key, value in overrides.items():
        if key == "fw_version":
            row[key] = value
        else:
            deploy[key] = value
    row["deploy"] = deploy
    return row


def _steps(*states: str) -> list[dict[str, str]]:
    return [{"state": state} for state in states]


class TestRollbackRun:
    def test_rollback_version_is_run_unique_and_marked(self) -> None:
        version = quickstart.rollback_version(1760000000)
        assert version == VERSION_R
        assert version.startswith("1.2.0-qs") and version.endswith("-rbtest")
        assert len(version) <= 31  # Fleetforge.begin's limit
        assert re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", version)
        assert quickstart.rollback_version(1760000001) != version

    def test_the_rollback_needle_is_the_hook_log_line(self) -> None:
        """Retyped: the needle is ff_mqtt.c's hook line, inside `#if FF_ROLLBACK_TEST`."""
        source = (COMPONENT_DIR / "src" / "ff_mqtt.c").read_text()
        assert quickstart.ROLLBACK_HOOK_NEEDLE == b"FF_ROLLBACK_TEST: ignoring the announce ack"
        assert quickstart.ROLLBACK_TEST_FLAGS == "-DFF_ROLLBACK_TEST=1"
        blocks = re.findall(r"#if FF_ROLLBACK_TEST\n(.*?)#endif", source, re.S)
        assert any("FF_ROLLBACK_TEST: ignoring the announce ack" in b for b in blocks)
        assert any("#define CONFIRM_TIMEOUT_S 60" in b for b in blocks)

    def test_has_rollback_hook(self) -> None:
        hooked = b"\x00junk\x1b[0;31mE (%lu) %s: FF_ROLLBACK_TEST: ignoring the announce ack on"
        assert quickstart.has_rollback_hook(hooked)
        assert not quickstart.has_rollback_hook(b"\x00junk FF_ROLLBACK_TEST\x00 announce ack")

    def test_confirmed_breach_on_the_deploy_state(self) -> None:
        row = _rollback_row(state="confirmed", is_terminal=True)
        message = quickstart.confirmed_breach(row, CMD_R, VERSION_R)
        assert message is not None and "P0" in message and VERSION_R in message

    def test_confirmed_breach_inside_the_steps(self) -> None:
        row = _rollback_row(steps=_steps("staged", "confirming", "confirmed", "rolled_back"))
        assert quickstart.confirmed_breach(row, CMD_R, VERSION_R) is not None

    def test_confirmed_breach_ignores_another_deploy(self) -> None:
        row = _rollback_row(cmd_id="c-b", state="confirmed", steps=_steps("confirmed"))
        assert quickstart.confirmed_breach(row, CMD_R, VERSION_R) is None

    def test_confirmed_breach_allows_r_on_its_own_version_while_confirming(self) -> None:
        row = _rollback_row(
            fw_version=VERSION_R,
            state="confirming",
            is_terminal=False,
            steps=_steps("staged", "confirming"),
        )
        assert quickstart.confirmed_breach(row, CMD_R, VERSION_R) is None

    def test_confirmed_breach_none_row(self) -> None:
        assert quickstart.confirmed_breach(None, CMD_R, VERSION_R) is None

    def test_check_rollback_row_passes_the_good_row(self) -> None:
        summary = quickstart.check_rollback_row(
            _rollback_row(), CMD_R, VERSION_B, VERSION_R, DETAIL_R
        )
        assert "rolled_back" in summary and VERSION_B in summary
        assert "rolling_back seen: yes" in summary

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"fw_version": VERSION_R}, "broken build"),
            ({"state": "confirmed"}, "confirm gate is broken"),
            ({"steps": _steps("staged", "rolled_back")}, "confirming"),
            ({"is_terminal": False}, "is_terminal"),
            ({"detail": "returned to ota_0; ota_1 did not confirm"}, "detail"),
            ({"cmd_id": "c-other"}, "cmd_id"),
        ],
    )
    def test_check_rollback_row_refuses(self, overrides: dict[str, object], match: str) -> None:
        with pytest.raises(quickstart.QuickstartError, match=match):
            quickstart.check_rollback_row(
                _rollback_row(**overrides), CMD_R, VERSION_B, VERSION_R, DETAIL_R
            )

    def test_check_rollback_row_without_rolling_back_still_passes(self) -> None:
        row = _rollback_row(steps=_steps("staged", "confirming", "rolled_back"))
        summary = quickstart.check_rollback_row(row, CMD_R, VERSION_B, VERSION_R, DETAIL_R)
        assert "rolling_back seen: no" in summary

    def test_wait_for_lines_fails_fast_on_a_forbidden_line(self, tmp_path: Path) -> None:
        run = quickstart.Run(
            tree=tmp_path, work=tmp_path, logs=tmp_path, base="", guest_base="", mqtt_uri=""
        )
        log = tmp_path / "qemu-R.log"
        log.write_text("boot\nW (1) ff-mqtt: now marked valid and CONFIRMED\n")
        started = time.monotonic()
        with pytest.raises(quickstart.QuickstartError, match="CONFIRMED"):
            quickstart.wait_for_lines(run, log, ["never-there"], timeout=5, forbidden=["CONFIRMED"])
        assert time.monotonic() - started < 2

        clean = tmp_path / "clean.log"
        clean.write_text("one\ntwo\nthree\n")
        end = quickstart.wait_for_lines(run, clean, ["one", "three"], 5, forbidden=["CONFIRMED"])
        assert end == len("one\ntwo\nthree")
        assert quickstart.wait_for_lines(run, clean, ["three"], 5, start=end - 5) == end

    def test_phase_board_runs_the_rollback_after_the_ota(self) -> None:
        source = inspect.getsource(quickstart.phase_board)
        calls = ("phase_enroll(", "phase_ota(", "phase_rollback(", "check_no_credentials(")
        positions = [source.index(call) for call in calls]
        assert positions == sorted(positions)
        assert "rollback_version(epoch)" in source and 'f"1.1.0-qs{epoch}"' in source
