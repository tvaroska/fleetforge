"""The transaction that crosses the apply reboot (R2-be-1) — firmware tripwires, as text.

No host test can execute `agent/main/*.c`, and the QEMU run is the only proof that the
record survives a reset. What CAN be held here, on every `just test`, are the rules whose
violation is silent on the bench and expensive in the field:

* the record lives in its OWN namespace and nothing erases NVS wholesale — ff_store's
  namespace is erased on a token change, and IDF's `phy` namespace shares the partition;
* the confirm-timeout path never touches the MQTT client — a broken image is exactly the
  image whose mqtt task may be wedged, and the rollback must not wait on it;
* the outcome states are published only by ff_mqtt.c, the session that observed them;
* ff_ota.c records the transaction only after the digest matched and finish() moved the
  boot pointer, and before `staged`;
* the record is cleared only for the cmd_id that was reported.

Same idiom as `test_ff_cfg.py` (whose comment-stripper is reused): the comments in these
files quote the very spellings they forbid, so the greps look at code only.
"""

import re
from pathlib import Path

from tests.test_ff_cfg import _code

AGENT_MAIN = Path(__file__).resolve().parent.parent / "agent" / "main"
FF_TXN_C = AGENT_MAIN / "ff_txn.c"
FF_TXN_H = AGENT_MAIN / "ff_txn.h"
FF_MQTT_C = AGENT_MAIN / "ff_mqtt.c"
FF_OTA_C = AGENT_MAIN / "ff_ota.c"
CMAKELISTS = AGENT_MAIN / "CMakeLists.txt"


def _function_body(source: str, name: str) -> str:
    """The body of C function `name` (its definition, not a prototype), by brace matching.

    A greedy regex would run to the end of the file; a lazy one stops at the first nested
    `}`. Counting braces is the only honest way to get one function out of C text.
    """
    match = re.search(rf"\b{name}\s*\([^;{{)]*\)\s*\{{", source)
    assert match is not None, f"no definition of {name}()"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end() : index]
    raise AssertionError(f"unbalanced braces in {name}()")


def test_the_record_is_compiled_in() -> None:
    assert '"ff_txn.c"' in CMAKELISTS.read_text()


def test_the_record_has_its_own_namespace_and_never_erases_the_partition() -> None:
    header = _code(FF_TXN_H)
    source = _code(FF_TXN_C)
    assert '#define FF_TXN_NAMESPACE "ff_txn"' in header
    assert "nvs_open(FF_TXN_NAMESPACE" in source
    assert "FF_STORE_NAMESPACE" not in source
    assert 'nvs_open("ff"' not in source
    assert "nvs_flash_erase" not in source
    # Keys fit NVS's 15-character limit, or nvs_set_* fails at runtime on a board.
    for key in re.findall(r'#define FF_TXN_KEY_[A-Z_]+ "([^"]+)"', header):
        assert len(key) <= 15, key


def test_the_confirm_timeout_path_never_touches_the_mqtt_client() -> None:
    """The CRITICAL invariant: no esp_mqtt_client_* call (they take the client's lock), and
    the immediate rollback is still there as the fallback."""
    body = _function_body(_code(FF_MQTT_C), "confirm_timeout_cb")
    assert "esp_mqtt_client_" not in body
    assert "enqueue_status" not in body and "ff_mqtt_publish_status" not in body
    assert "esp_ota_mark_app_invalid_rollback_and_reboot()" in body
    assert "ESP_ERROR_CHECK" not in body
    # The grace timer that bounds the report is the rollback itself.
    assert "esp_ota_mark_app_invalid_rollback_and_reboot()" in _function_body(
        _code(FF_MQTT_C), "rollback_now_cb"
    )


def test_the_outcome_is_reported_only_by_the_session_that_observed_it() -> None:
    mqtt = _code(FF_MQTT_C)
    ota = _code(FF_OTA_C)
    for state in ("FF_STATUS_CONFIRMED", "FF_STATUS_ROLLED_BACK", "FF_STATUS_CONFIRMING"):
        assert state in mqtt, f"ff_mqtt.c never reports {state}"
    for state in (
        "FF_STATUS_CONFIRMING",
        "FF_STATUS_CONFIRMED",
        "FF_STATUS_ROLLING_BACK",
        "FF_STATUS_ROLLED_BACK",
    ):
        assert state not in ota, f"ff_ota.c must not report {state}"


def test_confirmed_is_queued_only_on_a_successful_mark_valid() -> None:
    mqtt = _code(FF_MQTT_C)
    assert "static bool confirm_this_image(void)" in mqtt
    assert "else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING)" in mqtt


def test_a_late_ack_cannot_confirm_an_image_already_rolling_back() -> None:
    """The grace timer opens a window the old code did not have: the rollback decision is
    taken first, the reboot follows up to ROLLBACK_REPORT_GRACE_MS later. An announce ack
    in between must not mark the image valid."""
    mqtt = _code(FF_MQTT_C)
    timeout = _function_body(mqtt, "confirm_timeout_cb")
    assert timeout.index("s_rollback_decided = true;") < timeout.index("esp_timer_start_once(")
    assert mqtt.index("if (s_rollback_decided)") < mqtt.index(
        "else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING)"
    )


def test_the_transaction_is_recorded_after_verification_and_before_staged() -> None:
    """R2-fw-1 put the digest check before finish() (the boot switch); the record still
    follows finish(), because it is only true once the new slot is bootable."""
    ota = _code(FF_OTA_C)
    assert ota.count("ff_txn_save(") == 1, "recorded on more than one path"
    task = _function_body(ota, "ota_task")
    verified = task.index("strcmp(digest, cmd->sha256)")
    finish = task.index("esp_https_ota_finish(")
    saved = task.index("ff_txn_save(")
    bootable = task.index("is staged and bootable")
    staged = task.index("FF_STATUS_STAGED")
    assert verified < finish < saved < bootable < staged


def test_the_record_is_cleared_only_for_the_reported_cmd_id() -> None:
    txn = _code(FF_TXN_C)
    mqtt = _code(FF_MQTT_C)
    assert "esp_err_t ff_txn_clear_if(const char *cmd_id)" in txn
    assert "strcmp(stored, cmd_id)" in txn
    assert "ff_txn_clear_if(s_txn.cmd_id)" in mqtt
    assert re.search(r"\bff_txn_clear\s*\(", mqtt) is None
    assert "nvs_erase" not in mqtt
