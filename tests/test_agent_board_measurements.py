"""The board measurements in `up/announce` (R2b-fw-2, agent 0.4.7) — firmware tripwires, as text.

`flash_chip_size`, `partition_table_sha256` and `rollback_capable` are what the server's
pre-check reads to refuse a build whose table does not match the board, and to warn
before deploying to a board that cannot roll back (`spec/device-protocol.md` ->
`up/announce`, DECISIONS 2026-10-03 R2-spec-1 and 2026-10-04 R2b-spec-2). Every one of them
must be MEASURED on the board, never taken from the build: the build is exactly the thing
that may disagree with the board. No host test can run `agent/main/*.c`; the QEMU run in
`docs/runbooks/agent-qemu.md` is the live proof. What is held here, on every `just test`:

* the keys go out in the spec's order, right after `ota_slot_size`;
* no fallback to the image header's flash size, no derivation from the build config;
* the fingerprint follows the spec's geometry-only rule, and the worked `ab-4m-v1` value
  is reproducible from the checked-in `agent/partitions.csv`;
* `rollback_capable` is `true` or null — never `false` until R2b-test-5;
* it is observed from a PENDING_VERIFY boot in `classify_txn()`, never on the
  confirm/rollback path, and stored in the credential namespace;
* a NEW-at-target boot ends `confirmed` with a detail, only after a successful mark-valid.

Same idiom as `test_agent_txn.py`: comment-stripped C text, brace-matched bodies.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
AGENT_MAIN = AGENT_DIR / "main"
FF_IDENTITY_C = AGENT_MAIN / "ff_identity.c"
FF_IDENTITY_H = AGENT_MAIN / "ff_identity.h"
FF_MQTT_C = AGENT_MAIN / "ff_mqtt.c"
FF_STORE_C = AGENT_MAIN / "ff_store.c"
FF_STORE_H = AGENT_MAIN / "ff_store.h"
FF_TXN_C = AGENT_MAIN / "ff_txn.c"
FF_TXN_H = AGENT_MAIN / "ff_txn.h"
CMAKELISTS = AGENT_MAIN / "CMakeLists.txt"
PARTITIONS_CSV = AGENT_DIR / "partitions.csv"
VERSION_TXT = AGENT_DIR / "version.txt"
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"

AB_4M_V1_SHA256 = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"


def _announce() -> str:
    return _function_body(_code(FF_IDENTITY_C), "announce_object")


# ── the announce keys ──────────────────────────────────────────────────────────────────


def test_the_keys_go_out_in_spec_order_after_ota_slot_size() -> None:
    body = _announce()
    keys = (
        '"ota_slot_size"',
        '"flash_chip_size"',
        '"partition_table_sha256"',
        '"rollback_capable"',
        '"capabilities"',
    )
    positions = []
    for key in keys:
        assert key in body, f"announce_object() never emits {key}"
        positions.append(body.index(key))
    assert positions == sorted(positions), f"announce key order is not {keys}"


def test_the_chip_size_is_physical_with_no_fallback_and_nothing_comes_from_the_build() -> None:
    """`esp_flash_get_size()` is the image header's claim, and the bootloader config is a
    property of the image that was flashed, not of the bootloader on the board."""
    source = _code(FF_IDENTITY_C)
    assert "esp_flash_get_physical_size(" in source
    assert "esp_flash_get_size(" not in source
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE" not in source
    assert "spi_flash" in CMAKELISTS.read_text()


def test_an_unreadable_chip_size_is_omitted_not_nulled() -> None:
    """The spec: "a device that cannot read it omits the field". Not null, never 0."""
    source = _code(FF_IDENTITY_C)
    assert 'cJSON_AddNullToObject(root, "flash_chip_size")' not in source
    assert "if (s_have_flash_chip_size)" in _announce()


def test_the_fingerprint_follows_the_geometry_only_rule() -> None:
    body = _function_body(_code(FF_IDENTITY_C), "partition_fingerprint")
    assert "esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL)" in body
    assert "esp_flash_default_chip" in body, "external flash must be filtered out"
    assert "esp_partition_iterator_release(" in body, "an early exit leaks the iterator"
    assert '"%u:%u:%" PRIu32 ":%" PRIu32 "\\n"' in body, "decimal type:subtype:offset:size"
    assert "->address" in body
    for forbidden in ("->label", "encrypted", "readonly"):
        assert forbidden not in body, f"{forbidden} is not part of the fingerprint"
    assert "mbedtls_sha256_finish" in body


def _csv_fingerprint(path: Path) -> str:
    """The board-profiles.md reproduction recipe, verbatim in effect."""
    types = {"app": 0, "data": 1}
    subtypes = {
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
    rows = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(",")]
        subtype = (
            int(fields[2], 0) if fields[2].startswith("0x") else subtypes[fields[1]][fields[2]]
        )
        rows.append((types[fields[1]], subtype, int(fields[3], 0), int(fields[4], 0)))
    serial = "".join(f"{t}:{s}:{o}:{z}\n" for t, s, o, z in sorted(rows, key=lambda r: r[2]))
    return hashlib.sha256(serial.encode()).hexdigest()


def test_the_worked_ab_4m_v1_value_is_reproducible_from_the_csv() -> None:
    """The host-side twin of the QEMU `board:` line: the same rule over the checked-in
    table gives the value the spec's *Partition layouts* table prints."""
    assert _csv_fingerprint(PARTITIONS_CSV) == AB_4M_V1_SHA256
    row = re.search(
        r"^\|\s*`ab-4m-v1`\s*\|\s*\d+\s*\|\s*`([0-9a-f]{64})`", DEVICE_PROTOCOL.read_text(), re.M
    )
    assert row is not None, "the spec's Partition layouts table has no ab-4m-v1 row"
    assert row.group(1) == AB_4M_V1_SHA256


