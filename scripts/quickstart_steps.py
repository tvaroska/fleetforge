# ruff: noqa: S310, S603, S607
"""Phase 2a–2e QEMU board step runners (`phase_enroll`, `phase_ota`, `phase_rollback`, `phase_selftest_rollback`).

Stdlib-only: never imports `fleetforge`.
"""

from __future__ import annotations

import re
import sys
import time
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
    device,
    http,
    just,
    start_qemu,
    stop_qemu,
    wait_for_device_impl,
    wait_for_lines_impl,
)
from .quickstart_verify import (
    ARDUINO_DIR,
    ARDUINO_LAYOUT,
    CONFIRM_TIMEOUT_S,
    DETAIL_MAX,
    DEVICE_ID,
    ROLLBACK_HOOK_NEEDLE,
    ROLLBACK_MESSAGE,
    SELFTEST_FAIL_REASON,
    SELFTEST_RUN_LINE,
    STAGED_LINE,
    VERSION_A,
    QuickstartError,
    Run,
    _deploy_of,
    check_rollback_row,
    check_selftest,
    confirmed_breach,
    expected_selftest,
    parse_rows,
    selftest_failed_detail,
)
from .quickstart_window import phase_apply_waits as phase_apply_waits


def phase_enroll(run: Run, port_guest: str) -> Path:
    """README steps 2-5: token, ff_cfg, flash, watch it join (QEMU stands in for USB)."""
    run.say("\n== Phase 2a: enroll and heartbeat (QEMU for the USB flash) ==")
    if run.token is None:
        raise QuickstartError("no enrollment token was minted")
    run.say(
        f"  write .qemu/ff_cfg.bin: api {port_guest}, mqtt {run.mqtt_uri}, ethernet, hb 10 s, "
        "token <minted>"
    )
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
    wait_for_lines_impl(
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
    row = wait_for_device_impl(
        run,
        f"online at {VERSION_A} with layout {ARDUINO_LAYOUT} and capabilities ota, selftest, "
        "safe_window",
        lambda r: (
            r.get("online") is True
            and r.get("fw_version") == VERSION_A
            and r.get("partition_layout") == ARDUINO_LAYOUT
            and "ota" in (r.get("capabilities") or [])
            and "selftest" in (r.get("capabilities") or [])
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
    staged_line = STAGED_LINE.format(cmd=re.escape(cmd_id))
    wait_for_lines_impl(run, log_a, (staged_line,), timeout=240)
    slot = re.search(staged_line, log_a.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log_a.name}")
    slot_b = slot.group(1)
    wait_for_device_impl(
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
    wait_for_lines_impl(
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
    wait_for_lines_impl(
        run, log_b, (re.escape(f"morse: HELLO (firmware {version_b})"),), timeout=180
    )
    row = wait_for_device_impl(
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


def phase_rollback(
    run: Run, bundle_r: Path, version_b: str, version_r: str, slot_b: str, log_b: Path
) -> Path:
    """`R3-test-1`: deploy a build that cannot confirm; it must roll back to B, unaided."""
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
    wait_for_lines_impl(run, log_b, (staged_line,), timeout=240)
    slot = re.search(staged_line, log_b.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log_b.name}")
    slot_r = slot.group(1)
    if slot_r == slot_b:
        raise QuickstartError(f"R was staged into {slot_r}, the slot B is running from")
    wait_for_device_impl(
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
    never_confirmed = (r"CONFIRMED", r"transaction \S+: confirmed", re.escape(SELFTEST_RUN_LINE))
    wait_for_lines_impl(
        run,
        log_r,
        (re.escape(f"morse: {ROLLBACK_MESSAGE} (firmware {version_r})"),),
        timeout=180,
        forbidden=never_confirmed,
    )
    position = wait_for_lines_impl(
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
        wait_for_lines_impl(
            run, log_r2, (re.escape(f"morse: HELLO (firmware {version_b})"),), timeout=180
        )
    else:
        wait_for_lines_impl(
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

    row = wait_for_device_impl(
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
    """`R5-fw-2`: deploy a build whose self-test fails (`mode="fail"`) or never answers."""
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
    wait_for_lines_impl(run, log_prev, (staged_line,), timeout=240)
    slot = re.search(staged_line, log_prev.read_text(errors="replace"))
    if slot is None:
        raise QuickstartError(f"no `{staged_line}` in {log_prev.name}")
    slot_new = slot.group(1)
    if slot_new == slot_b:
        raise QuickstartError(f"{version} was staged into {slot_new}, the slot B is running from")
    wait_for_device_impl(
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
            r"the custom self-test failed ",
        )
        timeout = 180.0
        head = selftest_failed_detail(SELFTEST_FAIL_REASON, slot_b)
    else:
        decided = (rf"no self-test result {CONFIRM_TIMEOUT_S} s after an OTA boot",)
        timeout = CONFIRM_TIMEOUT_S + 120.0
        head = f"selftest timed out; returned to {slot_b}"
    for first in (
        rf"OTA boot: {CONFIRM_TIMEOUT_S} s from now",
        re.escape(f"morse: {message} (firmware {version})"),
    ):
        wait_for_lines_impl(run, log, (first,), timeout=180, forbidden=never_confirmed)
    position = wait_for_lines_impl(
        run, log, (*common, *decided), timeout=timeout, forbidden=never_confirmed
    )

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
    wait_for_lines_impl(
        run,
        log_back,
        (rolled_line, re.escape(f"morse: HELLO (firmware {version_b})")),
        timeout=180,
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

    row = wait_for_device_impl(
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
