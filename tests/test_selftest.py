"""The self-test (R5-fw-1 entrypoint, R5-fw-2 hold): maker API, runner, freeze, hold, examples.

The firmware registers a self-test, the session freezes the registration before its first
connect, and on a boot that needs confirming the announce ack starts the self-test instead
of confirming: only its pass confirms, a fail rolls back with the reason stored in `ff_txn`,
and the confirm deadline still applies (R5-fw-2). What is held here, on every `just test`:

* the public header: two functions, a three-value enum whose numbers are ABI, the
  function-pointer typedef, `extern "C"`, and no private hook in any public header;
* the runner: the registered function is called from its own task only, never from the
  caller of `ff_selftest_run()` (the esp-mqtt task in fw-2) and never from esp_timer; the
  task sits below the session's priorities; `ff_selftest_report()` never logs, allocates or
  blocks; a static semaphore, not a task notification, wakes the runner; the reason is
  printable ASCII without `;`; anything but PASS fails;
* the session wiring in the CRITICAL `ff_mqtt.c`: one freeze, in `ff_mqtt_run`, before the
  first connect, and nothing of the self-test API in the confirm timer, the rollback callback
  or the arming;
* the hold (`TestTheHold`): one run, from the announce-ack branch, after the late-ack guard
  and before the plain confirm, with the `timed out` head pre-stored first; the deadline's
  guard and its claim under the decision lock; the done callback's claim before any
  confirm, its stored reason before any reboot; the detail grammar and its 63-byte budget;
  the classification that composes the returned-to detail; the `selftest` capability;
* the Arduino members and both examples (register second, answer from the loop).

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
    FF_SELFTEST_C,
    FF_SELFTEST_H,
    FF_SELFTEST_INTERNAL_H,
    IDF_EXAMPLE_MAIN_C,
    agent_sources,
)
from tests.test_agent_txn import _function_body
from tests.test_arduino_library import _ff_calls
from tests.test_ff_cfg import _code
from tests.test_ota_component import _declared_functions

FF_MQTT_C = COMPONENT_SRC / "ff_mqtt.c"

PUBLIC_FUNCTIONS = {"ff_set_selftest", "ff_selftest_report"}
PRIVATE_HOOKS = ("ff_selftest_freeze", "ff_selftest_run")
# The esp_timer callbacks and the arming that runs before NVS: never a place for the
# self-test, not in fw-2 either.
TIMER_CONTEXT = ("confirm_timeout_cb", "rollback_now_cb", "ff_mqtt_arm_confirm_timer")


def _src() -> str:
    return _code(FF_SELFTEST_C)


def _define(name: str) -> int:
    match = re.search(rf"^#define {name} (\d+)$", _src(), re.M)
    assert match is not None, name
    return int(match.group(1))


def _member_body(code: str, signature: str) -> str:
    """The body of a C++ member whose parameter list `_function_body` cannot match (it
    stops at the first `)`, and `void (*test)()` has three)."""
    match = re.search(re.escape(signature) + r"\s*\{", code)
    assert match is not None, signature
    depth = 0
    for index in range(match.end() - 1, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[match.end() : index]
    raise AssertionError(f"unbalanced braces in {signature}")


class TestTheMakerHeader:
    def test_it_declares_two_functions(self) -> None:
        assert _declared_functions(FF_SELFTEST_H) == PUBLIC_FUNCTIONS

    def test_the_exact_prototypes(self) -> None:
        header = _code(FF_SELFTEST_H)
        assert "esp_err_t ff_set_selftest(ff_selftest_fn fn, void *ctx);" in header
        assert "esp_err_t ff_selftest_report(bool passed, const char *reason);" in header
        assert (
            "typedef ff_selftest_result_t (*ff_selftest_fn)"
            "(void *ctx, char *reason, size_t reason_len);"
        ) in header

    def test_the_enum_values_are_explicit(self) -> None:
        """The numbers are ABI once released: a reordering would flip PASS and FAIL."""
        enum = re.search(
            r"typedef enum \{(.*?)\} ff_selftest_result_t;", _code(FF_SELFTEST_H), re.S
        )
        assert enum is not None
        members = [m.strip() for m in enum.group(1).split(",") if m.strip()]
        assert members == [
            "FF_SELFTEST_PASS = 0",
            "FF_SELFTEST_FAIL = 1",
            "FF_SELFTEST_PENDING = 2",
        ]

    def test_extern_c_and_minimal_includes(self) -> None:
        header = _code(FF_SELFTEST_H)
        assert 'extern "C" {' in header
        includes = re.findall(r"^#include (\S+)", header, re.M)
        assert includes == ["<stdbool.h>", "<stddef.h>", '"esp_err.h"']

    def test_the_hooks_are_private(self) -> None:
        assert FF_SELFTEST_INTERNAL_H.parent == COMPONENT_SRC
        assert _declared_functions(FF_SELFTEST_INTERNAL_H) == set(PRIVATE_HOOKS)
        for header in sorted(COMPONENT_INCLUDE.glob("*.h")):
            code = _code(header)
            for hook in PRIVATE_HOOKS:
                assert hook not in code, (header.name, hook)
        for public in (ARDUINO_WRAPPER_H, ARDUINO_WRAPPER_CPP):
            for hook in PRIVATE_HOOKS:
                assert hook not in _code(public), (public.name, hook)


class TestTheRunner:
    def test_the_component_compiles_it(self) -> None:
        assert '"src/ff_selftest.c"' in COMPONENT_CMAKE.read_text()

    def test_the_registered_function_is_called_from_its_task_only(self) -> None:
        """Never from the caller of ff_selftest_run() (fw-2: the esp-mqtt event handler,
        where a slow test would stall the session and the PUBACK)."""
        src = _src()
        task = _function_body(src, "selftest_task")
        assert len(re.findall(r"\bfn\s*\(\s*ctx\s*,", task)) == 1
        assert "const ff_selftest_fn fn = s_fn;" in task
        for name in (
            "ff_selftest_run",
            "ff_selftest_freeze",
            "ff_set_selftest",
            "ff_selftest_report",
        ):
            body = _function_body(src, name)
            assert not re.search(r"\b(s_fn|fn)\s*\(", body), name
            assert "selftest_task(" not in body, name
        # The only call of either spelling in the file is the one inside selftest_task.
        assert len(re.findall(r"\b(?:s_fn|fn)\s*\(", src)) == 1

    def test_its_own_task_below_the_session(self) -> None:
        src = _src()
        run = _function_body(src, "ff_selftest_run")
        assert "xTaskCreate(selftest_task," in run
        assert "xTaskCreatePinnedToCore" not in src
        assert "FF_SELFTEST_PRIO" in run and "FF_SELFTEST_STACK" in run
        assert _define("FF_SELFTEST_PRIO") < 4, "below ff_hb/ff_tel (4) and esp-mqtt (5)"
        assert _define("FF_SELFTEST_STACK") >= 4096, "the header promises at least 4 KB"
        assert _define("FF_SELFTEST_REASON_BUF") == 64, "ff_txn's detail[64]"

    def test_report_never_logs_allocates_or_blocks(self) -> None:
        """The R4-fw-3 recursion guard, and a firmware calls it on every loop."""
        body = _function_body(_src(), "ff_selftest_report")
        for forbidden in (
            "ESP_LOG",
            "malloc",
            "calloc",
            "printf",
            "vTaskDelay",
            "xSemaphoreTake",
            "ulTaskNotifyTake",
        ):
            assert forbidden not in body, forbidden
        assert "xSemaphoreGive(s_sem)" in body

    def test_a_static_semaphore_not_a_task_notification(self) -> None:
        """A report may race the task's exit: notifying a deleted task is a use-after-free."""
        src = _src()
        assert "xTaskNotifyGive" not in src
        assert "xTaskNotify" not in src
        assert "xSemaphoreCreateBinaryStatic(&s_sem_buf)" in src
        assert "vSemaphoreDelete" not in src
        assert "portENTER_CRITICAL(&s_lock)" in src

    def test_the_reason_is_printable_ascii_without_semicolon(self) -> None:
        body = _function_body(_src(), "keep_reason")
        assert "c >= 0x20" in body
        assert "c <= 0x7e" in body
        assert "c != ';'" in body
        assert "dst[n] = '\\0';" in body
        report = _function_body(_src(), "ff_selftest_report")
        assert "keep_reason(kept, sizeof kept, reason, FF_SELFTEST_REPORT_SCAN)" in report
        assert report.index("keep_reason(") < report.index("portENTER_CRITICAL")

    def test_anything_but_pass_fails(self) -> None:
        task = _function_body(_src(), "selftest_task")
        assert "s_passed = r == FF_SELFTEST_PASS;" in task
        assert "== FF_SELFTEST_FAIL" not in task
        assert "r == FF_SELFTEST_PENDING" in task

    def test_the_first_answer_wins(self) -> None:
        """The function's return counts only if no report came in while it ran, and a report
        counts only while the agent waits."""
        src = _src()
        task = _function_body(src, "selftest_task")
        call = task.index("fn(ctx,")
        assert task.index("if (s_state == ST_RUNNING)", call) > call
        report = _function_body(src, "ff_selftest_report")
        assert "if (s_state != ST_RUNNING && s_state != ST_PENDING)" in report
        assert "return ESP_ERR_INVALID_STATE;" in report

    def test_registration_is_refused_once_frozen_or_taken(self) -> None:
        body = _function_body(_src(), "ff_set_selftest")
        assert "return ESP_ERR_INVALID_ARG;" in body
        assert "frozen = s_frozen;" in body
        assert body.count("return ESP_ERR_INVALID_STATE;") == 2
        run = _function_body(_src(), "ff_selftest_run")
        assert "s_frozen && s_fn != NULL && s_state == ST_IDLE" in run

    def test_the_tag(self) -> None:
        assert 'static const char *TAG = "ff-selftest";' in _src()


