"""The safe window (R5-fw-5): the firmware's rule gates every agent reboot.

`ff_set_safe_window_cb()` / `Fleetforge.onSafeWindow()` register a rule that says whether a
reboot right now is safe. While it says unsafe an apply reports `awaiting_safe_window` and a
decided rollback waits; a safe answer releases both (R5-spec-1 D7, D12). No host test can run
the firmware; the QEMU run is the proof (DECISIONS.md, R5-fw-5). What is held here, on every
`just test`, are the shapes that make it true:

* the maker header: one function, the rule's typedef, `extern "C"`, no private hook public;
* the registration: refusals (NULL, a second rule, after the freeze), the freeze logs once,
  the rule is called outside the lock and only from `ff_safe_window_is_safe()`, which says
  safe with nothing registered; neither hook logs;
* the poll period is at most a second and every wait loop sleeps it;
* the esp_timer side never calls the rule; the deadline, the backstop and a failed self-test
  hand the reboot to a static gate task, which waits, reports, re-checks and only then marks
  the image invalid and reboots, and never touches the confirm timer (D10);
* the command reboot task: created once, woken by notification, drain < window < log <
  `esp_restart()`, commits only under its lock without hold or abort;
* the verbs: the unsafe branch answers `deferred` and reports the right wait, never
  `applying`/`rolling_back`; `reboot` never reports a status; `cancel` holds the task across
  its one `ff_ota_cancel()` call and aborts it only on success;
* ff_ota: apply:auto waits in AWAITING (cancellable), checkpoint 6 commits `applying` under
  the lock, and the rule is asked again before `esp_restart()`;
* `on_stage` refuses while a reboot is on its way; the capability; the Arduino member; both
  examples register the rule third and the rule only reads a flag.

Comments are stripped before any grep (`_code`), the idiom of the other agent tripwires.
"""

import re

from tests.agent_src import (
    AGENT_MAIN_C,
    ARDUINO_WRAPPER_CPP,
    ARDUINO_WRAPPER_H,
    COMPONENT_CMAKE,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    EXAMPLE_INO,
    FF_SAFE_WINDOW_C,
    FF_SAFE_WINDOW_H,
    FF_SAFE_WINDOW_INTERNAL_H,
    IDF_EXAMPLE_MAIN_C,
)
from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, _function_body
from tests.test_arduino_library import _ff_calls
from tests.test_ff_cfg import _code
from tests.test_ota_component import _declared_functions
from tests.test_selftest import _member_body

FF_MQTT_INTERNAL_H = COMPONENT_SRC / "ff_mqtt_internal.h"
FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_OTA_H = COMPONENT_SRC / "ff_ota.h"

PRIVATE_HOOKS = ("ff_safe_window_freeze", "ff_safe_window_registered", "ff_safe_window_is_safe")
POLL = "vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));"


def _src() -> str:
    return _code(FF_SAFE_WINDOW_C)


def _mqtt() -> str:
    return _code(FF_MQTT_C)


def _ota() -> str:
    return _code(FF_OTA_C)


def _body(name: str) -> str:
    return _function_body(_mqtt(), name)


def _wait_loops(code: str) -> list[str]:
    """Every `while (...ff_safe_window_is_safe()...) { ... }` loop body."""
    return re.findall(r"while \([^{]*ff_safe_window_is_safe\(\)\) \{([^}]*)\}", code)


