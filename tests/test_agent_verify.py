"""Verify before the boot switch (R2-fw-1) — firmware tripwires, as text.

No host test can execute `agent/components/fleetforge/src/*.c`; the QEMU run (otadata byte-identical across a
corrupt stage) is the proof that the boot pointer never moves for a bad image. What CAN be
held here, on every `just test`, are the orderings that make that true. Each one is silent
on the bench — a board on a desk never loses power in the half-second window — and fatal
in the field, where it boots an image nobody verified:

* the slot is hashed BEFORE `esp_https_ota_finish()` (the call that moves the boot
  pointer), and only once;
* a mismatch aborts the update and touches neither `finish()` nor the boot partition;
* the boot partition is set in exactly one place: the undo for a failed `finish()`;
* flash encryption, which would break the premise (esp_ota_write would hold back a
  trailing block), is a compile error;
* the slot size is checked before anything is fetched or erased;
* a misspelt digest is refused at the command seam, before ff_ota is started, and is never
  normalised on the way.

Same idiom as `test_agent_txn.py`: comments are stripped, because they quote the very
spellings these tests forbid.
"""

import re

from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, _function_body
from tests.test_ff_cfg import _code


def _ota_task() -> str:
    return _function_body(_code(FF_OTA_C), "ota_task")


def test_the_digest_is_checked_before_the_boot_pointer_moves() -> None:
    task = _ota_task()
    assert task.count("partition_digest(") == 1, "one read-back, not one per side of finish()"
    assert task.index("partition_digest(") < task.index("esp_https_ota_finish(")


def test_a_mismatch_aborts_and_never_touches_the_boot_partition() -> None:
    task = _ota_task()
    start = task.index("strcmp(digest, cmd->sha256)")
    branch = task[start : task.index("goto done", start)]
    assert "esp_https_ota_abort(handle)" in branch
    assert "restore_boot_partition" not in branch
    assert "esp_https_ota_finish" not in branch
    # The seam guarantees lowercase; a case-insensitive compare would hide a seam bug.
    assert "strcasecmp(digest" not in _code(FF_OTA_C)


def test_the_boot_partition_is_set_only_by_the_undo() -> None:
    source = _code(FF_OTA_C)
    undo = _function_body(source, "restore_boot_partition")
    assert source.count("esp_ota_set_boot_partition(") == 1
    assert undo.count("esp_ota_set_boot_partition(") == 1


def test_flash_encryption_is_a_compile_error() -> None:
    assert re.search(r"#if CONFIG_SECURE_FLASH_ENC_ENABLED\s*\n\s*#error ", _code(FF_OTA_C))


def test_the_slot_size_is_checked_before_anything_is_fetched() -> None:
    task = _ota_task()
    guard = task.index("cmd->size > target->size")
    assert guard < task.index("resolve_artifact_url(") < task.index("esp_https_ota_begin(")


def test_a_malformed_digest_is_refused_at_the_seam() -> None:
    mqtt = _code(FF_MQTT_C)
    stage = _function_body(mqtt, "on_stage")
    assert stage.index("is_lowercase_sha256(") < stage.index("ff_ota_start(")
    assert '"artifact sha256 malformed"' in stage
    # Never normalised; and isxdigit() would let uppercase through.
    validator = _function_body(mqtt, "is_lowercase_sha256")
    for forbidden in ("tolower", "toupper", "isxdigit"):
        assert forbidden not in validator, forbidden