class TestTheSessionWiring:
    def test_one_freeze_before_the_first_connect(self) -> None:
        """The gate flag and the capability are set from the freeze, before the first
        connect: never at the ack (DECISIONS R5-fw-2, the no-deadline window)."""
        code = _code(FF_MQTT_C)
        assert code.count("ff_selftest_freeze(") == 1
        run = _function_body(code, "ff_mqtt_run")
        assert "s_selftest_registered = ff_selftest_freeze();" in run
        freeze = run.index("ff_selftest_freeze(")
        note = run.index("ff_identity_note_selftest(s_selftest_registered);")
        assert freeze < note
        for later in ("ff_txn_init(", "classify_txn(", "esp_mqtt_client_start("):
            assert note < run.index(later), later
        assert code.count("s_selftest_registered =") == 1, "written once, in ff_mqtt_run"

    def test_never_from_timer_context(self) -> None:
        """Durable: fw-2 may call ff_selftest_run from mqtt_event_handler, never from
        these (esp_timer task, or before NVS)."""
        code = _code(FF_MQTT_C)
        for name in TIMER_CONTEXT:
            body = _function_body(code, name)
            assert "ff_selftest_" not in body, name

    def test_the_stock_agent_never_registers(self) -> None:
        assert "ff_selftest" not in _code(AGENT_MAIN_C)


FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_IDENTITY_INTERNAL_H = COMPONENT_SRC / "ff_identity_internal.h"
# Pinned elsewhere (tests/test_agent_txn.py, tests/test_agent_board_measurements.py) by
# their FIRST occurrence: the done callback must not repeat them.
PINNED_CONFIRM = "else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING)"
PINNED_BOOTED_NEW = "s_txn.kind == TXN_BOOTED_NEW && accept_unverified_image()"
PINNED_LATE_ACK = "if (s_rollback_decided)"
# The esp_timer side of the confirm/rollback pair never writes flash, never takes the
# client lock and never touches the self-test API (the pre-store makes `timed out` free).
TIMER_FORBIDDEN = (
    "esp_mqtt_client_",
    "enqueue_status",
    "ff_mqtt_publish_status",
    "ESP_ERROR_CHECK",
    "ff_selftest_",
    "nvs_",
    "ff_store_",
    "ff_txn_",
)


def _published() -> str:
    match = re.search(r"case MQTT_EVENT_PUBLISHED:(.*?)\bcase\b", _code(FF_MQTT_C), re.S)
    assert match is not None
    return match.group(1)


def _done() -> str:
    return _function_body(_code(FF_MQTT_C), "selftest_done")


def _mqtt_define(name: str) -> str:
    match = re.search(rf"^#define {name} (.+)$", _code(FF_MQTT_C), re.M)
    assert match is not None, name
    return match.group(1).strip()


