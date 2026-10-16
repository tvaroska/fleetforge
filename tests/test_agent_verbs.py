"""The four verbs (R5-fw-4): `apply`, `cancel`, `rollback` and `reboot`, answered on `up/ack`.

No host test can execute the firmware; the QEMU run is the proof (DECISIONS.md, R5-fw-4).
What is held here, on every `just test`, are the shapes that make it true. Each one fails
silently on a board: a wrong answer the server files, a reboot loop through redelivery, a
rollback into an image that never booted, or a cancel that leaves a stale boot pointer.

* the wire: `up/ack` beside the other topics; the body keys in R5-spec-1 A5's order with
  explicit nulls; QoS 1 and NEVER retained; never on `up/status`; the handler never calls the
  immediate publisher; every ack detail an agent constant of at most 63 printable bytes;
* dispatch: `on_command` routes the four verbs, keeps `stage` as it was, answers every other
  id'd type `refused` / `not implemented`;
* D8: nothing in the handler path reboots; one static task does, after a drain delay; the id
  is persisted before the task starts and loaded before the first connect; a command with that
  id is a duplicate;
* refusal before change: in every verb, each `refused` answer precedes the first status,
  record write, mark-invalid or reboot of its accepted path;
* rollback: only to a VALID/UNDEFINED other slot that IDF also accepts; the confirming case
  claims the R5-fw-2 latch before it writes; the own-transaction case records, marks, and
  reverts the record if the mark fails; the returned-to image composes the command head;
* ff_ota: the phase query is read-only; the five cancel checkpoints sit where the plan puts
  them; the staged cancel puts the boot pointer back BEFORE it clears the record;
* the capabilities and the reboot-id store.

Comments are stripped before any grep (`_code`), the idiom of the other agent tripwires.
"""

import re

from tests.agent_src import COMPONENT_SRC
from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, FF_TXN_C, FF_TXN_H, _function_body
from tests.test_ff_cfg import _code

FF_OTA_H = COMPONENT_SRC / "ff_ota.h"
FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_MQTT_INTERNAL_H = COMPONENT_SRC / "ff_mqtt_internal.h"

VERBS = ("on_apply", "on_cancel", "on_rollback", "on_reboot")
HANDLER_PATH = ("on_command", *VERBS, "schedule_command_reboot")
# What an accepted path changes: the first of these in a verb's body must follow every
# `refused` answer (A5: `refused` means nothing changed).
CHANGES = (
    "schedule_command_reboot(",
    "enqueue_status(",
    "ff_txn_save(",
    "open_rollback_txn(",
    "store_rollback_head(",
    "esp_ota_mark_app_invalid_rollback(",
    "ff_ota_cancel(",
)


def _mqtt() -> str:
    return _code(FF_MQTT_C)


def _ota() -> str:
    return _code(FF_OTA_C)


def _body(name: str) -> str:
    return _function_body(_mqtt(), name)


def _defines(prefix: str) -> dict[str, str]:
    return dict(re.findall(rf'^#define ({prefix}[A-Z_]*) "([^"]*)"$', _mqtt(), re.M))


