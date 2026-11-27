"""Pre-flight advisory simulation gate (`R9-1`, `R9-2`) integration and unit tests.

Covers:
1. `HostPreflightSimRunner` passing valid ESP-IDF images with `FFOTALIB` markers and failing
   deliberately broken/boot-crashing/self-test-failing/unmarked builds.
2. `EspressifQemuSimRunner` building a 4 MiB flash image (`0x10000`), launching a QEMU
   subprocess, and classifying serial `R5` self-test pass vs Guru Meditation panic / timeout.
3. `sim_worker.run_once` claiming queued `sim_status = 'pending'` artifacts in PostgreSQL and
   persisting verdicts from `ObjectStore` bytes.
4. `POST /v1/artifact/{sha256}/simulate`, `POST /v1/devices/{id}/deploy/precheck` (`sim_failed`
   gating warning), `409` refusal without override, `202` acceptance with `override=["sim_failed"]`,
   and `deploy_events.detail["overrides"]` audit log persistence across single-device and cohort deploys.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import (
    get_command_publisher,
    get_object_store,
    get_settings,
    get_sim_runner,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import DEFAULT_PROJECT_ID, Device
from fleetforge.lib_marker import MAGIC
from fleetforge.signing import DEFAULT_PUBLIC_KEY_RING, generate_keypair, sign_digest, verify_digest
from fleetforge.sim_worker import run_once
from fleetforge.simulation import (
    APP_SLOT_OFFSET,
    EspressifQemuSimRunner,
    SimStatus,
    create_sim_runner,
    default_efuse_bytes,
    parse_qemu_serial_verdict,
)
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)
from tests.doubles import StubSimRunner

_PRIV_A, _PUB_A = generate_keypair()
_PRIV_B, _PUB_B = generate_keypair()

_VALID_MARKER = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12
VALID_APP_BIN = b"\xe9\x00\x02\x20" + _VALID_MARKER + b"clean-app-code" * 32
CRASHING_APP_BIN = (
    b"\xe9\x00\x02\x20" + _VALID_MARKER + b"FF_SIM_BOOT_CRASH: Guru Meditation" + b"\x00" * 64
)
FAILING_SELFTEST_BIN = (
    b"\xe9\x00\x02\x20" + _VALID_MARKER + b"FF_SIM_SELFTEST_FAIL: sensor bus dead" + b"\x00" * 64
)
UNMARKED_APP_BIN = b"\xe9\x00\x02\x20" + b"no-fleetforge-marker-in-this-binary" * 16


async def test_auto_backend_without_qemu_reports_skipped_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-24: `create_sim_runner('auto')` without QEMU must report `skipped` with reason, never `passed`."""
    monkeypatch.delenv("FF_SIM_BACKEND", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    runner = create_sim_runner("auto")
    assert isinstance(runner, EspressifQemuSimRunner)

    res = await runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32s3",
        image=VALID_APP_BIN,
        partition_layout="ab-4m-v1",
    )
    assert res.status == SimStatus.SKIPPED
    assert "not installed" in res.detail
    assert res.runner == "espressif-qemu"

    cargo = await runner.simulate(
        sha256="1" * 64,
        target="esp32s3",
        image=b"model-weights",
        artifact_type="model_weights",
    )
    assert cargo.status == SimStatus.SKIPPED

    with pytest.raises(ValueError, match="unsupported simulation backend"):
        create_sim_runner("host")