class TestTheHold:
    """R5-fw-2: the announce ack of a boot that needs confirming starts the self-test, and
    only its pass confirms (CRITICAL.md: the device-side confirm timer / rollback path)."""

    def test_one_run_from_the_ack_branch_between_the_guards(self) -> None:
        code = _code(FF_MQTT_C)
        assert code.count("ff_selftest_run(") == 1
        published = _published()
        run = published.index("ff_selftest_run(selftest_done)")
        assert published.index(PINNED_LATE_ACK) < run < published.index(PINNED_CONFIRM)
        hold = published[published.index(PINNED_LATE_ACK) : run]
        assert (
            "else if (s_selftest_registered && (pending_verify() || s_txn.kind == TXN_BOOTED_NEW))"
        ) in re.sub(r"\s+", " ", hold)

    def test_the_timed_out_head_is_stored_before_the_run(self) -> None:
        """The done callback may run at once on the other core: its `failed` head must land
        after this one, never under it."""
        published = _published()
        store = published.index("ff_txn_set_detail(s_txn.cmd_id, DETAIL_SELFTEST_TIMED_OUT)")
        assert store < published.index("ff_selftest_run(")
        guard = published.rindex("if (s_txn.kind == TXN_CONFIRMING)", 0, store)
        assert published.index(PINNED_LATE_ACK) < guard

    def test_a_runner_that_cannot_start_keeps_the_hold(self) -> None:
        """Fail-safe: no confirm without a pass; the deadline decides."""
        published = _published()
        run = published.index("ff_selftest_run(")
        tail = published[run : published.index(PINNED_CONFIRM)]
        assert "if (err != ESP_OK)" in tail
        for forbidden in ("confirm_this_image(", "accept_unverified_image(", "enqueue_status("):
            assert forbidden not in tail, forbidden

    def test_the_deadline_still_applies_to_a_held_image(self) -> None:
        body = _function_body(_code(FF_MQTT_C), "confirm_timeout_cb")
        assert "if (!pending_verify())" in body
        assert "if (s_ctx.session_confirmed && !s_selftest_registered)" in body
        assert "s_ctx.session_confirmed || !pending_verify()" not in body
        # Today's line, byte for byte (the quickstart's R phase greps it), and the new one.
        source = FF_MQTT_C.read_text()
        assert (
            '"no working session %d s after an OTA boot — marking this image invalid "\n'
            '                      "and rolling back to the previous slot"'
        ) in source
        assert '"no self-test result %d s after an OTA boot — marking this image invalid "' in (
            source
        )

    def test_the_deadline_claims_under_the_decision_lock(self) -> None:
        code = _code(FF_MQTT_C)
        assert "static portMUX_TYPE s_decide_lock = portMUX_INITIALIZER_UNLOCKED;" in code
        assert "static bool s_confirm_claimed;" in code
        assert "static volatile bool s_selftest_registered;" in code
        body = _function_body(code, "confirm_timeout_cb")
        enter = body.index("portENTER_CRITICAL(&s_decide_lock)")
        claim = body.index("s_rollback_decided = true;")
        leave = body.index("portEXIT_CRITICAL(&s_decide_lock)")
        assert enter < claim < leave < body.index("esp_timer_start_once(")
        assert enter < body.index("s_confirm_claimed") < leave
        # The backstop and the plain rollback both stay outside the lock.
        assert body.count("esp_ota_mark_app_invalid_rollback_and_reboot()") == 2
        assert leave < body.index("esp_ota_mark_app_invalid_rollback_and_reboot()")
        for name in ("confirm_timeout_cb", "rollback_now_cb", "rollback_report_task"):
            fn = _function_body(code, name)
            for forbidden in TIMER_FORBIDDEN:
                if name == "rollback_report_task" and forbidden == "enqueue_status":
                    continue  # its own task, the R2-be-1 report
                assert forbidden not in fn, (name, forbidden)

    def test_the_done_callback_claims_before_it_confirms(self) -> None:
        body = _done()
        enter = body.index("portENTER_CRITICAL(&s_decide_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_decide_lock)")
        assert enter < body.index("too_late = s_rollback_decided;") < leave
        assert enter < body.index("s_confirm_claimed = true;") < leave
        assert enter < body.index("s_rollback_decided = true;") < leave
        late = body.index("return;")
        assert leave < late
        assert "NOT confirming" in body[leave:late]
        for after in (
            "confirm_this_image(",
            "accept_unverified_image(",
            "ff_txn_set_detail(",
            "esp_timer_start_once(",
            "esp_ota_mark_app_invalid_rollback_and_reboot(",
            "enqueue_status(",
        ):
            assert late < body.index(after), after
        for pinned in (PINNED_CONFIRM, PINNED_BOOTED_NEW, PINNED_LATE_ACK):
            assert pinned not in body, pinned

    def test_a_pass_confirms_with_its_detail(self) -> None:
        body = _done()
        passed = body.index("if (passed) {", body.index("return;"))
        booted_new = body.index("if (kind == TXN_BOOTED_NEW)", passed)
        assert booted_new < body.index("accept_unverified_image()")
        detail = body.index("strlcpy(s_txn.detail, DETAIL_SELFTEST_PASSED, sizeof(s_txn.detail));")
        assert body.index("const bool marked = confirm_this_image();") < detail
        assert "if (marked && kind == TXN_CONFIRMING)" in body
        assert detail < body.index("s_txn.kind = TXN_ALREADY_CONFIRMED;", detail)
        assert detail < body.index("FF_STATUS_CONFIRMED, 100, s_txn.detail", detail)

    def test_a_fail_stores_its_reason_before_any_rollback(self) -> None:
        """D11: at decision time, before any wait (the grace timer, a safe window)."""
        body = _done()
        head = body.index("selftest_failed_head(head, sizeof(head), reason);")
        store = body.index("ff_txn_set_detail(s_txn.cmd_id, head);")
        assert head < store
        assert store < body.index("esp_timer_start_once(")
        assert store < body.index("esp_ota_mark_app_invalid_rollback_and_reboot(")
        assert "kind == TXN_CONFIRMING || kind == TXN_BOOTED_NEW" in body
        assert "esp_timer_create(&rollback_args, &s_rollback_timer)" in body
        assert "FF_STATUS_ROLLING_BACK" in body and "s_ctx.connected" in body

    def test_only_the_session_stores_a_detail(self) -> None:
        # 3 since R5-fw-4: the ack pre-store, the fail head, and store_rollback_head() (the
        # `rolled back by command` head of the rollback verb).
        assert _code(FF_MQTT_C).count("ff_txn_set_detail(") == 3
        for path in agent_sources():
            if path.name not in ("ff_mqtt.c", "ff_txn.c", "ff_txn.h"):
                assert "ff_txn_set_detail" not in _code(path), path.name

    def test_the_detail_grammar(self) -> None:
        assert _mqtt_define("DETAIL_SELFTEST_PASSED") == '"selftest passed"'
        assert _mqtt_define("DETAIL_SELFTEST_TIMED_OUT") == '"selftest timed out"'
        assert _mqtt_define("DETAIL_SELFTEST_FAILED") == '"selftest failed: "'
        assert _mqtt_define("DETAIL_SELFTEST_PREFIX") == '"selftest "'
        assert _mqtt_define("DETAIL_RETURNED_TO") == '"; returned to "'
        assert _mqtt_define("DETAIL_NO_REASON") == '"no reason given"'
        assert _mqtt_define("DETAIL_MAX") == "63"
        # 27 bytes of reason with an ota_N label: the number the maker header promises.
        assert 63 - len("selftest failed: ") - len("; returned to ") - len("ota_0") == 27
        assert len("selftest timed out; returned to ota_0") <= 63

    def test_the_head_shortens_the_reason_only(self) -> None:
        body = _function_body(_code(FF_MQTT_C), "selftest_failed_head")
        assert "strlen(DETAIL_SELFTEST_FAILED) + strlen(DETAIL_RETURNED_TO) + label_len" in body
        assert "DETAIL_MAX - fixed" in body
        assert 'snprintf(out, len, DETAIL_SELFTEST_FAILED "%.*s", budget, reason);' in body
        empty = body.index("reason[0] == '\\0'")
        assert empty < body.index("reason = DETAIL_NO_REASON;") < body.index("snprintf(")
        compose = _function_body(_code(FF_MQTT_C), "compose_returned_detail")
        assert '"%.*s" DETAIL_RETURNED_TO "%s"' in compose
        assert "strlen(DETAIL_SELFTEST_FAILED)" in compose, "never cut below the prefix"

    def test_classify_composes_only_under_the_prefix(self) -> None:
        body = _function_body(_code(FF_MQTT_C), "classify_txn")
        prefix = "strncmp(rec.detail, DETAIL_SELFTEST_PREFIX, strlen(DETAIL_SELFTEST_PREFIX))"
        assert body.count(prefix) == 2
        valid = body.index("state == ESP_OTA_IMG_VALID")
        assert valid < body.index(prefix) < body.index("DETAIL_SELFTEST_PASSED")
        rolled = body.index("s_txn.kind = TXN_ROLLED_BACK;")
        assert rolled < body.index(prefix, rolled)
        assert body.index(prefix, rolled) < body.index(
            "compose_returned_detail(s_txn.detail, sizeof(s_txn.detail), rec.detail,"
        )
        assert '"returned to %s; %s did not confirm"' in body

    def test_the_capability_is_claimed_only_when_registered(self) -> None:
        identity = _code(FF_IDENTITY_C)
        assert identity.count('cJSON_CreateString("selftest")') == 1
        guarded = re.search(
            r"if \(s_selftest_registered\) \{\s*cJSON_AddItemToArray\(capabilities, "
            r'cJSON_CreateString\("selftest"\)\);\s*\}',
            identity,
        )
        assert guarded is not None
        assert identity.index('cJSON_CreateString("ota")') < guarded.start()
        note = _function_body(identity, "ff_identity_note_selftest")
        assert "s_selftest_registered = registered;" in note
        assert "void ff_identity_note_selftest(bool registered);" in _code(FF_IDENTITY_INTERNAL_H)
        assert _code(FF_MQTT_C).count("ff_identity_note_selftest(") == 1