class TestTheWire:
    def test_the_ack_topic_is_built_beside_the_others(self) -> None:
        run = _body("ff_mqtt_run")
        assert 'TOPIC_ROOT "/%s/up/ack"' in run
        assert "char topic_ack[TOPIC_MAX];" in _mqtt()

    def test_the_body_keys_in_order_with_explicit_nulls(self) -> None:
        body = _body("enqueue_ack")
        keys = re.findall(r'cJSON_Add\w+ToObject\(root, "(\w+)"', body)
        # Each optional key appears twice (value or null); the first sightings give the order.
        order = list(dict.fromkeys(keys))
        assert order == ["id", "type", "result", "cmd_id", "detail"]
        for key in ("type", "cmd_id", "detail"):
            assert f'cJSON_AddNullToObject(root, "{key}")' in body, key

    def test_queued_qos1_never_retained(self) -> None:
        body = _body("enqueue_ack")
        call = re.search(r"esp_mqtt_client_enqueue\(([^;]*)\);", body)
        assert call is not None
        args = [arg.strip() for arg in call.group(1).split(",")]
        assert args == ["s_ctx.client", "s_ctx.topic_ack", "payload", "0", "QOS", "0", "true"]
        assert "esp_mqtt_client_publish" not in body

    def test_the_ack_topic_is_never_up_status(self) -> None:
        code = _mqtt()
        # Built in ff_mqtt_run, used in enqueue_ack, nowhere else.
        rest = code.replace(_body("ff_mqtt_run"), "").replace(_body("enqueue_ack"), "")
        assert "topic_ack" not in rest.replace("char topic_ack[TOPIC_MAX];", "")
        assert _body("enqueue_ack").count("s_ctx.topic_ack") == 2, "enqueued on, and logged"
        for name in ("enqueue_status", "ff_mqtt_publish_status", "build_status_payload"):
            assert "topic_ack" not in _body(name), name
        assert "topic_status" not in _body("enqueue_ack")

    def test_the_handler_never_publishes_immediately(self) -> None:
        for name in (*HANDLER_PATH, "enqueue_ack", "open_rollback_txn"):
            assert "ff_mqtt_publish_status" not in _body(name), name

    def test_the_three_spec_refusals_verbatim(self) -> None:
        acks = _defines("ACK_")
        assert acks["ACK_NOTHING_STAGED"] == "nothing staged for this cmd_id"
        assert acks["ACK_NO_IMAGE"] == "no confirmed image to return to"
        assert acks["ACK_CANCEL_FIRST"] == "cancel first"
        assert acks["ACK_NOT_IMPLEMENTED"] == "not implemented"
        assert (acks["ACK_ACCEPTED"], acks["ACK_DEFERRED"], acks["ACK_REFUSED"]) == (
            "accepted",
            "deferred",
            "refused",
        )

    def test_every_ack_detail_fits_and_is_printable(self) -> None:
        texts = {**_defines("ACK_"), **_defines("DETAIL_ROLLED_BACK_BY_COMMAND")}
        assert len(texts) >= 19
        for name, text in texts.items():
            assert 0 < len(text.encode()) <= 63, name
            assert all(0x20 <= ord(c) < 0x7F for c in text), name
            assert "http" not in text, name
            # Every detail has a compile-time check too (the three results are not details).
            if name not in ("ACK_ACCEPTED", "ACK_DEFERRED", "ACK_REFUSED"):
                assert f"ACK_FITS({name}" in _mqtt(), name

    def test_the_command_head_takes_the_returned_tail(self) -> None:
        head = _defines("DETAIL_ROLLED_BACK_BY_COMMAND")["DETAIL_ROLLED_BACK_BY_COMMAND"]
        assert head == "rolled back by command"
        assert ";" not in head, "classify_txn appends `; returned to <slot>`"
        assert not head.startswith("selftest "), "not a self-test outcome (R5-be-1)"
        assert len(head + "; returned to ota_0") <= 63

    def test_cancelled_is_declared_and_reported_by_both_owners(self) -> None:
        assert '#define FF_STATUS_CANCELLED "cancelled"' in _code(FF_MQTT_INTERNAL_H)
        assert "FF_STATUS_CANCELLED" in _function_body(_ota(), "report_cancelled")
        assert "enqueue_status(cmd_id, FF_STATUS_CANCELLED" in _body("on_cancel")


class TestDispatch:
    def test_the_four_verbs_are_routed(self) -> None:
        body = _body("on_command")
        for verb, call in (
            ("apply", "on_apply(root, id);"),
            ("cancel", "on_cancel(root, id);"),
            ("rollback", "on_rollback(id);"),
            ("reboot", "on_reboot(id);"),
        ):
            branch = body.index(f'strcmp(type, "{verb}") == 0')
            assert branch < body.index(call), verb

    def test_stage_is_unchanged_and_first(self) -> None:
        body = _body("on_command")
        stage = body.index('strcmp(type, "stage") == 0')
        assert stage < body.index('strcmp(type, "apply") == 0')
        assert "on_stage(root, id);" in body

    def test_every_other_type_is_refused_not_implemented(self) -> None:
        body = _body("on_command")
        tail = body[body.index('strcmp(type, "reboot") == 0') :]
        assert "enqueue_ack(id, type_echo, ACK_REFUSED, NULL, ACK_NOT_IMPLEMENTED);" in tail
        assert "is not implemented by this agent" not in body
        assert "enqueue_ack(id, type_echo, ACK_REFUSED, NULL, ACK_ID_TOO_LONG);" in body
        assert body.index("strlen(id) >= FF_TXN_MAX_CMD_ID") < body.index(
            'strcmp(type, "apply") == 0'
        )