# ── rollback_capable: measured, true-only ──────────────────────────────────────────────


def test_rollback_capable_is_true_or_null_never_false() -> None:
    """DECISIONS 2026-10-04 A3: `false` waits for R2b-test-5, which benches what a
    rollback-less bootloader leaves behind. R2b-test-5's follow-up deletes this pin (and
    gives ff_store_save_rollback_capable() its argument)."""
    source = _code(FF_IDENTITY_C)
    assert 'cJSON_AddTrueToObject(root, "rollback_capable")' in source
    assert 'cJSON_AddNullToObject(root, "rollback_capable")' in source
    assert 'cJSON_AddFalseToObject(root, "rollback_capable"' not in source
    assert 'cJSON_AddBoolToObject(root, "rollback_capable"' not in source
    assert "esp_err_t ff_store_save_rollback_capable(void)" in _code(FF_STORE_C)
    assert "esp_err_t ff_store_save_rollback_capable(void);" in _code(FF_STORE_H)
    assert "void ff_identity_note_rollback_capable(void);" in _code(FF_IDENTITY_H)


def test_it_is_observed_at_classification_from_pending_verify_before_the_record() -> None:
    mqtt = _code(FF_MQTT_C)
    classify = _function_body(mqtt, "classify_txn")
    assert "pending_verify()" in classify
    assert "ff_identity_note_rollback_capable()" in classify
    assert classify.index("pending_verify()") < classify.index(
        "ff_identity_note_rollback_capable()"
    )
    assert classify.index("ff_identity_note_rollback_capable()") < classify.index("ff_txn_load("), (
        "observed only when a record exists: a board OTA'd by a pre-0.4.0 agent never learns"
    )
    assert mqtt.count("ff_identity_note_rollback_capable(") == 1


def test_the_confirm_and_rollback_path_stays_free_of_the_observation() -> None:
    """Those run from esp_timer or right before a reboot; no NVS write belongs there, and a
    rollback is not evidence (mark_app_invalid writes INVALID from any state)."""
    mqtt = _code(FF_MQTT_C)
    for name in (
        "confirm_timeout_cb",
        "rollback_now_cb",
        "rollback_report_task",
        "ff_mqtt_arm_confirm_timer",
        "confirm_this_image",
    ):
        body = _function_body(mqtt, name)
        assert "ff_identity_note_rollback_capable" not in body, name
        assert "ff_store_" not in body, name
        assert "nvs_" not in body, name


