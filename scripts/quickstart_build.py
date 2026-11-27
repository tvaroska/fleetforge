# ruff: noqa: S310, S603, S607
"""Phase 0/1 tree & build runners, QEMU lifecycle, and API client for `lib_quickstart.py`.

Stdlib-only: never imports `fleetforge`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if not __package__:
    _repo_root = str(Path(__file__).resolve().parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    __package__ = "scripts"
    if __spec__ is not None:
        __spec__.name = f"scripts.{Path(__file__).stem}"

from .quickstart_verify import (
    API_CONTAINER,
    ARDUINO_DIR,
    ARDUINO_LAYOUT,
    ARDUINO_README,
    DEVICE_ID,
    IDF_DIR,
    IDF_README,
    LOOP_HEAD,
    LOOP_HEAD_SAYS,
    PINNED_PLATFORM_VERSION,
    PIO_ENVS,
    QEMU_CONTAINER,
    REPO_ROOT,
    REQUIRED_BLOCKS,
    ROLLBACK_MESSAGE,
    ROLLBACK_TEST_FLAGS,
    SAFE_WINDOW_CLOCK,
    SAFE_WINDOW_STUB,
    SELFTEST_FAIL_MESSAGE,
    SELFTEST_FAIL_REASON,
    SELFTEST_FAIL_STUB,
    SELFTEST_LOOP_ANSWER,
    SELFTEST_LOOP_SILENCED,
    SELFTEST_STUB,
    SELFTEST_TIMEOUT_MESSAGE,
    SKETCH,
    STAGED_LINE,
    VERSION_A,
    WINDOW_MESSAGE,
    Bundles,
    QuickstartError,
    Row,
    Run,
    SetupError,
    _deploy_of,
    _replace_once,
    _sections,
    _size,
    app_version,
    check_app,
    check_envs,
    check_no_warnings,
    efuse_burns,
    has_rollback_hook,
    idf_image,
    parse_rows,
    require_blocks,
    tail,
)


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
        if not src.is_file():
            continue
        dst = tree / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    if count == 0:
        raise QuickstartError("git ls-files listed nothing")
    return tree


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
    sketch.write_text(original)

    if skip_idf:
        run.say("  SKIP  ESP-IDF builds (--skip-idf)")
        return
    expected_rows = parse_rows((run.tree / "agent/partitions.csv").read_text())
    for name, target in (("idf-build", "esp32"), ("idf-build-s3", "esp32s3")):
        idf_build(run, name, idf[name], target, expected_rows)


def admin_password() -> str:
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
    """Every precondition, before a token is spent."""
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
    """Build QEMU bundles A, B, W (and R, F, T unless `only_safe_window`)."""
    bundle = run.tree / "lib-qemu" / ".pio" / "bundle" / "esp32-qemu"
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
        shutil.copytree(bundle_a, bundle)
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
    shutil.copytree(bundle_a, bundle)
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


def wait_for_lines_impl(
    run: Run,
    log: Path,
    patterns: Sequence[str],
    timeout: float,
    *,
    forbidden: Sequence[str] = (),
    start: int = 0,
    poll: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Wait for each regex in `patterns` in order in the growing QEMU log."""
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
        sleep_fn(poll)
    return position


def device(run: Run) -> dict[str, Any] | None:
    status, body, _ = http(run, "GET", "/v1/devices")
    if status != 200 or not isinstance(body, dict):
        raise QuickstartError(f"GET /v1/devices answered {status}")
    for row in body.get("devices", []):
        if isinstance(row, dict) and row.get("device_id") == DEVICE_ID:
            return row
    return None


def wait_for_device_impl(
    run: Run,
    what: str,
    ok: Callable[[dict[str, Any]], bool],
    timeout: float,
    log: Path,
    forbid: Callable[[dict[str, Any] | None], str | None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll the device row until `ok`; a message from `forbid` fails immediately."""
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
        sleep_fn(1)
    raise QuickstartError(
        f"GET /v1/devices: {DEVICE_ID} never {what} within {timeout:.0f} s; last row: "
        f"{json.dumps(row)[:600]}\n{tail(log)}"
    )


def _upload_and_stage(
    run: Run, version: str, bundle: Path | None, log: Path, slot_not: str | None
) -> tuple[str, str, int]:
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
    offset = wait_for_lines_impl(run, log, (staged_line,), timeout=240)
    slot = re.search(staged_line, log.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log.name}")
    if slot_not is not None and slot.group(1) == slot_not:
        raise QuickstartError(f"{version} was staged into {slot.group(1)}, the running slot")
    wait_for_device_impl(
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
    subprocess.run(["docker", "kill", QEMU_CONTAINER], capture_output=True, check=False)
    if run.qemu is not None:
        try:
            run.qemu.wait(timeout=60)
        except subprocess.TimeoutExpired:
            run.qemu.kill()
        run.qemu = None