class TestNeverRebootInsideDelivery:
    """D8: esp-mqtt sends the PUBACK only after the handler returns."""

    def test_nothing_on_the_handler_path_reboots(self) -> None:
        for name in HANDLER_PATH:
            body = _body(name)
            assert "esp_restart(" not in body, name
            assert "esp_ota_mark_app_invalid_rollback_and_reboot(" not in body, name

    def test_one_static_task_reboots_after_the_drain(self) -> None:
        task = _body("command_reboot_task")
        drain = task.index("vTaskDelay(pdMS_TO_TICKS(COMMAND_REBOOT_DRAIN_MS))")
        log = task.index('"rebooting for command %s"')
        assert drain < log < task.index("esp_restart()")
        assert re.search(r"^#define COMMAND_REBOOT_DRAIN_MS 1500$", _mqtt(), re.M)
        schedule = _body("schedule_command_reboot")
        assert "xTaskCreateStatic(command_reboot_task" in schedule
        assert re.search(r"\bxTaskCreate\(", schedule) is None
        assert "static StaticTask_t s_reboot_tcb;" in _mqtt()
        assert "static StackType_t s_reboot_stack[COMMAND_REBOOT_STACK];" in _mqtt()
        # Only the reboot task and the confirm/rollback timer paths ever reboot in ff_mqtt.c.
        assert _mqtt().count("esp_restart(") == 1

    def test_the_id_is_persisted_before_the_task_starts(self) -> None:
        schedule = _body("schedule_command_reboot")
        note = schedule.index("ff_txn_note_reboot_cmd(id)")
        copy = schedule.index("strlcpy(s_reboot_cmd_id, id")
        assert note < copy < schedule.index("xTaskCreateStatic(")
        assert schedule.index("if (s_cmd_reboot_pending)") < note

    def test_the_id_is_loaded_before_the_first_connect(self) -> None:
        run = _body("ff_mqtt_run")
        load = run.index("ff_txn_last_reboot_cmd(s_reboot_cmd_id, sizeof(s_reboot_cmd_id))")
        assert run.index("ff_txn_init();") < load < run.index("classify_txn();")
        assert load < run.index("esp_mqtt_client_start(")
        # Outside s_ctx, which ff_mqtt_run() memsets.
        assert "static char s_reboot_cmd_id[FF_TXN_MAX_CMD_ID];" in _mqtt()

    def test_a_command_with_that_id_is_a_duplicate(self) -> None:
        body = _body("on_command")
        persisted = body.index("strcmp(id, s_reboot_cmd_id) == 0")
        assert persisted < body.index('strcmp(type, "stage") == 0')
        tail = body[persisted : body.index("return;", persisted)]
        assert "this board already rebooted for it" in tail
        assert "enqueue_ack" not in tail

    def test_every_reboot_verb_schedules_last(self) -> None:
        for name in ("on_apply", "on_rollback", "on_reboot"):
            body = _body(name)
            assert body.count("schedule_command_reboot(id);") == 1, name
            tail = body[body.index("schedule_command_reboot(id);") :]
            assert tail.strip() == "schedule_command_reboot(id);", name
        assert "schedule_command_reboot(" not in _body("on_cancel")


class TestRefusalBeforeChange:
    def test_every_refusal_precedes_the_first_change(self) -> None:
        for name in VERBS:
            body = _body(name)
            changes = [body.index(c) for c in CHANGES if c in body]
            assert changes, name
            first_change = min(changes)
            refusals = [m.start() for m in re.finditer(r"ACK_REFUSED", body)]
            assert refusals, name
            for refusal in refusals:
                text = body[refusal : body.index(";", refusal)]
                if name == "on_rollback" and "ACK_CANNOT_MARK" in text:
                    continue  # the mark failed after the record: reverted first (below)
                if name == "on_cancel" and refusal > body.index("ff_ota_cancel("):
                    continue  # ff_ota_cancel's own refusals: it changed nothing
                assert refusal < first_change, (name, text)

    def test_cancel_reports_only_after_ff_ota_accepted(self) -> None:
        body = _body("on_cancel")
        cancel = body.index("ff_ota_cancel(cmd_id, &reported)")
        accepted = body.index('enqueue_ack(id, "cancel", ACK_ACCEPTED, cmd_id, NULL);')
        for refusal in ("ESP_ERR_NOT_FOUND", "ESP_ERR_INVALID_STATE", "ACK_CANNOT_PUT_BACK"):
            assert cancel < body.index(refusal, cancel) < accepted, refusal
        assert accepted < body.index("if (!reported)") < body.index("FF_STATUS_CANCELLED")

    def test_apply_walks_like_auto_after_its_ack(self) -> None:
        body = _body("on_apply")
        accepted = body.rindex('enqueue_ack(id, "apply", ACK_ACCEPTED, cmd_id, NULL);')
        applying = body.index("enqueue_status(cmd_id, FF_STATUS_APPLYING, 100, NULL)")
        rebooting = body.index("enqueue_status(cmd_id, FF_STATUS_REBOOTING, 100, NULL)")
        assert accepted < applying < rebooting < body.index("schedule_command_reboot(id);")
        assert "ff_ota_txn_phase(cmd_id," in body
        assert "phase != FF_OTA_TXN_STAGED" in body
        assert "ACK_NOTHING_STAGED" in body[body.index("phase != FF_OTA_TXN_STAGED") :]


