"""The self-test entrypoint (R5-fw-1): the maker API, the runner, the freeze, the examples.

Built ahead of R5-fw-2, unreleased: the firmware registers a self-test and the session
freezes the registration before its first connect, but nothing runs it yet (fw-2 calls
`ff_selftest_run()` from the announce-ack branch and holds the confirm on it). What is held
here, on every `just test`:

* the public header: two functions, a three-value enum whose numbers are ABI, the
  function-pointer typedef, `extern "C"`, and no private hook in any public header;
* the runner: the registered function is called from its own task only, never from the
  caller of `ff_selftest_run()` (the esp-mqtt task in fw-2) and never from esp_timer; the
  task sits below the session's priorities; `ff_selftest_report()` never logs, allocates or
  blocks; a static semaphore, not a task notification, wakes the runner; the reason is
  printable ASCII without `;`; anything but PASS fails;
* the session wiring in the CRITICAL `ff_mqtt.c`: one freeze, in `ff_mqtt_run`, before the
  first connect, and nothing in the confirm timer, the rollback callback or the arming;
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
        code = _code(FF_MQTT_C)
        assert code.count("ff_selftest_freeze(") == 1
        run = _function_body(code, "ff_mqtt_run")
        assert "(void)ff_selftest_freeze();" in run
        freeze = run.index("ff_selftest_freeze(")
        assert freeze < run.index("ff_txn_init(")
        assert freeze < run.index("esp_mqtt_client_start(")

    def test_nothing_runs_it_yet(self) -> None:
        """R5-fw-2 wires the runner, together with the hold and the capability."""
        assert "ff_selftest_run(" not in _code(FF_MQTT_C)

    def test_never_from_timer_context(self) -> None:
        """Durable: fw-2 may call ff_selftest_run from mqtt_event_handler, never from
        these (esp_timer task, or before NVS)."""
        code = _code(FF_MQTT_C)
        for name in TIMER_CONTEXT:
            body = _function_body(code, name)
            assert "ff_selftest_" not in body, name

    def test_the_stock_agent_never_registers(self) -> None:
        assert "ff_selftest" not in _code(AGENT_MAIN_C)


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
