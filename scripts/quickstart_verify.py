"""Pure verification helpers, README block parsers, and constants for `lib_quickstart.py`.

Stdlib-only: never imports `fleetforge`. Shared by `scripts/lib_quickstart.py` and
`scripts/quickstart_steps.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = "agent/components/fleetforge/examples"
ARDUINO_DIR = f"{EXAMPLES}/Basic"
IDF_DIR = f"{EXAMPLES}/basic_idf"
ARDUINO_README = f"{ARDUINO_DIR}/README.md"
IDF_README = f"{IDF_DIR}/README.md"
SKETCH = f"{ARDUINO_DIR}/Basic.ino"

MARKER = "# quickstart: "
REQUIRED_BLOCKS: dict[str, tuple[str, ...]] = {
    ARDUINO_README: ("arduino-build", "pio-own-project", "arduino-edit", "arduino-build-b"),
    IDF_README: ("idf-build", "idf-build-s3"),
}

OTA_SLOT_SIZE = 1966080
ARDUINO_LAYOUT = "ab-4m-arduino-v1"
DEVICE_ID = "000000000000"
QEMU_CONTAINER = "ff-qemu-esp32"
API_CONTAINER = "fleetforge-api"
VERSION_A = "1.0.0"

EFUSE_BURN_OPTIONS = (
    "CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK",
    "CONFIG_APP_ANTI_ROLLBACK",
    "CONFIG_SECURE_BOOT",
    "CONFIG_SECURE_BOOT_V1_ENABLED",
    "CONFIG_SECURE_BOOT_V2_ENABLED",
    "CONFIG_SECURE_FLASH_ENC_ENABLED",
    "CONFIG_FLASH_ENCRYPTION_ENABLED",
)
PINNED_PLATFORM_VERSION = "55.03.312"
PIO_ENVS = ("esp32", "esp32s3")
PIO_ROW = re.compile(r"^(\S+)\s+(SUCCESS|FAILED|IGNORED)\s+\d\d:\d\d:\d\d", re.M)
OUR_PATHS = re.compile(r"components/fleetforge|/examples/|Basic\.ino|basic_idf")

Row = tuple[str, str, str, int, int]

ROLLBACK_TEST_FLAGS = "-DFF_ROLLBACK_TEST=1"
ROLLBACK_HOOK_NEEDLE = b"FF_ROLLBACK_TEST: ignoring the announce ack"
ROLLBACK_MESSAGE = "BAD"
STAGED_LINE = r"update {cmd}: (ota_\d) is staged and bootable"

SELFTEST_FAIL_REASON = "i2c sensor 0x48 did not answer; tried 3 times"
SELFTEST_STUB = "static void selfTest() {}"
SELFTEST_FAIL_STUB = (
    f'static void selfTest() {{ Fleetforge.selfTestDone(false, "{SELFTEST_FAIL_REASON}"); }}'
)
SELFTEST_LOOP_ANSWER = "  Fleetforge.selfTestDone(true);"
SELFTEST_LOOP_SILENCED = "  // build T: loop() never answers the self-test;"
SELFTEST_FAIL_MESSAGE = "FAIL"
SELFTEST_TIMEOUT_MESSAGE = "WAIT"
SELFTEST_RUN_LINE = "running the custom self-test"
SELFTEST_REASON_MAX = 63
DETAIL_MAX = 63
CONFIRM_TIMEOUT_S = 300

SAFE_WINDOW_STUB = "static bool safeNow() { return betweenMessages; }"
SAFE_WINDOW_CLOCK = """static bool safeNow() {  // build W: a clock, not the message gap: unsafe 60 s of every 80 s
  static volatile int said = -1;
  const int safe = millis() % 80000UL >= 60000UL;
  if (safe != said) {
    said = safe;
    printf("window: %s at millis %lu\\n", safe ? "safe" : "unsafe", (unsigned long)millis());
  }
  return safe;
}"""
LOOP_HEAD = "void loop() {\n"
LOOP_HEAD_SAYS = "void loop() {\n  (void)safeNow();  // build W: say when the window flips\n"
WINDOW_MESSAGE = "WIN"
WINDOW_PERIOD_MS = 80000
WINDOW_UNSAFE_MS = 60000
COMMAND_REBOOT_DRAIN_MS = 1500
APPLY_WAITS_LINE = "apply {id}: the staged update {cmd} waits for a safe window"
REBOOT_WAITS_LINE = "command {id}: the reboot waits for a safe window"
RELEASING_LINE = "releasing the staged update"
REBOOTING_FOR_LINE = "rebooting for command {id}"
DETAIL_APPLY_WAITS = "apply waits for a safe window"
WINDOW_STEP_ORDER = (
    "staged",
    "awaiting_safe_window",
    "applying",
    "rebooting",
    "confirming",
    "confirmed",
)


class QuickstartError(Exception):
    """A check failed: exit 1."""


class SetupError(QuickstartError):
    """The environment is not ready (stack down, origins wrong, a board running): exit 2."""


def extract_blocks(text: str, source: str = "README") -> dict[str, str]:
    """Every fenced block whose first line is `# quickstart: <name>`, by name, body only."""
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        fence = re.match(r"^(\s*)(```+|~~~+)", lines[index])
        if fence is None:
            index += 1
            continue
        closing = fence.group(2)
        body: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].strip().startswith(closing):
            body.append(lines[index])
            index += 1
        index += 1
        if body and body[0].strip().startswith(MARKER):
            name = body[0].strip()[len(MARKER) :].strip()
            if name in blocks:
                raise QuickstartError(f"{source}: `{MARKER}{name}` appears twice")
            blocks[name] = "\n".join(body[1:]) + "\n"
    return blocks


def require_blocks(text: str, names: Sequence[str], source: str) -> dict[str, str]:
    """The named blocks of one README; a missing one is a hard failure, naming it."""
    blocks = extract_blocks(text, source)
    missing = [name for name in names if name not in blocks]
    if missing:
        raise QuickstartError(
            f"{source} has no `{MARKER}{missing[0]}` block"
            + (f" (also missing: {', '.join(missing[1:])})" if missing[1:] else "")
            + " — the scripted quickstart runs the README's blocks and nothing else"
        )
    return {name: blocks[name] for name in names}


@dataclass
class Run:
    """One run's state: where things are, what passed, everything printed."""

    tree: Path
    work: Path
    logs: Path
    base: str
    guest_base: str
    mqtt_uri: str
    results: list[tuple[str, str]] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    token: str | None = None
    token_id: str | None = None
    token_burned: bool = False
    qemu: subprocess.Popen[bytes] | None = None
    qemu_logs: list[Path] = field(default_factory=list)
    bearer: str | None = None
    block_env: dict[str, str] | None = None

    def say(self, line: str = "") -> None:
        self.transcript.append(line)
        print(line, flush=True)

    def passed(self, check: str, observed: str) -> None:
        self.results.append((check, observed))
        self.say(f"  PASS  {check}: {observed}")