class TestRollback:
    def test_only_a_valid_or_undefined_slot_is_returnable(self) -> None:
        body = _body("other_slot_is_returnable")
        assert "esp_ota_get_state_partition(other, &state)" in body
        accepted = set(re.findall(r"state != (ESP_OTA_IMG_\w+)", body))
        assert accepted == {"ESP_OTA_IMG_VALID", "ESP_OTA_IMG_UNDEFINED"}
        assert "state ==" not in body
        assert "ESP_OTA_IMG_NEW" not in body, "IDF itself accepts NEW; this agent must not"
        assert body.index("esp_ota_get_state_partition(") < body.index(
            "esp_ota_check_rollback_is_possible()"
        )
        assert "other == running" in body

    def test_a_returnable_slot_is_checked_before_anything_changes(self) -> None:
        body = _body("on_rollback")
        check = body.index("other_slot_is_returnable()")
        assert body.index("ff_ota_txn_phase(NULL,") < check
        for change in ("store_rollback_head(", "open_rollback_txn(", "s_rollback_decided = true"):
            assert check < body.index(change), change

    def test_the_confirming_case_claims_the_latch_first(self) -> None:
        body = _body("on_rollback")
        enter = body.index("portENTER_CRITICAL(&s_decide_lock)")
        claim = body.index("s_rollback_decided = true;")
        leave = body.index("portEXIT_CRITICAL(&s_decide_lock)")
        assert enter < body.index("s_confirm_claimed") < claim < leave
        assert leave < body.index("store_rollback_head(s_txn.cmd_id)")
        assert leave < body.index("esp_ota_mark_app_invalid_rollback()")
        assert "ACK_CONFIRMING_NOW" in body[leave : body.index("store_rollback_head(")]
        # The pinned first-occurrence literals of R5-fw-2 are not spelled here.
        assert "if (s_rollback_decided)" not in body
        assert (
            "if (s_rollback_decided)" not in _mqtt()[: _mqtt().index("case MQTT_EVENT_PUBLISHED")]
        )

    def test_the_own_transaction_records_marks_and_reverts(self) -> None:
        opener = _body("open_rollback_txn")
        save = opener.index("ff_txn_save(id, running->address, 0)")
        assert save < opener.index("store_rollback_head(id);")
        body = _body("on_rollback")
        own = body.index("const bool recorded = open_rollback_txn(id);")
        mark = body.index("esp_ota_mark_app_invalid_rollback()", own)
        revert = body.index("ff_txn_clear_if(id)", mark)
        refuse = body.index("ACK_CANNOT_MARK", mark)
        assert own < mark < revert < refuse
        accepted = body.rindex('enqueue_ack(id, "rollback", ACK_ACCEPTED, txn, NULL);')
        rolling = body.index("enqueue_status(txn, FF_STATUS_ROLLING_BACK")
        assert refuse < accepted < rolling < body.index("schedule_command_reboot(id);")

    def test_one_place_stores_the_command_head(self) -> None:
        assert "ff_txn_set_detail(txn_id, DETAIL_ROLLED_BACK_BY_COMMAND)" in _body(
            "store_rollback_head"
        )
        for name in (*VERBS, "open_rollback_txn"):
            assert "ff_txn_set_detail(" not in _body(name), name

    def test_the_returned_to_image_composes_the_command_head(self) -> None:
        body = _body("classify_txn")
        rolled = body.index("s_txn.kind = TXN_ROLLED_BACK;")
        command = body.index("strcmp(rec.detail, DETAIL_ROLLED_BACK_BY_COMMAND) == 0", rolled)
        assert command < body.index("compose_returned_detail(", rolled)
        valid = body.index("state == ESP_OTA_IMG_VALID")
        assert "DETAIL_ROLLED_BACK_BY_COMMAND" not in body[valid:rolled]

    def test_reboot_is_refused_while_unconfirmed_or_mid_update(self) -> None:
        body = _body("on_reboot")
        assert "ACK_UPDATE_IN_PROGRESS" in body and "ACK_NOT_CONFIRMED" in body
        assert (
            "pending_verify() || s_txn.kind == TXN_CONFIRMING || s_txn.kind == TXN_BOOTED_NEW"
            in (re.sub(r"\s+", " ", body))
        )
        accepted = body.rindex('enqueue_ack(id, "reboot", ACK_ACCEPTED, NULL, NULL);')
        assert body.index("ACK_NOT_CONFIRMED") < accepted
        assert "enqueue_status(" not in body, "a reboot opens no transaction"


