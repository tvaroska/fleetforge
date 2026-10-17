#!/usr/bin/env python3
"""Play the worked example's README quickstart, end to end (R3-fw-4, R3-test-1).

The acceptance of the worked example is "the README quickstart is exactly the steps a
reader follows, and a scripted run of those steps passes". This is that scripted run.
Stdlib only, like `agent/tools/*`: it never imports the project.

The READMEs are the script. Every shell command a reader types lives in a fenced block
whose first line is `# quickstart: <name>`, and this script runs those blocks VERBATIM
(`bash -euo pipefail`, from the tree root). A block it needs that a README lacks is a hard
failure, so the README and this script cannot drift apart.

    just lib-quickstart --build-only     # the compile half: no stack, no QEMU
    just lib-quickstart                  # plus the board steps, in QEMU against `just up`
    just lib-quickstart --build-only --fresh-pio-core
                                         # README blocks on an EMPTY PlatformIO core (R3-fw-7)

Phase 0 - a clean tree. `git ls-files --cached --others --exclude-standard`, copied into a
fresh `ff-quickstart-*` temp dir: a clean checkout including uncommitted work, with no
ignored build output (no `.pio/`, no `.qemu/`, no `lib-qemu/` state).

Phase 1 - builds (`phase_builds`). Arduino: blocks `arduino-build` (a bare `pio run`: the
summary table must list esp32 and esp32s3 as SUCCESS), then `pio-own-project` (the README's
"Your own project": three files copied next to the tree, `lib_deps` rewritten to a symlink to
the tree's library, both envs built), then `arduino-edit` +
`arduino-build-b` (build B must differ from A). With `--fresh-pio-core` (only with
`--build-only`) the README blocks run with `PLATFORMIO_CORE_DIR` on an empty directory, and
the run checks the pinned pioarduino platform was installed into it from nothing. ESP-IDF: blocks `idf-build` and
`idf-build-s3`, each inside the pinned `idf_image` from the justfile, on a container-local
copy of the tree (nothing is written to the host). Every build: zero `warning:` lines from
the library or the example, the app under the 1966080-byte slot. The IDF builds also
decode the built partition table (== `agent/partitions.csv`, layout ab-4m-v1), read the
resolved sdkconfig (rollback on, no eFuse burns) and the app descriptor (version 1.0.0).

Phase 2 - the board steps, in QEMU (`phase_enroll`, `phase_ota`, `phase_rollback`,
`phase_selftest_rollback` twice, `phase_apply_waits`). Only three things in the README need a board or a network
clone, and all are substituted:

* "git clone ... && cd fleetforge" -> the clean tree copy of Phase 0 (this run's `repo/`);

* "flash it over USB" -> `just lib-qemu --fresh`, booting the QEMU build of the same sketch
  (lib-qemu/platformio.ini: the example's env:esp32 plus the OpenCores NIC). The OTA
  artifact is that build too: the persona binary has no driver for QEMU's only NIC, so
  OTA'd into the emulator it would never reach the fleet and would, correctly, roll back;
* "the board reboots into build B" -> the deploy asks for `apply: "on_command"` and the
  script power-cycles the emulator itself. QEMU panics on `esp_restart()`
  (docs/runbooks/agent-qemu.md -> *The emulator cannot survive esp_restart()*); a real
  board takes the dashboard's default, `auto`.

Build B's version gets a run-unique suffix (`1.1.0-qs<epoch>`): a `(target, version)`
label is a promise about bytes, and a second run uploading rebuilt `1.1.0` bytes would be
refused with 409.

Secrets. The admin password comes from `$FF_ADMIN_PASSWORD` or the repo-root `.env`; it is
only ever sent in a request body, never printed, never put in argv. The enrollment token
lives in memory and in the temp tree's `.qemu/ff_cfg.bin` (0700, deleted at the end); it is
never printed, and the run fails if it appears in a QEMU log or in this script's output.
An unused token is revoked on the way out.

Phase 2c - the rollback (`phase_rollback`, R3-test-1), on the board `phase_ota` left
running build B. Build R is the committed sketch with `"SOS"` -> `"BAD"` and `"1.0.0"` ->
`1.2.0-qs<epoch>-rbtest` (same epoch as B), compiled with
`PLATFORMIO_BUILD_FLAGS=-DFF_ROLLBACK_TEST=1`: fault injection, never a README step. That
hook in ff_mqtt.c publishes the announce but throws its PUBACK away, so the session is
never confirmed, and shortens the confirm timer to 60 s. Before R is uploaded, its app.bin
must carry the hook's log string (and A's and B's must not): proof the flag reached the
library. R is deployed `on_command` and booted by a power cycle, as B was. It must run our
code (`confirming`), and 60 s later the timer marks it invalid and reboots. QEMU cannot
survive that `esp_restart()`, so a second power cycle stands in for the reset a real board
does by itself; the board returns to B's slot and reports `rolled_back`. The run fails at
once if R is ever reported `confirmed` (any API poll, R's console, or the final row on R's
version): that would mean the confirm gate is broken.

Phase 2d/2e - the self-test (`phase_selftest_rollback`, R5-fw-2). The committed sketch
registers a self-test that loop() answers, so A announces `selftest` and B confirms only after
`custom self-test passed` (detail `selftest passed`). Build F replaces the empty selfTest() with
one that answers `selfTestDone(false, "i2c sensor 0x48 did not answer; tried 3 times")`
(`"SOS"` -> `"FAIL"`, `1.3.0-qs<epoch>-stfail`); build T deletes loop()'s answer, so its
self-test never answers (`"WAIT"`, `1.4.0-qs<epoch>-sttimeout`). Both are plain builds (no
FF_ROLLBACK_TEST): their announce IS acknowledged and the hold is what must stop them. F
rolls itself back at once, T at the 300 s confirm deadline; each needs the power cycle R
needs, and each must come back on B with the row `rolled_back` and the exact detail
`selftest failed: i2c sensor 0x48 did not ans; returned to <B's slot>` (the reason shortened
to the 63-byte budget, `;` dropped) or `selftest timed out; returned to <B's slot>`. Neither
may ever log or report `confirmed`. Since R5-test-1 every phase also checks the server's
parse of the detail, `deploy.selftest` (`expected_selftest`): passed for B, null for R's legacy
wording, failed with the 27-byte reason for F, timed_out for T, each naming B's slot.

Phase 2f - apply into an unsafe window (`phase_apply_waits`, R5-test-1). Build W replaces the
committed `safeNow()` (reboot only between messages) with a clock that prints its flips,
`window: unsafe|safe at millis N` (unsafe 60 s of every 80 s), and makes loop() ask it first so
the flips are seen; `"SOS"` -> `"WIN"`, `1.5.0-qs<epoch>-window`. W is installed like B
(`on_command` + power cycle) and confirms; then B is staged again `on_command` and the real
`apply` verb is sent just after a fresh `window: unsafe` line. The board must answer
`deferred`, log that the staged update and the reboot wait, report `awaiting_safe_window` /
`apply waits for a safe window` until `window: safe`, and only then queue `applying` and
`rebooting` (at least 20 s after the verb). The emulator cannot survive the `esp_restart()`
that follows (its soft reset would panic with B PENDING_VERIFY and the bootloader would abort
B), so the script kills the container inside the agent's 1.5 s drain and boots it again; B
confirms with the steps staged < awaiting_safe_window < applying < rebooting < confirming <
confirmed, and the command's answer stays `deferred`. A soft reset that wins the race fails
the run as a harness race (rerun). `--only-safe-window` (dev only) builds A, B and W and runs
2a, 2b and 2f. With all of them the board phase takes about 55 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
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
# Every block this script runs, by README. A README that lacks one fails the run.
REQUIRED_BLOCKS: dict[str, tuple[str, ...]] = {
    ARDUINO_README: ("arduino-build", "pio-own-project", "arduino-edit", "arduino-build-b"),
    IDF_README: ("idf-build", "idf-build-s3"),
}

# spec/device-protocol.md -> up/announce: ota_slot_size. Every app must fit one slot.
OTA_SLOT_SIZE = 1966080
ARDUINO_LAYOUT = "ab-4m-arduino-v1"
DEVICE_ID = "000000000000"  # every emulated board (runbook: *Every emulated board is ...*)
QEMU_CONTAINER = "ff-qemu-esp32"
API_CONTAINER = "fleetforge-api"
VERSION_A = "1.0.0"
# One-way eFuse burns: any of these `=y` in a resolved sdkconfig is a failure (CRITICAL.md).
# EXACT names, never a prefix, the rule agent/tools/verify_bundle.py states: a resolved
# sdkconfig also holds SoC capability symbols such as CONFIG_SECURE_BOOT_V1_SUPPORTED=y,
# which say the chip could do it, not that this build burns anything. Retyped from there.
EFUSE_BURN_OPTIONS = (
    "CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK",
    "CONFIG_APP_ANTI_ROLLBACK",
    "CONFIG_SECURE_BOOT",
    "CONFIG_SECURE_BOOT_V1_ENABLED",
    "CONFIG_SECURE_BOOT_V2_ENABLED",
    "CONFIG_SECURE_FLASH_ENC_ENABLED",
    "CONFIG_FLASH_ENCRYPTION_ENABLED",
)
# A `warning:` line from these is ours (the library or the example), never the toolchain's.
# The pioarduino release platformio.ini pins (55.03.312-1) reports this in platform.json.
PINNED_PLATFORM_VERSION = "55.03.312"
PIO_ENVS = ("esp32", "esp32s3")
# One row of PlatformIO's `Environment  Status  Duration` summary table.
PIO_ROW = re.compile(r"^(\S+)\s+(SUCCESS|FAILED|IGNORED)\s+\d\d:\d\d:\d\d", re.M)
OUR_PATHS = re.compile(r"components/fleetforge|/examples/|Basic\.ino|basic_idf")

Row = tuple[str, str, str, int, int]

# R3-test-1: the broken build. The macro ff_mqtt.c tests (`#if FF_ROLLBACK_TEST`).
ROLLBACK_TEST_FLAGS = "-DFF_ROLLBACK_TEST=1"
# ff_mqtt.c's FF_ROLLBACK_TEST log line, ASCII prefix only (the line continues with an em dash).
ROLLBACK_HOOK_NEEDLE = b"FF_ROLLBACK_TEST: ignoring the announce ack"
ROLLBACK_MESSAGE = "BAD"
STAGED_LINE = r"update {cmd}: (ota_\d) is staged and bootable"

# R5-fw-2: the self-test builds. Each replacement must hit Basic.ino exactly once
# (_replace_once), or the build tests nothing.
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
# ff_selftest.c's buffer (63 bytes of reason) and the agent's detail[64] (63 bytes of detail).
SELFTEST_REASON_MAX = 63
DETAIL_MAX = 63
CONFIRM_TIMEOUT_S = 300  # ff_mqtt.c, a plain build (60 only under FF_ROLLBACK_TEST)
# Phase 2f (R5-test-1): build W's safe-window rule is a clock that says when it flips.
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
# ff_mqtt.c: the drain between `state=rebooting` and `esp_restart()` (a twin, pinned by a test).
COMMAND_REBOOT_DRAIN_MS = 1500
# ff_mqtt.c's log lines and FF_DETAIL_APPLY_WAITS (ff_mqtt_internal.h), twins pinned by tests.
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


# ── the README block convention ─────────────────────────────────────────────────────────


def extract_blocks(text: str, source: str = "README") -> dict[str, str]:
    """Every fenced block whose first line is `# quickstart: <name>`, by name, body only.

    The marker line is dropped; the rest of the block is returned exactly as written. A
    name used twice is an error: the script could not tell which one the reader follows.
    """
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
        index += 1  # the closing fence
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


# ── small helpers ───────────────────────────────────────────────────────────────────────


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
    block_env: dict[str, str] | None = None  # env for README blocks only (--fresh-pio-core)

    def say(self, line: str = "") -> None:
        self.transcript.append(line)
        print(line, flush=True)

    def passed(self, check: str, observed: str) -> None:
        self.results.append((check, observed))
        self.say(f"  PASS  {check}: {observed}")


def parse_rows(text: str) -> list[Row]:
    """An ESP-IDF partition CSV as (name, type, subtype, offset, size); sizes may be 24K/1M.

    Lines that are not rows (comments, and the decoder's own "Parsing binary partition
    input..." chatter) are skipped. A numeric subtype is written as lowercase hex.
    """
    rows: list[Row] = []
    for line in text.splitlines():
        stripped = line.strip()
        fields = [part.strip() for part in stripped.split(",")]
        if stripped.startswith("#") or len(fields) < 5:
            continue
        subtype = fields[2]
        if subtype.isdigit() or subtype.lower().startswith("0x"):
            subtype = f"0x{int(subtype, 0):02x}"  # the decoder prints a custom subtype as "64"
        rows.append((fields[0], fields[1], subtype, _number(fields[3]), _number(fields[4])))
    return rows


def _number(text: str) -> int:
    multiplier = {"K": 1024, "M": 1024 * 1024}.get(text[-1:].upper(), 1)
    digits = text[:-1] if multiplier != 1 else text
    return int(digits, 0) * multiplier


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


def run_logged(
    run: Run,
    argv: Sequence[str],
    log_name: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 3600,
) -> str:
    """Run a command in the tree, output to logs/<log_name>; fail with its tail."""
    log = run.logs / log_name
    started = time.monotonic()
    with log.open("w") as handle:
        result = subprocess.run(
            list(argv),
            cwd=run.tree,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=timeout,
            check=False,
        )
    output = log.read_text(errors="replace")
    run.say(f"  ran   {log_name} in {time.monotonic() - started:.0f} s (exit {result.returncode})")
    if result.returncode != 0:
        raise QuickstartError(f"{log_name} exited {result.returncode}:\n{tail(log)}")
    return output


def run_block(run: Run, name: str, body: str) -> str:
    """One README block, verbatim, from the tree root."""
    run.say(f"  block `{name}`:")
    for line in body.rstrip("\n").splitlines():
        run.say(f"    $ {line}")
    return run_logged(
        run, ["bash", "-euo", "pipefail", "-c", body], f"{name}.log", env=run.block_env
    )


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


# ── Phase 0: a clean tree ───────────────────────────────────────────────────────────────


def clean_tree(source: Path, root: Path) -> Path:
    """Copy what a clean checkout plus uncommitted work holds; no ignored build output."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=source,
        capture_output=True,
        check=True,
    ).stdout.decode()
    tree = root / "repo"
    count = 0
    for relative in filter(None, listed.split("\0")):
        src = source / relative
        if not src.is_file():  # a tracked file deleted in the worktree
            continue
        dst = tree / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    if count == 0:
        raise QuickstartError("git ls-files listed nothing")
    return tree


