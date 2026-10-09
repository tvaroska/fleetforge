"""A/B slot apply and the atomic switch (R2-fw-2) — firmware tripwires, as text.

No host test can execute `agent/components/fleetforge/src/*.c`; the QEMU run (an otadata decode per sector) is
the proof. What CAN be held here, on every `just test`, are the rules that make it true.
Each one is silent on the bench and fatal in the field. A bench board is staged once and
rebooted by hand. A fleet board receives a second deploy while the first one waits for its
reboot, or while its new image is still confirming:

* the slot is chosen ONCE, and that same pointer is what esp_https_ota writes
  (`.partition.staging`), what is hashed and what is recorded;
* a stage is refused before any I/O while the running image is PENDING_VERIFY, and while
  the boot pointer names a slot other than the running one. The second is the brick.
  IDF v5.5.5's `esp_rewrite_ota_data()` picks the new seq with
  `while (seq > id+1 + i*N) i++`, and equality stops it. Switching to the slot the active
  otadata entry already names therefore writes the SAME seq into the other sector, which
  is the running image's entry. Both sectors then name the staged slot, and a rollback
  boots the broken image again;
* the target is never the running slot;
* the undo after a failed `finish()` writes otadata only if the boot pointer moved. The
  same equal-seq rewrite otherwise writes a NEW entry for the known-good image;
* the refusals never touch the transaction record of the stage that is waiting.

Same idiom as `test_agent_verify.py`: comments are stripped, because they quote the very
spellings these tests forbid.
"""

from tests.test_agent_txn import FF_OTA_C, _function_body
from tests.test_ff_cfg import _code


def _ota_task() -> str:
    return _function_body(_code(FF_OTA_C), "ota_task")


def _chooser() -> str:
    return _function_body(_code(FF_OTA_C), "choose_target_slot")


def test_the_slot_is_chosen_once_and_handed_to_esp_https_ota() -> None:
    assert _code(FF_OTA_C).count("esp_ota_get_next_update_partition(") == 1
    assert "esp_ota_get_next_update_partition(" in _chooser()
    task = _ota_task()
    assert task.index("choose_target_slot(cmd)") < task.index("resolve_artifact_url(")
    assert task.index(".staging = target") < task.index("esp_https_ota_begin(")


def test_an_unconfirmed_running_image_refuses_the_stage_before_any_io() -> None:
    chooser = _chooser()
    assert "ESP_OTA_IMG_PENDING_VERIFY" in chooser
    assert '"the running image is not confirmed yet"' in chooser
    task = _ota_task()
    assert (
        task.index("choose_target_slot(")
        < task.index("resolve_artifact_url(")
        < task.index("esp_https_ota_begin(")
    )


def test_a_staged_image_is_never_overwritten() -> None:
    chooser = _chooser()
    assert "esp_ota_get_boot_partition()" in chooser
    assert "boot != running" in chooser
    assert '"an update is already staged and waits for a reboot"' in chooser
    # A confirming image is refused first, then a waiting staged one, then the slot is
    # computed.
    assert (
        chooser.index("ESP_OTA_IMG_PENDING_VERIFY")
        < chooser.index("esp_ota_get_boot_partition()")
        < chooser.index("esp_ota_get_next_update_partition(")
    )


def test_the_target_is_never_the_running_slot() -> None:
    assert "target == running" in _chooser()


def test_the_undo_writes_otadata_only_if_the_pointer_moved() -> None:
    undo = _function_body(_code(FF_OTA_C), "restore_boot_partition")
    assert undo.index("esp_ota_get_boot_partition()") < undo.index("esp_ota_set_boot_partition(")
    assert "boot == running" in undo
    assert "nothing to undo" in undo


def test_the_refusals_never_touch_the_transaction_record() -> None:
    chooser = _chooser()
    assert "ff_txn_" not in chooser
    # Read-only: no otadata write, no erase, no mark.
    for forbidden in (
        "esp_ota_set_boot_partition",
        "esp_ota_mark_",
        "esp_ota_erase_",
        "esp_partition_erase",
        "esp_partition_write",
    ):
        assert forbidden not in chooser, forbidden
