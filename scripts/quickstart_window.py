# ruff: noqa: S310, S603, S607
"""Phase 2f QEMU safe-window step runner (`phase_apply_waits`) for `lib_quickstart.py`.

Stdlib-only: never imports `fleetforge`.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

if not __package__:
    _repo_root = str(Path(__file__).resolve().parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    __package__ = "scripts"
    if __spec__ is not None:
        __spec__.name = f"scripts.{Path(__file__).stem}"

from .quickstart_build import (
    _command_result,
    _kill_emulator,
    _upload_and_stage,
    device,
    http,
    start_qemu,
    stop_qemu,
    wait_for_device_impl,
    wait_for_lines_impl,
)
from .quickstart_verify import (
    APPLY_WAITS_LINE,
    ARDUINO_DIR,
    COMMAND_REBOOT_DRAIN_MS,
    DETAIL_APPLY_WAITS,
    DEVICE_ID,
    QEMU_CONTAINER,
    REBOOT_WAITS_LINE,
    REBOOTING_FOR_LINE,
    RELEASING_LINE,
    SELFTEST_RUN_LINE,
    WINDOW_MESSAGE,
    WINDOW_STEP_ORDER,
    QuickstartError,
    Run,
    _deploy_of,
    _step_states,
    check_selftest,
    check_window_log,
    expected_selftest,
    parse_rows,
    steps_in_order,
)


def phase_apply_waits(
    run: Run, bundle_w: Path, version_w: str, version_b: str, slot_b: str, log_prev: Path
) -> Path:
    """`R5-test-1`: send `apply` into an unsafe window; the reboot must wait."""
    run.say("\n== Phase 2f: apply into an unsafe window: the reboot must wait ==")
    cmd_w, slot_w, _ = _upload_and_stage(run, version_w, bundle_w, log_prev, slot_b)
    time.sleep(1)
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)
    log_w = start_qemu(run, fresh=False, log_name="qemu-W.log")
    wait_for_lines_impl(
        run,
        log_w,
        (
            r"announce acknowledged by the broker",
            re.escape(SELFTEST_RUN_LINE),
            r"custom self-test passed",
            r"CONFIRMED",
        ),
        timeout=180,
    )
    wait_for_lines_impl(
        run, log_w, (re.escape(f"morse: {WINDOW_MESSAGE} (firmware {version_w})"),), timeout=180
    )
    row = wait_for_device_impl(
        run,
        f"confirmed at {version_w}",
        lambda r: (
            r.get("fw_version") == version_w
            and _deploy_of(r).get("cmd_id") == cmd_w
            and _deploy_of(r).get("state") == "confirmed"
        ),
        timeout=60,
        log=log_w,
    )
    if _deploy_of(row).get("detail") != "selftest passed":
        raise QuickstartError(f"W's confirmed deploy has detail {_deploy_of(row).get('detail')!r}")
    capabilities = row.get("capabilities") or []
    if "apply" not in capabilities or "safe_window" not in capabilities:
        raise QuickstartError(f"W does not announce apply and safe_window: {capabilities}")
    run.passed(
        f"GET /v1/devices {DEVICE_ID} on W",
        f"fw_version {version_w} in {slot_w}, confirmed, detail 'selftest passed', capabilities "
        f"{capabilities}",
    )

    cmd, slot, offset = _upload_and_stage(run, version_b, None, log_w, slot_w)
    if slot != slot_b:
        raise QuickstartError(f"B was staged into {slot}, not its own slot {slot_b}")
    offset = wait_for_lines_impl(run, log_w, (r"window: unsafe at millis \d+",), 120, start=offset)
    status, body, _ = http(run, "POST", f"/v1/devices/{DEVICE_ID}/deploys/{cmd}/apply")
    t_apply = time.monotonic()
    if status != 202 or not isinstance(body, dict):
        raise QuickstartError(f"POST …/deploys/{cmd}/apply answered {status}: {body}")
    command_id = str(body["command_id"])
    run.passed("POST /v1/devices/…/deploys/…/apply", f"202, command_id {command_id}")
    early = (rf"cmd_id={re.escape(cmd)} state=applying", re.escape(RELEASING_LINE))
    wait_for_lines_impl(
        run,
        log_w,
        (
            re.escape(APPLY_WAITS_LINE.format(id=command_id, cmd=cmd)),
            re.escape(REBOOT_WAITS_LINE.format(id=command_id)),
        ),
        timeout=60,
        start=offset,
        forbidden=early,
    )
    result = None
    deadline = time.monotonic() + 30
    while result is None and time.monotonic() < deadline:
        result = _command_result(run, command_id)
        if result is None:
            time.sleep(0.5)
    if result != "deferred":
        raise QuickstartError(f"the apply was answered {result!r}, wanted 'deferred'")
    row = wait_for_device_impl(
        run,
        "awaiting_safe_window",
        lambda r: (
            _deploy_of(r).get("cmd_id") == cmd
            and _deploy_of(r).get("state") == "awaiting_safe_window"
        ),
        timeout=30,
        log=log_w,
    )
    deploy = _deploy_of(row)
    if deploy.get("detail") != DETAIL_APPLY_WAITS or deploy.get("is_terminal") is not False:
        raise QuickstartError(f"the waiting deploy is {json.dumps(deploy)[:400]}")
    if row.get("fw_version") != version_w:
        raise QuickstartError(f"fw_version {row.get('fw_version')} while waiting, wanted W")
    run.passed(
        "apply into the unsafe window",
        f"answered {result}; deploy awaiting_safe_window, detail {deploy.get('detail')!r}, "
        f"is_terminal False, fw_version {version_w}",
    )
    samples = 0
    text = log_w.read_text(errors="replace")
    while not re.search(r"window: safe at millis \d+", text[offset:]):
        sample = device(run)
        sampled = _deploy_of(sample)
        if sampled.get("cmd_id") != cmd or sampled.get("state") != "awaiting_safe_window":
            raise QuickstartError(
                f"before the window opened the deploy moved to {sampled.get('state')}"
            )
        samples += 1
        if time.monotonic() - t_apply > 120:
            raise QuickstartError("no `window: safe` within 120 s of the apply")
        time.sleep(1)
        text = log_w.read_text(errors="replace")
    run.passed("deploy while the window was unsafe", f"awaiting_safe_window in {samples} samples")

    wait_for_lines_impl(
        run,
        log_w,
        (r"window: safe at millis \d+", rf"cmd_id={re.escape(cmd)} state=applying"),
        timeout=60,
        start=offset,
        poll=0.1,
    )
    waited = time.monotonic() - t_apply
    wait_for_lines_impl(
        run, log_w, (rf"cmd_id={re.escape(cmd)} state=rebooting",), 10, start=offset, poll=0.1
    )
    land_by = time.monotonic() + 0.4
    while time.monotonic() < land_by:
        if "rebooting" in _step_states(_deploy_of(device(run))):
            break
        time.sleep(0.1)
    _kill_emulator(run)
    run.say(f"  kill  docker kill {QEMU_CONTAINER} inside the {COMMAND_REBOOT_DRAIN_MS} ms drain")
    if waited < 20:
        raise QuickstartError(
            f"`state=applying` only {waited:.1f} s after the apply: it never waited"
        )
    text = log_w.read_text(errors="replace")[offset:]
    summary = check_window_log(text, cmd, command_id)
    run.passed("build W's console after the apply", f"{summary}; applying {waited:.0f} s after it")
    rebooting_for = text.find(REBOOTING_FOR_LINE.format(id=command_id))
    if rebooting_for >= 0:
        run.say(f"  note  `{REBOOTING_FOR_LINE.format(id=command_id)}` was logged before the kill")
        if re.search(r"rst:", text[rebooting_for:]):
            raise QuickstartError(
                "the emulator soft-reset before the kill — a harness race against the "
                "esp_restart limit, not an agent defect; rerun"
            )

    log_back = start_qemu(run, fresh=False, log_name="qemu-W-B.log")
    wait_for_lines_impl(
        run,
        log_back,
        (
            rf"transaction {re.escape(cmd)}: confirming on {slot_b}",
            r"announce acknowledged by the broker",
            r"custom self-test passed",
            r"CONFIRMED",
        ),
        timeout=180,
    )
    wait_for_lines_impl(
        run, log_back, (re.escape(f"morse: HELLO (firmware {version_b})"),), timeout=180
    )
    loaded = re.findall(
        r"Loaded app from partition at offset (0x[0-9a-fA-F]+)",
        log_back.read_text(errors="replace"),
    )
    if loaded:
        rows = parse_rows((run.tree / ARDUINO_DIR / "partitions.csv").read_text())
        offset_b = next(o for name, _, _, o, _ in rows if name == slot_b)
        if int(loaded[-1], 16) != offset_b:
            raise QuickstartError(
                f"after the apply the bootloader loaded {loaded[-1]}, not {slot_b}"
            )
        run.passed("bootloader after the apply", f"loaded {loaded[-1]} = {slot_b}")
    else:
        run.say("  note  the bootloader does not log `Loaded app from partition` (not checked)")
    row = wait_for_device_impl(
        run,
        f"confirmed at {version_b} (2f)",
        lambda r: (
            r.get("fw_version") == version_b
            and _deploy_of(r).get("cmd_id") == cmd
            and _deploy_of(r).get("state") == "confirmed"
            and _deploy_of(r).get("is_terminal") is True
        ),
        timeout=60,
        log=log_back,
    )
    deploy = _deploy_of(row)
    if deploy.get("detail") != "selftest passed":
        raise QuickstartError(f"B's confirmed deploy has detail {deploy.get('detail')!r}")
    check_selftest(run, "B confirmed after the wait", row, expected_selftest("passed", slot_b))
    steps = _step_states(deploy)
    if not steps_in_order(steps, WINDOW_STEP_ORDER):
        raise QuickstartError(f"the deploy's steps are not {WINDOW_STEP_ORDER} in order: {steps}")
    final = _command_result(run, command_id)
    if final != "deferred":
        raise QuickstartError(f"the apply's result became {final!r}: the first answer must win")
    run.passed(
        f"GET /v1/devices {DEVICE_ID} after the window",
        f"deploy {cmd} confirmed (is_terminal True), detail 'selftest passed', fw_version "
        f"{version_b}, steps {steps}, command result still {final}",
    )
    return log_back