@dataclass
class Bundles:
    """The QEMU bundles. R, F and T are None under `--only-safe-window` (dev only)."""

    a: Path
    b: Path
    w: Path
    r: Path | None = None
    f: Path | None = None
    t: Path | None = None


def _need(bundle: Path | None, name: str) -> Path:
    if bundle is None:
        raise QuickstartError(f"bundle {name} was not built (--only-safe-window)")
    return bundle


def _number(text: str) -> int:
    multiplier = {"K": 1024, "M": 1024 * 1024}.get(text[-1:].upper(), 1)
    digits = text[:-1] if multiplier != 1 else text
    return int(digits, 0) * multiplier


def parse_rows(text: str) -> list[Row]:
    """An ESP-IDF partition CSV as (name, type, subtype, offset, size); sizes may be 24K/1M."""
    rows: list[Row] = []
    for line in text.splitlines():
        stripped = line.strip()
        fields = [part.strip() for part in stripped.split(",")]
        if stripped.startswith("#") or len(fields) < 5:
            continue
        subtype = fields[2]
        if subtype.isdigit() or subtype.lower().startswith("0x"):
            subtype = f"0x{int(subtype, 0):02x}"
        rows.append((fields[0], fields[1], subtype, _number(fields[3]), _number(fields[4])))
    return rows