class TestTheMakerHeader:
    def test_one_function_and_the_rule_typedef(self) -> None:
        assert _declared_functions(FF_SAFE_WINDOW_H) == {"ff_set_safe_window_cb"}
        header = _code(FF_SAFE_WINDOW_H)
        assert "typedef bool (*ff_safe_window_fn)(void *ctx);" in header
        assert "esp_err_t ff_set_safe_window_cb(ff_safe_window_fn fn, void *ctx);" in header

    def test_extern_c_and_minimal_includes(self) -> None:
        header = _code(FF_SAFE_WINDOW_H)
        assert 'extern "C" {' in header
        assert re.findall(r"^#include (\S+)", header, re.M) == ["<stdbool.h>", '"esp_err.h"']

    def test_the_contract_is_written_down(self) -> None:
        """What a maker needs to know is in the header itself, not only in a doc."""
        text = FF_SAFE_WINDOW_H.read_text()
        for phrase in (
            "ADDITIVE-ONLY",
            "Never from an ISR, never",
            "from the esp_timer task",
            "confirm deadline",
            "keeps counting",
            "awaiting_safe_window",
            "deferred",
            "ESP_ERR_INVALID_STATE",
        ):
            assert phrase in text, phrase

    def test_the_hooks_are_private(self) -> None:
        assert FF_SAFE_WINDOW_INTERNAL_H.parent == COMPONENT_SRC
        assert _declared_functions(FF_SAFE_WINDOW_INTERNAL_H) == set(PRIVATE_HOOKS)
        for header in sorted(COMPONENT_INCLUDE.glob("*.h")):
            for hook in PRIVATE_HOOKS:
                assert hook not in _code(header), (header.name, hook)
        for public in (ARDUINO_WRAPPER_H, ARDUINO_WRAPPER_CPP):
            for hook in PRIVATE_HOOKS:
                assert hook not in _code(public), (public.name, hook)

    def test_the_component_compiles_it(self) -> None:
        assert '"src/ff_safe_window.c"' in COMPONENT_CMAKE.read_text()
        assert 'static const char *TAG = "ff-safe";' in _src()

    def test_the_stock_agent_never_registers(self) -> None:
        assert "ff_safe_window" not in _code(AGENT_MAIN_C)
        assert "ff_set_safe_window_cb" not in _code(AGENT_MAIN_C)


class TestTheRegistration:
    def test_refused_when_null_taken_or_frozen(self) -> None:
        body = _function_body(_src(), "ff_set_safe_window_cb")
        assert body.index("if (fn == NULL)") < body.index("return ESP_ERR_INVALID_ARG;")
        assert "frozen = s_frozen;" in body and "taken = s_fn != NULL;" in body
        assert "if (!frozen && !taken)" in body
        assert body.count("return ESP_ERR_INVALID_STATE;") == 2
        # Logged outside the lock, never inside it.
        assert body.index("portEXIT_CRITICAL(&s_lock)") < body.index("ESP_LOGE")

    def test_the_freeze_logs_once(self) -> None:
        body = _function_body(_src(), "ff_safe_window_freeze")
        assert "s_frozen = true;" in body
        assert "static bool s_logged;" in body
        assert "const bool log_now = !s_logged;" in body
        assert body.index("portEXIT_CRITICAL(&s_lock)") < body.index("if (log_now)")
        source = FF_SAFE_WINDOW_C.read_text()
        assert "a safe-window rule is registered: every reboot this agent makes" in source
        assert "no safe-window rule registered: this agent reboots as soon as it" in source

    def test_the_rule_is_called_once_outside_the_lock(self) -> None:
        src = _src()
        assert len(re.findall(r"\bfn\s*\(\s*ctx\s*\)", src)) == 1
        body = _function_body(src, "ff_safe_window_is_safe")
        leave = body.index("portEXIT_CRITICAL(&s_lock)")
        assert body.index("const ff_safe_window_fn fn = s_fn;") < leave
        assert leave < body.index("return fn(ctx);")
        # Unregistered means safe (R5-spec-1 D12).
        null = body.index("if (fn == NULL)")
        assert null < body.index("return true;") < body.index("return fn(ctx);")

    def test_registered_never_calls_the_rule(self) -> None:
        """The esp_timer task asks this one: it may only read the registration."""
        body = _function_body(_src(), "ff_safe_window_registered")
        assert "s_fn != NULL" in body
        assert not re.search(r"\b(?:s_)?fn\s*\(", body)

    def test_the_hooks_never_log_or_block(self) -> None:
        for name in ("ff_safe_window_registered", "ff_safe_window_is_safe"):
            body = _function_body(_src(), name)
            for forbidden in ("ESP_LOG", "vTaskDelay", "malloc", "printf", "xSemaphoreTake"):
                assert forbidden not in body, (name, forbidden)