class TestTheArduinoWrapper:
    def test_the_members(self) -> None:
        header = _code(ARDUINO_WRAPPER_H)
        assert "bool onSelfTest(void (*test)());" in header
        assert "bool selfTestDone(bool passed, const char *reason = nullptr);" in header

    def test_each_member_makes_one_call(self) -> None:
        wrapper = _code(ARDUINO_WRAPPER_CPP)
        on = _member_body(wrapper, "bool FleetforgeClass::onSelfTest(void (*test)())")
        assert _ff_calls(on) == ["ff_set_selftest"]
        done = _member_body(
            wrapper, "bool FleetforgeClass::selfTestDone(bool passed, const char *reason)"
        )
        assert _ff_calls(done) == ["ff_selftest_report"]
        assert "== ESP_OK" in done

    def test_the_trampoline_says_pending(self) -> None:
        wrapper = _code(ARDUINO_WRAPPER_CPP)
        trampoline = _member_body(
            wrapper, "static ff_selftest_result_t sketch_selftest(void *, char *, size_t)"
        )
        assert "s_sketch_selftest();" in trampoline
        assert "return FF_SELFTEST_PENDING;" in trampoline

    def test_the_log_tag_is_raised(self) -> None:
        assert '"ff-selftest"' in _code(ARDUINO_WRAPPER_CPP)