def app_version(head: bytes) -> str:
    """`esp_app_desc_t.version` from the first bytes of an app image (header 24 + segment 8)."""
    desc = head[32:]
    if desc[:4] != bytes.fromhex("3254cdab"):
        raise QuickstartError("no esp_app_desc_t magic at offset 32 of the app image")
    return desc[16:48].split(b"\0", 1)[0].decode("ascii", "replace")


def efuse_burns(sdkconfig_lines: Sequence[str]) -> list[str]:
    """The lines of a resolved sdkconfig that turn on a one-way eFuse burn (exact names)."""
    return [
        line.strip()
        for line in sdkconfig_lines
        if line.strip().split("=", 1)[0] in EFUSE_BURN_OPTIONS and line.strip().endswith("=y")
    ]


def our_warnings(output: str) -> list[str]:
    return [line for line in output.splitlines() if "warning:" in line and OUR_PATHS.search(line)]


def idf_image() -> str:
    """The digest-pinned image, read from the justfile rather than copied."""
    match = re.search(r'^idf_image := "([^"]+)"', (REPO_ROOT / "justfile").read_text(), re.M)
    if match is None:
        raise QuickstartError("no idf_image in the justfile")
    return match.group(1)


def tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def check_no_warnings(run: Run, label: str, output: str) -> None:
    found = our_warnings(output)
    if found:
        raise QuickstartError(
            f"{label}: {len(found)} warning(s) from the library/example:\n" + "\n".join(found[:20])
        )
    run.passed(f"{label} warnings from components/fleetforge or the example", "0")


def pio_summary(output: str) -> dict[str, str]:
    """PlatformIO's `Environment  Status  Duration` table as {env: status}."""
    return {m.group(1): m.group(2) for m in PIO_ROW.finditer(output)}


def check_envs(run: Run, label: str, output: str, envs: Sequence[str]) -> None:
    """Every env in `envs` is a SUCCESS row of the summary table."""
    table = pio_summary(output)
    bad = {env: table.get(env, "missing") for env in envs if table.get(env) != "SUCCESS"}
    if bad:
        raise QuickstartError(f"{label}: PlatformIO summary is not all SUCCESS: {bad} ({table})")
    run.passed(f"{label} PlatformIO summary", f"{', '.join(envs)} SUCCESS")


def check_app(run: Run, label: str, size: int) -> None:
    if not 0 < size < OTA_SLOT_SIZE:
        raise QuickstartError(f"{label}: app is {size} B, the slot is {OTA_SLOT_SIZE} B")
    run.passed(f"{label} app size", f"{size} B < {OTA_SLOT_SIZE} B")


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for chunk in text.split("@@ff ")[1:]:
        name, _, body = chunk.partition("\n")
        sections[name.strip()] = body
    if "end" not in sections:
        raise QuickstartError("the IDF container stopped before printing its checks")
    return sections


def _size(path: Path) -> int:
    if not path.is_file():
        raise QuickstartError(f"no {path}")
    return path.stat().st_size


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise QuickstartError(f"Basic.ino has no single {old} to replace with {new}")
    return text.replace(old, new)


def rollback_version(epoch: int) -> str:
    """Build R's version: run-unique (409 on a rerun otherwise) and marked as a fault test."""
    return f"1.2.0-qs{epoch}-rbtest"


def selftest_fail_version(epoch: int) -> str:
    """Build F's version: run-unique and marked (the self-test answers fail)."""
    return f"1.3.0-qs{epoch}-stfail"


def selftest_timeout_version(epoch: int) -> str:
    """Build T's version: run-unique and marked (the self-test never answers)."""
    return f"1.4.0-qs{epoch}-sttimeout"