# ── Phase 1: builds ─────────────────────────────────────────────────────────────────────


def phase_builds(run: Run, skip_idf: bool) -> None:
    run.say("\n== Phase 1: build from the READMEs ==")
    arduino = require_blocks(
        (run.tree / ARDUINO_README).read_text(), REQUIRED_BLOCKS[ARDUINO_README], ARDUINO_README
    )
    idf = require_blocks(
        (run.tree / IDF_README).read_text(), REQUIRED_BLOCKS[IDF_README], IDF_README
    )

    sketch = run.tree / SKETCH
    original = sketch.read_text()
    core = Path(run.block_env["PLATFORMIO_CORE_DIR"]) if run.block_env else None
    if core is not None and any(core.iterdir()):
        raise QuickstartError(f"the fresh PlatformIO core {core} is not empty before the build")
    out = run_block(run, "arduino-build", arduino["arduino-build"])
    check_envs(run, "Arduino build A", out, PIO_ENVS)
    check_no_warnings(run, "Arduino build A", out)
    if core is not None:
        platform_json = core / "platforms" / "espressif32" / "platform.json"
        if not platform_json.is_file():
            raise QuickstartError(f"{platform_json} missing after the build from an empty core")
        version = json.loads(platform_json.read_text()).get("version")
        if version != PINNED_PLATFORM_VERSION:
            raise QuickstartError(f"{platform_json}: version {version}, pin is 55.03.312-1")
        run.passed(
            "fresh PlatformIO core",
            f"empty before; {platform_json}: platform espressif32 {version} installed from the pin",
        )
    pio = run.tree / ARDUINO_DIR / ".pio" / "build"
    for env in PIO_ENVS:
        check_app(run, f"Arduino A {env}", _size(pio / env / "firmware.bin"))
    build_a = run.work / "arduino-A-esp32.bin"
    shutil.copy2(pio / "esp32" / "firmware.bin", build_a)

    own = run_block(run, "pio-own-project", arduino["pio-own-project"])
    check_no_warnings(run, "Own project", own)
    check_envs(run, "Own project", own, PIO_ENVS)
    own_build = run.tree.parent / "my-blinker" / ".pio" / "build"
    for env in PIO_ENVS:
        check_app(run, f"Own project {env}", _size(own_build / env / "firmware.bin"))
    ini = (run.tree.parent / "my-blinker" / "platformio.ini").read_text()
    if f"symlink://{run.tree}/agent/components/fleetforge" not in ini:
        raise QuickstartError("my-blinker/platformio.ini lib_deps does not point at the tree")
    run.passed("own project (lib_deps -> the clone)", "esp32, esp32s3 SUCCESS")

    run_block(run, "arduino-edit", arduino["arduino-edit"])
    edited = sketch.read_text()
    if '"HELLO"' not in edited or '"1.1.0"' not in edited:
        raise QuickstartError("arduino-edit did not change the message and the version")
    out = run_block(run, "arduino-build-b", arduino["arduino-build-b"])
    check_no_warnings(run, "Arduino build B", out)
    build_b = pio / "esp32" / "firmware.bin"
    check_app(run, "Arduino B esp32", _size(build_b))
    if build_b.read_bytes() == build_a.read_bytes():
        raise QuickstartError("build B is byte-identical to build A")
    run.passed("Arduino B differs from A", "yes")
    sketch.write_text(original)  # Phase 2 starts from build A's source

    if skip_idf:
        run.say("  SKIP  ESP-IDF builds (--skip-idf)")
        return
    expected_rows = parse_rows((run.tree / "agent/partitions.csv").read_text())
    for name, target in (("idf-build", "esp32"), ("idf-build-s3", "esp32s3")):
        idf_build(run, name, idf[name], target, expected_rows)