class TestThePoll:
    def test_at_most_a_second(self) -> None:
        match = re.search(
            r"^#define FF_SAFE_WINDOW_POLL_MS (\d+)$", _code(FF_SAFE_WINDOW_INTERNAL_H), re.M
        )
        assert match is not None
        assert int(match.group(1)) <= 1000
        assert int(match.group(1)) == 250, "the worked examples' window is 800 ms"
        assert "_Static_assert(FF_SAFE_WINDOW_POLL_MS <= 1000" in _code(FF_SAFE_WINDOW_INTERNAL_H)

    def test_every_wait_loop_sleeps_the_poll(self) -> None:
        mqtt_loops = _wait_loops(_mqtt())
        ota_loops = _wait_loops(_ota())
        # gate (2), the command task's final re-check (1); ff_ota's wait and re-check (2).
        assert len(mqtt_loops) == 3, mqtt_loops
        assert len(ota_loops) == 2, ota_loops
        for loop in (*mqtt_loops, *ota_loops):
            assert loop.strip() == POLL, loop
        assert POLL in _body("wait_for_the_window")


class TestTheTimerSide:
    def test_the_esp_timer_path_never_calls_the_rule(self) -> None:
        for name in (
            "confirm_timeout_cb",
            "rollback_now_cb",
            "ff_mqtt_arm_confirm_timer",
            "start_rollback_gate",
        ):
            assert "ff_safe_window_is_safe" not in _body(name), name

    def test_the_deadline_hands_off_after_its_claim_and_logs(self) -> None:
        body = _body("confirm_timeout_cb")
        leave = body.index("portEXIT_CRITICAL(&s_decide_lock)")
        no_session = body.index('"no working session %d s after an OTA boot')
        no_result = body.index('"no self-test result %d s after an OTA boot')
        tail = body[max(no_session, no_result) :]
        handoff = tail.index("if (ff_safe_window_registered())")
        assert leave < max(no_session, no_result)
        assert handoff < tail.index("start_rollback_gate();") < tail.index("return;")
        assert tail.index("start_rollback_gate();") < tail.index("esp_timer_start_once(")

    def test_the_backstop_hands_off_before_its_immediate_reboot(self) -> None:
        body = _body("confirm_timeout_cb")
        backstop = body[body.index("if (already_decided)") :]
        backstop = backstop[: backstop.index("esp_ota_mark_app_invalid_rollback_and_reboot()")]
        assert "if (ff_safe_window_registered())" in backstop
        assert "start_rollback_gate();" in backstop
        # A `rollback` command's reboot waits in its own task: no second party.
        assert "if (!commanded)" in backstop
        assert "commanded = s_rollback_commanded;" in body

    def test_a_failed_self_test_hands_off_after_its_head(self) -> None:
        body = _body("selftest_done")
        head = body.index("ff_txn_set_detail(s_txn.cmd_id, head);")
        handoff = body.index("if (ff_safe_window_registered())")
        assert head < handoff < body.index("start_rollback_gate();")
        assert body.index("start_rollback_gate();") < body.index("esp_timer_start_once(")

    def test_the_gate_starts_once_from_a_static_task(self) -> None:
        body = _body("start_rollback_gate")
        enter = body.index("portENTER_CRITICAL(&s_decide_lock)")
        claim = body.index("s_gate_started = true;")
        leave = body.index("portEXIT_CRITICAL(&s_decide_lock)")
        assert enter < body.index("first = !s_gate_started;") < claim < leave
        assert leave < body.index("xTaskCreateStatic(rollback_gate_task")
        assert re.search(r"\bxTaskCreate\(", body) is None
        assert "ROLLBACK_GATE_PRIO" in body and "ROLLBACK_GATE_STACK" in body
        assert re.search(r"^#define ROLLBACK_GATE_PRIO 6$", _mqtt(), re.M)
        assert re.search(r"^#define ROLLBACK_GATE_STACK 4096$", _mqtt(), re.M)
        assert "static StackType_t s_gate_stack[ROLLBACK_GATE_STACK];" in _mqtt()
        assert _mqtt().count("xTaskCreateStatic(rollback_gate_task") == 1

    def test_the_gate_waits_reports_rechecks_then_rolls_back(self) -> None:
        body = _body("rollback_gate_task")
        first = body.index("if (!ff_safe_window_is_safe())")
        waiting = body.index("s_rollback_waiting = true;")
        awaiting = body.index("FF_STATUS_AWAITING_SAFE_WINDOW")
        detail = body.index("FF_DETAIL_ROLLBACK_WAITS")
        rolling = body.index("FF_STATUS_ROLLING_BACK")
        grace = body.index("vTaskDelay(pdMS_TO_TICKS(ROLLBACK_REPORT_GRACE_MS))")
        recheck = body.rindex("while (!ff_safe_window_is_safe())")
        reboot = body.index("esp_ota_mark_app_invalid_rollback_and_reboot()")
        assert first < waiting < awaiting < detail < rolling < grace < recheck < reboot
        assert "kind == TXN_CONFIRMING || kind == TXN_BOOTED_NEW" in body
        assert "FF_STATUS_APPLYING" not in body
        assert "the rollback is decided and waits for a safe window" in FF_MQTT_C.read_text()

    def test_the_gate_never_touches_the_confirm_timer(self) -> None:
        """D10: the deadline is never paused, extended or re-armed by the wait."""
        for name in ("rollback_gate_task", "start_rollback_gate"):
            body = _body(name)
            for forbidden in (
                "s_confirm_timer",
                "esp_timer_stop",
                "esp_timer_start_once",
                "esp_timer_restart",
                "s_confirm_deadline_s",
            ):
                assert forbidden not in body, (name, forbidden)

    def test_a_reconnect_repeats_the_wait(self) -> None:
        body = _body("report_txn_on_connect")
        switch_end = body.index("case TXN_NONE:")
        repeat = body.index("if (s_rollback_waiting", switch_end)
        assert repeat < body.index("FF_DETAIL_ROLLBACK_WAITS", repeat)

    def test_without_a_rule_the_timer_path_is_unchanged(self) -> None:
        """The stock agent: every gate hand-off sits behind the registered check."""
        code = _mqtt()
        calls = [m.start() for m in re.finditer(r"start_rollback_gate\(\);", code)]
        guards = [m.start() for m in re.finditer(r"if \(ff_safe_window_registered\(\)\)", code)]
        assert len(calls) == len(guards) == 3, "the deadline, the backstop, the self-test"
        for call, guard in zip(calls, guards, strict=True):
            assert guard < call < guard + 120