class TestFfOta:
    def test_the_public_api(self) -> None:
        header = _code(FF_OTA_H)
        assert (
            "ff_ota_txn_t ff_ota_txn_phase(const char *cmd_id, char *held, size_t held_len);"
            in (header)
        )
        assert "esp_err_t ff_ota_cancel(const char *cmd_id, bool *reported);" in header
        for name in ("FF_OTA_TXN_NONE = 0", "FF_OTA_TXN_TRANSFER", "FF_OTA_TXN_STAGED"):
            assert name in header
        assert "FF_OTA_TXN_APPLYING" in header

    def test_the_phase_query_is_read_only(self) -> None:
        for name in ("ff_ota_txn_phase", "staged_phase"):
            body = _function_body(_ota(), name)
            for forbidden in (
                "fail(",
                "ff_mqtt_publish_status",
                "report_cancelled",
                "ff_txn_save",
                "ff_txn_clear_if",
                "esp_ota_set_boot_partition",
                "restore_boot_partition",
                "cancel_staged",
                "esp_restart",
                "s_cancel = ",
                "s_phase = ",
                "s_running = ",
            ):
                assert forbidden not in body, (name, forbidden)

    def test_the_checkpoints_in_order(self) -> None:
        task = _function_body(_ota(), "ota_task")
        resolve = task.index("resolve_artifact_url(")
        begin = task.index("esp_https_ota_begin(")
        first = task.index("cancel_requested()", resolve)
        assert resolve < first < begin
        loop = task.index("while ((err = esp_https_ota_perform(handle))")
        in_loop = task.index("cancel_requested()", loop)
        assert in_loop < task.index("int so_far", loop), "at the top of every pass"
        assert task.index("if (cancel_seen)") < task.index("if (stalled)")
        seen = task[
            task.index("if (cancel_seen)") : task.index("goto done", task.index("if (cancel_seen)"))
        ]
        assert "esp_https_ota_abort(handle)" in seen and "report_cancelled(cmd)" in seen

    def test_a_cancel_before_finish_aborts(self) -> None:
        task = _function_body(_ota(), "ota_task")
        verified = task.index("strcmp(digest, cmd->sha256)")
        finish = task.index("esp_https_ota_finish(")
        window = task[verified:finish]
        check = window.index("if (cancel_requested())")
        branch = window[check : window.index("goto done", check)]
        assert "esp_https_ota_abort(handle)" in branch
        assert "report_cancelled(cmd)" in branch

    def test_a_cancel_after_finish_puts_the_pointer_back_and_never_records(self) -> None:
        task = _function_body(_ota(), "ota_task")
        finish = task.index("esp_https_ota_finish(")
        save = task.index("ff_txn_save(")
        window = task[finish:save]
        enter = window.index("portENTER_CRITICAL(&s_cancel_lock)")
        assert (
            enter
            < window.index("cancel_now = s_cancel;")
            < window.index("s_phase = cmd->apply_now ? FF_OTA_TXN_APPLYING : FF_OTA_TXN_STAGED;")
        )
        cancel = window.index("if (cancel_now)")
        branch = window[cancel : window.index("goto done", cancel)]
        assert "restore_boot_partition(WHY_CANCELLED)" in branch
        assert "report_cancelled(cmd)" in branch
        assert "ff_txn_" not in branch

    def test_the_staged_exit_acts_under_the_lock(self) -> None:
        task = _function_body(_ota(), "ota_task")
        done = task[task.index("done:") :]
        enter = done.index("portENTER_CRITICAL(&s_cancel_lock)")
        decide = done.index("s_cancel && s_phase == FF_OTA_TXN_STAGED")
        free_slot = done.index("s_running = false;")
        leave = done.index("portEXIT_CRITICAL(&s_cancel_lock)")
        assert enter < decide < free_slot < leave, "decided and freed in one critical section"
        assert (
            leave < done.index("cancel_staged(cmd->cmd_id)") < done.index("report_cancelled(cmd)")
        )

    def test_the_staged_cancel_puts_back_before_it_clears(self) -> None:
        body = _function_body(_ota(), "cancel_staged")
        put_back = body.index("restore_boot_partition(WHY_CANCELLED)")
        clear = body.index("ff_txn_clear_if(cmd_id)")
        assert put_back < clear
        guard = body[put_back:clear]
        assert "return false;" in guard, "the record is cleared only if the put-back worked"
        for forbidden in ("esp_partition_erase", "esp_ota_erase", "nvs_erase"):
            assert forbidden not in body

    def test_cancel_decides_under_the_lock(self) -> None:
        body = _function_body(_ota(), "ff_ota_cancel")
        enter = body.index("portENTER_CRITICAL(&s_cancel_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_cancel_lock)")
        refuse = body.index("s_phase == FF_OTA_TXN_APPLYING")
        accept = body.index("s_cancel = true;")
        assert enter < refuse < accept < leave
        assert body.index("*reported = true;") < leave
        assert leave < body.index("staged_phase(cmd_id, NULL, 0)") < body.index("cancel_staged(")

    def test_the_undo_still_has_one_boot_switch(self) -> None:
        ota = _ota()
        assert ota.count("esp_ota_set_boot_partition(") == 1
        assert "static bool restore_boot_partition(const char *why)" in ota
        assert ota.count("restore_boot_partition(WHY_REJECTED)") == 1
        assert '"boot partition put back to %s: %s"' in ota

    def test_a_fresh_transaction_resets_the_handshake(self) -> None:
        start = _function_body(_ota(), "ff_ota_start")
        enter = start.index("portENTER_CRITICAL(&s_cancel_lock)")
        assert start.index("strlcpy(s_running_cmd_id") < enter
        assert enter < start.index("s_cancel = false;") < start.index("s_running = true")


