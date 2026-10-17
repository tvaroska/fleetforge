#!/usr/bin/env python3
"""A failing self-test rolls back; `apply` waits for the safe window — simulated (R5-test-1).

Two simulated boards (`fleetforge.simulator fleet`, child processes) through the REAL dev
stack: the api, the broker, the ingestor and the database. No browser and no QEMU (the real
agent is `just lib-quickstart`'s phases 2d/2e/2f). Stdlib only, like `lib_quickstart.py`: it
never imports the project.

    V   announces ota apply cancel rollback reboot selftest; an image whose version carries
        `-stfail` fails its self-test (`--selftest-fail-marker=-stfail`); window `auto`
    W   the same with `--safe-window file:<out>/W.safe`: safe while that file exists, so this
        script opens the window by creating it (no timer to race)

Scenarios, in order (each sets W's window explicitly first):

    selftest-passed  V: deploy a good build (`apply: auto`) -> `confirmed`, detail exactly
                     `selftest passed`, deploy.selftest == passed
    selftest-failed  V: deploy the `-stfail` build -> `rolled_back` with the exact detail
                     `selftest failed: simulated self-test failure; returned to ota_1`,
                     deploy.selftest == failed/reason/ota_1, never `confirmed`, fw back
    apply-now        W, window open: `on_command` -> `staged` -> apply -> `accepted` ->
                     `confirmed`, never `awaiting_safe_window`
    apply-waits      W, window closed: `on_command` -> `staged` -> apply -> `deferred`,
                     `awaiting_safe_window` / `apply waits for a safe window` for 15 samples
                     1 s apart with fw unchanged; open the window -> `confirmed` within 20 s,
                     steps staged < awaiting_safe_window < applying < rebooting < confirming
                     < confirmed, the command's result still `deferred`
    cancel-waiting   W, window closed: a deferred apply, then cancel -> `cancelled`; open the
                     window, 8 s later still `cancelled`, fw unchanged, no `applying`
    auto-waits       W, window closed: `apply: auto` parks at `awaiting_safe_window` (detail
                     null) for 10 s; open the window -> `confirmed`
    traps            the password and every ffe_/ffa_ token absent from the simulator logs
                     and this script's output; no URL in this script's output

    python3 -u scripts/selftest_window_e2e.py [base=http://localhost:${FF_HTTP_PORT:-8088}]
                                              [out=/tmp/ff-r5-test-1]

or `just selftest-window-e2e`. The admin password comes from `$FF_ADMIN_PASSWORD` or the
repo-root `.env`; it is never printed and never put in an argv (the simulators get it through
their environment). Every run uses fresh board prefixes and labels and leaves two boards and
seven labels in the dev database; it deletes nothing. Evidence: `<out>/run.json` and the
simulator logs `<out>/sim-V.log`, `<out>/sim-W.log`.

Exit 0 all pass, 1 a scenario failed, 2 setup failed (stack down, login refused, the boards
never came online). `E2E_BREAK=<scenario id>` flips that scenario's primary expectation, to
prove the harness can fail (the vacuity check every `*-e2e` harness carries).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_HEAD = REPO_ROOT / "tests" / "fixtures" / "firmware" / "esp32s3.app.head.bin"
DEFAULT_OUT = "/tmp/ff-r5-test-1"
TARGET = "esp32s3"
LAYOUT = "ab-4m-v1"

# The app descriptor's version field (esp_app_desc_t), as `commands-e2e.mjs` writes it.
VERSION_AT = 0x30
VERSION_LEN = 32
# One valid library marker (`fleetforge.lib_marker`), so the pre-check does not gate on
# `no_library_marker`: magic, format byte 1 at 16, the lib version at 20.
LIB_MARKER_MAGIC = bytes.fromhex("14a948d18f12cfdd46464f54414c4942")
LIB_MARKER_LEN = 64
LIB_MARKER_VERSION = b"0.0.0-e2e"
# Gating warning codes named one by one; a listed code that is not raised is a no-op.
OVERRIDES = ["rollback_incapable", "no_library_marker"]

R5_CAPABILITIES = ("ota", "apply", "cancel", "rollback", "reboot", "selftest")
STFAIL_MARKER = "-stfail"
SAFE_FILE = "W.safe"

SCENARIOS = (
    "selftest-passed",
    "selftest-failed",
    "apply-now",
    "apply-waits",
    "cancel-waiting",
    "auto-waits",
    "traps",
)

# The simulator's words (`fleetforge.simulator.device`), retyped: this script imports nothing.
SELFTEST_PASSED = "selftest passed"
SELFTEST_FAIL_REASON = "simulated self-test failure"
DETAIL_APPLY_WAITS = "apply waits for a safe window"
WAIT_ORDER = (
    "staged",
    "awaiting_safe_window",
    "applying",
    "rebooting",
    "confirming",
    "confirmed",
)
PASSED = {"outcome": "passed", "reason": None, "returned_to": None}

# A token is its prefix plus at least 16 URL-safe characters; "the ffa_ token is never
# printed" (the simulator's own words) is not one.
TOKEN_RE = re.compile(r"ff[ae]_[A-Za-z0-9_\-]{16,}")
URL_RE = re.compile(r"https?://")

ONLINE_TIMEOUT_S = 90.0
ANSWER_TIMEOUT_S = 30.0
POLL_S = 0.5


class SetupError(Exception):
    """The stack, the login or the boards: exit 2."""


# ── Pure helpers (unit-tested in tests/test_selftest_window_e2e.py) ─────────────────────


def derive_device_id(name: str) -> str:
    """`fleetforge.simulator.device.derive_device_id`, retyped (a parity test pins it)."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return bytes([(digest[0] & 0xFE) | 0x02, *digest[1:6]]).hex()


