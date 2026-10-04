"""A download that stops making progress fails (R2-fw-5) — firmware tripwires, as text.

No host test can execute `agent/main/*.c`; the QEMU run (store proxy blackholed mid-download:
`failed` / `download stalled` 60-80 s after the last byte, otadata unchanged, the next
deploy runs) is the proof. What CAN be held here, on every `just test`, are the orderings
that make that true. Each one fails silently: the board stays safe on its old image, and
simply never takes another update until somebody power-cycles it.

* the stall budget is a bounded wall-clock number, at least two read timeouts long;
* the clock starts after `esp_https_ota_begin()` and before the perform loop, so the wait
  for the first body byte counts;
* the stall check runs before the loop's size-less `continue`, so a command without a size
  is guarded too;
* the stall branch aborts and never finishes, never writes otadata or the transaction
  record, and never logs the URL;
* the stall branch comes before the `download failed` branch (after the break `err` is still
  IN_PROGRESS, which would otherwise be misreported);
* `done:` still clears `s_running`, which is what frees the update slot.

Same idiom as `test_agent_txn.py`: comments are stripped, because they quote the very
spellings these tests forbid.
"""

import re

from tests.test_agent_txn import FF_OTA_C, _function_body
from tests.test_ff_cfg import _code

PERFORM_LOOP = "while ((err = esp_https_ota_perform(handle))"


def _ota_task() -> str:
    return _function_body(_code(FF_OTA_C), "ota_task")


def _define(source: str, name: str) -> int:
    match = re.search(rf"^#define {name} (\d+)\s*$", source, flags=re.MULTILINE)
    assert match is not None, f"no #define {name}"
    return int(match.group(1))


def _perform_loop_body(task: str) -> str:
    """The body of the perform while-loop, by brace matching."""
    start = task.index(PERFORM_LOOP)
    open_brace = task.index("{", start)
    depth = 0
    for index in range(open_brace, len(task)):
        if task[index] == "{":
            depth += 1
        elif task[index] == "}":
            depth -= 1
            if depth == 0:
                return task[open_brace + 1 : index]
    raise AssertionError("unbalanced braces in the perform loop")


def _stall_branch(task: str) -> str:
    start = task.index("if (stalled)")
    return task[start : task.index("goto done", start)]


def test_the_stall_budget_is_bounded_and_longer_than_two_reads() -> None:
    source = _code(FF_OTA_C)
    stall = _define(source, "OTA_STALL_MS")
    timeout = _define(source, "OTA_HTTP_TIMEOUT_MS")
    assert stall >= 2 * timeout, "one slow read must never trip the guard"
    # A budget that drifts towards "forever" is the bug this task fixed.
    assert stall <= 120_000
    assert "_Static_assert(OTA_STALL_MS >= 2 * OTA_HTTP_TIMEOUT_MS" in source


def test_the_clock_starts_after_begin_and_before_the_loop() -> None:
    task = _ota_task()
    clock = task.index("esp_timer_get_time(")
    assert task.index("esp_https_ota_begin(") < clock < task.index(PERFORM_LOOP)


def test_a_sizeless_command_cannot_skip_the_stall_check() -> None:
    body = _perform_loop_body(_ota_task())
    assert "continue" in body
    assert body.index("esp_timer_get_time(") < body.index("continue")
    assert body.index("stalled = true") < body.index("continue")


def test_the_stall_branch_aborts_and_touches_nothing_else() -> None:
    branch = _stall_branch(_ota_task())
    assert "esp_https_ota_abort(handle)" in branch
    assert 'fail(cmd, "download stalled")' in branch
    for forbidden in (
        "ff_txn_save",
        "esp_https_ota_finish",
        "restore_boot_partition",
        "esp_ota_set_boot_partition",
        "cmd->url",
        "resolved",
    ):
        assert forbidden not in branch, forbidden


def test_a_stall_is_not_misreported_as_a_failed_download() -> None:
    task = _ota_task()
    assert task.index("if (stalled)") < task.index('fail(cmd, "download failed")')


def test_done_still_frees_the_update_slot() -> None:
    task = _ota_task()
    assert "s_running = false" in task[task.index("done:") :]
