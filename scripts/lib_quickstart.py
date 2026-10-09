#!/usr/bin/env python3
"""Play the worked example's README quickstart, end to end (R3-fw-4).

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

Phase 2 - the board steps, in QEMU (`phase_enroll`, `phase_ota`). Only three things in the
README need a board or a network clone, and all are substituted:

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

R3-test-1 adds the rollback run as one more phase (`phase_rollback`) after `phase_ota`.
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


def just(run: Run, *args: str, log_name: str, timeout: float = 3600) -> str:
    return run_logged(run, ["just", *args], log_name, timeout=timeout)


def build_bundles(run: Run, version_b: str) -> tuple[Path, Path]:
    """Bundle A (the sketch as committed) and B (the README edit, run-unique version)."""
    bundle = run.tree / "lib-qemu" / ".pio" / "bundle" / "esp32-qemu"
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-A.log")
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
    just(run, "lib-bundle", "esp32-qemu", log_name="lib-bundle-B.log")
    bundle_b = run.work / "bundleB"
    shutil.copytree(bundle, bundle_b)

    sketch.write_text(original)
    shutil.rmtree(bundle)
    shutil.copytree(bundle_a, bundle)  # `lib-qemu --fresh` flashes from this directory
    if (bundle_a / "app.bin").read_bytes() == (bundle_b / "app.bin").read_bytes():
        raise QuickstartError("QEMU bundle B's app.bin is identical to A's")
    run.passed(
        "QEMU bundles",
        f"A {_size(bundle_a / 'app.bin')} B, B {_size(bundle_b / 'app.bin')} B, B != A",
    )
    return bundle_a, bundle_b


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


def wait_for_lines(run: Run, log: Path, patterns: Sequence[str], timeout: float) -> None:
    """Each pattern (a regex), in order, in the growing log; the emulator must stay up."""
    deadline = time.monotonic() + timeout
    position = 0
    pending = list(patterns)
    while pending:
        text = log.read_text(errors="replace")
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
        time.sleep(0.5)


def device(run: Run) -> dict[str, Any] | None:
    status, body, _ = http(run, "GET", "/v1/devices")
    if status != 200 or not isinstance(body, dict):
        raise QuickstartError(f"GET /v1/devices answered {status}")
    for row in body.get("devices", []):
        if isinstance(row, dict) and row.get("device_id") == DEVICE_ID:
            return row
    return None


def wait_for_device(
    run: Run, what: str, ok: Callable[[dict[str, Any]], bool], timeout: float, log: Path
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = device(run)
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
        f"online at {VERSION_A} with layout {ARDUINO_LAYOUT} and capability ota",
        lambda r: (
            r.get("online") is True
            and r.get("fw_version") == VERSION_A
            and r.get("partition_layout") == ARDUINO_LAYOUT
            and "ota" in (r.get("capabilities") or [])
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


def phase_ota(run: Run, bundle_b: Path, version_b: str, log_a: Path) -> None:
    """README steps 6-7: upload build B, deploy it, watch it confirm and report its version."""
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
    wait_for_lines(run, log_a, (r"is staged and bootable",), timeout=240)
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
    wait_for_lines(
        run,
        log_b,
        (r"CONFIRMED", re.escape(f"morse: HELLO (firmware {version_b})")),
        timeout=180,
    )
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
    run.passed(
        f"GET /v1/devices {DEVICE_ID} after the update",
        f"fw_version {row['fw_version']}, deploy {deploy['state']} (is_terminal "
        f"{deploy['is_terminal']}), {time.monotonic() - started:.0f} s after the deploy",
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


def phase_board(run: Run) -> None:
    password = preflight(run)
    run.say("\n== Phase 2: the board steps, in QEMU against the dev stack ==")
    run.passed(
        "preflight", f"{run.base} healthy, api origins 10.0.2.2, no {QEMU_CONTAINER} running"
    )
    version_b = f"1.1.0-qs{int(time.time())}"
    _, bundle_b = build_bundles(run, version_b)
    login_and_mint(run, password)
    del password
    log_a = phase_enroll(run, run.guest_base)
    phase_ota(run, bundle_b, version_b, log_a)
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
            phase_board(run)
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