class TestTheCommandReboot:
    def test_created_once_and_woken_thereafter(self) -> None:
        schedule = _body("schedule_command_reboot")
        assert "if (s_reboot_task == NULL)" in schedule
        assert schedule.index("xTaskCreateStatic(command_reboot_task") < schedule.index(
            "xTaskNotifyGive(s_reboot_task);"
        )
        assert _mqtt().count("xTaskCreateStatic(command_reboot_task") == 1
        assert "vTaskDelete" not in _body("command_reboot_task")

    def test_drain_window_log_restart(self) -> None:
        task = _body("command_reboot_task")
        notify = task.index("ulTaskNotifyTake(pdTRUE, portMAX_DELAY)")
        drain = task.index("vTaskDelay(pdMS_TO_TICKS(COMMAND_REBOOT_DRAIN_MS))")
        window = task.index("wait_for_the_window()")
        log = task.index('"rebooting for command %s"')
        assert notify < drain < window < log < task.index("esp_restart()")

    def test_commit_only_under_the_lock_without_hold_or_abort(self) -> None:
        body = _body("wait_for_the_window")
        safe = body.index("if (!hold && ff_safe_window_is_safe())")
        enter = body.index("portENTER_CRITICAL(&s_reboot_lock)", safe)
        check = body.index("committed = !s_reboot_hold && !s_reboot_abort;", enter)
        commit = body.index("s_reboot_committed = true;", check)
        leave = body.index("portEXIT_CRITICAL(&s_reboot_lock)", commit)
        assert enter < check < commit < leave
        # The rule is asked outside the lock.
        sections = re.findall(r"portENTER_CRITICAL\(&s_reboot_lock\);(.*?)portEXIT", body, re.S)
        assert sections and all("ff_safe_window_is_safe" not in s for s in sections)
        assert "return false;" in body[:safe], "an abort is seen before the rule is asked"

    def test_a_released_deferred_verb_reports_then_rechecks(self) -> None:
        task = _body("command_reboot_task")
        deferred = task.index("if (s_reboot_deferred)")
        applying = task.index("FF_STATUS_APPLYING", deferred)
        rebooting = task.index("FF_STATUS_REBOOTING", deferred)
        rolling = task.index("FF_STATUS_ROLLING_BACK", deferred)
        recheck = task.index("while (!ff_safe_window_is_safe())", deferred)
        assert deferred < applying < rebooting < rolling < recheck < task.index("esp_restart()")
        assert "s_reboot_txn_id" in task[applying - 40 : applying]
        assert "s_rollback_txn_id" in task[rolling - 40 : rolling]

    def test_an_abort_returns_the_task_to_idle(self) -> None:
        task = _body("command_reboot_task")
        idle = task[task.index("if (!wait_for_the_window())") : task.index("continue;")]
        enter = idle.index("portENTER_CRITICAL(&s_reboot_lock)")
        leave = idle.index("portEXIT_CRITICAL(&s_reboot_lock)")
        for reset in (
            "s_reboot_abort = false;",
            "s_reboot_hold = false;",
            "s_cmd_reboot_pending = false;",
        ):
            assert enter < idle.index(reset) < leave, reset
        assert "esp_restart" not in idle
        assert "the deferred apply was cancelled — no reboot" in FF_MQTT_C.read_text()