def lib_marker() -> bytes:
    marker = bytearray(LIB_MARKER_LEN)
    marker[: len(LIB_MARKER_MAGIC)] = LIB_MARKER_MAGIC
    marker[16] = 1
    marker[20 : 20 + len(LIB_MARKER_VERSION)] = LIB_MARKER_VERSION
    return bytes(marker)


def build_image(head: bytes, label: str) -> bytes:
    """The app head with `label` in the descriptor, plus one library marker."""
    encoded = label.encode("utf-8")
    if len(encoded) >= VERSION_LEN:
        raise ValueError(f"label {label} does not fit the {VERSION_LEN}-byte version field")
    image = bytearray(head)
    image[VERSION_AT : VERSION_AT + VERSION_LEN] = encoded.ljust(VERSION_LEN, b"\0")
    return bytes(image) + lib_marker()


def labels_for(run: str) -> dict[str, str]:
    """Run-unique labels: a `(target, version)` label is a promise about bytes."""
    return {
        "baseline": f"1.0.0-sw{run}",
        "good": f"1.1.0-sw{run}",
        "stfail": f"1.2.0-sw{run}{STFAIL_MARKER}",
        "now": f"1.3.0-sw{run}",
        "waits": f"1.4.0-sw{run}",
        "cancel": f"1.5.0-sw{run}",
        "auto": f"1.6.0-sw{run}",
    }


def step_states(deploy: dict[str, Any] | None) -> list[str]:
    if not deploy:
        return []
    return [str(s.get("state")) for s in deploy.get("steps") or [] if isinstance(s, dict)]


def steps_in_order(steps: Sequence[str], names: Sequence[str]) -> bool:
    """Every name is in `steps`, and their first occurrences are strictly in this order."""
    if any(name not in steps for name in names):
        return False
    firsts = [steps.index(name) for name in names]
    return all(a < b for a, b in zip(firsts, firsts[1:], strict=False))


def check_selftest(deploy: dict[str, Any] | None, expected: dict[str, Any] | None) -> str | None:
    """None when `deploy.selftest` is exactly `expected`, else what is wrong."""
    actual = (deploy or {}).get("selftest")
    if actual == expected:
        return None
    return f"deploy.selftest is {json.dumps(actual)}, wanted {json.dumps(expected)}"


def failed_selftest(slot: str) -> dict[str, Any]:
    return {"outcome": "failed", "reason": SELFTEST_FAIL_REASON, "returned_to": slot}


def failed_detail(slot: str) -> str:
    return f"selftest failed: {SELFTEST_FAIL_REASON}; returned to {slot}"


def find_secrets(text: str, password: str) -> list[str]:
    """What must never be in a log: the password and any enrollment/admin token."""
    found: list[str] = []
    if password and password in text:
        found.append("the admin password")
    found.extend(f"a {m.group(0)[:4]} token" for m in TOKEN_RE.finditer(text))
    return found