def selftest_failed_detail(reason: str, slot: str) -> str:
    """The `rolled_back` detail the agent reports for a failed self-test (`R5-spec-1` D11)."""
    kept = "".join(c for c in reason if " " <= c <= "~" and c != ";")[:SELFTEST_REASON_MAX]
    if not kept:
        kept = "no reason given"
    budget = DETAIL_MAX - len("selftest failed: ") - len("; returned to ") - len(slot)
    return f"selftest failed: {kept[: max(budget, 0)]}; returned to {slot}"


def window_version(epoch: int) -> str:
    """Build W's version: run-unique and marked (its safe-window rule is a clock)."""
    return f"1.5.0-qs{epoch}-window"


def expected_selftest(mode: str, slot: str) -> dict[str, Any] | None:
    """The server's `deploy.selftest` (`R5-be-1`) for the real agent's detail, by phase."""
    if mode == "passed":
        return {"outcome": "passed", "reason": None, "returned_to": None}
    if mode == "fail":
        detail = selftest_failed_detail(SELFTEST_FAIL_REASON, slot)
        reason = detail[len("selftest failed: ") : detail.rindex("; returned to ")]
        return {"outcome": "failed", "reason": reason, "returned_to": slot}
    if mode == "timeout":
        return {"outcome": "timed_out", "reason": None, "returned_to": slot}
    if mode == "legacy":
        return None
    raise ValueError(f"no expected self-test for mode {mode!r}")


def _deploy_of(row: dict[str, Any] | None) -> dict[str, Any]:
    deploy = (row or {}).get("deploy")
    return deploy if isinstance(deploy, dict) else {}


def _step_states(deploy: dict[str, Any]) -> list[str]:
    return [str(s.get("state")) for s in deploy.get("steps") or [] if isinstance(s, dict)]


def check_selftest(run: Run, what: str, row: dict[str, Any] | None, expected: Any) -> None:
    """`deploy.selftest` is exactly `expected`, or the run fails."""
    actual = _deploy_of(row).get("selftest")
    if actual != expected:
        raise QuickstartError(
            f"{what}: deploy.selftest is {json.dumps(actual)}, wanted {json.dumps(expected)}"
        )
    run.passed(f"deploy.selftest ({what})", json.dumps(actual))


def steps_in_order(steps: Sequence[str], names: Sequence[str]) -> bool:
    """Every name is in `steps`, and their first occurrences are strictly in this order."""
    if any(name not in steps for name in names):
        return False
    firsts = [steps.index(name) for name in names]
    return all(a < b for a, b in zip(firsts, firsts[1:], strict=False))


def check_window_log(text: str, cmd_id: str, command_id: str) -> str:
    """Build W's console from the `apply` on: verify the reboot waited for the safe window."""
    problems = []
    applying = re.search(rf"cmd_id={re.escape(cmd_id)} state=applying", text)
    rebooting = re.search(rf"cmd_id={re.escape(cmd_id)} state=rebooting", text)
    deferral = text.find(APPLY_WAITS_LINE.format(id=command_id, cmd=cmd_id))
    reboot_waits = text.find(REBOOT_WAITS_LINE.format(id=command_id))
    if RELEASING_LINE in text:
        problems.append(f"`{RELEASING_LINE}`: the apply was accepted, not deferred")
    if deferral < 0:
        problems.append("no `the staged update ... waits for a safe window` line")
    if reboot_waits < 0:
        problems.append("no `the reboot waits for a safe window` line")
    safe = None
    for match in re.finditer(r"window: safe at millis (\d+)", text):
        if deferral >= 0 and match.start() > deferral:
            safe = match
            break
    if safe is None:
        problems.append("no `window: safe at millis N` after the deferral")
    elif int(safe.group(1)) % WINDOW_PERIOD_MS < WINDOW_UNSAFE_MS:
        problems.append(f"the safe line's millis {safe.group(1)} is inside the unsafe stretch")
    if applying is None or rebooting is None:
        problems.append("no queued `state=applying` / `state=rebooting` for the stage")
    elif safe is not None and applying.start() < safe.start():
        problems.append("`state=applying` before the window opened")
    elif applying is not None and rebooting is not None and rebooting.start() < applying.start():
        problems.append("`state=rebooting` before `state=applying`")
    if applying is not None and deferral >= 0 and applying.start() < deferral:
        problems.append("`state=applying` before the deferral")
    if problems:
        raise QuickstartError("build W's console: " + "; ".join(problems))
    assert safe is not None  # noqa: S101 - verified above when problems is empty
    return (
        f"deferred, the reboot waited, then `window: safe at millis {safe.group(1)}` "
        f"({int(safe.group(1)) % WINDOW_PERIOD_MS} ms into the period), then applying, rebooting"
    )


