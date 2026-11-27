"""Database-backed staged project cohort rollout and RBAC integration tests (`T-4`, `T-10`).

Drives `POST /v1/projects/{slug}/deploy`, `handle_up_message`, and `advance_project_rollout_on_confirm`
against a real migrated PostgreSQL database (`0 skipped`), exercising `SELECT ... FOR UPDATE`,
per-target canary gating, bounded wave concurrency, `rolled_back` auto-halt, and project RBAC.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import AuthContext, get_command_publisher, get_object_store, get_settings
from fleetforge.broker import CommandPublishError
from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.deploys import DeploySender, DeploySnapshot, RequestedRollout, latest_deploys
from fleetforge.firmware.manifest import BUILTIN_LAYOUTS
from fleetforge.ingest.handlers import handle_up_message
from fleetforge.lib_marker import MAGIC
from fleetforge.partition_profiles import LayoutCatalog
from fleetforge.rollout import (
    advance_project_rollout_on_confirm,
    deploy_version_to_project_devices,
    reconcile_open_rollouts,
)
from fleetforge.signing import generate_keypair, sign_digest
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)

_PRIV_A, _PUB_A = generate_keypair()
_PRIV_B, _PUB_B = generate_keypair()


def _make_rollout_device(
    proj_id: uuid.UUID,
    idx: int,
    now: dt.datetime,
    *,
    prefix: str = "a4cf12b3de0",
    platform_type: str = "esp32c6",
    fw: str = "1.0.0",
    is_dev: bool = False,
    caps: list[str] | None = None,
) -> Device:
    return Device(
        device_id=f"{prefix}{idx}",
        project_id=proj_id,
        platform_type=platform_type,
        fw_version=fw,
        partition_layout="ab-4m-v1",
        ota_slot_size=1966080,
        rollback_capable=True,
        capabilities=caps if caps is not None else ["ota"],
        link_type="wifi",
        power_class="always_on",
        is_dev=is_dev,
        presence_reported=True,
        last_seen=now,
    )


def _mock_rollout_session(devices: list[Device]) -> Any:
    dev_by_id = {d.device_id: d for d in devices}
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: devices))
    session.get = AsyncMock(
        side_effect=lambda model, key: (
            dev_by_id.get(str(key)) if model is Device else SimpleNamespace(name="ci-token")
        )
    )
    art_row = SimpleNamespace(
        sha256="c" * 64,
        size_bytes=2048,
        partition_layout="ab-4m-v1",
        has_lib_marker=True,
        sig="ecdsa-p256-sha256:AA==",
    )
    session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: art_row))
    return session


async def test_project_cohort_rollout_canary_waves_halt_and_rbac_over_postgres(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """T-4: end-to-end PostgreSQL cohort rollout covering RBAC, canary, waves, and auto-halt."""
    _ = clean_tables
    store = MemoryObjectStore()
    publisher = FakeCommandPublisher()
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

    super_token = await login_admin(admin_app)
    super_headers = {"Authorization": f"Bearer {super_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        r_proj = await client.post(
            "/v1/projects",
            headers=super_headers,
            json={"slug": "rover-fleet", "name": "Rover Fleet"},
        )
        assert r_proj.status_code == 201, r_proj.text
        proj_id = r_proj.json()["id"]

        r_other = await client.post(
            "/v1/projects",
            headers=super_headers,
            json={"slug": "other-proj", "name": "Other Project"},
        )
        assert r_other.status_code == 201, r_other.text

        r_viewer = await client.post(
            "/v1/users",
            headers=super_headers,
            json={
                "username": "viewer1",
                "password": "viewer-password-123",
                "project": "rover-fleet",
                "role": "viewer",
            },
        )
        assert r_viewer.status_code == 201, r_viewer.text
        viewer_login = await client.post(
            "/v1/auth/login",
            json={"username": "viewer1", "password": "viewer-password-123"},
        )
        assert viewer_login.status_code == 200
        viewer_headers = {"Authorization": f"Bearer {viewer_login.cookies['ff_session']}"}

        r_wrong_tok = await client.post(
            "/v1/auth/tokens",
            headers=super_headers,
            json={"name": "other-deploy", "project": "other-proj", "scopes": ["deploy"]},
        )
        assert r_wrong_tok.status_code == 201
        wrong_proj_headers = {"Authorization": f"Bearer {r_wrong_tok.json()['token']}"}

        r_deploy_tok = await client.post(
            "/v1/auth/tokens",
            headers=super_headers,
            json={
                "name": "rover-deploy",
                "project": "rover-fleet",
                "scopes": ["read", "write:artifacts", "deploy"],
            },
        )
        assert r_deploy_tok.status_code == 201
        deploy_headers = {"Authorization": f"Bearer {r_deploy_tok.json()['token']}"}

        marker = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12
        fw_bytes = b"\xe9\x00\x00\x00" + marker + b"firmware-payload" * 32
        fw_sha = hashlib.sha256(fw_bytes).hexdigest()
        fw_sig = sign_digest(fw_sha, _PRIV_A)
        r_up = await client.post(
            "/v1/artifact",
            params={
                "project": "rover-fleet",
                "target": "esp32c6",
                "version": "3.0.0",
                "partition_layout": "ab-4m-v1",
                "sig": fw_sig,
            },
            headers=deploy_headers,
            content=fw_bytes,
        )
        assert r_up.status_code == 201, r_up.text

        now = now_utc()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            session.add_all(
                [
                    _make_rollout_device(
                        proj_id,
                        idx,
                        now,
                        prefix="a4cf12b3de2",
                        fw="2.9.0",
                        is_dev=(idx == 1),
                        caps=["ota", "sig", "cancel"],
                    )
                    for idx in (1, 2, 3, 4)
                ]
            )
            await session.commit()

        for forbidden_hdrs in (viewer_headers, wrong_proj_headers):
            r_forb = await client.post(
                "/v1/projects/rover-fleet/deploy",
                headers=forbidden_hdrs,
                json={
                    "version": "3.0.0",
                    "target_filter": "all",
                    "canary": True,
                    "max_parallel": 2,
                },
            )
            assert r_forb.status_code == 403, r_forb.text

        r_w1 = await client.post(
            "/v1/projects/rover-fleet/deploy",
            headers=deploy_headers,
            json={"version": "3.0.0", "target_filter": "all", "canary": True, "max_parallel": 2},
        )
        assert r_w1.status_code == 202, r_w1.text
        w1 = r_w1.json()
        assert [d["device_id"] for d in w1["deployed"]] == ["a4cf12b3de21"]
        canary_cmd_id = w1["deployed"][0]["cmd_id"]
        assert set(w1["skipped"]) == {"a4cf12b3de22", "a4cf12b3de23", "a4cf12b3de24"}
        assert all("waiting for canary a4cf12b3de21" in msg for msg in w1["skipped"].values())

        publisher.published.clear()
        async with sessionmaker() as session:
            await handle_up_message(
                session,
                topic="ff/v1/d/a4cf12b3de21/up/announce",
                payload=json.dumps(
                    {
                        "device_id": "a4cf12b3de21",
                        "platform_type": "esp32c6",
                        "fw_version": "3.0.0",
                        "partition_layout": "ab-4m-v1",
                        "ota_slot_size": 1966080,
                        "rollback_capable": True,
                        "capabilities": ["ota", "sig", "cancel"],
                    }
                ).encode(),
                retained=True,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            await handle_up_message(
                session,
                topic="ff/v1/d/a4cf12b3de21/up/status",
                payload=json.dumps(
                    {"cmd_id": canary_cmd_id, "state": "confirmed", "pct": 100}
                ).encode(),
                retained=True,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            await session.commit()

        async with sessionmaker() as session:
            advanced = await advance_project_rollout_on_confirm(
                session,
                device_id="a4cf12b3de21",
                cmd_id=canary_cmd_id,
                settings=settings,
                publisher=publisher,
            )
        assert [d.device_id for d in advanced] == ["a4cf12b3de22", "a4cf12b3de23"]
        de22_cmd_id = advanced[0].cmd_id
        de23_cmd_id = advanced[1].cmd_id

        publisher.published.clear()
        async with sessionmaker() as session:
            await handle_up_message(
                session,
                topic="ff/v1/d/a4cf12b3de22/up/status",
                payload=json.dumps(
                    {
                        "cmd_id": de22_cmd_id,
                        "state": "rolled_back",
                        "detail": "selftest timed out; returned to ota_0",
                    }
                ).encode(),
                retained=True,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            await handle_up_message(
                session,
                topic="ff/v1/d/a4cf12b3de23/up/status",
                payload=json.dumps(
                    {"cmd_id": de23_cmd_id, "state": "confirmed", "pct": 100}
                ).encode(),
                retained=True,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            await session.commit()

        async with sessionmaker() as session:
            halted_advance = await advance_project_rollout_on_confirm(
                session,
                device_id="a4cf12b3de23",
                cmd_id=de23_cmd_id,
                settings=settings,
                publisher=publisher,
            )
        assert halted_advance == []
        assert publisher.published == []

        r_halted = await client.post(
            "/v1/projects/rover-fleet/deploy",
            headers=deploy_headers,
            json={"version": "3.0.0", "target_filter": "all", "canary": True, "max_parallel": 2},
        )
        assert r_halted.status_code == 202, r_halted.text
        halted_body = r_halted.json()
        assert halted_body["deployed"] == []
        assert "rollout halted" in halted_body["skipped"]["a4cf12b3de24"]


async def test_reconcile_does_not_restage_a_superseded_version(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """A-21: reconcile ticks and replayed confirm events for superseded versions never downgrade."""
    _ = clean_tables
    store = MemoryObjectStore()
    publisher = FakeCommandPublisher()
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

    token = await login_admin(admin_app)
    headers = {"Authorization": f"Bearer {token}"}
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    marker = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12

    async with client_for(admin_app, base_url="https://testserver") as client:
        proj_resp = await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "reconcile-fleet", "name": "Reconcile Fleet"},
        )
        assert proj_resp.status_code == 201, proj_resp.text
        proj_id = proj_resp.json()["id"]

        for ver in ("1.0.0", "2.0.0", "2.5.0", "3.0.0"):
            fw = b"\xe9\x00\x00\x00" + marker + f"fw-{ver}".encode() * 32
            sha = hashlib.sha256(fw).hexdigest()
            up = await client.post(
                "/v1/artifact",
                params={
                    "project": "reconcile-fleet",
                    "target": "esp32c6",
                    "version": ver,
                    "partition_layout": "ab-4m-v1",
                    "sig": sign_digest(sha, _PRIV_A),
                },
                headers=headers,
                content=fw,
            )
            assert up.status_code == 201, up.text

        now = now_utc()
        board_ids = [f"a4cf12b3de3{i}" for i in (1, 2, 3, 4)]
        async with sessionmaker() as s:
            s.add_all(
                [
                    _make_rollout_device(
                        proj_id,
                        i,
                        now,
                        prefix="a4cf12b3de3",
                        fw="0.9.0",
                        is_dev=(i == 1),
                        caps=["ota", "sig", "cancel"],
                    )
                    for i in (1, 2, 3, 4)
                ]
            )
            await s.commit()

        async def _confirm(did: str, cmd_id: str, ver: str) -> None:
            async with sessionmaker() as s:
                await handle_up_message(
                    s,
                    topic=f"ff/v1/d/{did}/up/announce",
                    payload=json.dumps(
                        {
                            "device_id": did,
                            "platform_type": "esp32c6",
                            "fw_version": ver,
                            "partition_layout": "ab-4m-v1",
                            "ota_slot_size": 1966080,
                            "rollback_capable": True,
                            "capabilities": ["ota", "sig", "cancel"],
                        }
                    ).encode(),
                    retained=True,
                    received_at=now_utc(),
                    tolerance=settings.presence_tolerance,
                )
                await handle_up_message(
                    s,
                    topic=f"ff/v1/d/{did}/up/status",
                    payload=json.dumps(
                        {"cmd_id": cmd_id, "state": "confirmed", "pct": 100}
                    ).encode(),
                    retained=True,
                    received_at=now_utc(),
                    tolerance=settings.presence_tolerance,
                )
                await s.commit()

        r1 = await client.post(
            "/v1/projects/reconcile-fleet/deploy",
            headers=headers,
            json={"version": "1.0.0", "target_filter": "all", "canary": True, "max_parallel": 5},
        )
        assert r1.status_code == 202, r1.text
        v1_canary_cmd = r1.json()["deployed"][0]["cmd_id"]
        await _confirm(board_ids[0], v1_canary_cmd, "1.0.0")

        publisher.published.clear()
        async with sessionmaker() as s:
            recovered_v1 = await reconcile_open_rollouts(s, settings=settings, publisher=publisher)
        assert [d.device_id for d in recovered_v1] == board_ids[1:]
        for item in recovered_v1:
            await _confirm(item.device_id, item.cmd_id, "1.0.0")

        publisher.published.clear()
        async with sessionmaker() as s:
            done_v1 = await reconcile_open_rollouts(s, settings=settings, publisher=publisher)
        assert done_v1 == []
        assert publisher.published == []

        r2 = await client.post(
            "/v1/projects/reconcile-fleet/deploy",
            headers=headers,
            json={"version": "2.0.0", "target_filter": "all", "canary": True, "max_parallel": 5},
        )
        assert r2.status_code == 202, r2.text
        v2_canary_cmd = r2.json()["deployed"][0]["cmd_id"]
        await _confirm(board_ids[0], v2_canary_cmd, "2.0.0")

        publisher.published.clear()
        async with sessionmaker() as s:
            wave2_v2 = await advance_project_rollout_on_confirm(
                s,
                device_id=board_ids[0],
                cmd_id=v2_canary_cmd,
                settings=settings,
                publisher=publisher,
            )
        assert [d.device_id for d in wave2_v2] == board_ids[1:]
        for item in wave2_v2:
            await _confirm(item.device_id, item.cmd_id, "2.0.0")

        publisher.published.clear()
        for _ in range(6):
            async with sessionmaker() as s:
                reconciled = await reconcile_open_rollouts(
                    s, settings=settings, publisher=publisher
                )
            assert reconciled == []
        async with sessionmaker() as s:
            replayed = await advance_project_rollout_on_confirm(
                s,
                device_id=board_ids[0],
                cmd_id=v1_canary_cmd,
                settings=settings,
                publisher=publisher,
            )
        assert replayed == []
        assert publisher.published == []

        r25 = await client.post(
            "/v1/projects/reconcile-fleet/deploy",
            headers=headers,
            json={"version": "2.5.0", "target_filter": "all", "canary": True, "max_parallel": 5},
        )
        assert r25.status_code == 202, r25.text
        v25_canary_cmd = r25.json()["deployed"][0]["cmd_id"]
        await _confirm(board_ids[0], v25_canary_cmd, "2.5.0")

        r30 = await client.post(
            "/v1/projects/reconcile-fleet/deploy",
            headers=headers,
            json={"version": "3.0.0", "target_filter": "all", "canary": True, "max_parallel": 5},
        )
        assert r30.status_code == 202, r30.text

        async with sessionmaker() as s:
            status_25 = await s.scalar(
                text(
                    "SELECT status FROM project_rollouts "
                    "WHERE project_id = (:pid)::uuid AND version = '2.5.0'"
                ),
                {"pid": proj_id},
            )
        assert status_25 == "superseded"

        publisher.published.clear()
        async with sessionmaker() as s:
            reconciled_after_30 = await reconcile_open_rollouts(
                s, settings=settings, publisher=publisher
            )
        assert reconciled_after_30 == []
        assert publisher.published == []


async def test_publish_failure_recovery_mid_wave_cleanup_and_per_target_canary_over_postgres(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """T-21 / A-3: mid-wave broker failure cleanup, retry recovery, per-target canary, and cancel halt."""
    _ = clean_tables
    store = MemoryObjectStore()
    publisher = FakeCommandPublisher()
    settings = settings_for_tests(
        s3_endpoint_url="http://minio:9000",
        s3_bucket="fleetforge",
        artifact_url_secret="s" * 32,
        public_base_url="https://fleet.example.com",
        mqtt_command_username="ff-commander",
        mqtt_command_password="pw",
        confirm_timeout_s=120,
        artifact_signing_key=_PRIV_A,
        artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
    )
    admin_app.dependency_overrides[get_settings] = lambda: settings
    admin_app.dependency_overrides[get_object_store] = lambda: store
    admin_app.dependency_overrides[get_command_publisher] = lambda: publisher

    token = await login_admin(admin_app)
    headers = {"Authorization": f"Bearer {token}"}
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    marker = MAGIC + b"\x01\x00\x00\x00" + b"0.7.0\x00".ljust(32, b"\x00") + b"\x00" * 12

    async with client_for(admin_app, base_url="https://testserver") as client:
        p = await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "edge-fleet", "name": "Edge Fleet"},
        )
        assert p.status_code == 201, p.text
        proj_id = p.json()["id"]

        for target in ("esp32c6", "esp32s3"):
            fw = b"\xe9\x00\x00\x00" + marker + f"{target}-1.2.0".encode() * 32
            sha = hashlib.sha256(fw).hexdigest()
            up = await client.post(
                "/v1/artifact",
                params={
                    "project": "edge-fleet",
                    "target": target,
                    "version": "1.2.0",
                    "partition_layout": "ab-4m-v1",
                    "sig": sign_digest(sha, _PRIV_A),
                },
                headers=headers,
                content=fw,
            )
            assert up.status_code == 201, up.text

        now = now_utc()
        async with sessionmaker() as s:
            s.add_all(
                [
                    _make_rollout_device(
                        proj_id,
                        idx,
                        now,
                        prefix="a4cf12b3ee0",
                        is_dev=(idx == 1),
                        caps=["ota", "sig", "cancel"],
                    )
                    for idx in (1, 2, 3)
                ]
            )
            await s.commit()

        # Mid-wave broker failure: batch fails, board 1 succeeds, boards 2 & 3 get publish_failed -> 503.
        publisher.fail_publish_many_with = CommandPublishError("batch failed")
        publisher.fail_with = CommandPublishError("broker dropped mid-wave")
        publisher.fail_after_publishes = 1
        r_fail = await client.post(
            "/v1/projects/edge-fleet/deploy",
            headers=headers,
            json={"version": "1.2.0", "target_filter": "all", "canary": False, "max_parallel": 3},
        )
        assert r_fail.status_code == 503, r_fail.text

        async with sessionmaker() as s:
            snaps = await latest_deploys(
                s, device_ids=["a4cf12b3ee01", "a4cf12b3ee02", "a4cf12b3ee03"]
            )
        assert snaps["a4cf12b3ee01"].state == "requested"
        assert snaps["a4cf12b3ee02"].state == "failed"
        assert snaps["a4cf12b3ee02"].reason == "publish_failed"
        assert snaps["a4cf12b3ee03"].state == "failed"
        assert snaps["a4cf12b3ee03"].reason == "publish_failed"

        # Retry succeeds because server-authored `publish_failed` does not halt the cohort.
        publisher.fail_publish_many_with = None
        publisher.fail_with = None
        publisher.fail_after_publishes = None
        publisher.published.clear()

        c6_canary_cmd = snaps["a4cf12b3ee01"].cmd_id
        assert c6_canary_cmd is not None
        async with sessionmaker() as s:
            await handle_up_message(
                s,
                topic="ff/v1/d/a4cf12b3ee01/up/announce",
                payload=json.dumps(
                    {
                        "device_id": "a4cf12b3ee01",
                        "platform_type": "esp32c6",
                        "fw_version": "1.2.0",
                        "partition_layout": "ab-4m-v1",
                        "ota_slot_size": 1966080,
                        "rollback_capable": True,
                        "capabilities": ["ota", "sig", "cancel"],
                    }
                ).encode(),
                retained=True,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            await handle_up_message(
                s,
                topic="ff/v1/d/a4cf12b3ee01/up/status",
                payload=json.dumps({"cmd_id": c6_canary_cmd, "state": "confirmed"}).encode(),
                retained=True,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            s.add_all(
                [
                    _make_rollout_device(
                        proj_id,
                        idx,
                        now,
                        prefix="a4cf12b3ee0",
                        platform_type="esp32s3",
                        is_dev=(idx == 4),
                        caps=["ota", "sig", "cancel"],
                    )
                    for idx in (4, 5)
                ]
            )
            await s.commit()

        # Per-target canary: `esp32c6` boards (`ee02`, `ee03`) stage, while `esp32s3` stages ONLY `ee04`.
        r_retry = await client.post(
            "/v1/projects/edge-fleet/deploy",
            headers=headers,
            json={"version": "1.2.0", "target_filter": "all", "canary": True, "max_parallel": 5},
        )
        assert r_retry.status_code == 202, r_retry.text
        retry_body = r_retry.json()
        assert {d["device_id"] for d in retry_body["deployed"]} == {
            "a4cf12b3ee02",
            "a4cf12b3ee03",
            "a4cf12b3ee04",
        }
        assert "waiting for canary a4cf12b3ee04" in retry_body["skipped"]["a4cf12b3ee05"]

        # Stalled offline board + cohort cancel (`POST /deploy/cancel`) halts auto-advancement.
        ee02_cmd = next(
            d["cmd_id"] for d in retry_body["deployed"] if d["device_id"] == "a4cf12b3ee02"
        )
        async with sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE devices SET presence_reported = false, last_seen = :stale "
                    "WHERE device_id = 'a4cf12b3ee02'"
                ),
                {"stale": now - dt.timedelta(minutes=10)},
            )
            await handle_up_message(
                s,
                topic="ff/v1/d/a4cf12b3ee02/up/status",
                payload=json.dumps(
                    {"cmd_id": ee02_cmd, "state": "downloading", "pct": 25}
                ).encode(),
                retained=False,
                received_at=now - dt.timedelta(minutes=10),
                tolerance=settings.presence_tolerance,
            )
            await s.commit()

        r_cancel = await client.post(
            "/v1/projects/edge-fleet/deploy/cancel",
            headers=headers,
            json={"version": "1.2.0"},
        )
        assert r_cancel.status_code == 202, r_cancel.text
        cancelled_ids = {c["device_id"] for c in r_cancel.json()["cancelled"]}
        assert "a4cf12b3ee02" in cancelled_ids

        publisher.published.clear()
        async with sessionmaker() as s:
            await handle_up_message(
                s,
                topic="ff/v1/d/a4cf12b3ee02/up/status",
                payload=json.dumps(
                    {"cmd_id": ee02_cmd, "state": "cancelled", "detail": "cancelled by operator"}
                ).encode(),
                retained=False,
                received_at=now_utc(),
                tolerance=settings.presence_tolerance,
            )
            await s.commit()
        async with sessionmaker() as s:
            adv = await advance_project_rollout_on_confirm(
                s,
                device_id="a4cf12b3ee01",
                cmd_id=c6_canary_cmd,
                settings=settings,
                publisher=publisher,
            )
        assert adv == []
        assert publisher.published == []


async def test_staged_cohort_rollout_canary_wave_and_auto_halt() -> None:
    proj_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    devices = [_make_rollout_device(proj_id, i, now, is_dev=(i == 2)) for i in range(1, 8)]
    session = _mock_rollout_session(devices)

    admin = AuthContext(
        token_id=uuid.uuid4(),
        subject="ci",
        expires_at=now + dt.timedelta(hours=1),
        scopes=("deploy",),
        is_superadmin=True,
    )
    settings = settings_for_tests(
        artifact_url_secret="s" * 32,
        public_base_url="https://fleet.example.com",
    )
    publisher = AsyncMock()
    catalog = LayoutCatalog(layouts=BUILTIN_LAYOUTS, by_fingerprint={}, pending=frozenset())

    def _snap(state: str, detail: str | None = None) -> DeploySnapshot:
        return DeploySnapshot(
            cmd_id="cmd-1",
            state=state,
            at=now,
            is_terminal=True,
            artifact_version="1.2.0",
            from_version="1.0.0",
            pct=None,
            detail=detail,
            steps=(),
            sent_by=None,
            selftest=None,
        )

    with (
        patch("fleetforge.rollout.load_layout_catalog", return_value=catalog),
        patch("fleetforge.rollout.latest_open_transaction", return_value=None),
        patch("fleetforge.rollout.record_requested", AsyncMock()),
    ):
        with patch("fleetforge.rollout.latest_deploys", return_value={}):
            deployed, skipped = await deploy_version_to_project_devices(
                session,
                project_id=proj_id,
                version="1.2.0",
                target_filter="all",
                apply="auto",
                override=(),
                admin=admin,
                settings=settings,
                publisher=publisher,
                canary=True,
                max_parallel=3,
            )
            assert [d.device_id for d in deployed] == ["a4cf12b3de02"]
            assert len(skipped) == 6
            assert all("waiting for canary a4cf12b3de02" in msg for msg in skipped.values())

        devices[1].fw_version = "1.2.0"
        with patch(
            "fleetforge.rollout.latest_deploys",
            return_value={"a4cf12b3de02": _snap("confirmed")},
        ):
            deployed2, skipped2 = await deploy_version_to_project_devices(
                session,
                project_id=proj_id,
                version="1.2.0",
                target_filter="all",
                apply="auto",
                override=(),
                admin=admin,
                settings=settings,
                publisher=publisher,
                canary=True,
                max_parallel=3,
            )
            assert len(deployed2) == 3
            assert sum("queued for next wave" in msg for msg in skipped2.values()) == 3

        with patch(
            "fleetforge.rollout.latest_deploys",
            return_value={"a4cf12b3de02": _snap("rolled_back", "confirm timer expired")},
        ):
            deployed3, skipped3 = await deploy_version_to_project_devices(
                session,
                project_id=proj_id,
                version="1.2.0",
                target_filter="all",
                apply="auto",
                override=(),
                admin=admin,
                settings=settings,
                publisher=publisher,
                canary=True,
                max_parallel=3,
            )
            assert deployed3 == []
            assert all("rollout halted" in msg for msg in skipped3.values())


async def test_advance_project_rollout_on_confirm_progresses_waves_without_republishing_in_flight() -> (
    None
):
    proj_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    devices = [_make_rollout_device(proj_id, i, now, is_dev=(i == 1)) for i in range(1, 6)]
    session = _mock_rollout_session(devices)
    settings = settings_for_tests(
        artifact_url_secret="s" * 32,
        public_base_url="https://fleet.example.com",
    )
    publisher = AsyncMock()
    catalog = LayoutCatalog(layouts=BUILTIN_LAYOUTS, by_fingerprint={}, pending=frozenset())
    rollout_req = RequestedRollout(
        artifact_version="1.2.0",
        apply="auto",
        sent_by=DeploySender(subject="ci", token_id="tok-1", credential="ci-token"),
        rollout={
            "target_filter": "all",
            "canary": True,
            "max_parallel": 2,
            "override": [],
            "target_chip": "esp32c6",
        },
    )

    with (
        patch("fleetforge.rollout.requested_rollout", return_value=rollout_req),
        patch("fleetforge.rollout.load_layout_catalog", return_value=catalog),
        patch("fleetforge.rollout.latest_open_transaction", return_value=None),
        patch("fleetforge.rollout.record_requested", AsyncMock()),
        patch(
            "fleetforge.rollout.latest_deploys",
            return_value={
                "a4cf12b3de01": DeploySnapshot(
                    cmd_id="cmd-1",
                    state="confirmed",
                    at=now,
                    is_terminal=True,
                    artifact_version="1.2.0",
                    from_version="1.0.0",
                    pct=None,
                    detail=None,
                    steps=(),
                    sent_by=None,
                    selftest=None,
                ),
                "a4cf12b3de02": DeploySnapshot(
                    cmd_id="cmd-2",
                    state="downloading",
                    at=now,
                    is_terminal=False,
                    artifact_version="1.2.0",
                    from_version="1.0.0",
                    pct=50,
                    detail=None,
                    steps=(),
                    sent_by=None,
                    selftest=None,
                ),
            },
        ),
    ):
        advanced = await advance_project_rollout_on_confirm(
            session,
            device=devices[0],
            cmd_id="cmd-1",
            settings=settings,
            publisher=publisher,
        )
        assert [d.device_id for d in advanced] == ["a4cf12b3de03"]
