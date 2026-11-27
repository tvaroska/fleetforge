#!/usr/bin/env python3
"""A failing self-test rolls back; `apply` waits for the safe window — simulated (`R5-test-1`).

Drives two simulated boards (`V` with `auto` safe window and `W` with `file:<out>/W.safe`)
through the real dev stack across seven scenarios (`selftest-passed`, `selftest-failed`,
`apply-now`, `apply-waits`, `cancel-waiting`, `auto-waits`, `traps`), with `E2E_BREAK`
vacuity support. Stdlib only: never imports the project.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

if not __package__:
    _repo_root = str(Path(__file__).resolve().parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    __package__ = "scripts"
    if __spec__ is not None:
        __spec__.name = f"scripts.{Path(__file__).stem}"

from .selftest_window_helpers import (
    ANSWER_TIMEOUT_S as ANSWER_TIMEOUT_S,
)
from .selftest_window_helpers import (
    APP_HEAD as APP_HEAD,
)
from .selftest_window_helpers import (
    DEFAULT_OUT as DEFAULT_OUT,
)
from .selftest_window_helpers import (
    DETAIL_APPLY_WAITS as DETAIL_APPLY_WAITS,
)
from .selftest_window_helpers import (
    LAYOUT as LAYOUT,
)
from .selftest_window_helpers import (
    LIB_MARKER_LEN as LIB_MARKER_LEN,
)
from .selftest_window_helpers import (
    LIB_MARKER_MAGIC as LIB_MARKER_MAGIC,
)
from .selftest_window_helpers import (
    LIB_MARKER_VERSION as LIB_MARKER_VERSION,
)
from .selftest_window_helpers import (
    ONLINE_TIMEOUT_S as ONLINE_TIMEOUT_S,
)
from .selftest_window_helpers import (
    OVERRIDES as OVERRIDES,
)
from .selftest_window_helpers import (
    PASSED as PASSED,
)
from .selftest_window_helpers import (
    POLL_S as POLL_S,
)
from .selftest_window_helpers import (
    R5_CAPABILITIES as R5_CAPABILITIES,
)
from .selftest_window_helpers import (
    REPO_ROOT as REPO_ROOT,
)
from .selftest_window_helpers import (
    SAFE_FILE as SAFE_FILE,
)
from .selftest_window_helpers import (
    SCENARIOS as SCENARIOS,
)
from .selftest_window_helpers import (
    SELFTEST_FAIL_REASON as SELFTEST_FAIL_REASON,
)
from .selftest_window_helpers import (
    SELFTEST_PASSED as SELFTEST_PASSED,
)
from .selftest_window_helpers import (
    STFAIL_MARKER as STFAIL_MARKER,
)
from .selftest_window_helpers import (
    TARGET as TARGET,
)
from .selftest_window_helpers import (
    TOKEN_RE as TOKEN_RE,
)
from .selftest_window_helpers import (
    URL_RE as URL_RE,
)
from .selftest_window_helpers import (
    VERSION_AT as VERSION_AT,
)
from .selftest_window_helpers import (
    VERSION_LEN as VERSION_LEN,
)
from .selftest_window_helpers import (
    WAIT_ORDER as WAIT_ORDER,
)
from .selftest_window_helpers import (
    Api as Api,
)
from .selftest_window_helpers import (
    Board as Board,
)
from .selftest_window_helpers import (
    Outcome as Outcome,
)
from .selftest_window_helpers import (
    Run as Run,
)
from .selftest_window_helpers import (
    SetupError as SetupError,
)
from .selftest_window_helpers import (
    _deploy as _deploy,
)
from .selftest_window_helpers import (
    build_image as build_image,
)
from .selftest_window_helpers import (
    check_selftest as check_selftest,
)
from .selftest_window_helpers import (
    derive_device_id as derive_device_id,
)
from .selftest_window_helpers import (
    failed_detail as failed_detail,
)
from .selftest_window_helpers import (
    failed_selftest as failed_selftest,
)
from .selftest_window_helpers import (
    find_secrets as find_secrets,
)
from .selftest_window_helpers import (
    labels_for as labels_for,
)
from .selftest_window_helpers import (
    lib_marker as lib_marker,
)
from .selftest_window_helpers import (
    read_password as read_password,
)
from .selftest_window_helpers import (
    simulator_argv as simulator_argv,
)
from .selftest_window_helpers import (
    start_board as start_board,
)
from .selftest_window_helpers import (
    step_states as step_states,
)
from .selftest_window_helpers import (
    steps_in_order as steps_in_order,
)
from .selftest_window_helpers import (
    stop_boards as stop_boards,
)


def scenario_selftest_passed(run: Run) -> Outcome:
    result = Outcome()
    board, label = run.boards["V"], run.labels["good"]
    cmd_id = run.api.deploy(board.device_id, label, "auto")
    run.cmd_ids["good"] = cmd_id
    if cmd_id is None:
        result.problems.append("POST deploy was not answered 202")
        return result
    row, held = run.deploy_row(board, cmd_id, lambda d: bool(d.get("is_terminal")), 90)
    deploy = _deploy(row)
    if not held:
        result.problems.append(f"never terminal: {json.dumps(deploy)}")
        return result
    wanted = "rolled_back" if run.broken("selftest-passed") else "confirmed"
    if deploy.get("state") != wanted:
        result.problems.append(f"ended {deploy.get('state')}, wanted {wanted}")
    if deploy.get("detail") != SELFTEST_PASSED:
        result.problems.append(f"detail {deploy.get('detail')!r}, wanted {SELFTEST_PASSED!r}")
    if (problem := check_selftest(deploy, PASSED)) is not None:
        result.problems.append(problem)
    fw = run.wait_fw(board, label)
    if fw != label:
        result.problems.append(f"fw_version {fw}, wanted {label}")
    result.evidence = {
        "cmd_id": cmd_id,
        "state": deploy.get("state"),
        "detail": deploy.get("detail"),
        "selftest": deploy.get("selftest"),
        "steps": step_states(deploy),
        "fw_version": fw,
    }
    return result


def scenario_selftest_failed(run: Run) -> Outcome:
    result = Outcome()
    board, label = run.boards["V"], run.labels["stfail"]
    cmd_id = run.api.deploy(board.device_id, label, "auto")
    run.cmd_ids["stfail"] = cmd_id
    if cmd_id is None:
        result.problems.append("POST deploy was not answered 202")
        return result
    seen: set[str] = set()
    row, held = run.deploy_row(board, cmd_id, lambda d: bool(d.get("is_terminal")), 90, seen)
    deploy = _deploy(row)
    if not held:
        result.problems.append(f"never terminal: {json.dumps(deploy)}")
        return result
    expected = failed_selftest("ota_1")
    if run.broken("selftest-failed"):
        expected = PASSED
    if deploy.get("state") != "rolled_back" or deploy.get("is_terminal") is not True:
        result.problems.append(
            f"ended {deploy.get('state')} is_terminal {deploy.get('is_terminal')}, "
            "wanted rolled_back / true"
        )
    if deploy.get("detail") != failed_detail("ota_1"):
        result.problems.append(
            f"detail {deploy.get('detail')!r}, wanted {failed_detail('ota_1')!r}"
        )
    if (problem := check_selftest(deploy, expected)) is not None:
        result.problems.append(problem)
    steps = step_states(deploy)
    for needed in ("confirming", "rolled_back"):
        if needed not in steps:
            result.problems.append(f"no {needed} step: {steps}")
    if "confirmed" in steps or "confirmed" in seen:
        result.problems.append("the -stfail image was reported confirmed")
    fw = run.wait_fw(board, run.labels["good"])
    if fw != run.labels["good"]:
        result.problems.append(f"fw_version {fw}, wanted back on {run.labels['good']}")
    result.evidence = {
        "cmd_id": cmd_id,
        "state": deploy.get("state"),
        "is_terminal": deploy.get("is_terminal"),
        "detail": deploy.get("detail"),
        "selftest": deploy.get("selftest"),
        "steps": steps,
        "states_seen": sorted(seen),
        "fw_version": fw,
    }
    return result


def scenario_apply_now(run: Run) -> Outcome:
    result = Outcome()
    board, label = run.boards["W"], run.labels["now"]
    run.window(True)
    cmd_id, problem = run.staged(board, "now")
    if cmd_id is None:
        result.problems.append(problem)
        return result
    command_id = run.api.verb(board.device_id, cmd_id, "apply")
    run.command_ids["apply-now"] = command_id
    answer = run.answer(board, command_id)
    wanted = "deferred" if run.broken("apply-now") else "accepted"
    if (answer or {}).get("result") != wanted:
        result.problems.append(f"apply answered {json.dumps(answer)}, wanted {wanted}")
    row, held = run.deploy_row(board, cmd_id, lambda d: bool(d.get("is_terminal")), 60)
    deploy = _deploy(row)
    if not held or deploy.get("state") != "confirmed":
        result.problems.append(f"ended {deploy.get('state')}, wanted confirmed")
    steps = step_states(deploy)
    if "awaiting_safe_window" in steps:
        result.problems.append(f"an open window still waited: {steps}")
    fw = run.wait_fw(board, label)
    if fw != label:
        result.problems.append(f"fw_version {fw}, wanted {label}")
    result.evidence = {
        "cmd_id": cmd_id,
        "command_id": command_id,
        "answer": (answer or {}).get("result"),
        "state": deploy.get("state"),
        "detail": deploy.get("detail"),
        "steps": steps,
        "fw_version": fw,
    }
    return result


def scenario_apply_waits(run: Run) -> Outcome:
    result = Outcome()
    board, label, before = run.boards["W"], run.labels["waits"], run.labels["now"]
    run.window(False)
    cmd_id, problem = run.staged(board, "waits")
    if cmd_id is None:
        result.problems.append(problem)
        return result
    command_id = run.api.verb(board.device_id, cmd_id, "apply")
    run.command_ids["apply-waits"] = command_id
    answer = run.answer(board, command_id)
    wanted = "accepted" if run.broken("apply-waits") else "deferred"
    if (answer or {}).get("result") != wanted:
        result.problems.append(f"apply answered {json.dumps(answer)}, wanted {wanted}")
    row, held = run.deploy_row(
        board, cmd_id, lambda d: d.get("state") == "awaiting_safe_window", 15
    )
    deploy = _deploy(row)
    if not held:
        result.problems.append(f"never awaiting_safe_window: {json.dumps(deploy)}")
        return result
    waiting_detail = deploy.get("detail")
    if waiting_detail != DETAIL_APPLY_WAITS:
        result.problems.append(f"detail {waiting_detail!r}, wanted {DETAIL_APPLY_WAITS!r}")
    samples: list[str] = []
    for index in range(15):
        time.sleep(1)
        sample = run.api.device(board.device_id)
        sampled = _deploy(sample)
        samples.append(f"{sampled.get('state')}/{(sample or {}).get('fw_version')}")
        if sampled.get("state") != "awaiting_safe_window" or sampled.get("cmd_id") != cmd_id:
            result.problems.append(
                f"sample {index}: {sampled.get('cmd_id')} {sampled.get('state')}"
            )
        if (sample or {}).get("fw_version") != before:
            result.problems.append(f"sample {index}: fw_version {(sample or {}).get('fw_version')}")
        if "applying" in step_states(sampled):
            result.problems.append(f"sample {index}: applying before the window opened")
    run.window(True)
    opened = time.monotonic()
    row, held = run.deploy_row(board, cmd_id, lambda d: d.get("state") == "confirmed", 20)
    took = time.monotonic() - opened
    deploy = _deploy(row)
    if not held:
        result.problems.append(
            f"not confirmed within 20 s of the window opening: {deploy.get('state')}"
        )
    if deploy.get("detail") != SELFTEST_PASSED:
        result.problems.append(f"detail {deploy.get('detail')!r}, wanted {SELFTEST_PASSED!r}")
    steps = step_states(deploy)
    if not steps_in_order(steps, WAIT_ORDER):
        result.problems.append(f"steps out of order: {steps}")
    fw = run.wait_fw(board, label)
    if fw != label:
        result.problems.append(f"fw_version {fw}, wanted {label}")
    still = run.api.command(board.device_id, command_id) if command_id else None
    if (still or {}).get("result") != "deferred":
        result.problems.append(f"the command's result became {(still or {}).get('result')}")
    result.evidence = {
        "cmd_id": cmd_id,
        "command_id": command_id,
        "answer": (answer or {}).get("result"),
        "waiting_state": "awaiting_safe_window",
        "waiting_detail": waiting_detail,
        "samples_1s": samples,
        "confirmed_after_s": round(took, 1),
        "state": deploy.get("state"),
        "detail": deploy.get("detail"),
        "steps": steps,
        "fw_version": fw,
        "command_result_after": (still or {}).get("result"),
    }
    return result


def scenario_cancel_waiting(run: Run) -> Outcome:
    result = Outcome()
    board, before = run.boards["W"], run.labels["waits"]
    run.window(False)
    cmd_id, problem = run.staged(board, "cancel")
    if cmd_id is None:
        result.problems.append(problem)
        return result
    apply_id = run.api.verb(board.device_id, cmd_id, "apply")
    run.command_ids["cancel-waiting-apply"] = apply_id
    answer = run.answer(board, apply_id)
    if (answer or {}).get("result") != "deferred":
        result.problems.append(f"apply answered {json.dumps(answer)}, wanted deferred")
    run.deploy_row(board, cmd_id, lambda d: d.get("state") == "awaiting_safe_window", 15)
    cancel_id = run.api.verb(board.device_id, cmd_id, "cancel")
    run.command_ids["cancel-waiting-cancel"] = cancel_id
    cancelled = run.answer(board, cancel_id)
    if (cancelled or {}).get("result") != "accepted":
        result.problems.append(f"cancel answered {json.dumps(cancelled)}, wanted accepted")
    row, held = run.deploy_row(
        board,
        cmd_id,
        lambda d: d.get("state") == "cancelled" and d.get("is_terminal") is True,
        30,
    )
    if not held:
        result.problems.append(f"not cancelled: {json.dumps(_deploy(row))}")
    run.window(True)
    time.sleep(8)
    after = run.api.device(board.device_id)
    deploy = _deploy(after)
    wanted = "confirmed" if run.broken("cancel-waiting") else "cancelled"
    if deploy.get("cmd_id") != cmd_id or deploy.get("state") != wanted:
        result.problems.append(
            f"8 s after the window opened: {deploy.get('cmd_id')} {deploy.get('state')}, "
            f"wanted {wanted}"
        )
    if (after or {}).get("fw_version") != before:
        result.problems.append(f"fw_version {(after or {}).get('fw_version')}, wanted {before}")
    if "applying" in step_states(deploy):
        result.problems.append("a cancelled deferred apply was released (applying)")
    result.evidence = {
        "cmd_id": cmd_id,
        "apply_command_id": apply_id,
        "apply_answer": (answer or {}).get("result"),
        "cancel_command_id": cancel_id,
        "cancel_answer": (cancelled or {}).get("result"),
        "state_after_window": deploy.get("state"),
        "is_terminal": deploy.get("is_terminal"),
        "steps": step_states(deploy),
        "fw_version": (after or {}).get("fw_version"),
    }
    return result


def scenario_auto_waits(run: Run) -> Outcome:
    result = Outcome()
    board, label, before = run.boards["W"], run.labels["auto"], run.labels["waits"]
    run.window(False)
    cmd_id = run.api.deploy(board.device_id, label, "auto")
    run.cmd_ids["auto"] = cmd_id
    if cmd_id is None:
        result.problems.append("POST deploy was not answered 202")
        return result
    row, held = run.deploy_row(
        board, cmd_id, lambda d: d.get("state") == "awaiting_safe_window", 60
    )
    deploy = _deploy(row)
    if not held:
        result.problems.append(f"never parked: {json.dumps(deploy)}")
        return result
    waiting_detail = deploy.get("detail")
    wanted = DETAIL_APPLY_WAITS if run.broken("auto-waits") else None
    if waiting_detail != wanted:
        result.problems.append(f"detail {waiting_detail!r}, wanted {wanted!r}")
    samples: list[str] = []
    for index in range(10):
        time.sleep(1)
        sample = run.api.device(board.device_id)
        sampled = _deploy(sample)
        samples.append(f"{sampled.get('state')}/{(sample or {}).get('fw_version')}")
        if (
            sampled.get("state") != "awaiting_safe_window"
            or (sample or {}).get("fw_version") != before
        ):
            result.problems.append(f"sample {index}: {samples[-1]}")
    run.window(True)
    row, held = run.deploy_row(board, cmd_id, lambda d: d.get("state") == "confirmed", 30)
    deploy = _deploy(row)
    if not held:
        result.problems.append(f"not confirmed after the window opened: {deploy.get('state')}")
    fw = run.wait_fw(board, label)
    if fw != label:
        result.problems.append(f"fw_version {fw}, wanted {label}")
    result.evidence = {
        "cmd_id": cmd_id,
        "waiting_detail": waiting_detail,
        "samples_1s": samples,
        "state": deploy.get("state"),
        "steps": step_states(deploy),
        "fw_version": fw,
    }
    return result


def scenario_traps(run: Run) -> Outcome:
    result = Outcome()
    logs = {b.key: b.log.read_text(errors="replace") for b in run.boards.values()}
    output = "\n".join(run.output)
    found: dict[str, list[str]] = {
        f"sim-{key}.log": find_secrets(text, run.password) for key, text in logs.items()
    }
    found["harness output"] = find_secrets(output, run.password)
    if run.broken("traps"):
        if "the admin password" not in found["harness output"]:
            result.problems.append("the password is not in the harness output (break)")
    for where, secrets in found.items():
        if secrets:
            result.problems.append(f"{where}: {', '.join(sorted(set(secrets)))}")
    if URL_RE.search(output):
        result.problems.append("a URL in the harness output")
    for key, text in logs.items():
        if re.search(r"https?://[^\s…]*\?", text):
            result.problems.append(f"sim-{key}.log: a URL with a query string")
    result.evidence = {
        "scanned": [*sorted(f"sim-{k}.log" for k in logs), "harness output"],
        "log_bytes": {k: len(v) for k, v in logs.items()},
    }
    return result


SCENARIO_FUNCTIONS: dict[str, Callable[[Run], Outcome]] = {
    "selftest-passed": scenario_selftest_passed,
    "selftest-failed": scenario_selftest_failed,
    "apply-now": scenario_apply_now,
    "apply-waits": scenario_apply_waits,
    "cancel-waiting": scenario_cancel_waiting,
    "auto-waits": scenario_auto_waits,
    "traps": scenario_traps,
}


def wait_online(run: Run, board: Board) -> bool:
    """The board's row says online and announces every R5 capability."""
    _, online = run.until(
        lambda: run.api.device(board.device_id),
        lambda row: (
            isinstance(row, dict)
            and row.get("online") is True
            and set(R5_CAPABILITIES) <= set(row.get("capabilities") or [])
        ),
        ONLINE_TIMEOUT_S,
    )
    return online