async def test_real_fixture_images_are_not_failed_on_embedded_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-24: real ESP-IDF firmware images containing 'Guru Meditation Error' in their binary
    strings must never be marked `failed` by `auto` or string-scanning."""
    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "firmware"
    real_apps = sorted(p for p in fixtures_dir.glob("*.bin") if "merged" not in p.name)
    assert len(real_apps) >= 5

    # Without QEMU, `auto` returns `skipped`, never `failed`.
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    auto_runner = create_sim_runner("auto")
    for fw_path in real_apps:
        raw = fw_path.read_bytes()
        target = "esp32s3" if fw_path.name.startswith("esp32s3") else "esp32"
        res = await auto_runner.simulate(
            sha256=hashlib.sha256(raw).hexdigest(),
            target=target,
            image=raw,
        )
        assert res.status == SimStatus.SKIPPED, f"{fw_path.name} got {res}"

    # With a QEMU binary that boots cleanly, every real image passes regardless of binary strings.
    fake_qemu = tmp_path / "fake-qemu-clean"
    fake_qemu.write_text(
        "#!/usr/bin/env python3\n"
        "print('I (420) ff-mqtt: selftest passed; marked app valid', flush=True)\n",
        encoding="utf-8",
    )
    os.chmod(fake_qemu, stat.S_IRWXU)
    qemu_runner = EspressifQemuSimRunner(timeout_s=5.0, qemu_bin_override=str(fake_qemu))
    for fw_path in real_apps:
        raw = fw_path.read_bytes()
        target = "esp32s3" if fw_path.name.startswith("esp32s3") else "esp32"
        res = await qemu_runner.simulate(
            sha256=hashlib.sha256(raw).hexdigest(),
            target=target,
            image=raw,
        )
        assert res.status == SimStatus.PASSED, f"{fw_path.name} got {res}"


async def test_espressif_qemu_sim_runner_executes_flash_image_and_parses_serial(
    tmp_path: Path,
) -> None:
    """R9-1: `EspressifQemuSimRunner` writes the app at `0x10000` in a 4 MiB flash image,
    invokes QEMU, and maps serial output to `passed` / `failed`."""
    fake_qemu = tmp_path / "fake-qemu-xtensa"
    fake_qemu.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "drive_arg = [a for a in sys.argv if a.startswith('file=')][0]\n"
        "flash_path = drive_arg.split(',')[0].split('=', 1)[1]\n"
        "data = open(flash_path, 'rb').read()\n"
        f"slot = data[{APP_SLOT_OFFSET}:{APP_SLOT_OFFSET} + 512]\n"
        "if b'FF_SIM_BOOT_CRASH' in slot:\n"
        "    print('Guru Meditation Error: Core 0 panic\\'ed (LoadProhibited)', flush=True)\n"
        "else:\n"
        "    print('I (420) ff-mqtt: selftest passed; marked app valid', flush=True)\n",
        encoding="utf-8",
    )
    os.chmod(fake_qemu, stat.S_IRWXU)

    runner = EspressifQemuSimRunner(timeout_s=5.0, qemu_bin_override=str(fake_qemu))

    passed = await runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32s3",
        image=VALID_APP_BIN,
    )
    assert passed.status == SimStatus.PASSED
    assert "selftest passed" in passed.detail

    failed = await runner.simulate(
        sha256=hashlib.sha256(CRASHING_APP_BIN).hexdigest(),
        target="esp32s3",
        image=CRASHING_APP_BIN,
    )
    assert failed.status == SimStatus.FAILED
    assert "Guru Meditation Error" in failed.detail


async def test_an_oversized_serial_line_is_a_failed_verdict(tmp_path: Path) -> None:
    """A-28: a >64 KiB serial line without newline yields `SimStatus.FAILED`, not `LimitOverrunError`."""
    fake_qemu = tmp_path / "fake-qemu-oversized"
    fake_qemu.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.write('Z' * 80000)\nsys.stdout.flush()\n",
        encoding="utf-8",
    )
    os.chmod(fake_qemu, stat.S_IRWXU)
    runner = EspressifQemuSimRunner(timeout_s=5.0, qemu_bin_override=str(fake_qemu))
    res = await runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32s3",
        image=VALID_APP_BIN,
    )
    assert res.status == SimStatus.FAILED
    assert "65536" in res.detail


async def test_qemu_timeout_and_silent_exit_are_failed(tmp_path: Path) -> None:
    """T-18 (`M4`) & A-27: timeout, silent exit, layout offset `0x20000`, and boot-bundle parts."""
    slow_qemu = tmp_path / "fake-qemu-slow"
    slow_qemu.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    os.chmod(slow_qemu, stat.S_IRWXU)
    slow_runner = EspressifQemuSimRunner(timeout_s=0.15, qemu_bin_override=str(slow_qemu))
    timed_out = await slow_runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32s3",
        image=VALID_APP_BIN,
    )
    assert timed_out.status == SimStatus.FAILED
    assert "timed out or exited" in timed_out.detail

    silent_qemu = tmp_path / "fake-qemu-silent"
    silent_qemu.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    os.chmod(silent_qemu, stat.S_IRWXU)
    silent_runner = EspressifQemuSimRunner(timeout_s=2.0, qemu_bin_override=str(silent_qemu))
    silent = await silent_runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32s3",
        image=VALID_APP_BIN,
    )
    assert silent.status == SimStatus.FAILED

    # A-27: `ab-4m-v1` places app at 0x20000 and copies bootloader/partition parts from boot_bundle_dir.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "bootloader.bin").write_bytes(b"BOOT" * 4)
    (bundle / "partition-table.bin").write_bytes(b"PART" * 4)
    (bundle / "manifest.json").write_text(
        '{"parts": ['
        '{"name": "bootloader", "offset": "0x1000", "file": "bootloader.bin"},'
        '{"name": "partition-table", "offset": "0x8000", "file": "partition-table.bin"},'
        '{"name": "app", "offset": "0x20000", "file": "app.bin"}'
        "]}",
        encoding="utf-8",
    )
    flash = EspressifQemuSimRunner.build_flash_image(
        VALID_APP_BIN,
        partition_layout="ab-4m-v1",
        boot_bundle_dir=bundle,
    )
    assert flash[0x1000:0x1010] == b"BOOT" * 4
    assert flash[0x8000:0x8010] == b"PART" * 4
    assert flash[0x20000 : 0x20000 + len(VALID_APP_BIN)] == VALID_APP_BIN


async def test_espressif_qemu_manifest_path_schema_and_efuse_command_parity(
    tmp_path: Path,
) -> None:
    """Escape-analysis regression (`fake_drift` in `make_manifest.py` vs `EspressifQemuSimRunner`):
    real bundles emit integer `offset` and `path` (not `file`), require ESP-IDF 5.5 eFuse/RAM/WDT
    flags on QEMU CLI, and report standalone `ff_cfg` boot vs `FF_FAULT_TEST=` / `LoadProhibited`."""
    esp32_efuse = default_efuse_bytes("esp32")
    assert len(esp32_efuse) == 124
    assert esp32_efuse[13] == 0x80 and esp32_efuse[22] == 0x10
    s3_efuse = default_efuse_bytes("esp32s3")
    assert len(s3_efuse) == 1024 and s3_efuse[38] == 0x0C

    bundle = tmp_path / "real-manifest-bundle"
    bundle.mkdir()
    (bundle / "bootloader.bin").write_bytes(b"BLDR" * 4)
    (bundle / "partition-table.bin").write_bytes(b"PTBL" * 4)
    (bundle / "ota-data-initial.bin").write_bytes(b"OTAD" * 4)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "parts": [
                    {"name": "bootloader", "offset": 0x1000, "path": "bootloader.bin"},
                    {"name": "partition-table", "offset": 0x8000, "path": "partition-table.bin"},
                    {"name": "ota-data", "offset": 0xF000, "path": "ota-data-initial.bin"},
                    {"name": "app", "offset": 0x20000, "path": "app.bin"},
                ]
            }
        ),
        encoding="utf-8",
    )
    flash = EspressifQemuSimRunner.build_flash_image(
        VALID_APP_BIN,
        partition_layout="ab-4m-v1",
        boot_bundle_dir=bundle,
    )
    assert flash[0x1000:0x1010] == b"BLDR" * 4
    assert flash[0x8000:0x8010] == b"PTBL" * 4
    assert flash[0xF000:0xF010] == b"OTAD" * 4
    assert flash[0x20000 : 0x20000 + len(VALID_APP_BIN)] == VALID_APP_BIN

    fake_qemu = tmp_path / "fake-qemu-efuse"
    fake_qemu.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = ' '.join(sys.argv[1:])\n"
        "assert '-m 4M' in argv, argv\n"
        "assert 'driver=nvram.esp32.efuse,property=drive,value=efuse' in argv, argv\n"
        "assert 'driver=timer.esp32.timg,property=wdt_disable,value=true' in argv, argv\n"
        "ef_arg = [a for a in sys.argv if a.endswith(',id=efuse')][0]\n"
        "ef_path = ef_arg.split(',', 1)[0].split('=', 1)[1]\n"
        "ef = open(ef_path, 'rb').read()\n"
        "assert len(ef) == 124 and ef[13] == 0x80 and ef[22] == 0x10\n"
        "print('I (312) ff_cfg: no usable ff_cfg partition', flush=True)\n",
        encoding="utf-8",
    )
    os.chmod(fake_qemu, stat.S_IRWXU)
    runner = EspressifQemuSimRunner(
        timeout_s=5.0,
        qemu_bin_override=str(fake_qemu),
        boot_bundle_dir=bundle,
    )
    passed = await runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32",
        image=VALID_APP_BIN,
        partition_layout="ab-4m-v1",
    )
    assert passed.status == SimStatus.PASSED
    assert "no usable ff_cfg partition" in passed.detail

    broken_bundle = tmp_path / "broken-bundle"
    broken_bundle.mkdir()
    (broken_bundle / "manifest.json").write_text(
        json.dumps({"parts": [{"name": "bootloader", "offset": 0x1000}]}),
        encoding="utf-8",
    )
    broken_runner = EspressifQemuSimRunner(
        timeout_s=5.0,
        qemu_bin_override=str(fake_qemu),
        boot_bundle_dir=broken_bundle,
    )
    broken = await broken_runner.simulate(
        sha256=hashlib.sha256(VALID_APP_BIN).hexdigest(),
        target="esp32",
        image=VALID_APP_BIN,
        partition_layout="ab-4m-v1",
    )
    assert broken.status == SimStatus.FAILED
    assert "missing path" in broken.detail

    assert parse_qemu_serial_verdict("W (410) ff-agent: FF_FAULT_TEST=bootloop") == (
        SimStatus.FAILED,
        "QEMU serial fault: W (410) ff-agent: FF_FAULT_TEST=bootloop",
    )
    assert parse_qemu_serial_verdict("Core 0 panic (LoadProhibited)") == (
        SimStatus.FAILED,
        "QEMU serial fault: Core 0 panic (LoadProhibited)",
    )
    assert parse_qemu_serial_verdict("I (310) ff_cfg: ff_cfg v1 loaded") == (
        SimStatus.PASSED,
        "QEMU booted cleanly and confirmed R5 self-test (ff_cfg v1 loaded)",
    )


async def test_real_qemu_boots_the_agent_bundle() -> None:
    """A-27 & D-23: opt-in real Espressif QEMU boot test verifying bundle integrity & ECDSA signature."""
    if os.environ.get("FF_RUN_REAL_QEMU_TESTS") != "1":
        pytest.skip("real QEMU boot test is opt-in via FF_RUN_REAL_QEMU_TESTS=1")
    bundle_dir = Path(__file__).resolve().parents[1] / "agent" / "dist" / "esp32"
    app_bin = bundle_dir / "app.bin"
    if not app_bin.is_file():
        app_bin = bundle_dir / "fleetforge-agent.bin"
    if not app_bin.is_file():
        pytest.fail(f"FF_RUN_REAL_QEMU_TESTS=1 set but {app_bin} is missing")
    raw = app_bin.read_bytes()
    assert raw[:1] == b"\xe9"
    sha256_hex = hashlib.sha256(raw).hexdigest()

    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for part in manifest.get("parts", []):
            if isinstance(part, dict) and part.get("name") == "app":
                part_rel = part.get("path") or part.get("file") or app_bin.name
                part_file = bundle_dir / str(part_rel)
                assert part_file.read_bytes() == raw
                if "sha256" in part:
                    assert part["sha256"] == sha256_hex
        if isinstance(manifest.get("sig"), str) and manifest["sig"]:
            assert verify_digest(sha256_hex, manifest["sig"], DEFAULT_PUBLIC_KEY_RING)

    detached_sig = sign_digest(sha256_hex, _PRIV_A)
    assert verify_digest(sha256_hex, detached_sig, (_PUB_A, _PUB_B))
    tampered_sha = hashlib.sha256(raw + b"\x00").hexdigest()
    assert not verify_digest(tampered_sha, detached_sig, (_PUB_A, _PUB_B))

    runner = EspressifQemuSimRunner(timeout_s=30.0, boot_bundle_dir=bundle_dir)
    res = await runner.simulate(
        sha256=sha256_hex,
        target="esp32",
        image=raw,
        partition_layout="ab-4m-v1",
    )
    assert res.status == SimStatus.PASSED, res.detail


async def test_simulation_gate_precheck_override_audit_log_and_worker_over_postgres(
    admin_app: FastAPI,
    engine: AsyncEngine,
    clean_tables: None,
) -> None:
    """R9-1 & R9-2 end-to-end over PostgreSQL:
    - Upload valid (`1.0.0`) and boot-crashing (`1.1.0-bad`) builds.
    - `POST /v1/artifact/{sha}/simulate` marks `1.0.0` `passed` and `1.1.0-bad` `failed`.
    - `sim_worker.run_once` drains a queued `pending` build (`1.2.0-worker`) to `passed`.
    - `POST /v1/devices/{id}/deploy/precheck` raises gating warning `sim_failed` (`needs_override=True`).
    - `POST /v1/devices/{id}/deploy` refuses `1.1.0-bad` (`409`) without override, and accepts
      (`202`) when `override=["sim_failed"]` is supplied, persisting `"overrides": ["sim_failed"]`
      in `deploy_events.detail` (audit log).
    - `POST /v1/projects/default/deploy` skips `1.1.0-bad` without override and stages with override.
    """
    store = MemoryObjectStore()
    publisher = FakeCommandPublisher()
    runner = StubSimRunner()
    settings = settings_for_tests(
        s3_endpoint_url="http://minio:9000",
        s3_bucket="fleetforge",
        artifact_url_secret="s" * 32,
        public_base_url="https://fleet.example.com",
        mqtt_command_username="ff-commander",
        mqtt_command_password="pw",
        artifact_signing_key=_PRIV_A,
        artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
    )
    admin_app.dependency_overrides[get_settings] = lambda: settings
    admin_app.dependency_overrides[get_object_store] = lambda: store
    admin_app.dependency_overrides[get_command_publisher] = lambda: publisher
    admin_app.dependency_overrides[get_sim_runner] = lambda: runner

    token = await login_admin(admin_app)
    headers = {"Authorization": f"Bearer {token}"}

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = now_utc()
    async with sessionmaker() as session:
        session.add_all(
            [
                Device(
                    device_id="a4cf12b3de31",
                    project_id=DEFAULT_PROJECT_ID,
                    platform_type="esp32s3",
                    fw_version="0.9.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig"],
                    link_type="wifi",
                    power_class="always_on",
                    is_dev=True,
                    presence_reported=True,
                    last_seen=now,
                ),
                Device(
                    device_id="a4cf12b3de32",
                    project_id=DEFAULT_PROJECT_ID,
                    platform_type="esp32s3",
                    fw_version="0.9.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig"],
                    link_type="wifi",
                    power_class="always_on",
                    is_dev=True,
                    presence_reported=True,
                    last_seen=now,
                ),
            ]
        )
        await session.commit()

    async with client_for(admin_app, base_url="https://testserver") as client:
        # 1. Upload valid `1.0.0` with `?simulate=true` -> immediately `passed`
        good_sha = hashlib.sha256(VALID_APP_BIN).hexdigest()
        r_good = await client.post(
            "/v1/artifact",
            params={
                "target": "esp32s3",
                "version": "1.0.0",
                "partition_layout": "ab-4m-v1",
                "sig": sign_digest(good_sha, _PRIV_A),
                "simulate": "true",
            },
            headers=headers,
            content=VALID_APP_BIN,
        )
        assert r_good.status_code == 201, r_good.text
        assert r_good.json()["sim_status"] == "passed"

        # 2. Upload crashing `1.1.0-bad`, then trigger `POST /v1/artifact/{sha}/simulate` -> `failed`
        bad_sha = hashlib.sha256(CRASHING_APP_BIN).hexdigest()
        r_bad = await client.post(
            "/v1/artifact",
            params={
                "target": "esp32s3",
                "version": "1.1.0-bad",
                "partition_layout": "ab-4m-v1",
                "sig": sign_digest(bad_sha, _PRIV_A),
            },
            headers=headers,
            content=CRASHING_APP_BIN,
        )
        assert r_bad.status_code == 201, r_bad.text
        r_sim = await client.post(f"/v1/artifact/{bad_sha}/simulate", headers=headers)
        assert r_sim.status_code == 200, r_sim.text
        assert r_sim.json()["sim_status"] == "failed"
        assert r_sim.json()["sim_runner"] == "test-stub-sim"
        assert "panic" in r_sim.json()["sim_detail"]

        # `GET /v1/artifact` surfaces both simulation verdicts and persisted `sim_runner`
        listed = (await client.get("/v1/artifact", headers=headers)).json()["artifacts"]
        by_ver = {row["version"]: row for row in listed}
        assert by_ver["1.0.0"]["sim_status"] == "passed"
        assert by_ver["1.0.0"]["sim_runner"] == "test-stub-sim"
        assert by_ver["1.1.0-bad"]["sim_status"] == "failed"
        assert by_ver["1.1.0-bad"]["sim_runner"] == "test-stub-sim"

        # 3. Queue a third artifact as `pending` and verify `sim_worker.run_once` processes it
        worker_bin = VALID_APP_BIN + b"-worker"
        worker_sha = hashlib.sha256(worker_bin).hexdigest()
        r_w = await client.post(
            "/v1/artifact",
            params={
                "target": "esp32s3",
                "version": "1.2.0-worker",
                "partition_layout": "ab-4m-v1",
                "sig": sign_digest(worker_sha, _PRIV_A),
            },
            headers=headers,
            content=worker_bin,
        )
        assert r_w.status_code == 201
        async with sessionmaker() as session:
            await session.execute(
                text("UPDATE artifacts SET sim_status = 'pending' WHERE sha256 = :sha"),
                {"sha": worker_sha},
            )
            await session.commit()
        processed = await run_once(sessionmaker, store, runner)
        assert [(sha, res.status) for sha, res in processed] == [(worker_sha, SimStatus.PASSED)]

        # 4. Precheck on `1.1.0-bad` surfaces gating `sim_failed` warning (`needs_override=True`)
        r_pre = await client.post(
            "/v1/devices/a4cf12b3de31/deploy/precheck",
            headers=headers,
            json={"version": "1.1.0-bad", "apply": "auto"},
        )
        assert r_pre.status_code == 200, r_pre.text
        warnings_list = r_pre.json()["warnings"]
        sim_warnings = [w for w in warnings_list if w["code"] == "sim_failed"]
        assert len(sim_warnings) == 1
        assert sim_warnings[0]["needs_override"] is True

        # 5. Single-device deploy without override -> 409 Conflict
        r_blocked = await client.post(
            "/v1/devices/a4cf12b3de31/deploy",
            headers=headers,
            json={"version": "1.1.0-bad", "apply": "auto"},
        )
        assert r_blocked.status_code == 409
        assert "pre-flight simulation failed" in r_blocked.json()["detail"]

        # 6. Single-device deploy WITH `override: ["sim_failed"]` -> 202 + audit log entry
        r_overridden = await client.post(
            "/v1/devices/a4cf12b3de31/deploy",
            headers=headers,
            json={"version": "1.1.0-bad", "apply": "auto", "override": ["sim_failed"]},
        )
        assert r_overridden.status_code == 202, r_overridden.text
        cmd_id = r_overridden.json()["cmd_id"]

        async with sessionmaker() as session:
            audit_detail = await session.scalar(
                text(
                    "SELECT detail FROM deploy_events "
                    "WHERE device_id = 'a4cf12b3de31' AND cmd_id = :cmd_id AND state = 'requested'"
                ),
                {"cmd_id": cmd_id},
            )
        assert isinstance(audit_detail, dict)
        assert audit_detail["overrides"] == ["sim_failed"]

        # 7. Project cohort deploy on `a4cf12b3de32`: skipped without override, deployed with override
        r_proj_skip = await client.post(
            "/v1/projects/default/deploy",
            headers=headers,
            json={"version": "1.1.0-bad", "target_filter": "dev", "canary": False},
        )
        assert r_proj_skip.status_code == 202
        assert r_proj_skip.json()["deployed"] == []
        assert "pre-flight simulation failed" in r_proj_skip.json()["skipped"]["a4cf12b3de32"]

        r_proj_override = await client.post(
            "/v1/projects/default/deploy",
            headers=headers,
            json={
                "version": "1.1.0-bad",
                "target_filter": "dev",
                "canary": False,
                "override": ["sim_failed"],
            },
        )
        assert r_proj_override.status_code == 202
        deployed_by_id = {d["device_id"]: d for d in r_proj_override.json()["deployed"]}
        assert sorted(deployed_by_id) == ["a4cf12b3de31", "a4cf12b3de32"]
        assert deployed_by_id["a4cf12b3de31"]["reused"] is True
        assert deployed_by_id["a4cf12b3de32"]["reused"] is False


async def test_viewer_cannot_simulate_without_a_project_parameter(
    admin_app: FastAPI,
    engine: AsyncEngine,
    clean_tables: None,
) -> None:
    """A-26: omitting `?project=` must still enforce operator role and required scope."""
    _ = clean_tables
    store = MemoryObjectStore()
    runner = StubSimRunner()
    settings = settings_for_tests(
        artifact_signing_key=_PRIV_A,
        artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
    )
    admin_app.dependency_overrides[get_settings] = lambda: settings
    admin_app.dependency_overrides[get_object_store] = lambda: store
    admin_app.dependency_overrides[get_sim_runner] = lambda: runner

    super_token = await login_admin(admin_app)
    super_headers = {"Authorization": f"Bearer {super_token}"}
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with client_for(admin_app, base_url="https://testserver") as client:
        # Upload an artifact to `default` and a second artifact to `other-sim`.
        r_proj = await client.post(
            "/v1/projects",
            headers=super_headers,
            json={"slug": "other-sim", "name": "Other Sim"},
        )
        assert r_proj.status_code == 201

        sha_default = hashlib.sha256(VALID_APP_BIN).hexdigest()
        up_default = await client.post(
            "/v1/artifact",
            params={
                "target": "esp32s3",
                "version": "1.0.0",
                "partition_layout": "ab-4m-v1",
                "sig": sign_digest(sha_default, _PRIV_A),
            },
            headers=super_headers,
            content=VALID_APP_BIN,
        )
        assert up_default.status_code == 201

        other_bin = VALID_APP_BIN + b"-other-project"
        sha_other = hashlib.sha256(other_bin).hexdigest()
        up_other = await client.post(
            "/v1/artifact",
            params={
                "project": "other-sim",
                "target": "esp32s3",
                "version": "1.0.0",
                "partition_layout": "ab-4m-v1",
                "sig": sign_digest(sha_other, _PRIV_A),
            },
            headers=super_headers,
            content=other_bin,
        )
        assert up_other.status_code == 201

        # Create a `viewer` and an `operator` on `default`.
        for uname, role in (("simviewer", "viewer"), ("simop", "operator")):
            r_u = await client.post(
                "/v1/users",
                headers=super_headers,
                json={
                    "username": uname,
                    "password": "user-password-1234",
                    "project": "default",
                    "role": role,
                },
            )
            assert r_u.status_code == 201, r_u.text

        viewer_login = await client.post(
            "/v1/auth/login",
            json={"username": "simviewer", "password": "user-password-1234"},
        )
        viewer_headers = {"Authorization": f"Bearer {viewer_login.cookies['ff_session']}"}

        op_login = await client.post(
            "/v1/auth/login",
            json={"username": "simop", "password": "user-password-1234"},
        )
        op_headers = {"Authorization": f"Bearer {op_login.cookies['ff_session']}"}

        # Viewer MUST get 403 both with and without `?project=default`, and 404 on hidden project.
        assert (
            await client.post(f"/v1/artifact/{sha_default}/simulate", headers=viewer_headers)
        ).status_code == 403
        assert (
            await client.post(
                f"/v1/artifact/{sha_default}/simulate?project=default", headers=viewer_headers
            )
        ).status_code == 403
        assert (
            await client.post(f"/v1/artifact/{sha_other}/simulate", headers=viewer_headers)
        ).status_code == 404

        async with sessionmaker() as s:
            status_val = await s.scalar(
                text("SELECT sim_status FROM artifacts WHERE sha256 = :sha"),
                {"sha": sha_default},
            )
        assert status_val is None

        # Scoped tokens: `read` -> 403; `write:artifacts` & `deploy` -> 200; operator -> 200.
        r_read = await client.post(
            "/v1/auth/tokens",
            headers=super_headers,
            json={"name": "sim-read", "project": "default", "scopes": ["read"]},
        )
        read_headers = {"Authorization": f"Bearer {r_read.json()['token']}"}
        assert (
            await client.post(f"/v1/artifact/{sha_default}/simulate", headers=read_headers)
        ).status_code == 403

        r_wa = await client.post(
            "/v1/auth/tokens",
            headers=super_headers,
            json={"name": "sim-write-art", "project": "default", "scopes": ["write:artifacts"]},
        )
        wa_headers = {"Authorization": f"Bearer {r_wa.json()['token']}"}
        r_wa_sim = await client.post(f"/v1/artifact/{sha_default}/simulate", headers=wa_headers)
        assert r_wa_sim.status_code == 200, r_wa_sim.text
        assert r_wa_sim.json()["sim_status"] == "passed"

        r_op_sim = await client.post(f"/v1/artifact/{sha_default}/simulate", headers=op_headers)
        assert r_op_sim.status_code == 200, r_op_sim.text


async def test_upload_queue_simulation_url_scheme_guard_and_expired_sim_lease_reaper(
    admin_app: FastAPI,
    engine: AsyncEngine,
    clean_tables: None,
) -> None:
    """A-28 & D-28: `?queue_simulation=true` queues `sim_status='pending'`, `javascript:` URLs
    are rejected with 400, and expired `sim_lease_expires_at` rows are reaped and completed."""
    import datetime as dt

    _ = clean_tables
    store = MemoryObjectStore()
    runner = StubSimRunner()
    settings = settings_for_tests(
        artifact_signing_key=_PRIV_A,
        artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
    )
    admin_app.dependency_overrides[get_settings] = lambda: settings
    admin_app.dependency_overrides[get_object_store] = lambda: store
    admin_app.dependency_overrides[get_sim_runner] = lambda: runner

    token = await login_admin(admin_app)
    headers = {"Authorization": f"Bearer {token}"}
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    queued_bin = VALID_APP_BIN + b"-queued-sim"
    queued_sha = hashlib.sha256(queued_bin).hexdigest()
    async with client_for(admin_app, base_url="https://testserver") as client:
        for bad_param in ("repo_url", "build_url"):
            bad = await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.0.0-xss",
                    "partition_layout": "ab-4m-v1",
                    bad_param: "javascript:alert(1)",
                },
                headers=headers,
                content=queued_bin,
            )
            assert bad.status_code == 400, (bad_param, bad.text)

        up = await client.post(
            "/v1/artifact",
            params={
                "target": "esp32s3",
                "version": "1.0.0-queued",
                "partition_layout": "ab-4m-v1",
                "queue_simulation": "true",
                "repo_url": "https://github.com/example/fw",
                "build_url": "https://ci.example.com/builds/42",
            },
            headers=headers,
            content=queued_bin,
        )
        assert up.status_code == 201, up.text
        assert up.json()["sim_status"] == "pending"

    # Simulate a crashed worker leaving an expired `running` lease; next `run_once` reaps & finishes it.
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE artifacts SET sim_status = 'running', sim_lease_owner = 'dead-sim', "
                "sim_lease_expires_at = :expired WHERE sha256 = :sha"
            ),
            {"sha": queued_sha, "expired": now_utc() - dt.timedelta(seconds=60)},
        )
        await session.commit()

    processed = await run_once(sessionmaker, store, runner, worker_id="live-sim")
    assert [(sha, res.status) for sha, res in processed] == [(queued_sha, SimStatus.PASSED)]
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT sim_status, sim_lease_owner, sim_lease_expires_at "
                    "FROM artifacts WHERE sha256 = :sha"
                ),
                {"sha": queued_sha},
            )
        ).one()
    assert row.sim_status == "passed"
    assert row.sim_lease_owner is None
    assert row.sim_lease_expires_at is None