# Runs in the pinned IDF image. The tree is mounted read-only and copied inside, so the
# build (as root) writes nothing to the host. The block arrives in $BLOCK, never spliced
# into this string. After it, the facts the host checks, each after an `@@ff` marker.
IDF_PROGRAM = r"""
set -euo pipefail
. "$IDF_PATH/export.sh" > /dev/null 2>&1
mkdir -p /w
tar -C /src --exclude=.pio -cf - . | tar -C /w -xf -
cd /w
bash -euo pipefail -c "$BLOCK"
cd "/w/$IDF_DIR"
echo "@@ff size"; stat -c %s build/fleetforge_basic.bin
echo "@@ff target"; grep '^CONFIG_IDF_TARGET=' sdkconfig
echo "@@ff partitions"
python "$IDF_PATH/components/partition_table/gen_esp32part.py" \
    build/partition_table/partition-table.bin
echo "@@ff sdkconfig"
grep -E '^CONFIG_[A-Z0-9_]*(ROLLBACK|SECURE|FLASH_ENC)[A-Z0-9_]*=' sdkconfig || true
echo "@@ff appdesc"; od -An -tx1 -v -N 128 build/fleetforge_basic.bin
echo "@@ff end"
"""


def idf_build(run: Run, name: str, body: str, target: str, expected_rows: list[Row]) -> None:
    run.say(f"  block `{name}` in {idf_image().split('@')[0]} (pinned digest):")
    for line in body.rstrip("\n").splitlines():
        run.say(f"    $ {line}")
    argv = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{run.tree}:/src:ro",
        "-e",
        "BLOCK",
        "-e",
        f"IDF_DIR={IDF_DIR}",
        "--entrypoint",
        "bash",
        idf_image(),
        "-c",
        IDF_PROGRAM,
    ]
    env = {**os.environ, "BLOCK": body}
    out = run_logged(run, argv, f"{name}.log", env=env)
    build_log, _, facts_text = out.partition("@@ff size")
    facts = _sections("@@ff size" + facts_text)
    label = f"ESP-IDF {target}"
    check_no_warnings(run, label, build_log)
    check_app(run, label, int(facts["size"].strip()))
    if facts["target"].strip() != f'CONFIG_IDF_TARGET="{target}"':
        raise QuickstartError(f"{label}: built for {facts['target'].strip()}")
    rows = parse_rows(facts["partitions"])
    if rows != expected_rows:
        raise QuickstartError(
            f"{label}: built table {rows} != agent/partitions.csv {expected_rows}"
        )
    run.passed(f"{label} decoded partition-table.bin", "== agent/partitions.csv (ab-4m-v1), 6 rows")
    config = facts["sdkconfig"].split()
    if "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" not in config:
        raise QuickstartError(f"{label}: resolved sdkconfig has no rollback: {config}")
    burns = efuse_burns(config)
    if burns:
        raise QuickstartError(f"{label}: eFuse-burning options are on: {burns}")
    run.passed(
        f"{label} resolved sdkconfig", "ROLLBACK_ENABLE=y, no anti-rollback/secure boot/flash enc"
    )
    version = app_version(bytes.fromhex("".join(facts["appdesc"].split())))
    if version != VERSION_A:
        raise QuickstartError(f"{label}: app version {version!r}, wanted {VERSION_A}")
    run.passed(f"{label} app version", version)


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


# ── Phase 2: the board steps, in QEMU ───────────────────────────────────────────────────


def admin_password() -> str:
    """$FF_ADMIN_PASSWORD, else the repo-root .env line (update-flow-e2e.mjs's rule)."""
    value = os.environ.get("FF_ADMIN_PASSWORD")
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("FF_ADMIN_PASSWORD="):
                raw = line[len("FF_ADMIN_PASSWORD=") :].strip()
                return re.sub(r"^(['\"])(.*)\1$", r"\2", raw)
    raise SetupError("no FF_ADMIN_PASSWORD in the environment or in the repo-root .env")


def http(
    run: Run,
    method: str,
    path: str,
    body: bytes | dict[str, Any] | None = None,
    *,
    content_type: str = "application/json",
    auth: bool = True,
) -> tuple[int, Any, Any]:
    """One API call: (status, parsed JSON or None, headers). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    request = urllib.request.Request(run.base + path, data=data, method=method)
    if data is not None:
        request.add_header("content-type", content_type)
    if auth and run.bearer is not None:
        request.add_header("Authorization", f"Bearer {run.bearer}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw, status, headers = response.read(), response.status, response.headers
    except urllib.error.HTTPError as error:
        raw, status, headers = error.read(), error.code, error.headers
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, parsed, headers


def preflight(run: Run) -> str:
    """Every precondition, before a token is spent. Each failure says the exact fix."""
    try:
        status, _, _ = http(run, "GET", "/v1/healthz", auth=False)
    except OSError as error:
        raise SetupError(
            f"the API at {run.base} does not answer ({error}) — run: just up"
        ) from None
    if status != 200:
        raise SetupError(f"GET {run.base}/v1/healthz answered {status} — run: just up")
    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            API_CONTAINER,
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    env = {
        key: value
        for key, _, value in (line.partition("=") for line in inspect.stdout.splitlines())
    }
    fix = (
        "FF_PUBLIC_BASE_URL=http://10.0.2.2:8088 FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000 "
        "docker compose -f docker-compose.yml -f docker-compose.override.yml up -d api"
    )
    for key in ("PUBLIC_BASE_URL", "S3_PUBLIC_ENDPOINT_URL"):
        if not env.get(key, "").startswith("http://10.0.2.2:"):
            raise SetupError(
                f"{API_CONTAINER} has {key}={env.get(key)!r}: the emulated board can only reach "
                f"10.0.2.2 (runbook: *For an OTA run, the download URLs must be 10.0.2.2 too*). "
                f"Recreate the api with:\n  {fix}\nand put it back afterwards without the two variables."
            )
    running = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{QEMU_CONTAINER}$"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if running:
        raise SetupError(
            f"{QEMU_CONTAINER} is already running — stop it: just agent-qemu-stop esp32"
        )
    return admin_password()


def login_and_mint(run: Run, password: str) -> None:
    status, _, headers = http(run, "POST", "/v1/auth/login", {"password": password}, auth=False)
    if status != 200:
        raise SetupError(f"POST /v1/auth/login answered {status} (wrong FF_ADMIN_PASSWORD?)")
    for cookie in headers.get_all("Set-Cookie") or []:
        match = re.match(r"\s*ff_session=([^;]+)", cookie)
        if match:
            run.bearer = match.group(1)
    if run.bearer is None:
        raise SetupError("login set no ff_session cookie")
    status, body, _ = http(run, "POST", "/v1/enrollment-tokens", {})
    if status not in (200, 201) or not isinstance(body, dict) or "token" not in body:
        raise QuickstartError(f"POST /v1/enrollment-tokens answered {status}")
    run.token, run.token_id = str(body["token"]), str(body["id"])
    run.passed("enrollment token minted", f"id {run.token_id} (the token itself is never printed)")


def just(
    run: Run,
    *args: str,
    log_name: str,
    timeout: float = 3600,
    env: dict[str, str] | None = None,
) -> str:
    return run_logged(run, ["just", *args], log_name, env=env, timeout=timeout)


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
    """The `rolled_back` detail the agent reports for a failed self-test (R5-spec-1 D11).

    A twin of ff_selftest.c's keep_reason (printable ASCII 0x20..0x7e without `;`, at most
    63 bytes) and ff_mqtt.c's selftest_failed_head / compose_returned_detail: only the
    reason is shortened, so that the whole detail fits 63 bytes; an empty reason becomes
    `no reason given`.
    """
    kept = "".join(c for c in reason if " " <= c <= "~" and c != ";")[:SELFTEST_REASON_MAX]
    if not kept:
        kept = "no reason given"
    budget = DETAIL_MAX - len("selftest failed: ") - len("; returned to ") - len(slot)
    return f"selftest failed: {kept[: max(budget, 0)]}; returned to {slot}"


def window_version(epoch: int) -> str:
    """Build W's version: run-unique and marked (its safe-window rule is a clock)."""
    return f"1.5.0-qs{epoch}-window"


def expected_selftest(mode: str, slot: str) -> dict[str, Any] | None:
    """The server's `deploy.selftest` (R5-be-1) for the real agent's detail, by phase.

    `passed` (B confirms), `fail` (build F at `slot`), `timeout` (build T at `slot`), and
    `legacy` (build R: a timeout before the ack keeps the R2 wording, not a self-test outcome).
    """
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


