"""R3-fw-5 — a wrong flash layout is announced as `unknown`, and the OTA path needs rollback.

Firmware tripwires, as text, in the idiom of `test_agent_board_measurements.py`: no host test
can run `agent/components/fleetforge/src/*.c`, so what is held here, on every `just test`, is
the shape that makes the QEMU run in `docs/runbooks/agent-qemu.md` (*A wrong flash layout is
refused*) come out right:

* the board's table of known layouts IS `SUPPORTED_LAYOUTS`, id for id and fingerprint for
  fingerprint, so a board says what map it carries and the server agrees on what that means;
* the reserved `unknown` is spelled the same on both ends and is never a supported layout;
* the announce sends the detected id, never this build's compiled one;
* a table too big to be any supported layout is announced as `unknown`, a table that cannot
  be measured keeps the build's id (the fingerprint goes out null and the server fails open);
* the boot-time error names the fix and carries no hex run the diagnostics bundle would read
  as a device id;
* `ff_ota.c` does not compile without `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`;
* the T2 fixture is a plausible, bootable, wrong table.
"""

from __future__ import annotations

import re
from pathlib import Path

from fleetforge.firmware.manifest import SUPPORTED_LAYOUTS, UNKNOWN_PARTITION_LAYOUT
from tests.agent_src import COMPONENT_SRC
from tests.test_agent_board_measurements import _csv_fingerprint
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code

FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_IDENTITY_INTERNAL_H = COMPONENT_SRC / "ff_identity_internal.h"
FF_OTA_C = COMPONENT_SRC / "ff_ota.c"
WRONG_LAYOUT_CSV = Path(__file__).resolve().parent / "fixtures" / "wrong-layout-partitions.csv"


def _identity() -> str:
    return _code(FF_IDENTITY_C)


def _known_layouts() -> dict[str, str]:
    table = re.search(r"FF_KNOWN_LAYOUTS\[\]\s*=\s*\{(.*?)\};", _identity(), re.DOTALL)
    assert table is not None, "ff_identity.c has no FF_KNOWN_LAYOUTS table"
    rows = re.findall(r'\{\s*"([a-z0-9.-]+)"\s*,\s*"([0-9a-f]{64})"\s*\}', table.group(1))
    assert rows, "FF_KNOWN_LAYOUTS did not parse"
    assert len(rows) == len(dict(rows)), "a layout id is listed twice"
    return dict(rows)


def _unknown_error_format() -> str:
    body = _function_body(_identity(), "log_unknown_layout")
    call = re.search(r"ESP_LOGE\(TAG,\s*((?:\"(?:[^\"\\]|\\.)*\"\s*)+)", body)
    assert call is not None, "log_unknown_layout() logs no ESP_LOGE"
    return "".join(re.findall(r"\"((?:[^\"\\]|\\.)*)\"", call.group(1)))


# ── the layout id is detected by fingerprint ───────────────────────────────────────────


def test_the_board_knows_exactly_the_supported_layouts() -> None:
    assert _known_layouts() == {
        layout: profile.partition_table_sha256 for layout, profile in SUPPORTED_LAYOUTS.items()
    }


def test_unknown_is_spelled_alike_on_both_ends_and_never_supported() -> None:
    define = re.search(
        r'#define FF_PARTITION_LAYOUT_UNKNOWN "([^"]+)"', _code(FF_IDENTITY_INTERNAL_H)
    )
    assert define is not None
    assert define.group(1) == UNKNOWN_PARTITION_LAYOUT == "unknown"
    assert UNKNOWN_PARTITION_LAYOUT not in SUPPORTED_LAYOUTS
    assert UNKNOWN_PARTITION_LAYOUT not in _known_layouts()


def test_the_announce_sends_the_detected_layout_not_the_compiled_one() -> None:
    body = _function_body(_identity(), "announce_object")
    assert 'cJSON_AddStringToObject(root, "partition_layout", s_partition_layout)' in body
    assert "FF_PARTITION_LAYOUT" not in body