def has_rollback_hook(app: bytes) -> bool:
    """Whether an app image was compiled with FF_ROLLBACK_TEST (its log string is in it)."""
    return ROLLBACK_HOOK_NEEDLE in app


def confirmed_breach(row: dict[str, Any] | None, cmd_id: str, version_r: str) -> str | None:
    """Return the P0 sentence if the broken build's deploy was ever reported `confirmed`."""
    deploy = _deploy_of(row)
    if deploy.get("cmd_id") != cmd_id:
        return None
    if deploy.get("state") == "confirmed" or "confirmed" in _step_states(deploy):
        return (
            f"the deliberately broken build {version_r} reported confirmed (deploy {cmd_id}) "
            "— the confirm gate is broken (P0, CRITICAL path)"
        )
    return None


def check_rollback_row(
    row: dict[str, Any] | None,
    cmd_id: str,
    version_b: str,
    version_r: str,
    expected_detail: str,
) -> str:
    """Verify final device row after a broken build: rolled back to B, never confirmed."""
    breach = confirmed_breach(row, cmd_id, version_r)
    if breach is not None:
        raise QuickstartError(breach)
    deploy = _deploy_of(row)
    steps = _step_states(deploy)
    fw_version = (row or {}).get("fw_version")
    problems = []
    if deploy.get("cmd_id") != cmd_id:
        problems.append(f"deploy.cmd_id {deploy.get('cmd_id')!r} is not {cmd_id}")
    if deploy.get("state") != "rolled_back":
        problems.append(f"deploy.state {deploy.get('state')!r}, wanted rolled_back")
    if deploy.get("is_terminal") is not True:
        problems.append(f"is_terminal {deploy.get('is_terminal')!r}")
    if fw_version == version_r:
        problems.append(
            f"fw_version is the broken build {version_r}: the board stayed on it (P0, CRITICAL)"
        )
    elif fw_version != version_b:
        problems.append(f"fw_version {fw_version!r}, wanted the previous build {version_b}")
    if "confirming" not in steps:
        problems.append("no `confirming` step: R never ran our code under the confirm timer")
    if "rolled_back" not in steps:
        problems.append("no `rolled_back` step")
    if expected_detail not in str(deploy.get("detail") or ""):
        problems.append(f"detail {deploy.get('detail')!r} does not name {expected_detail!r}")
    if problems:
        raise QuickstartError("the broken build's final row: " + "; ".join(problems))
    return (
        f"deploy rolled_back (is_terminal True), fw_version {fw_version} (the previous build), "
        f"detail {deploy.get('detail')!r}, steps {steps}, no confirmed; rolling_back seen: "
        f"{'yes' if 'rolling_back' in steps else 'no (best effort, not required)'}"
    )


def check_no_credentials(run: Run) -> None:
    if run.token is None:
        raise QuickstartError("no enrollment token was minted")
    sources = [(path.name, path.read_text(errors="replace")) for path in run.qemu_logs]
    sources += [(path.name, path.read_text(errors="replace")) for path in run.logs.glob("*.log")]
    sources.append(("this script's output", "\n".join(run.transcript)))
    for name, text in sources:
        if run.token in text:
            raise QuickstartError(f"the enrollment token appears in {name}")
        if '"mqtt_password":"' in text:
            raise QuickstartError(f"an mqtt_password value appears in {name}")
    run.passed(
        "credentials in every transcript",
        f"token 0 occurrences, mqtt_password 0 ({len(sources)} files)",
    )