def check_selftest(run: Run, what: str, row: dict[str, Any] | None, expected: Any) -> None:
    """`deploy.selftest` is exactly `expected`, or the run fails; a passed check either way."""
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
    """Build W's console from the `apply` on: the reboot waited for the window. A summary.

    In order: both deferral lines, then `window: safe at millis N` (N inside the safe stretch),
    then the queued `state=applying` and `state=rebooting`. Never `releasing the staged update`
    (the accepted path), and no `state=applying` before the safe line.
    """
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
    assert safe is not None  # noqa: S101 - no problem means a safe line was found
    return (
        f"deferred, the reboot waited, then `window: safe at millis {safe.group(1)}` "
        f"({int(safe.group(1)) % WINDOW_PERIOD_MS} ms into the period), then applying, rebooting"
    )


def has_rollback_hook(app: bytes) -> bool:
    """Whether an app image was compiled with FF_ROLLBACK_TEST (its log string is in it)."""
    return ROLLBACK_HOOK_NEEDLE in app


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise QuickstartError(f"Basic.ino has no single {old} to replace with {new}")
    return text.replace(old, new)


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


def build_bundles(
    run: Run,
    version_b: str,
    version_r: str,
    version_f: str,
    version_t: str,
    version_w: str,
    *,
    only_safe_window: bool = False,
) -> Bundles:
    """Bundles A (as committed), B (the README edit), R (FF_ROLLBACK_TEST, must roll back),
    F (the self-test fails), T (the self-test never answers) and W (the safe-window rule is
    a clock: unsafe 60 s of every 80 s, phase 2f). `only_safe_window` builds A, B and W."""
    bundle = run.tree / "lib-qemu" / ".pio" / "bundle" / "esp32-qemu"
    # A stray PLATFORMIO_BUILD_FLAGS in the operator's shell must not reach A or B.
    plain_env = {k: v for k, v in os.environ.items() if k != "PLATFORMIO_BUILD_FLAGS"}
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-A.log", env=plain_env)
    bundle_a = run.work / "bundleA"
    shutil.copytree(bundle, bundle_a)

    sketch = run.tree / SKETCH
    original = sketch.read_text()
    arduino = require_blocks(
        sketch.parent.joinpath("README.md").read_text(), ("arduino-edit",), ARDUINO_README
    )
    run_block(run, "arduino-edit", arduino["arduino-edit"])
    run.say(
        f'  then  "1.1.0" -> "{version_b}" in Basic.ino: a version names one image, and '
        "a rerun uploading rebuilt 1.1.0 bytes would get 409"
    )
    edited = sketch.read_text()
    if edited.count('"1.1.0"') != 1:
        raise QuickstartError('arduino-edit left no single "1.1.0" to make run-unique')
    sketch.write_text(edited.replace('"1.1.0"', f'"{version_b}"'))
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-B.log", env=plain_env)
    bundle_b = run.work / "bundleB"
    shutil.copytree(bundle, bundle_b)

    sketch.write_text(original)
    run.say(
        f"  then  W: safeNow() is a clock that prints its flips (unsafe 60 s of every 80 s), "
        f'loop() asks it first, "SOS" -> "{WINDOW_MESSAGE}", "1.0.0" -> "{version_w}" (plain '
        "build): the reboot of an apply must wait for the window"
    )
    windowed = _replace_once(original, SAFE_WINDOW_STUB, SAFE_WINDOW_CLOCK)
    windowed = _replace_once(windowed, LOOP_HEAD, LOOP_HEAD_SAYS)
    windowed = _replace_once(windowed, '"SOS"', f'"{WINDOW_MESSAGE}"')
    sketch.write_text(_replace_once(windowed, f'"{VERSION_A}"', f'"{version_w}"'))
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-W.log", env=plain_env)
    bundle_w = run.work / "bundleW"
    shutil.copytree(bundle, bundle_w)
    sketch.write_text(original)

    if only_safe_window:
        shutil.rmtree(bundle)
        shutil.copytree(bundle_a, bundle)  # `lib-qemu --fresh` flashes from this directory
        app_a, app_b, app_w = ((b / "app.bin").read_bytes() for b in (bundle_a, bundle_b, bundle_w))
        if len({app_a, app_b, app_w}) != 3:
            raise QuickstartError("two of the QEMU bundles A, B, W have the same app.bin")
        if has_rollback_hook(app_w):
            raise QuickstartError("bundle W carries the FF_ROLLBACK_TEST hook")
        run.passed("QEMU bundles (--only-safe-window)", f"A, B, W {len(app_w)} B; all differ")
        check_app(run, "QEMU bundle W", len(app_w))
        return Bundles(a=bundle_a, b=bundle_b, w=bundle_w)

    run.say(
        f'  then  R: "SOS" -> "{ROLLBACK_MESSAGE}", "1.0.0" -> "{version_r}" in Basic.ino, '
        f"built with PLATFORMIO_BUILD_FLAGS={ROLLBACK_TEST_FLAGS} (fault injection, not a "
        "README step): an image that never confirms"
    )
    broken = _replace_once(original, '"SOS"', f'"{ROLLBACK_MESSAGE}"')
    sketch.write_text(_replace_once(broken, f'"{VERSION_A}"', f'"{version_r}"'))
    just(
        run,
        "lib-bundle",
        "esp32-qemu",
        log_name="lib-bundle-R.log",
        env={**os.environ, "PLATFORMIO_BUILD_FLAGS": ROLLBACK_TEST_FLAGS},
    )
    bundle_r = run.work / "bundleR"
    shutil.copytree(bundle, bundle_r)

    run.say(
        f'  then  F: selfTest() answers selfTestDone(false, "{SELFTEST_FAIL_REASON}"), '
        f'"SOS" -> "{SELFTEST_FAIL_MESSAGE}", "1.0.0" -> "{version_f}" (plain build): the '
        "self-test fails"
    )
    failing = _replace_once(original, SELFTEST_STUB, SELFTEST_FAIL_STUB)
    failing = _replace_once(failing, '"SOS"', f'"{SELFTEST_FAIL_MESSAGE}"')
    sketch.write_text(_replace_once(failing, f'"{VERSION_A}"', f'"{version_f}"'))
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-F.log", env=plain_env)
    bundle_f = run.work / "bundleF"
    shutil.copytree(bundle, bundle_f)

    run.say(
        f'  then  T: loop() never calls selfTestDone(true), "SOS" -> '
        f'"{SELFTEST_TIMEOUT_MESSAGE}", "1.0.0" -> "{version_t}" (plain build): the self-test '
        "never answers"
    )
    silent = _replace_once(original, SELFTEST_LOOP_ANSWER, SELFTEST_LOOP_SILENCED)
    silent = _replace_once(silent, '"SOS"', f'"{SELFTEST_TIMEOUT_MESSAGE}"')
    sketch.write_text(_replace_once(silent, f'"{VERSION_A}"', f'"{version_t}"'))
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-T.log", env=plain_env)
    bundle_t = run.work / "bundleT"
    shutil.copytree(bundle, bundle_t)

    sketch.write_text(original)
    shutil.rmtree(bundle)
    shutil.copytree(bundle_a, bundle)  # `lib-qemu --fresh` flashes from this directory
    app_a, app_b, app_r, app_f, app_t, app_w = (
        (b / "app.bin").read_bytes()
        for b in (bundle_a, bundle_b, bundle_r, bundle_f, bundle_t, bundle_w)
    )
    if app_a == app_b:
        raise QuickstartError("QEMU bundle B's app.bin is identical to A's")
    run.passed("QEMU bundles", f"A {len(app_a)} B, B {len(app_b)} B, B != A")
    if not has_rollback_hook(app_r):
        raise QuickstartError(
            f"the {ROLLBACK_TEST_FLAGS} flag did not reach ff_mqtt.c: bundle R has no rollback "
            "hook; refusing to upload a build that would confirm"
        )
    if has_rollback_hook(app_a) or has_rollback_hook(app_b):
        raise QuickstartError("bundle A or B carries the FF_ROLLBACK_TEST hook")
    run.passed("QEMU bundle R rollback hook", "R carries FF_ROLLBACK_TEST; A and B do not")
    if app_r in (app_a, app_b):
        raise QuickstartError("QEMU bundle R's app.bin is identical to A's or B's")
    run.passed("QEMU bundle R differs from A and B", "yes")
    check_app(run, "QEMU bundle R", len(app_r))
    for name, app in (("F", app_f), ("T", app_t), ("W", app_w)):
        if has_rollback_hook(app):
            raise QuickstartError(f"bundle {name} carries the FF_ROLLBACK_TEST hook")
    apps = [app_a, app_b, app_r, app_f, app_t, app_w]
    if len(set(apps)) != len(apps):
        raise QuickstartError("two of the QEMU bundles A, B, R, F, T, W have the same app.bin")
    run.passed(
        "QEMU bundles F, T and W",
        f"F {len(app_f)} B, T {len(app_t)} B, W {len(app_w)} B; no rollback hook; all six "
        "app.bin differ",
    )
    check_app(run, "QEMU bundle F", len(app_f))
    check_app(run, "QEMU bundle T", len(app_t))
    check_app(run, "QEMU bundle W", len(app_w))
    return Bundles(a=bundle_a, b=bundle_b, w=bundle_w, r=bundle_r, f=bundle_f, t=bundle_t)