def test_the_layout_is_detected_once_at_init_after_the_fingerprint() -> None:
    body = _function_body(_identity(), "measure_board")
    fingerprint = body.index("partition_fingerprint(")
    detect = body.index("s_partition_layout = detect_layout()")
    assert fingerprint < detect
    assert "log_unknown_layout()" in body[detect:]
    assert _identity().count("s_partition_layout =") == 2, "one initialiser, one detection"


def test_too_many_entries_is_unknown_and_an_unmeasured_table_keeps_the_build_id() -> None:
    fingerprint = _function_body(_identity(), "partition_fingerprint")
    branch = fingerprint[fingerprint.index("if (count == FF_PT_MAX_ENTRIES)") :]
    assert "s_pt_too_many = true;" in branch[: branch.index("return false;")]

    detect = _function_body(_identity(), "detect_layout")
    too_many = detect.index("if (s_pt_too_many)")
    unmeasured = detect.index("if (!s_have_partition_sha256)")
    assert too_many < unmeasured, "too many entries must win over 'no fingerprint'"
    assert "return FF_PARTITION_LAYOUT_UNKNOWN;" in detect[too_many:unmeasured]
    assert "return FF_PARTITION_LAYOUT;" in detect[unmeasured:]
    assert "strcmp(s_partition_sha256, FF_KNOWN_LAYOUTS[i].sha256)" in detect
    assert detect.rstrip().endswith("return FF_PARTITION_LAYOUT_UNKNOWN;")


def test_the_boot_error_names_the_fix_and_no_device_id_lookalike() -> None:
    """`frontend/src/diagnostics.ts` takes the last ff-id line's first 12-hex run as the
    device id, so the line must not carry one; the fingerprint is in the `board:` line."""
    text = _unknown_error_format()
    for needle in ("unknown", "USB", "partitions.csv", "never changes over the air"):
        assert needle in text, needle
    assert text.count("%s") == 4 and text.count("%d") == 1
    assert not re.search(r"[0-9a-f]{12}", text, re.IGNORECASE)
    for source in re.findall(r'#define FF_LAYOUT_SOURCE "([^"]+)"', _identity()):
        assert "partitions.csv" in source
        assert not re.search(r"[0-9a-f]{12}", source, re.IGNORECASE)


def test_the_board_line_names_the_announced_layout() -> None:
    body = _function_body(_identity(), "ff_identity_init")
    assert "layout %s, rollback_capable %s" in body
    assert "s_partition_layout" in body


# ── the OTA path needs a bootloader that rolls back ────────────────────────────────────


def test_ff_ota_does_not_compile_without_rollback() -> None:
    guard = re.search(
        r'#include "sdkconfig\.h".*?#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE\s*\n'
        r'\s*#error "([^"]+)"\s*\n\s*#endif',
        _code(FF_OTA_C),
        re.DOTALL,
    )
    assert guard is not None
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in guard.group(1)
    assert "`" not in guard.group(1)


# ── the T2 fixture ─────────────────────────────────────────────────────────────────────


def test_the_wrong_layout_fixture_boots_but_is_not_supported() -> None:
    fingerprint = _csv_fingerprint(WRONG_LAYOUT_CSV)
    assert fingerprint not in {p.partition_table_sha256 for p in SUPPORTED_LAYOUTS.values()}

    rows = {}
    for line in WRONG_LAYOUT_CSV.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            name, kind, subtype, offset, size = (field.strip() for field in line.split(","))
            rows[name] = (kind, subtype, int(offset, 0), int(size, 0))
    assert rows["otadata"][2] == 0xE000 and rows["ota_0"][2] == 0x10000
    assert rows["ota_0"][3] == rows["ota_1"][3] == 0x1C0000 == 1835008
    assert rows["ota_1"][2] == rows["ota_0"][2] + rows["ota_0"][3]
    assert rows["ff_cfg"] == ("data", "0x40", 0x3D0000, 0x1000)