def read_password() -> str | None:
    """$FF_ADMIN_PASSWORD, else the repo-root .env line (the update-flow-e2e.mjs rule)."""
    value = os.environ.get("FF_ADMIN_PASSWORD")
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("FF_ADMIN_PASSWORD="):
                raw = line[len("FF_ADMIN_PASSWORD=") :].strip()
                return re.sub(r"^(['\"])(.*)\1$", r"\2", raw)
    return None


# ── The API ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Api:
    base: str
    bearer: str | None = None

    def http(
        self,
        method: str,
        path: str,
        body: bytes | dict[str, Any] | None = None,
        *,
        content_type: str = "application/json",
    ) -> tuple[int, Any, Any]:
        """(status, parsed JSON or None, headers). Never raises on 4xx/5xx."""
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("content-type", content_type)
        if self.bearer is not None:
            request.add_header("Authorization", f"Bearer {self.bearer}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw, status, headers = response.read(), response.status, response.headers
        except urllib.error.HTTPError as error:
            raw, status, headers = error.read(), error.code, error.headers
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, parsed, headers

    def login(self, password: str) -> None:
        status, _, headers = self.http("POST", "/v1/auth/login", {"password": password})
        if status != 200:
            raise SetupError(f"POST /v1/auth/login answered {status} (wrong FF_ADMIN_PASSWORD?)")
        for cookie in headers.get_all("Set-Cookie") or []:
            match = re.match(r"\s*ff_session=([^;]+)", cookie)
            if match:
                self.bearer = match.group(1)
        if self.bearer is None:
            raise SetupError("login set no ff_session cookie")

    def upload(self, label: str, image: bytes) -> int:
        query = urllib.parse.urlencode(
            {"target": TARGET, "version": label, "partition_layout": LAYOUT}
        )
        status, _, _ = self.http(
            "POST", f"/v1/artifact?{query}", image, content_type="application/octet-stream"
        )
        return status

    def device(self, device_id: str) -> dict[str, Any] | None:
        status, body, _ = self.http("GET", "/v1/devices")
        if status != 200 or not isinstance(body, dict):
            return None
        for row in body.get("devices") or []:
            if isinstance(row, dict) and row.get("device_id") == device_id:
                return row
        return None

    def deploy(self, device_id: str, label: str, apply: str) -> str | None:
        status, body, _ = self.http(
            "POST",
            f"/v1/devices/{device_id}/deploy",
            {"version": label, "apply": apply, "override": OVERRIDES},
        )
        if status != 202 or not isinstance(body, dict):
            return None
        return str(body.get("cmd_id")) if body.get("cmd_id") else None

    def verb(self, device_id: str, cmd_id: str, kind: str) -> str | None:
        status, body, _ = self.http("POST", f"/v1/devices/{device_id}/deploys/{cmd_id}/{kind}")
        if status != 202 or not isinstance(body, dict):
            return None
        return str(body.get("command_id")) if body.get("command_id") else None

    def command(self, device_id: str, command_id: str) -> dict[str, Any] | None:
        status, body, _ = self.http("GET", f"/v1/devices/{device_id}/commands/{command_id}")
        return body if status == 200 and isinstance(body, dict) else None


# ── The boards ───────────────────────────────────────────────────────────────────────────


@dataclass
class Board:
    key: str
    prefix: str
    extra: list[str]
    log: Path
    process: subprocess.Popen[bytes] | None = None

    @property
    def name(self) -> str:
        return f"{self.prefix}-01"

    @property
    def device_id(self) -> str:
        return derive_device_id(self.name)


def start_board(board: Board, base: str, baseline: str, password: str) -> None:
    argv = [
        "uv",
        "run",
        "python",
        "-m",
        "fleetforge.simulator",
        "fleet",
        "--count",
        "1",
        "--prefix",
        board.prefix,
        "--platform-type",
        TARGET,
        "--partition-layout",
        LAYOUT,
        "--fw-version",
        baseline,
        "--heartbeat-interval",
        "5",
        "--confirm-timeout",
        "20",
        "--duration",
        "900",  # the safety net: an orphaned simulator exits on its own
        "--api-base",
        base,
        "--capabilities",
        *R5_CAPABILITIES,
        f"--selftest-fail-marker={STFAIL_MARKER}",
        *board.extra,
    ]
    # The password goes through the environment, never argv.
    env = {**os.environ, "PYTHONPATH": "src", "FF_ADMIN_PASSWORD": password}
    with board.log.open("wb") as log:
        board.process = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def stop_boards(boards: Sequence[Board]) -> None:
    """SIGINT the process group (`uv run` forks the interpreter), SIGKILL after 5 s."""
    running = [b.process for b in boards if b.process is not None and b.process.poll() is None]
    for process in running:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    for process in running:
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


# ── The run ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    problems: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Run:
    api: Api
    out: Path
    password: str
    labels: dict[str, str]
    boards: dict[str, Board]
    breaking: str | None
    output: list[str] = field(default_factory=list)
    cmd_ids: dict[str, str | None] = field(default_factory=dict)
    command_ids: dict[str, str | None] = field(default_factory=dict)

    def say(self, line: str) -> None:
        self.output.append(line)
        print(line, flush=True)

    def broken(self, scenario: str) -> bool:
        return self.breaking == scenario

    @property
    def safe_file(self) -> Path:
        return self.out / SAFE_FILE

    def window(self, open_: bool) -> None:
        if open_:
            self.safe_file.touch()
        else:
            self.safe_file.unlink(missing_ok=True)

    def until(
        self, fetch: Callable[[], Any], ok: Callable[[Any], bool], timeout: float
    ) -> tuple[Any, bool]:
        """Poll `fetch` until `ok`; (last value, whether it held)."""
        deadline = time.monotonic() + timeout
        while True:
            value = fetch()
            if ok(value):
                return value, True
            if time.monotonic() > deadline:
                return value, False
            time.sleep(POLL_S)

    def deploy_row(
        self,
        board: Board,
        cmd_id: str,
        ok: Callable[[dict[str, Any]], bool],
        timeout: float,
        seen: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """The device row once its deploy is `cmd_id` and `ok(deploy)`; records every state."""

        def fetch() -> dict[str, Any] | None:
            row = self.api.device(board.device_id)
            deploy = (row or {}).get("deploy") or {}
            if seen is not None and deploy.get("cmd_id") == cmd_id:
                seen.update(step_states(deploy))
                seen.add(str(deploy.get("state")))
            return row

        def holds(row: dict[str, Any] | None) -> bool:
            deploy = (row or {}).get("deploy") or {}
            return deploy.get("cmd_id") == cmd_id and ok(deploy)

        row, held = self.until(fetch, holds, timeout)
        return row, held

    def answer(self, board: Board, command_id: str | None) -> dict[str, Any] | None:
        if command_id is None:
            return None
        command, _ = self.until(
            lambda: self.api.command(board.device_id, command_id),
            lambda c: isinstance(c, dict) and c.get("result") is not None,
            ANSWER_TIMEOUT_S,
        )
        return command if isinstance(command, dict) else None

    def fw(self, board: Board) -> str | None:
        row = self.api.device(board.device_id)
        return None if row is None else row.get("fw_version")

    def wait_fw(self, board: Board, version: str, timeout: float = 15.0) -> str | None:
        fw, _ = self.until(lambda: self.fw(board), lambda v: v == version, timeout)
        return fw if isinstance(fw, str) or fw is None else str(fw)

    def staged(self, board: Board, key: str, apply: str = "on_command") -> tuple[str | None, str]:
        """Deploy `labels[key]` and wait for `staged`; (cmd_id or None, problem text)."""
        cmd_id = self.api.deploy(board.device_id, self.labels[key], apply)
        self.cmd_ids[key] = cmd_id
        if cmd_id is None:
            return None, f"POST deploy {self.labels[key]} was not answered 202"
        row, held = self.deploy_row(board, cmd_id, lambda d: d.get("state") == "staged", 60)
        if not held:
            return None, f"never staged: {json.dumps((row or {}).get('deploy'))}"
        return cmd_id, ""


def _deploy(row: dict[str, Any] | None) -> dict[str, Any]:
    return (row or {}).get("deploy") or {}


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
    # A fresh board starts in ota_0; selftest-passed moved it to ota_1, the stfail image ran
    # in ota_0 and the board returned to ota_1.
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
        # Flipped: demand the password in the output, which must never be there.
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