def start_qemu(run: Run, fresh: bool, log_name: str) -> Path:
    log = run.logs / log_name
    handle = log.open("wb")
    argv = ["just", "lib-qemu", *(["--fresh"] if fresh else [])]
    run.say(f"  boot  {' '.join(argv)} (log: {log_name})")
    run.qemu = subprocess.Popen(
        argv, cwd=run.tree, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT
    )
    handle.close()
    run.qemu_logs.append(log)
    return log


def stop_qemu(run: Run) -> None:
    subprocess.run(
        ["just", "agent-qemu-stop", "esp32"], cwd=run.tree, capture_output=True, check=False
    )
    if run.qemu is not None:
        try:
            run.qemu.wait(timeout=60)
        except subprocess.TimeoutExpired:
            run.qemu.kill()
        run.qemu = None


def wait_for_lines(
    run: Run,
    log: Path,
    patterns: Sequence[str],
    timeout: float,
    *,
    forbidden: Sequence[str] = (),
    start: int = 0,
    poll: float = 0.5,
) -> int:
    """Each pattern (a regex), in order, in the growing log; the emulator must stay up.

    A `forbidden` regex matching anywhere in the log fails at once. Searching starts at
    offset `start`; the offset just past the last match is returned. `poll` is the re-read
    period (phase 2f races the agent's 1.5 s reboot drain with 0.1).
    """
    deadline = time.monotonic() + timeout
    position = start
    pending = list(patterns)
    while pending:
        text = log.read_text(errors="replace")
        for pattern in forbidden:
            bad = re.search(pattern, text)
            if bad:
                line_start = text.rfind("\n", 0, bad.start()) + 1
                line_end = text.find("\n", bad.end())
                line = text[line_start : line_end if line_end >= 0 else None].strip()
                raise QuickstartError(f"{log.name}: forbidden `{pattern}` in: {line}")
        match = re.search(pending[0], text[position:])
        if match:
            line_start = text.rfind("\n", 0, position + match.start()) + 1
            line_end = text.find("\n", position + match.end())
            run.passed(
                f"log: {pending[0]}", text[line_start : line_end if line_end >= 0 else None].strip()
            )
            position += match.end()
            pending.pop(0)
            continue
        if run.qemu is not None and run.qemu.poll() is not None:
            raise QuickstartError(f"the emulator exited before `{pending[0]}`:\n{tail(log)}")
        if time.monotonic() > deadline:
            raise QuickstartError(f"no `{pending[0]}` within {timeout:.0f} s:\n{tail(log)}")
        time.sleep(poll)
    return position


def device(run: Run) -> dict[str, Any] | None:
    status, body, _ = http(run, "GET", "/v1/devices")
    if status != 200 or not isinstance(body, dict):
        raise QuickstartError(f"GET /v1/devices answered {status}")
    for row in body.get("devices", []):
        if isinstance(row, dict) and row.get("device_id") == DEVICE_ID:
            return row
    return None


def wait_for_device(
    run: Run,
    what: str,
    ok: Callable[[dict[str, Any]], bool],
    timeout: float,
    log: Path,
    forbid: Callable[[dict[str, Any] | None], str | None] | None = None,
) -> dict[str, Any]:
    """Poll the device row until `ok`; a message from `forbid` on any poll fails at once."""
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = device(run)
        if forbid is not None:
            message = forbid(row)
            if message is not None:
                raise QuickstartError(message)
        if row is not None and ok(row):
            return row
        time.sleep(1)
    raise QuickstartError(
        f"GET /v1/devices: {DEVICE_ID} never {what} within {timeout:.0f} s; last row: "
        f"{json.dumps(row)[:600]}\n{tail(log)}"
    )


def phase_enroll(run: Run, port_guest: str) -> Path:
    """README steps 2-5: token, ff_cfg, flash, watch it join (QEMU stands in for USB)."""
    run.say("\n== Phase 2a: enroll and heartbeat (QEMU for the USB flash) ==")
    if run.token is None:
        raise QuickstartError("no enrollment token was minted")
    run.say(
        f"  write .qemu/ff_cfg.bin: api {port_guest}, mqtt {run.mqtt_uri}, ethernet, hb 10 s, "
        "token <minted>"
    )
    # The recipe's docker line is `@`-quiet: the token is never echoed.
    just(
        run,
        "agent-cfg",
        "--api-base",
        port_guest,
        "--mqtt-uri",
        run.mqtt_uri,
        "--link",
        "ethernet",
        "--hb",
        "10",
        "--token",
        run.token,
        log_name="agent-cfg.log",
    )
    log = start_qemu(run, fresh=True, log_name="qemu-A.log")
    wait_for_lines(
        run,
        log,
        (
            r"ff-lib: fleetforge library",
            r"ff-enroll: enroll 200",
            r"announce acknowledged by the broker",
            re.escape(f"morse: SOS (firmware {VERSION_A})"),
        ),
        timeout=180,
    )
    run.token_burned = True
    row = wait_for_device(
        run,
        f"online at {VERSION_A} with layout {ARDUINO_LAYOUT} and capabilities ota, selftest, "
        "safe_window",
        lambda r: (
            r.get("online") is True
            and r.get("fw_version") == VERSION_A
            and r.get("partition_layout") == ARDUINO_LAYOUT
            and "ota" in (r.get("capabilities") or [])
            # R5-fw-2: the committed sketch registers a self-test, and the announce says so.
            and "selftest" in (r.get("capabilities") or [])
            # R5-fw-5: and a safe-window rule (reboots only between messages).
            and "safe_window" in (r.get("capabilities") or [])
        ),
        timeout=60,
        log=log,
    )
    run.passed(
        f"GET /v1/devices {DEVICE_ID}",
        f"online {row['online']}, fw_version {row['fw_version']}, partition_layout "
        f"{row['partition_layout']}, capabilities {row['capabilities']}, agent_version "
        f"{row.get('agent_version')}",
    )
    return log


def phase_ota(run: Run, bundle_b: Path, version_b: str, log_a: Path) -> tuple[str, Path]:
    """README steps 6-7: upload build B, deploy it, watch it confirm and report its version.

    Returns B's slot label (from the stage line) and B's console log.
    """
    run.say("\n== Phase 2b: deploy build B (on_command + power cycle for the self-reboot) ==")
    status, body, _ = http(
        run,
        "POST",
        f"/v1/artifact?target=esp32&version={version_b}&partition_layout={ARDUINO_LAYOUT}",
        (bundle_b / "app.bin").read_bytes(),
        content_type="application/octet-stream",
    )
    if status not in (200, 201):
        raise QuickstartError(f"upload of {version_b} answered {status}: {body}")
    run.passed("POST /v1/artifact (bundle B app.bin)", f"{status}, version {version_b}")
    status, body, _ = http(
        run,
        "POST",
        f"/v1/devices/{DEVICE_ID}/deploy",
        {"version": version_b, "apply": "on_command"},
    )
    if status != 202 or not isinstance(body, dict):
        raise QuickstartError(f"deploy of {version_b} answered {status}: {body}")
    cmd_id = str(body["cmd_id"])
    run.passed("POST /v1/devices/…/deploy apply=on_command", f"202, cmd_id {cmd_id}")

    started = time.monotonic()
    staged_line = STAGED_LINE.format(cmd=re.escape(cmd_id))
    wait_for_lines(run, log_a, (staged_line,), timeout=240)
    slot = re.search(staged_line, log_a.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log_a.name}")
    slot_b = slot.group(1)
    wait_for_device(
        run,
        "staged",
        lambda r: (
            (r.get("deploy") or {}).get("cmd_id") == cmd_id
            and (r.get("deploy") or {}).get("state") == "staged"
        ),
        timeout=60,
        log=log_a,
    )
    run.passed("deploy state", f"staged ({time.monotonic() - started:.0f} s after the deploy)")
    time.sleep(1)
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)

    log_b = start_qemu(run, fresh=False, log_name="qemu-B.log")
    # R5-fw-2: the ack starts B's self-test, and only its pass confirms.
    wait_for_lines(
        run,
        log_b,
        (
            r"announce acknowledged by the broker",
            re.escape(SELFTEST_RUN_LINE),
            r"custom self-test passed",
            r"CONFIRMED",
        ),
        timeout=180,
    )
    wait_for_lines(run, log_b, (re.escape(f"morse: HELLO (firmware {version_b})"),), timeout=180)
    row = wait_for_device(
        run,
        f"confirmed at {version_b}",
        lambda r: (
            r.get("fw_version") == version_b
            and (r.get("deploy") or {}).get("cmd_id") == cmd_id
            and (r.get("deploy") or {}).get("state") == "confirmed"
            and (r.get("deploy") or {}).get("is_terminal") is True
        ),
        timeout=60,
        log=log_b,
    )
    deploy = row["deploy"]
    if deploy.get("detail") != "selftest passed":
        raise QuickstartError(
            f"B's confirmed deploy has detail {deploy.get('detail')!r}, wanted 'selftest passed'"
        )
    if "selftest" not in (row.get("capabilities") or []):
        raise QuickstartError(f"B does not announce selftest: {row.get('capabilities')}")
    check_selftest(run, "B confirmed", row, expected_selftest("passed", slot_b))
    run.passed(
        f"GET /v1/devices {DEVICE_ID} after the update",
        f"fw_version {row['fw_version']}, deploy {deploy['state']} (is_terminal "
        f"{deploy['is_terminal']}), detail {deploy['detail']!r}, capabilities "
        f"{row['capabilities']}, {time.monotonic() - started:.0f} s after the deploy",
    )
    return slot_b, log_b


