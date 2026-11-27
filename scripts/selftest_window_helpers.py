"""Pure helpers, HTTP API client, simulator process manager, and `Run` state for `selftest_window_e2e.py`.

Stdlib-only: never imports `fleetforge`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
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
DEFAULT_OUT = "/tmp/ff-r5-test-1"  # noqa: S108
TARGET = "esp32s3"
LAYOUT = "ab-4m-v1"

VERSION_AT = 0x30
VERSION_LEN = 32
LIB_MARKER_MAGIC = bytes.fromhex("14a948d18f12cfdd46464f54414c4942")
LIB_MARKER_LEN = 64
LIB_MARKER_VERSION = b"0.0.0-e2e"
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

TOKEN_RE = re.compile(r"ff[ae]_[A-Za-z0-9_\-]{16,}")
URL_RE = re.compile(r"https?://")

ONLINE_TIMEOUT_S = 90.0
ANSWER_TIMEOUT_S = 30.0
POLL_S = 0.5


class SetupError(Exception):
    """The stack, the login or the boards: exit 2."""


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
    """$FF_ADMIN_PASSWORD, else the repo-root .env line."""
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
        request = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
        if data is not None:
            request.add_header("content-type", content_type)
        if self.bearer is not None:
            request.add_header("Authorization", f"Bearer {self.bearer}")
        host = urllib.parse.urlsplit(self.base).hostname or ""
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if host in ("127.0.0.1", "localhost", "::1")
            else urllib.request.build_opener()
        )
        try:
            with opener.open(request, timeout=30) as response:  # noqa: S310
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


def simulator_argv(board: Board, base: str, baseline: str) -> list[str]:
    sim_python = os.environ.get("FF_SIM_PYTHON")
    prefix = [sim_python] if sim_python else ["uv", "run", "python"]
    return [
        *prefix,
        "-m",
        "fleetforge.simulator",
        "fleet",
        "--count=1",
        f"--prefix={board.prefix}",
        f"--platform-type={TARGET}",
        f"--partition-layout={LAYOUT}",
        f"--fw-version={baseline}",
        "--heartbeat-interval=5",
        "--confirm-timeout=20",
        "--duration=900",
        f"--api-base={base}",
        "--capabilities",
        *R5_CAPABILITIES,
        f"--selftest-fail-marker={STFAIL_MARKER}",
        *board.extra,
    ]


def start_board(board: Board, base: str, baseline: str, password: str) -> None:
    argv = simulator_argv(board, base, baseline)
    env = {**os.environ, "PYTHONPATH": "src", "FF_ADMIN_PASSWORD": password}
    with board.log.open("wb") as log:
        board.process = subprocess.Popen(  # noqa: S603
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