class TestTheExamples:
    def test_the_sketch_registers_second_and_answers_from_loop(self) -> None:
        sketch = _code(EXAMPLE_INO)
        setup = re.search(r"void setup\(\)\s*\{\s*[^;]+;\s*([^;]+);", sketch)
        assert setup is not None
        assert setup.group(1).strip() == "Fleetforge.onSelfTest(selfTest)"
        loop = re.search(r"void loop\(\)\s*\{(.*)\}\s*$", sketch, re.S)
        assert loop is not None
        body = loop.group(1)
        assert body.count("Fleetforge.selfTestDone(true);") == 1
        assert body.index("blink(*letter);") < body.index("Fleetforge.selfTestDone(true);")

    def test_the_idf_example_registers_second_and_answers_from_its_loop(self) -> None:
        main = _code(IDF_EXAMPLE_MAIN_C)
        app = _function_body(main, "app_main")
        statements = [s.strip() for s in app.split(";")]
        assert statements[0] == "fleetforge_start()"
        assert statements[1] == "ff_set_selftest(selftest, NULL)"
        assert "return FF_SELFTEST_PENDING;" in _function_body(main, "selftest")
        loop = app[app.index("for (unsigned loops") :]
        assert loop.count("ff_selftest_report(true, NULL);") == 1
        assert loop.index("blink(*letter);") < loop.index("ff_selftest_report(true, NULL);")