def _deploy_of(row: dict[str, Any] | None) -> dict[str, Any]:
    deploy = (row or {}).get("deploy")
    return deploy if isinstance(deploy, dict) else {}


def _step_states(deploy: dict[str, Any]) -> list[str]:
    return [str(s.get("state")) for s in deploy.get("steps") or [] if isinstance(s, dict)]


def confirmed_breach(row: dict[str, Any] | None, cmd_id: str, version_r: str) -> str | None:
    """The P0 sentence if the broken build's deploy was ever reported `confirmed`, else None.

    R's own version on the row is NOT a breach: its announce is published and retained by
    design, so the row shows it while R is `confirming`. Only the final row must not.
    """
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
    """The final row after the broken build: rolled back to B, never confirmed. A summary."""
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


def phase_rollback(
    run: Run, bundle_r: Path, version_b: str, version_r: str, slot_b: str, log_b: Path
) -> Path:
    """R3-test-1: deploy a build that cannot confirm; it must roll back to B, unaided.

    Returns the console log of the board back on B (the next phase stages from it).

    The board's decision is its own: the confirm timer marks R invalid and reboots. QEMU
    cannot survive that `esp_restart()` (runbook: *The emulator cannot survive
    esp_restart()*), so a power cycle stands in for the reset a real board does itself.
    """
    run.say(
        "\n== Phase 2c: deploy a broken build (FF_ROLLBACK_TEST): it must roll back, "
        "never confirm =="
    )
    status, body, _ = http(
        run,
        "POST",
        f"/v1/artifact?target=esp32&version={version_r}&partition_layout={ARDUINO_LAYOUT}",
        (bundle_r / "app.bin").read_bytes(),
        content_type="application/octet-stream",
    )
    if status not in (200, 201):
        raise QuickstartError(f"upload of {version_r} answered {status}: {body}")
    run.passed("POST /v1/artifact (bundle R app.bin)", f"{status}, version {version_r}")
    status, body, _ = http(
        run,
        "POST",
        f"/v1/devices/{DEVICE_ID}/deploy",
        {"version": version_r, "apply": "on_command"},
    )
    if status != 202 or not isinstance(body, dict):
        raise QuickstartError(f"deploy of {version_r} answered {status}: {body}")
    cmd_id = str(body["cmd_id"])
    run.passed("POST /v1/devices/…/deploy R apply=on_command", f"202, cmd_id {cmd_id}")

    def breach(row: dict[str, Any] | None) -> str | None:
        return confirmed_breach(row, cmd_id, version_r)

    started = time.monotonic()
    staged_line = STAGED_LINE.format(cmd=re.escape(cmd_id))
    wait_for_lines(run, log_b, (staged_line,), timeout=240)
    slot = re.search(staged_line, log_b.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log_b.name}")
    slot_r = slot.group(1)
    if slot_r == slot_b:
        raise QuickstartError(f"R was staged into {slot_r}, the slot B is running from")
    wait_for_device(
        run,
        "staged (R)",
        lambda r: _deploy_of(r).get("cmd_id") == cmd_id and _deploy_of(r).get("state") == "staged",
        timeout=60,
        log=log_b,
        forbid=breach,
    )
    run.passed(
        "deploy state (R)",
        f"staged into {slot_r} (B runs from {slot_b}), {time.monotonic() - started:.0f} s "
        "after the deploy",
    )
    time.sleep(1)
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)

    log_r = start_qemu(run, fresh=False, log_name="qemu-R.log")
    # R never acks, so the self-test never runs: a timeout before the ack keeps the R2 wording.
    never_confirmed = (r"CONFIRMED", r"transaction \S+: confirmed", re.escape(SELFTEST_RUN_LINE))
    wait_for_lines(
        run,
        log_r,
        (re.escape(f"morse: {ROLLBACK_MESSAGE} (firmware {version_r})"),),
        timeout=180,
        forbidden=never_confirmed,
    )
    position = wait_for_lines(
        run,
        log_r,
        (
            r"OTA boot: 60 s from now to reach the fleet or roll back",
            rf"ff-lib: fleetforge library \S+, firmware {re.escape(version_r)}",
            rf"transaction {re.escape(cmd_id)}: confirming on {slot_r}",
            re.escape(ROLLBACK_HOOK_NEEDLE.decode()),
            r"no working session 60 s after an OTA boot",
        ),
        timeout=180,
        forbidden=never_confirmed,
    )
    row = device(run)
    if row is not None and row.get("fw_version") == version_r:
        run.say(f"  note  R appeared in the fleet on its own version ({version_r}), as designed")

    # The timer has fired: R is marked invalid and reboots (in QEMU: the esp_restart panic).
    rolled_line = (
        rf"transaction {re.escape(cmd_id)}: rolled_back \(returned to {slot_b}; "
        rf"{slot_r} did not confirm\)"
    )
    reset = r"rst:|Guru Meditation|abort\(\)|" + rolled_line
    deadline = time.monotonic() + 15
    seen = None
    while time.monotonic() < deadline and seen is None:
        seen = re.search(reset, log_r.read_text(errors="replace")[position:])
        if seen is None:
            time.sleep(0.5)
    run.say(
        "  note  after the timeout: "
        + (repr(seen.group(0)) if seen else "no reset line within 15 s")
        + " (QEMU cannot survive esp_restart; the power cycle stands in for the reset)"
    )
    text_r = log_r.read_text(errors="replace")
    if re.search(r"CONFIRMED|transaction \S+: confirmed", text_r):
        raise QuickstartError(
            f"{log_r.name}: the broken build {version_r} logged confirmed — the confirm gate "
            "is broken (P0, CRITICAL path)"
        )
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)

    log_r2 = start_qemu(run, fresh=False, log_name="qemu-R2.log")
    if re.search(rolled_line, text_r[position:]):
        run.passed(f"log: {rolled_line}", "in qemu-R.log (the soft reset did not panic)")
        wait_for_lines(
            run, log_r2, (re.escape(f"morse: HELLO (firmware {version_b})"),), timeout=180
        )
    else:
        wait_for_lines(
            run,
            log_r2,
            (rolled_line, re.escape(f"morse: HELLO (firmware {version_b})")),
            timeout=180,
        )
    text_r2 = log_r2.read_text(errors="replace")
    loaded = re.findall(r"Loaded app from partition at offset (0x[0-9a-fA-F]+)", text_r2)
    if loaded:
        rows = parse_rows((run.tree / ARDUINO_DIR / "partitions.csv").read_text())
        offset_b = next(offset for name, _, _, offset, _ in rows if name == slot_b)
        if int(loaded[-1], 16) != offset_b:
            raise QuickstartError(
                f"after the rollback the bootloader loaded {loaded[-1]}, not {slot_b} "
                f"({offset_b:#x})"
            )
        run.passed("bootloader after the rollback", f"loaded {loaded[-1]} = {slot_b}")
    else:
        run.say("  note  the bootloader does not log `Loaded app from partition` (not checked)")

    row = wait_for_device(
        run,
        "rolled_back (R)",
        lambda r: (
            _deploy_of(r).get("cmd_id") == cmd_id
            and _deploy_of(r).get("state") == "rolled_back"
            and _deploy_of(r).get("is_terminal") is True
        ),
        timeout=120,
        log=log_r2,
        forbid=breach,
    )
    summary = check_rollback_row(
        row, cmd_id, version_b, version_r, f"returned to {slot_b}; {slot_r} did not confirm"
    )
    # A timeout before the ack keeps the R2 wording: not a self-test outcome (R5-be-1).
    check_selftest(run, "R rolled back", row, expected_selftest("legacy", slot_b))
    run.passed(
        f"GET /v1/devices {DEVICE_ID} after the broken build",
        f"{summary}; {time.monotonic() - started:.0f} s after the deploy",
    )
    return log_r2


