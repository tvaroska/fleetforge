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
from typing import Any

from tests.agent_src import COMPONENT_SRC, c_call_sites_in_family, read_c_family
from tests.agent_test_helpers import _code, _function_body

FF_OTA_H = COMPONENT_SRC / "ff_ota.h"
PREDICATE_CALL = "if (ff_ota_is_handling("


def _on_stage() -> str:
    return _function_body(_code(read_c_family("ff_mqtt")), "on_stage")


def _predicate() -> str:
    return _function_body(_code(read_c_family("ff_ota")), "ff_ota_is_handling")


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
    source = _code(read_c_family("ff_ota"))
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
    start = _function_body(_code(read_c_family("ff_ota")), "ff_ota_start")
    guard = start.index("if (s_running)")
    assert "return ESP_ERR_INVALID_STATE;" in start[guard : start.index("}", guard)]


async def test_stage_id_survives_its_own_reboot(
    monkeypatch: Any,
) -> None:
    """D-6: `ota_task` persists `cmd->cmd_id` via `ff_txn_note_reboot_cmd` before `esp_restart()`,
    `on_stage` drops any `stage` matching `held_txn_id()`, and the simulator deduplicates a
    post-reboot `stage` redelivery after clearing RAM `seen`."""
    import asyncio
    import hashlib
    import json

    from fleetforge.simulator.device import (
        CONFIRM_NEVER,
        LINK_FAST,
        DeviceIdentity,
        LinkProfile,
        run_always_on,
    )
    from tests.doubles import (
        FIRMWARE,
        FakeClient,
        FakeHttpResponse,
        FakeMessage,
        credential_for,
        drive_until,
        stage_command,
        statuses,
    )

    assert "ota_task" in c_call_sites_in_family("ff_ota", "ff_txn_note_reboot_cmd")
    task_body = _function_body(_code(read_c_family("ff_ota")), "ota_task")
    assert task_body.index("ff_txn_note_reboot_cmd(cmd->cmd_id)") < task_body.index("esp_restart()")

    stage_body = _on_stage()
    held_guard = stage_body.index("held_txn_id() != NULL && strcmp(held_txn_id(), cmd.cmd_id) == 0")
    assert held_guard < stage_body.index("ff_mqtt_publish_status(")
    assert held_guard < stage_body.index("ff_ota_start(")

    fetched: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> FakeHttpResponse:
        _ = timeout
        fetched.append(url)
        return FakeHttpResponse(FIRMWARE)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("fleetforge.simulator.device.REBOOT_DELAY_S", 0.0)

    identity = DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2", capabilities=("ota",))
    cmd_bytes = stage_command(cmd_id="cmd-stage-reboot-1", version="1.6.0")
    other_cmd = json.dumps({"proto": 1, "id": "cmd-ping-2", "type": "identify"}).encode()
    clients: list[FakeClient] = []

    def factory() -> Any:
        fake = FakeClient()
        topic = f"ff/v1/d/{identity.device_id}/dn/cmd"
        if not clients:
            fake.inbox.put_nowait(FakeMessage(topic, cmd_bytes))
        elif len(clients) == 1:
            # Redeliver `cmd-stage-reboot-1` on the post-reboot session (RAM `seen` was cleared),
            # and again after another command overwrites RAM's 1-slot `last_command_id` (`held_txn_id` catches it!).
            fake.inbox.put_nowait(FakeMessage(topic, cmd_bytes))
            fake.inbox.put_nowait(FakeMessage(topic, other_cmd))
            fake.inbox.put_nowait(FakeMessage(topic, cmd_bytes))
        clients.append(fake)
        return fake

    stop = asyncio.Event()
    await drive_until(
        run_always_on(
            identity,
            credential_for(identity.device_id),
            client_factory=factory,
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=0.01,
            confirm=CONFIRM_NEVER,
            confirm_timeout_s=30.0,
            stop=stop,
        ),
        lambda: len(clients) >= 2 and len(statuses(clients[1])) >= 1,
        stop,
    )
    await asyncio.sleep(0.05)
    assert len(fetched) == 1, f"expected 1 download across reboot redelivery, got {len(fetched)}"
    post_reboot_states = [s["state"] for s in statuses(clients[1])]
    assert "failed" not in post_reboot_states, post_reboot_states
    assert hashlib.sha256(FIRMWARE).hexdigest() is not None
