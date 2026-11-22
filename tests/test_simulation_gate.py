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
from fleetforge.signing import generate_keypair, sign_digest
from fleetforge.sim_worker import run_once
from fleetforge.simulation import (
    APP_SLOT_OFFSET,
    EspressifQemuSimRunner,
    SimStatus,
    create_sim_runner,
)
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)
from tests.test_doubles import StubSimRunner

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