def phase_selftest_rollback(
    run: Run,
    bundle: Path,
    versions: tuple[str, str],
    message: str,
    mode: str,
    slot_b: str,
    log_prev: Path,
) -> Path:
    """R5-fw-2: deploy a build whose self-test fails (`mode="fail"`) or never answers
    (`"timeout"`). Its announce IS acknowledged, so only the hold stands between it and a
    confirm; it must roll back to B with the self-test detail on the wire.

    `versions` is (B's version, this build's version). Returns the log of the board back on B.
    """
    version_b, version = versions
    label = "2d" if mode == "fail" else "2e"
    what = "fails" if mode == "fail" else "never answers"
    run.say(f"\n== Phase {label}: deploy a build whose self-test {what}: it must roll back ==")
    status, body, _ = http(
        run,
        "POST",
        f"/v1/artifact?target=esp32&version={version}&partition_layout={ARDUINO_LAYOUT}",
        (bundle / "app.bin").read_bytes(),
        content_type="application/octet-stream",
    )
    if status not in (200, 201):
        raise QuickstartError(f"upload of {version} answered {status}: {body}")
    run.passed(f"POST /v1/artifact ({version} app.bin)", f"{status}, version {version}")
    status, body, _ = http(
        run, "POST", f"/v1/devices/{DEVICE_ID}/deploy", {"version": version, "apply": "on_command"}
    )
    if status != 202 or not isinstance(body, dict):
        raise QuickstartError(f"deploy of {version} answered {status}: {body}")
    cmd_id = str(body["cmd_id"])
    run.passed(f"POST /v1/devices/…/deploy {version} apply=on_command", f"202, cmd_id {cmd_id}")

    def breach(row: dict[str, Any] | None) -> str | None:
        return confirmed_breach(row, cmd_id, version)

    started = time.monotonic()
    staged_line = STAGED_LINE.format(cmd=re.escape(cmd_id))
    wait_for_lines(run, log_prev, (staged_line,), timeout=240)
    slot = re.search(staged_line, log_prev.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log_prev.name}")
    slot_new = slot.group(1)
    if slot_new == slot_b:
        raise QuickstartError(f"{version} was staged into {slot_new}, the slot B is running from")
    wait_for_device(
        run,
        f"staged ({version})",
        lambda r: _deploy_of(r).get("cmd_id") == cmd_id and _deploy_of(r).get("state") == "staged",
        timeout=60,
        log=log_prev,
        forbid=breach,
    )
    run.passed(f"deploy state ({version})", f"staged into {slot_new} (B runs from {slot_b})")
    time.sleep(1)
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)

    log = start_qemu(run, fresh=False, log_name=f"qemu-{mode}.log")
    never_confirmed = (
        r"CONFIRMED",
        r"transaction \S+: confirmed",
        r"custom self-test passed",
    )
    common = (
        rf"transaction {re.escape(cmd_id)}: confirming on {slot_new}",
        r"announce acknowledged by the broker",
        re.escape(SELFTEST_RUN_LINE),
    )
    if mode == "fail":
        kept = "".join(c for c in SELFTEST_FAIL_REASON if c != ";")
        decided: tuple[str, ...] = (
            re.escape(f"custom self-test failed: {kept}"),
            # ASCII prefix only: the line continues with an em dash.
            r"the custom self-test failed ",
        )
        timeout = 180.0
        head = selftest_failed_detail(SELFTEST_FAIL_REASON, slot_b)
    else:
        decided = (rf"no self-test result {CONFIRM_TIMEOUT_S} s after an OTA boot",)
        timeout = CONFIRM_TIMEOUT_S + 120.0
        head = f"selftest timed out; returned to {slot_b}"
    # Each from the top of the log: the sketch's loop() and the library's task interleave.
    for first in (
        rf"OTA boot: {CONFIRM_TIMEOUT_S} s from now",
        re.escape(f"morse: {message} (firmware {version})"),
    ):
        wait_for_lines(run, log, (first,), timeout=180, forbidden=never_confirmed)
    position = wait_for_lines(
        run, log, (*common, *decided), timeout=timeout, forbidden=never_confirmed
    )

    # The image marked itself invalid and reboots (in QEMU: the esp_restart panic).
    reset = r"rst:|Guru Meditation|abort\(\)"
    deadline = time.monotonic() + 15
    seen = None
    while time.monotonic() < deadline and seen is None:
        seen = re.search(reset, log.read_text(errors="replace")[position:])
        if seen is None:
            time.sleep(0.5)
    run.say(
        "  note  after the decision: "
        + (repr(seen.group(0)) if seen else "no reset line within 15 s")
        + " (QEMU cannot survive esp_restart; the power cycle stands in for the reset)"
    )
    if re.search(r"CONFIRMED|transaction \S+: confirmed", log.read_text(errors="replace")):
        raise QuickstartError(
            f"{log.name}: {version} logged confirmed — the self-test hold is broken (P0, "
            "CRITICAL path)"
        )
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)

    log_back = start_qemu(run, fresh=False, log_name=f"qemu-{mode}-back.log")
    rolled_line = rf"transaction {re.escape(cmd_id)}: rolled_back \({re.escape(head)}\)"
    wait_for_lines(
        run,
        log_back,
        (rolled_line, re.escape(f"morse: HELLO (firmware {version_b})")),
        timeout=180,
        # B is VALID: a boot that needs no confirm never runs the self-test.
        forbidden=(re.escape(SELFTEST_RUN_LINE),),
    )
    text = log_back.read_text(errors="replace")
    loaded = re.findall(r"Loaded app from partition at offset (0x[0-9a-fA-F]+)", text)
    if loaded:
        rows = parse_rows((run.tree / ARDUINO_DIR / "partitions.csv").read_text())
        offset_b = next(offset for name, _, _, offset, _ in rows if name == slot_b)
        if int(loaded[-1], 16) != offset_b:
            raise QuickstartError(
                f"after the rollback the bootloader loaded {loaded[-1]}, not {slot_b} "
                f"({offset_b:#x})"
            )
        run.passed("bootloader after the rollback", f"loaded {loaded[-1]} = {slot_b}")
    else:
        run.say("  note  the bootloader does not log `Loaded app from partition` (not checked)")

    row = wait_for_device(
        run,
        f"rolled_back ({version})",
        lambda r: (
            _deploy_of(r).get("cmd_id") == cmd_id
            and _deploy_of(r).get("state") == "rolled_back"
            and _deploy_of(r).get("is_terminal") is True
        ),
        timeout=120,
        log=log_back,
        forbid=breach,
    )
    summary = check_rollback_row(row, cmd_id, version_b, version, head)
    detail = str(_deploy_of(row).get("detail"))
    if detail != head:
        raise QuickstartError(f"{version}'s detail is {detail!r}, wanted exactly {head!r}")
    if len(detail.encode()) > DETAIL_MAX:
        raise QuickstartError(f"{version}'s detail is {len(detail.encode())} bytes, over 63")
    check_selftest(run, f"{version} rolled back", row, expected_selftest(mode, slot_b))
    run.passed(
        f"GET /v1/devices {DEVICE_ID} after {version}",
        f"{summary}; detail exactly {detail!r} ({len(detail.encode())} bytes); "
        f"{time.monotonic() - started:.0f} s after the deploy",
    )
    return log_back


def _upload_and_stage(
    run: Run, version: str, bundle: Path | None, log: Path, slot_not: str | None
) -> tuple[str, str, int]:
    """Upload `bundle` (None: the label exists already), deploy `version` `on_command`, and
    wait for the stage line in `log` and the server's `staged`. (cmd_id, slot, log offset)."""
    if bundle is not None:
        status, body, _ = http(
            run,
            "POST",
            f"/v1/artifact?target=esp32&version={version}&partition_layout={ARDUINO_LAYOUT}",
            (bundle / "app.bin").read_bytes(),
            content_type="application/octet-stream",
        )
        if status not in (200, 201):
            raise QuickstartError(f"upload of {version} answered {status}: {body}")
        run.passed(f"POST /v1/artifact ({version} app.bin)", f"{status}, version {version}")
    status, body, _ = http(
        run, "POST", f"/v1/devices/{DEVICE_ID}/deploy", {"version": version, "apply": "on_command"}
    )
    if status != 202 or not isinstance(body, dict):
        raise QuickstartError(f"deploy of {version} answered {status}: {body}")
    cmd_id = str(body["cmd_id"])
    run.passed(
        f"POST /v1/devices/…/deploy {version} apply=on_command",
        f"202, cmd_id {cmd_id}, reused {body.get('reused')}",
    )
    if body.get("reused") is not False:
        raise QuickstartError(f"the deploy of {version} reused an intent: {body}")
    staged_line = STAGED_LINE.format(cmd=re.escape(cmd_id))
    offset = wait_for_lines(run, log, (staged_line,), timeout=240)
    slot = re.search(staged_line, log.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log.name}")
    if slot_not is not None and slot.group(1) == slot_not:
        raise QuickstartError(f"{version} was staged into {slot.group(1)}, the running slot")
    wait_for_device(
        run,
        f"staged ({version})",
        lambda r: _deploy_of(r).get("cmd_id") == cmd_id and _deploy_of(r).get("state") == "staged",
        timeout=60,
        log=log,
    )
    run.passed(f"deploy state ({version})", f"staged into {slot.group(1)}")
    return cmd_id, slot.group(1), offset


def _command_result(run: Run, command_id: str) -> str | None:
    status, body, _ = http(run, "GET", f"/v1/devices/{DEVICE_ID}/commands/{command_id}")
    if status != 200 or not isinstance(body, dict):
        raise QuickstartError(f"GET …/commands/{command_id} answered {status}")
    result = body.get("result")
    return None if result is None else str(result)


def _kill_emulator(run: Run) -> None:
    """`docker kill` at once (no `just`, no grace period), then reap `just lib-qemu`."""
    subprocess.run(["docker", "kill", QEMU_CONTAINER], capture_output=True, check=False)
    if run.qemu is not None:
        try:
            run.qemu.wait(timeout=60)
        except subprocess.TimeoutExpired:
            run.qemu.kill()
        run.qemu = None