class TestTheVerbs:
    def _branches(self, name: str) -> tuple[str, str]:
        body = _body(name)
        safe = body.index("const bool safe = ff_safe_window_is_safe();")
        split = body.index("} else {", safe)
        end = body.index("schedule_command_reboot(id);")
        return body[safe:split], body[split:end]

    def test_apply_deferred_reports_the_apply_wait(self) -> None:
        accepted, deferred = self._branches("on_apply")
        assert 'enqueue_ack(id, "apply", ACK_ACCEPTED, cmd_id, NULL);' in accepted
        assert "FF_STATUS_APPLYING" in accepted
        assert 'enqueue_ack(id, "apply", ACK_DEFERRED, cmd_id, NULL);' in deferred
        assert (
            "enqueue_status(cmd_id, FF_STATUS_AWAITING_SAFE_WINDOW, 100, FF_DETAIL_APPLY_WAITS)"
            in deferred
        )
        for forbidden in ("FF_STATUS_APPLYING", "FF_STATUS_REBOOTING", "ACK_ACCEPTED"):
            assert forbidden not in deferred, forbidden
        body = _body("on_apply")
        assert "s_reboot_kind = CMD_REBOOT_APPLY;" in body
        assert "s_reboot_deferred = !safe;" in body
        assert "phase == FF_OTA_TXN_AWAITING" in body, "apply:auto waiting: a no-op"

    def test_rollback_deferred_reports_the_rollback_wait(self) -> None:
        accepted, deferred = self._branches("on_rollback")
        assert "FF_STATUS_ROLLING_BACK" in accepted
        assert 'enqueue_ack(id, "rollback", ACK_DEFERRED, txn, NULL);' in deferred
        assert "FF_STATUS_AWAITING_SAFE_WINDOW" in deferred
        assert "FF_DETAIL_ROLLBACK_WAITS" in deferred
        assert "FF_STATUS_ROLLING_BACK" not in deferred
        body = _body("on_rollback")
        # Decided and marked first (the D12 shape); only the reboot waits.
        assert body.index("esp_ota_mark_app_invalid_rollback()") < body.index(
            "ff_safe_window_is_safe()"
        )
        assert "s_reboot_kind = CMD_REBOOT_ROLLBACK;" in body
        claim = body.index("s_rollback_decided = true;")
        assert body.index("s_rollback_commanded = true;") < body.index(
            "portEXIT_CRITICAL(&s_decide_lock)", claim
        )

    def test_reboot_is_deferred_without_a_status(self) -> None:
        body = _body("on_reboot")
        assert 'enqueue_ack(id, "reboot", ACK_DEFERRED, NULL, NULL);' in body
        assert "enqueue_status(" not in body
        assert "s_reboot_kind = CMD_REBOOT_PLAIN;" in body
        assert "s_reboot_deferred = false;" in body

    def test_the_rule_is_asked_after_every_refusal(self) -> None:
        for name in ("on_apply", "on_rollback", "on_reboot"):
            body = _body(name)
            ask = body.index("ff_safe_window_is_safe()")
            assert body.count("ff_safe_window_is_safe()") == 1, name
            refusals = [m.start() for m in re.finditer(r"ACK_REFUSED", body)]
            for refusal in refusals:
                text = body[refusal : body.index(";", refusal)]
                if "ACK_CANNOT_MARK" in text:
                    continue
                assert refusal < ask, (name, text)

    def test_the_details(self) -> None:
        header = _code(FF_MQTT_INTERNAL_H)
        assert '#define FF_STATUS_AWAITING_SAFE_WINDOW "awaiting_safe_window"' in header
        assert '#define FF_DETAIL_APPLY_WAITS "apply waits for a safe window"' in header
        assert '#define FF_DETAIL_ROLLBACK_WAITS "rollback waits for a safe window"' in header
        for text in ("apply waits for a safe window", "rollback waits for a safe window"):
            assert len(text) <= 63
        assert "_Static_assert(sizeof(FF_DETAIL_APPLY_WAITS) <= 64" in header
        assert "_Static_assert(sizeof(FF_DETAIL_ROLLBACK_WAITS) <= 64" in header


