#!/usr/bin/env python3
"""Play the worked example's README quickstart, end to end (`R3-fw-4`, `R3-test-1`).

Runs the verbatim `# quickstart: <name>` fenced shell blocks from the Arduino and ESP-IDF
example READMEs (`--build-only` / `--fresh-pio-core`), and when run against the live dev
stack exercises enrollment, OTA update, broken-build rollback, failing/timed-out self-tests,
and safe-window `apply` deferral inside QEMU. Stdlib only: never imports `fleetforge`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

from .quickstart_build import (
    IDF_PROGRAM as IDF_PROGRAM,
)
from .quickstart_build import (
    _command_result as _command_result,
)
from .quickstart_build import (
    _kill_emulator as _kill_emulator,
)
from .quickstart_build import (
    _upload_and_stage as _upload_and_stage,
)
from .quickstart_build import (
    admin_password as admin_password,
)
from .quickstart_build import (
    build_bundles as build_bundles,
)
from .quickstart_build import (
    clean_tree as clean_tree,
)
from .quickstart_build import (
    device as device,
)
from .quickstart_build import (
    http as http,
)
from .quickstart_build import (
    idf_build as idf_build,
)
from .quickstart_build import (
    just as just,
)
from .quickstart_build import (
    login_and_mint as login_and_mint,
)
from .quickstart_build import (
    phase_builds as phase_builds,
)
from .quickstart_build import (
    preflight as preflight,
)
from .quickstart_build import (
    run_block as run_block,
)
from .quickstart_build import (
    run_logged as run_logged,
)
from .quickstart_build import (
    start_qemu as start_qemu,
)
from .quickstart_build import (
    stop_qemu as stop_qemu,
)
from .quickstart_build import (
    wait_for_device_impl,
    wait_for_lines_impl,
)
from .quickstart_steps import (
    phase_apply_waits as phase_apply_waits,
)
from .quickstart_steps import (
    phase_enroll as phase_enroll,
)
from .quickstart_steps import (
    phase_ota as phase_ota,
)
from .quickstart_steps import (
    phase_rollback as phase_rollback,
)
from .quickstart_steps import (
    phase_selftest_rollback as phase_selftest_rollback,
)
from .quickstart_verify import (
    API_CONTAINER as API_CONTAINER,
)
from .quickstart_verify import (
    APPLY_WAITS_LINE as APPLY_WAITS_LINE,
)
from .quickstart_verify import (
    ARDUINO_DIR as ARDUINO_DIR,
)
from .quickstart_verify import (
    ARDUINO_LAYOUT as ARDUINO_LAYOUT,
)
from .quickstart_verify import (
    ARDUINO_README as ARDUINO_README,
)
from .quickstart_verify import (
    COMMAND_REBOOT_DRAIN_MS as COMMAND_REBOOT_DRAIN_MS,
)
from .quickstart_verify import (
    CONFIRM_TIMEOUT_S as CONFIRM_TIMEOUT_S,
)
from .quickstart_verify import (
    DETAIL_APPLY_WAITS as DETAIL_APPLY_WAITS,
)
from .quickstart_verify import (
    DETAIL_MAX as DETAIL_MAX,
)
from .quickstart_verify import (
    DEVICE_ID as DEVICE_ID,
)
from .quickstart_verify import (
    EFUSE_BURN_OPTIONS as EFUSE_BURN_OPTIONS,
)
from .quickstart_verify import (
    EXAMPLES as EXAMPLES,
)
from .quickstart_verify import (
    IDF_DIR as IDF_DIR,
)
from .quickstart_verify import (
    IDF_README as IDF_README,
)
from .quickstart_verify import (
    LOOP_HEAD as LOOP_HEAD,
)
from .quickstart_verify import (
    LOOP_HEAD_SAYS as LOOP_HEAD_SAYS,
)
from .quickstart_verify import (
    MARKER as MARKER,
)
from .quickstart_verify import (
    OTA_SLOT_SIZE as OTA_SLOT_SIZE,
)
from .quickstart_verify import (
    OUR_PATHS as OUR_PATHS,
)
from .quickstart_verify import (
    PINNED_PLATFORM_VERSION as PINNED_PLATFORM_VERSION,
)
from .quickstart_verify import (
    PIO_ENVS as PIO_ENVS,
)
from .quickstart_verify import (
    PIO_ROW as PIO_ROW,
)
from .quickstart_verify import (
    QEMU_CONTAINER as QEMU_CONTAINER,
)
from .quickstart_verify import (
    REBOOT_WAITS_LINE as REBOOT_WAITS_LINE,
)
from .quickstart_verify import (
    REBOOTING_FOR_LINE as REBOOTING_FOR_LINE,
)
from .quickstart_verify import (
    RELEASING_LINE as RELEASING_LINE,
)
from .quickstart_verify import (
    REPO_ROOT as REPO_ROOT,
)
from .quickstart_verify import (
    REQUIRED_BLOCKS as REQUIRED_BLOCKS,
)
from .quickstart_verify import (
    ROLLBACK_HOOK_NEEDLE as ROLLBACK_HOOK_NEEDLE,
)
from .quickstart_verify import (
    ROLLBACK_MESSAGE as ROLLBACK_MESSAGE,
)
from .quickstart_verify import (
    ROLLBACK_TEST_FLAGS as ROLLBACK_TEST_FLAGS,
)
from .quickstart_verify import (
    SAFE_WINDOW_CLOCK as SAFE_WINDOW_CLOCK,
)
from .quickstart_verify import (
    SAFE_WINDOW_STUB as SAFE_WINDOW_STUB,
)
from .quickstart_verify import (
    SELFTEST_FAIL_MESSAGE as SELFTEST_FAIL_MESSAGE,
)
from .quickstart_verify import (
    SELFTEST_FAIL_REASON as SELFTEST_FAIL_REASON,
)
from .quickstart_verify import (
    SELFTEST_FAIL_STUB as SELFTEST_FAIL_STUB,
)
from .quickstart_verify import (
    SELFTEST_LOOP_ANSWER as SELFTEST_LOOP_ANSWER,
)
from .quickstart_verify import (
    SELFTEST_LOOP_SILENCED as SELFTEST_LOOP_SILENCED,
)
from .quickstart_verify import (
    SELFTEST_REASON_MAX as SELFTEST_REASON_MAX,
)
from .quickstart_verify import (
    SELFTEST_RUN_LINE as SELFTEST_RUN_LINE,
)
from .quickstart_verify import (
    SELFTEST_STUB as SELFTEST_STUB,
)
from .quickstart_verify import (
    SELFTEST_TIMEOUT_MESSAGE as SELFTEST_TIMEOUT_MESSAGE,
)
from .quickstart_verify import (
    SKETCH as SKETCH,
)
from .quickstart_verify import (
    STAGED_LINE as STAGED_LINE,
)
from .quickstart_verify import (
    VERSION_A as VERSION_A,
)
from .quickstart_verify import (
    WINDOW_MESSAGE as WINDOW_MESSAGE,
)
from .quickstart_verify import (
    WINDOW_PERIOD_MS as WINDOW_PERIOD_MS,
)
from .quickstart_verify import (
    WINDOW_STEP_ORDER as WINDOW_STEP_ORDER,
)
from .quickstart_verify import (
    WINDOW_UNSAFE_MS as WINDOW_UNSAFE_MS,
)
from .quickstart_verify import (
    Bundles as Bundles,
)
from .quickstart_verify import (
    QuickstartError as QuickstartError,
)
from .quickstart_verify import (
    Row as Row,
)
from .quickstart_verify import (
    Run as Run,
)
from .quickstart_verify import (
    SetupError as SetupError,
)
from .quickstart_verify import (
    _deploy_of as _deploy_of,
)
from .quickstart_verify import (
    _need as _need,
)
from .quickstart_verify import (
    _number as _number,
)
from .quickstart_verify import (
    _replace_once as _replace_once,
)
from .quickstart_verify import (
    _sections as _sections,
)
from .quickstart_verify import (
    _size as _size,
)
from .quickstart_verify import (
    _step_states as _step_states,
)
from .quickstart_verify import (
    app_version as app_version,
)
from .quickstart_verify import (
    check_app as check_app,
)
from .quickstart_verify import (
    check_envs as check_envs,
)
from .quickstart_verify import (
    check_no_credentials as check_no_credentials,
)
from .quickstart_verify import (
    check_no_warnings as check_no_warnings,
)
from .quickstart_verify import (
    check_rollback_row as check_rollback_row,
)
from .quickstart_verify import (
    check_selftest as check_selftest,
)
from .quickstart_verify import (
    check_window_log as check_window_log,
)
from .quickstart_verify import (
    confirmed_breach as confirmed_breach,
)
from .quickstart_verify import (
    efuse_burns as efuse_burns,
)
from .quickstart_verify import (
    expected_selftest as expected_selftest,
)
from .quickstart_verify import (
    extract_blocks as extract_blocks,
)
from .quickstart_verify import (
    has_rollback_hook as has_rollback_hook,
)
from .quickstart_verify import (
    idf_image as idf_image,
)
from .quickstart_verify import (
    our_warnings as our_warnings,
)
from .quickstart_verify import (
    parse_rows as parse_rows,
)
from .quickstart_verify import (
    pio_summary as pio_summary,
)
from .quickstart_verify import (
    require_blocks as require_blocks,
)
from .quickstart_verify import (
    rollback_version as rollback_version,
)
from .quickstart_verify import (
    selftest_fail_version as selftest_fail_version,
)
from .quickstart_verify import (
    selftest_failed_detail as selftest_failed_detail,
)
from .quickstart_verify import (
    selftest_timeout_version as selftest_timeout_version,
)
from .quickstart_verify import (
    steps_in_order as steps_in_order,
)
from .quickstart_verify import (
    tail as tail,
)
from .quickstart_verify import (
    window_version as window_version,
)


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
    """Wait for each regex in `patterns` in order, honouring `lib_quickstart.time.sleep`."""
    return wait_for_lines_impl(
        run,
        log,
        patterns,
        timeout,
        forbidden=forbidden,
        start=start,
        poll=poll,
        sleep_fn=lambda s: time.sleep(s),
    )


def wait_for_device(
    run: Run,
    what: str,
    ok: Callable[[dict[str, Any]], bool],
    timeout: float,
    log: Path,
    forbid: Callable[[dict[str, Any] | None], str | None] | None = None,
) -> dict[str, Any]:
    """Poll the device row until `ok`, honouring `lib_quickstart.time.sleep`."""
    return wait_for_device_impl(
        run,
        what,
        ok,
        timeout,
        log,
        forbid=forbid,
        sleep_fn=lambda s: time.sleep(s),
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