class TestCapabilities:
    def test_the_four_verbs_follow_ota_unguarded(self) -> None:
        identity = _code(FF_IDENTITY_C)
        ota = identity.index('cJSON_CreateString("ota")')
        selftest = identity.index("if (s_selftest_registered)")
        positions = []
        for verb in ("apply", "cancel", "rollback", "reboot"):
            assert identity.count(f'cJSON_CreateString("{verb}")') == 1, verb
            positions.append(identity.index(f'cJSON_CreateString("{verb}")'))
        assert ota < positions[0] < positions[1] < positions[2] < positions[3] < selftest
        between = identity[ota:selftest]
        assert "if (" not in between, "unconditional: every image of this agent does them"
        assert '"safe_window"' not in identity, "R5-fw-5"


class TestTheRebootIdStore:
    def test_its_own_namespace_and_short_key(self) -> None:
        header = _code(FF_TXN_H)
        assert '#define FF_TXN_CMD_NAMESPACE "ff_cmd"' in header
        assert '#define FF_TXN_KEY_REBOOT_CMD "reboot_id"' in header
        assert "esp_err_t ff_txn_note_reboot_cmd(const char *id);" in header
        assert "esp_err_t ff_txn_last_reboot_cmd(char *out, size_t len);" in header

    def test_never_erased_and_locked(self) -> None:
        txn = _code(FF_TXN_C)
        for name, mode in (
            ("ff_txn_note_reboot_cmd", "NVS_READWRITE"),
            ("ff_txn_last_reboot_cmd", "NVS_READONLY"),
        ):
            body = _function_body(txn, name)
            assert f"nvs_open(FF_TXN_CMD_NAMESPACE, {mode}" in body, name
            assert "nvs_erase" not in body and "erase_locked" not in body, name
            assert body.index("lock()") < body.index("nvs_open("), name
        note = _function_body(txn, "ff_txn_note_reboot_cmd")
        assert "strlen(id) >= FF_TXN_MAX_CMD_ID" in note
        assert note.index("nvs_set_str(handle, FF_TXN_KEY_REBOOT_CMD, id)") < note.index(
            "nvs_commit(handle)"
        )
        # The stage's record erase never reaches this namespace.
        assert "FF_TXN_CMD_NAMESPACE" not in _function_body(txn, "ff_txn_save")