class TestCancelOfADeferredApply:
    def test_one_ff_ota_cancel_held_and_aborted_only_on_success(self) -> None:
        body = _body("on_cancel")
        assert body.count("ff_ota_cancel(") == 1
        cancel = body.index("ff_ota_cancel(cmd_id, &reported)")
        hold = body.index("s_reboot_hold = true;")
        assert body.index("s_reboot_kind == CMD_REBOOT_APPLY && s_reboot_deferred") < hold
        assert "!s_reboot_committed" in body[:hold]
        assert hold < cancel
        after = body[cancel:]
        release = after.index("s_reboot_hold = false;")
        ok = after.index("if (err == ESP_OK)")
        abort = after.index("s_reboot_abort = true;")
        assert release < ok < abort
        assert abort < after.index("if (err == ESP_ERR_NOT_FOUND)")

    def test_an_accepted_apply_stays_uncancellable(self) -> None:
        body = _body("on_cancel")
        took = body.index("if (!took_back)")
        refusal = body[took : body.index("return;", took)]
        assert "ACK_ALREADY_APPLYING" in refusal
        assert body.index("if (!took_back)") < body.index("ff_ota_cancel(")


class TestFfOta:
    def test_apply_auto_waits_in_awaiting(self) -> None:
        task = _function_body(_ota(), "ota_task")
        assert (
            task.count("s_phase = cmd->apply_now ? FF_OTA_TXN_AWAITING : FF_OTA_TXN_STAGED;") == 2
        )
        staged = task.index("FF_STATUS_STAGED")
        on_command = task.index("if (!cmd->apply_now)", staged)
        wait = task.index("FF_STATUS_AWAITING_SAFE_WINDOW", on_command)
        assert "FF_DETAIL_APPLY_WAITS" in task[wait : wait + 120]
        loop = task.index("while (!cancel_requested() && !ff_safe_window_is_safe())")
        assert on_command < loop

    def test_checkpoint_six_commits_applying_under_the_lock(self) -> None:
        task = _function_body(_ota(), "ota_task")
        loop = task.index("while (!cancel_requested() && !ff_safe_window_is_safe())")
        enter = task.index("portENTER_CRITICAL(&s_cancel_lock)", loop)
        read = task.index("cancel_apply = s_cancel;", enter)
        commit = task.index("s_phase = FF_OTA_TXN_APPLYING;", read)
        leave = task.index("portEXIT_CRITICAL(&s_cancel_lock)", commit)
        applying = task.index("ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_APPLYING")
        assert loop < enter < read < commit < leave < applying
        assert task.index("if (cancel_apply)", leave) < applying
        assert task.count("s_phase = FF_OTA_TXN_APPLYING;") == 1

    def test_the_rule_is_asked_again_before_the_restart(self) -> None:
        task = _function_body(_ota(), "ota_task")
        drain = task.index("vTaskDelay(pdMS_TO_TICKS(OTA_PUBLISH_DRAIN_MS));")
        recheck = task.index("while (!ff_safe_window_is_safe())", drain)
        assert drain < recheck < task.index("esp_restart()")

    def test_the_staged_exit_takes_awaiting_too(self) -> None:
        task = _function_body(_ota(), "ota_task")
        done = task[task.index("done:") :]
        assert "s_phase == FF_OTA_TXN_STAGED || s_phase == FF_OTA_TXN_AWAITING" in done

    def test_cancel_reaches_awaiting(self) -> None:
        body = _function_body(_ota(), "ff_ota_cancel")
        refused = re.findall(r"s_phase == (FF_OTA_TXN_\w+)", body)
        assert "FF_OTA_TXN_AWAITING" not in refused
        assert "FF_OTA_TXN_AWAITING" in _code(FF_OTA_H)