def setup(run: Run) -> None:
    """The stack, the login, the uploads and two boards online with the R5 capabilities."""
    try:
        status, _, _ = run.api.http("GET", "/v1/healthz")
    except OSError as error:
        raise SetupError(f"the API does not answer ({error}) — run: just up") from None
    if status != 200:
        raise SetupError(f"GET /v1/healthz answered {status} — run: just up")
    run.api.login(run.password)
    run.say("login    admin session acquired (never printed)")
    head = APP_HEAD.read_bytes()
    for key, label in run.labels.items():
        if key == "baseline":
            continue
        status = run.api.upload(label, build_image(head, label))
        if status not in (200, 201):
            raise SetupError(f"uploading {label} answered {status}")
    uploaded = [label for key, label in run.labels.items() if key != "baseline"]
    run.say(f"upload   {len(uploaded)} labels ({', '.join(uploaded)})")
    run.window(False)
    for board in run.boards.values():
        start_board(board, run.api.base, run.labels["baseline"], run.password)
        run.say(f"board    {board.key} = {board.name} -> {board.device_id}")
    for board in run.boards.values():
        if not wait_online(run, board):
            raise SetupError(
                f"board {board.key} ({board.device_id}) never came online with "
                f"{' '.join(R5_CAPABILITIES)} — see {board.log}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    base = args[0] if args else f"http://localhost:{os.environ.get('FF_HTTP_PORT', '8088')}"
    out = Path(args[1] if len(args) > 1 else DEFAULT_OUT)
    breaking = os.environ.get("E2E_BREAK") or None
    if breaking is not None and breaking not in SCENARIOS:
        print(f"SETUP FAILED: E2E_BREAK={breaking} is not one of {', '.join(SCENARIOS)}")
        return 2
    password = read_password()
    if not password:
        print("SETUP FAILED: no FF_ADMIN_PASSWORD in the environment or in the repo-root .env")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    tag = format(int(time.time()), "x")
    boards = {
        "V": Board("V", f"sw{tag}v", [], out / "sim-V.log"),
        "W": Board(
            "W", f"sw{tag}w", ["--safe-window", f"file:{out / SAFE_FILE}"], out / "sim-W.log"
        ),
    }
    run = Run(
        api=Api(base.rstrip("/")),
        out=out,
        password=password,
        labels=labels_for(tag),
        boards=boards,
        breaking=breaking,
    )
    if breaking:
        run.say(f"break    E2E_BREAK={breaking}: that scenario must FAIL")
    results: dict[str, Outcome] = {}
    try:
        try:
            setup(run)
        except (SetupError, OSError) as error:
            print(f"SETUP FAILED: {error}")
            return 2
        for scenario in SCENARIOS:
            try:
                outcome = SCENARIO_FUNCTIONS[scenario](run)
            except (OSError, ValueError, KeyError) as error:
                outcome = Outcome(problems=[f"{type(error).__name__}: {error}"])
            results[scenario] = outcome
            verdict = "PASS" if not outcome.problems else "FAIL"
            run.say(f"{verdict} {scenario}")
            for problem in outcome.problems:
                run.say(f"     - {problem}")
            for key, value in outcome.evidence.items():
                run.say(f"     {key}: {json.dumps(value)}")
    finally:
        stop_boards(list(boards.values()))
    passed = sum(1 for outcome in results.values() if not outcome.problems)
    run.say(f"{passed}/{len(SCENARIOS)} pass")
    (out / "run.json").write_text(
        json.dumps(
            {
                "run": tag,
                "break": breaking,
                "labels": run.labels,
                "boards": {
                    b.key: {"name": b.name, "device_id": b.device_id} for b in boards.values()
                },
                "cmd_ids": run.cmd_ids,
                "command_ids": run.command_ids,
                "scenarios": {
                    name: {
                        "pass": not outcome.problems,
                        "problems": outcome.problems,
                        "evidence": outcome.evidence,
                    }
                    for name, outcome in results.items()
                },
                "passed": passed,
                "total": len(SCENARIOS),
            },
            indent=2,
        )
        + "\n"
    )
    return 0 if passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    sys.exit(main())