def test_the_observation_is_stored_in_the_credential_namespace() -> None:
    """ "ff", so a re-flash with a new token clears it and an OTA keeps it; never "ff_txn",
    which ff_txn_save() erases at every stage."""
    header = _code(FF_STORE_H)
    source = _code(FF_STORE_C)
    match = re.search(r'#define FF_STORE_KEY_ROLLBACK_CAPABLE "([^"]+)"', header)
    assert match is not None
    assert len(match.group(1)) <= 15, "NVS keys are at most 15 characters"
    for name in ("ff_store_load_rollback_capable", "ff_store_save_rollback_capable"):
        body = _function_body(source, name)
        assert "nvs_open(FF_STORE_NAMESPACE" in body, name
        assert "FF_STORE_KEY_ROLLBACK_CAPABLE" in body, name
        assert "nvs_erase" not in body, name
    assert "nvs_set_u8(handle, FF_STORE_KEY_ROLLBACK_CAPABLE, 1)" in source
    assert "value == 1" in _function_body(source, "ff_store_load_rollback_capable")
    assert "nvs_flash_erase" not in source
    for path in (FF_TXN_C, FF_TXN_H):
        assert "ROLLBACK_CAPABLE" not in _code(path)
        assert "rb_cap" not in _code(path)


def test_the_note_writes_nvs_only_once() -> None:
    body = _function_body(_code(FF_IDENTITY_C), "ff_identity_note_rollback_capable")
    assert body.index("if (s_rollback_capable)") < body.index("ff_store_save_rollback_capable(")
    init = _function_body(_code(FF_IDENTITY_C), "ff_identity_init")
    assert "ff_store_load_rollback_capable()" in init
    assert "ff_store_save_rollback_capable" not in init


# ── NEW-at-target ──────────────────────────────────────────────────────────────────────


def test_new_at_target_is_classified_and_the_stale_fallthrough_remains() -> None:
    classify = _function_body(_code(FF_MQTT_C), "classify_txn")
    assert "state == ESP_OTA_IMG_NEW" in classify
    assert "TXN_BOOTED_NEW" in classify
    assert "DETAIL_BOOTED_NEW" in classify
    assert "stale transaction record" in classify
    new_branch = classify[classify.index("state == ESP_OTA_IMG_NEW") :]
    new_branch = new_branch[: new_branch.index("return;")]
    for forbidden in ("ff_identity_note_rollback_capable", "ff_store_", "nvs_"):
        assert forbidden not in new_branch, forbidden


def test_new_at_target_confirms_only_after_a_mark_valid_from_new() -> None:
    mqtt = _code(FF_MQTT_C)
    existing = "else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING)"
    new = "s_txn.kind == TXN_BOOTED_NEW && accept_unverified_image()"
    assert existing in mqtt, "the R2-be-1 confirm line must stay byte-identical"
    assert new in mqtt
    assert mqtt.index("if (s_rollback_decided)") < mqtt.index(existing) < mqtt.index(new)
    accept = _function_body(mqtt, "accept_unverified_image")
    assert "ESP_OTA_IMG_NEW" in accept
    assert accept.index("ESP_OTA_IMG_NEW") < accept.index(
        "esp_ota_mark_app_valid_cancel_rollback()"
    )
    assert "err == ESP_OK" in accept


def test_new_at_target_never_promises_a_rollback_deadline() -> None:
    body = _function_body(_code(FF_MQTT_C), "report_txn_on_connect")
    booted_new = body[body.index("case TXN_BOOTED_NEW:") :]
    booted_new = booted_new[: booted_new.index("break;")]
    assert "FF_STATUS_CONFIRMING" not in booted_new
    assert "enqueue_status" not in booted_new
    # A reconnect before the PUBACK repeats the same detail.
    assert "s_txn.detail[0] != '\\0' ? s_txn.detail : NULL" in body


def test_the_agent_version_is_at_least_0_4_7() -> None:
    version = VERSION_TXT.read_text().strip()
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None, version
    numbers = tuple(int(part) for part in match.groups())
    assert numbers >= (0, 4, 7), f"the measurements ship in agent 0.4.7, not {version}"