def phase_apply_waits(
    run: Run, bundle_w: Path, version_w: str, version_b: str, slot_b: str, log_prev: Path
) -> Path:
    """R5-test-1: the real `apply` verb sent into an unsafe window; the reboot must wait.

    Build W's safe-window rule is a clock (unsafe 60 s of every 80 s) that prints each flip.
    W is installed first (`on_command` + power cycle, like B); then B is staged again and the
    `apply` verb is sent just after a fresh `window: unsafe` line: the board must answer
    `deferred`, report `awaiting_safe_window`, and only at `window: safe` walk `applying`,
    `rebooting`. QEMU cannot survive the `esp_restart()` that follows (runbook: *The emulator
    cannot survive esp_restart()*): the soft reset panics while B is PENDING_VERIFY and the
    bootloader would abort B. So the emulator is killed inside the agent's 1.5 s drain and
    started again; the boot pointer was written at `staged`, so it boots B, as the apply would
    have. Returns the console log of the board back on B.
    """
    run.say("\n== Phase 2f: apply into an unsafe window: the reboot must wait ==")
    cmd_w, slot_w, _ = _upload_and_stage(run, version_w, bundle_w, log_prev, slot_b)
    time.sleep(1)
    run.say("  power-cycle: just agent-qemu-stop esp32; just lib-qemu")
    stop_qemu(run)
    log_w = start_qemu(run, fresh=False, log_name="qemu-W.log")
    wait_for_lines(
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
    wait_for_lines(
        run, log_w, (re.escape(f"morse: {WINDOW_MESSAGE} (firmware {version_w})"),), timeout=180
    )
    row = wait_for_device(
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

    # B again: its artifact exists (2b); a confirmed transaction is terminal, so a new cmd_id.
    cmd, slot, offset = _upload_and_stage(run, version_b, None, log_w, slot_w)
    if slot != slot_b:
        raise QuickstartError(f"B was staged into {slot}, not its own slot {slot_b}")
    # Into a fresh unsafe stretch: the flip is printed at most one message late (~6 s), so at
    # least ~50 s of it remain when the verb is sent.
    offset = wait_for_lines(run, log_w, (r"window: unsafe at millis \d+",), 120, start=offset)
    status, body, _ = http(run, "POST", f"/v1/devices/{DEVICE_ID}/deploys/{cmd}/apply")
    t_apply = time.monotonic()
    if status != 202 or not isinstance(body, dict):
        raise QuickstartError(f"POST …/deploys/{cmd}/apply answered {status}: {body}")
    command_id = str(body["command_id"])
    run.passed("POST /v1/devices/…/deploys/…/apply", f"202, command_id {command_id}")
    early = (rf"cmd_id={re.escape(cmd)} state=applying", re.escape(RELEASING_LINE))
    wait_for_lines(
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
    row = wait_for_device(
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

    wait_for_lines(
        run,
        log_w,
        (r"window: safe at millis \d+", rf"cmd_id={re.escape(cmd)} state=applying"),
        timeout=60,
        start=offset,
        poll=0.1,
    )
    waited = time.monotonic() - t_apply
    wait_for_lines(
        run, log_w, (rf"cmd_id={re.escape(cmd)} state=rebooting",), 10, start=offset, poll=0.1
    )
    # The kill race: the agent drains COMMAND_REBOOT_DRAIN_MS, then esp_restart(). Give the two
    # statuses a moment to reach the broker (or see them land), then kill the emulator.
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
    wait_for_lines(
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
    wait_for_lines(run, log_back, (re.escape(f"morse: HELLO (firmware {version_b})"),), timeout=180)
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
    row = wait_for_device(
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


def phase_board(run: Run, only_safe_window: bool = False) -> None:
    password = preflight(run)
    run.say("\n== Phase 2: the board steps, in QEMU against the dev stack ==")
    run.passed(
        "preflight", f"{run.base} healthy, api origins 10.0.2.2, no {QEMU_CONTAINER} running"
    )
    epoch = int(time.time())
    version_b = f"1.1.0-qs{epoch}"
    version_r = rollback_version(epoch)
    version_f = selftest_fail_version(epoch)
    version_t = selftest_timeout_version(epoch)
    version_w = window_version(epoch)
    bundles = build_bundles(
        run,
        version_b,
        version_r,
        version_f,
        version_t,
        version_w,
        only_safe_window=only_safe_window,
    )
    login_and_mint(run, password)
    del password
    log_a = phase_enroll(run, run.guest_base)
    slot_b, log_b = phase_ota(run, bundles.b, version_b, log_a)
    log_back = log_b
    if not only_safe_window:
        log_back = phase_rollback(run, _need(bundles.r, "R"), version_b, version_r, slot_b, log_b)
        log_back = phase_selftest_rollback(
            run,
            _need(bundles.f, "F"),
            (version_b, version_f),
            SELFTEST_FAIL_MESSAGE,
            "fail",
            slot_b,
            log_back,
        )
        log_back = phase_selftest_rollback(
            run,
            _need(bundles.t, "T"),
            (version_b, version_t),
            SELFTEST_TIMEOUT_MESSAGE,
            "timeout",
            slot_b,
            log_back,
        )
    phase_apply_waits(run, bundles.w, version_w, version_b, slot_b, log_back)
    stop_qemu(run)
    check_no_credentials(run)
    try:
        row = wait_for_device(
            run, "offline", lambda r: r.get("online") is False, 30, run.qemu_logs[-1]
        )
        run.say(f"  note  unplugged: online {row['online']} (the broker published the LWT)")
    except QuickstartError:
        run.say("  note  still online 30 s after the unplug (reported, not a failure)")


# ── main ────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--build-only", action="store_true", help="Phase 1 only: no stack, no QEMU")
    parser.add_argument(
        "--base",
        default=f"http://localhost:{os.environ.get('FF_HTTP_PORT', '8088')}",
        help="the dev API origin, as seen from this host",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temp tree (it then holds a LIVE credential in .qemu/)",
    )
    parser.add_argument(
        "--skip-idf", action="store_true", help="skip the ESP-IDF builds (dev only)"
    )
    parser.add_argument(
        "--only-safe-window",
        action="store_true",
        help="build A, B and W only and run 2a, 2b, 2f (dev only; the acceptance run is the full one)",
    )
    parser.add_argument(
        "--fresh-pio-core",
        action="store_true",
        help="run the README blocks on an empty PlatformIO core (needs --build-only; ~4.5 GB)",
    )
    args = parser.parse_args(argv)
    if args.fresh_pio_core and not args.build_only:
        parser.error("--fresh-pio-core needs --build-only (the board phase gains nothing)")

    port = args.base.rsplit(":", 1)[-1].strip("/")
    root = Path(tempfile.mkdtemp(prefix="ff-quickstart-"))
    root.chmod(0o700)
    run = Run(
        tree=root / "repo",
        work=root / "work",
        logs=root / "logs",
        base=args.base.rstrip("/"),
        guest_base=f"http://10.0.2.2:{port}",
        mqtt_uri=f"mqtt://10.0.2.2:{os.environ.get('FF_MQTT_PORT', '8883')}",
    )
    run.work.mkdir()
    run.logs.mkdir()
    if args.fresh_pio_core:
        core = root / "pio-core"
        core.mkdir()
        run.block_env = {**os.environ, "PLATFORMIO_CORE_DIR": str(core)}
        run.say(f"  PlatformIO core for the README blocks: {core} (empty)")
    started = time.monotonic()
    code = 1
    try:
        run.say(f"== Phase 0: clean tree in {root} ==")
        clean_tree(REPO_ROOT, root)
        run.passed(
            "clean tree", "git ls-files --cached --others --exclude-standard, no build output"
        )
        phase_builds(run, args.skip_idf)
        if not args.build_only:
            phase_board(run, only_safe_window=args.only_safe_window)
        code = 0
    except SetupError as error:
        run.say(f"\nSETUP FAILED: {error}")
        code = 2
    except (QuickstartError, subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        run.say(f"\nFAILED: {error}")
        code = 1
    finally:
        if run.qemu is not None:
            stop_qemu(run)
        if run.token_id is not None and not run.token_burned:
            try:
                status, _, _ = http(run, "POST", f"/v1/enrollment-tokens/{run.token_id}/revoke")
                run.say(f"  the unused enrollment token {run.token_id} was revoked ({status})")
            except OSError as error:
                run.say(f"  could NOT revoke enrollment token {run.token_id}: {error}")
        if args.keep:
            run.say(f"\nKEPT {root} — it holds a LIVE credential (.qemu/); delete it when done.")
        else:
            shutil.rmtree(root, ignore_errors=True)

    run.say(f"\n== {'PASS' if code == 0 else 'FAIL'} ({time.monotonic() - started:.0f} s) ==")
    if code == 0:
        width = max(len(check) for check, _ in run.results)
        for check, observed in run.results:
            print(f"  {check.ljust(width)}  {observed}")
    return code


if __name__ == "__main__":
    sys.exit(main())