class TestTheStageRefusal:
    def test_a_stage_waits_for_no_reboot(self) -> None:
        body = _body("on_stage")
        handling = body.index("ff_ota_is_handling(")
        decided = body.index("const bool decided = s_rollback_decided;")
        refusal = body.index("if (s_cmd_reboot_pending || decided)")
        start = body.index("ff_ota_start(")
        assert handling < decided < refusal < start
        branch = body[refusal : body.index("return;", refusal)]
        assert "FF_STATUS_FAILED" in branch
        assert "ACK_ROLLBACK_DECIDED" in branch and "ACK_REBOOT_PENDING" in branch
        assert "if (s_rollback_decided)" not in body, "the pinned first-occurrence literal"


class TestTheCapability:
    def test_claimed_only_when_registered_after_selftest(self) -> None:
        identity = _code(FF_IDENTITY_C)
        assert identity.index('cJSON_CreateString("selftest")') < identity.index(
            'cJSON_CreateString("safe_window")'
        )
        note = _function_body(identity, "ff_identity_note_safe_window")
        assert "s_safe_window_registered = registered;" in note
        assert _mqtt().count("ff_identity_note_safe_window(") == 1
        assert _mqtt().count("ff_safe_window_freeze(") == 1


class TestTheArduinoWrapper:
    def test_the_member(self) -> None:
        assert "bool onSafeWindow(bool (*isSafe)());" in _code(ARDUINO_WRAPPER_H)
        wrapper = _code(ARDUINO_WRAPPER_CPP)
        member = _member_body(wrapper, "bool FleetforgeClass::onSafeWindow(bool (*isSafe)())")
        assert _ff_calls(member) == ["ff_set_safe_window_cb"]
        assert "!= ESP_OK" in member
        trampoline = _member_body(wrapper, "static bool sketch_safe_window(void *)")
        assert trampoline.strip() == "return s_sketch_safe_window();"
        assert '#include "ff_safe_window.h"' in ARDUINO_WRAPPER_CPP.read_text()

    def test_the_log_tag_is_raised(self) -> None:
        assert '"ff-safe"' in _code(ARDUINO_WRAPPER_CPP)


class TestTheExamples:
    def test_the_sketch_registers_third_and_reads_a_flag(self) -> None:
        sketch = _code(EXAMPLE_INO)
        setup = re.search(r"void setup\(\)\s*\{\s*[^;]+;\s*[^;]+;\s*([^;]+);", sketch)
        assert setup is not None
        assert setup.group(1).strip() == "Fleetforge.onSafeWindow(safeNow)"
        rule = re.search(r"static bool safeNow\(\) \{(.*?)\}", sketch)
        assert rule is not None and rule.group(1).strip() == "return betweenMessages;"
        assert "static volatile bool betweenMessages" in sketch
        # One line, so a T2 variant can replace it.
        assert EXAMPLE_INO.read_text().count("static bool safeNow()") == 1
        loop = re.search(r"void loop\(\)\s*\{(.*)\}\s*$", sketch, re.S)
        assert loop is not None
        body = loop.group(1)
        assert body.index("betweenMessages = false;") < body.index("blink(*letter);")
        assert body.index("blink(*letter);") < body.index("betweenMessages = true;")
        assert body.index("betweenMessages = true;") < body.index("Fleetforge.selfTestDone(true);")

    def test_the_idf_example_registers_third_and_reads_a_flag(self) -> None:
        main = _code(IDF_EXAMPLE_MAIN_C)
        app = _function_body(main, "app_main")
        statements = [s.strip() for s in app.split(";")]
        assert statements[2] == "ff_set_safe_window_cb(between_messages, NULL)"
        assert _function_body(main, "between_messages").strip() == "(void)ctx; return s_idle;"
        assert "static volatile bool s_idle;" in main
        loop = app[app.index("for (unsigned loops") :]
        assert loop.index("s_idle = false;") < loop.index("blink(*letter);")
        assert loop.index("blink(*letter);") < loop.index("s_idle = true;")
        assert loop.index("s_idle = true;") < loop.index("ff_selftest_report(true, NULL);")
