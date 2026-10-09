"""A re-delivered stage for the update in progress is ignored, not failed (R2-fw-6).

No host test can execute `agent/components/fleetforge/src/*.c`; the QEMU run (a re-POST of the deploy that is
downloading, or of the image staged and waiting, answers `reused: true` with the same
cmd_id, and the board logs "already carrying out — ignored" instead of publishing `failed`
against it) is the proof. What CAN be held here, on every `just test`, is what makes that
true. Each one fails silently: the board carries on with its update while the server has
already filed the deploy as `failed` and drops the real outcome when it arrives.

* the predicate is declared in ff_ota.h, the module that owns "what is running";
* on_stage() asks it BEFORE any parse, publish or ff_ota_start(), and the ignore branch
  publishes nothing, starts nothing and never touches the URL;
* a DIFFERENT cmd_id still gets its honest `failed` (the ff_ota_start() contract is kept);
* the predicate is read-only: no publish, no transaction write or clear, no otadata write,
  and it never assigns `s_running`;
* the in-flight answer comes before the staged one, so a running update for another id is
  never confused with a staged image;
* `s_running_cmd_id` has exactly one writer, in ff_ota_start(), before `s_running = true`,
  and ota_task never touches it.

Same idiom as `test_agent_txn.py`: comments are stripped, because they quote the very
spellings these tests forbid.
"""

import re

from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, _function_body
from tests.test_ff_cfg import _code

FF_OTA_H = FF_OTA_C.with_name("ff_ota.h")
PREDICATE_CALL = "if (ff_ota_is_handling("


def _on_stage() -> str:
    return _function_body(_code(FF_MQTT_C), "on_stage")


def _predicate() -> str:
    return _function_body(_code(FF_OTA_C), "ff_ota_is_handling")


def test_the_predicate_is_declared_by_the_module_that_owns_the_update() -> None:
    assert "bool ff_ota_is_handling(const char *cmd_id);" in _code(FF_OTA_H)


def test_on_stage_drops_a_redelivery_before_anything_is_parsed_or_published() -> None:
    body = _on_stage()
    assert PREDICATE_CALL in body
    check = body.index(PREDICATE_CALL)
    assert check < body.index("ff_mqtt_publish_status(")
    assert check < body.index("ff_ota_start(")
    assert check < body.index('cJSON_GetObjectItemCaseSensitive(root, "artifact")')

    branch = body[check : body.index("return;", check)]
    assert "ff_mqtt_publish_status" not in branch, "an ignored re-delivery publishes nothing"
    assert "ff_ota_start" not in branch
    # The URL is a bearer credential, and it is not even parsed at this point.
    assert "url" not in branch


def test_a_different_cmd_id_is_still_refused_honestly() -> None:
    body = _on_stage()
    refusal = body.index("err == ESP_ERR_INVALID_STATE")
    assert '"another update is already in progress"' in body[refusal:]


def test_the_predicate_is_read_only() -> None:
    body = _predicate()
    for needed in (
        "s_running",
        "s_running_cmd_id",
        "esp_ota_get_boot_partition(",
        "esp_ota_get_running_partition(",
        "ff_txn_load(",
    ):
        assert needed in body, f"ff_ota_is_handling() no longer reads {needed}"
    for forbidden in (
        "fail(",
        "ff_mqtt_publish_status",
        "ff_txn_save",
        "ff_txn_clear_if",
        "esp_ota_set_boot_partition",
        "esp_ota_begin",
        "esp_partition_erase",
        "esp_restart",
    ):
        assert forbidden not in body, f"ff_ota_is_handling() must not call {forbidden}"
    assert re.search(r"\bs_running\s*=(?!=)", body) is None, "the predicate never assigns s_running"


def test_the_in_flight_answer_comes_first_and_does_not_fall_through() -> None:
    body = _predicate()
    in_flight = body.index("if (s_running)")
    assert in_flight < body.index("esp_ota_get_boot_partition(")
    assert in_flight < body.index("ff_txn_load(")
    # A running update for another id answers false right there: between finish() and
    # `done:` the record and the boot pointer belong to the RUNNING update.
    branch = body[in_flight : body.index("}", in_flight)]
    assert "return strcmp(cmd_id, s_running_cmd_id) == 0;" in branch


def test_the_running_id_has_one_writer_before_the_flag_goes_up() -> None:
    source = _code(FF_OTA_C)
    writes = [
        match.start()
        for match in re.finditer(
            r"strlcpy\(\s*s_running_cmd_id|memcpy\(\s*s_running_cmd_id"
            r"|s_running_cmd_id\s*\[[^\]]*\]\s*=(?!=)",
            source,
        )
    ]
    assert len(writes) == 1, f"s_running_cmd_id must have exactly one writer, found {len(writes)}"

    start = _function_body(source, "ff_ota_start")
    write = re.search(r"strlcpy\(\s*s_running_cmd_id", start)
    assert write is not None, "the one write must be in ff_ota_start()"
    assert write.start() < start.index("s_running = true")

    assert "s_running_cmd_id" not in _function_body(source, "ota_task")


def test_ff_ota_start_still_refuses_while_running() -> None:
    start = _function_body(_code(FF_OTA_C), "ff_ota_start")
    guard = start.index("if (s_running)")
    assert "return ESP_ERR_INVALID_STATE;" in start[guard : start.index("}", guard)]
