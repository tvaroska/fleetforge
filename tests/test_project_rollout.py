"""Database-backed staged project cohort rollout and RBAC integration tests (`T-4`).

Drives `POST /v1/projects/{slug}/deploy`, `handle_up_message`, and `advance_project_rollout_on_confirm`
against a real migrated PostgreSQL database (`0 skipped`), exercising `SELECT ... FOR UPDATE`,
per-target canary gating, bounded wave concurrency, `rolled_back` auto-halt, and project RBAC.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import get_command_publisher, get_object_store, get_settings
from fleetforge.broker import CommandPublishError
from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.ingest.handlers import handle_up_message
from fleetforge.rollout import advance_project_rollout_on_confirm, reconcile_open_rollouts
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


async def test_project_cohort_rollout_canary_waves_halt_and_rbac_over_postgres(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """T-4: end-to-end PostgreSQL cohort rollout:
    1. Viewer & cross-project tokens are refused (`403`) on `POST /v1/projects/{slug}/deploy`.
    2. Canary wave stages ONLY the `is_dev=True` canary board (`max_parallel=2, canary=True`).
    3. `handle_up_message` (`confirmed`) + `advance_project_rollout_on_confirm` stages Wave 2
       (`max_parallel=2` -> 2 fleet boards, 1 remaining queued).
    4. `handle_up_message` (`rolled_back`) on one Wave 2 board halts further wave promotion.
    """
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
        # Create project `rover-fleet` and a second project `other-proj`
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

        # Create a viewer user on `rover-fleet` and verify 403 on `/deploy`
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

        # Mint a token pinned to `other-proj` with `deploy` scope -> must get 403 on `rover-fleet`
        r_wrong_tok = await client.post(
            "/v1/auth/tokens",
            headers=super_headers,
            json={"name": "other-deploy", "project": "other-proj", "scopes": ["deploy"]},
        )
        assert r_wrong_tok.status_code == 201
        wrong_proj_headers = {"Authorization": f"Bearer {r_wrong_tok.json()['token']}"}

        # Mint a valid `deploy`-scoped token pinned to `rover-fleet`
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

        # Upload signed firmware `3.0.0` for `esp32c6` into `rover-fleet`
        from fleetforge.lib_marker import MAGIC

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

        # Seed 4 online `esp32c6` boards (board 1 = dev canary; boards 2, 3, 4 = fleet)
        now = now_utc()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            for idx in (1, 2, 3, 4):
                session.add(
                    Device(
                        device_id=f"a4cf12b3de2{idx}",
                        project_id=proj_id,
                        platform_type="esp32c6",
                        fw_version="2.9.0",
                        partition_layout="ab-4m-v1",
                        ota_slot_size=1966080,
                        rollback_capable=True,
                        capabilities=["ota", "sig", "cancel"],
                        link_type="wifi",
                        power_class="always_on",
                        is_dev=(idx == 1),
                        presence_reported=True,
                        last_seen=now,
                    )
                )
            await session.commit()

        # RBAC checks: viewer gets 403, cross-project token gets 403
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

        # Wave 1 (Canary): only `a4cf12b3de21` stages; boards 2, 3, 4 wait for canary
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

        # Canary confirms via real ingestor `handle_up_message` -> `advance_project_rollout_on_confirm`
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
        # Wave 2 stages max_parallel=2 fleet boards (`de22`, `de23`), leaving `de24` queued for Wave 3!
        assert [d.device_id for d in advanced] == ["a4cf12b3de22", "a4cf12b3de23"]
        de22_cmd_id = advanced[0].cmd_id
        de23_cmd_id = advanced[1].cmd_id

        # Board `de22` rolls back on `3.0.0`, while `de23` confirms -> cohort MUST auto-halt (`de24` NOT staged)!
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

        # Re-POSTing `/v1/projects/rover-fleet/deploy` for `3.0.0` also halts cleanly!
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
    """A-21: after rolling out 1.0.0 and then 2.0.0 across four boards, repeated reconcile
    ticks and replayed confirm events for 1.0.0 must never downgrade or flip any board."""
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

    from fleetforge.lib_marker import MAGIC

    marker = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12

    async with client_for(admin_app, base_url="https://testserver") as client:
        proj_resp = await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "reconcile-fleet", "name": "Reconcile Fleet"},
        )
        assert proj_resp.status_code == 201, proj_resp.text
        proj_id = proj_resp.json()["id"]

        for ver in ("1.0.0", "2.0.0"):
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
            for idx, did in enumerate(board_ids):
                s.add(
                    Device(
                        device_id=did,
                        project_id=proj_id,
                        platform_type="esp32c6",
                        fw_version="0.9.0",
                        partition_layout="ab-4m-v1",
                        ota_slot_size=1966080,
                        rollback_capable=True,
                        capabilities=["ota", "sig", "cancel"],
                        link_type="wifi",
                        power_class="always_on",
                        is_dev=(idx == 0),
                        presence_reported=True,
                        last_seen=now,
                    )
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

        # Roll out 1.0.0: canary -> missed confirm recovered by reconcile -> rest confirm 1.0.0.
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

        # Next reconcile tick closes 1.0.0 as completed with zero new commands.
        publisher.published.clear()
        async with sessionmaker() as s:
            done_v1 = await reconcile_open_rollouts(s, settings=settings, publisher=publisher)
        assert done_v1 == []
        assert publisher.published == []

        # Roll out 2.0.0 across all four boards.
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

        # Six reconcile ticks + a replayed 1.0.0 confirm must produce ZERO downgrades or flips.
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


async def test_publish_failure_recovery_mid_wave_cleanup_and_per_target_canary_over_postgres(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """T-21 / A-3: PostgreSQL-backed rollout edge cases (migrated from mock-session unit tests):
    1. Mid-wave broker failure closes EVERY remaining unpublished intent with `publish_failed`
       before answering HTTP 503.
    2. A server-authored `publish_failed` never halts a subsequent retry of the cohort.
    3. Confirming canary on `esp32c6` unlocks Wave 2 for `esp32c6`, while `esp32s3` still
       stages only its single canary first.
    4. A stalled offline downloader does not starve online boards, and `POST /deploy/cancel`
       halts background `advance_project_rollout_on_confirm`.
    """
    import datetime as dt

    from sqlalchemy import text

    from fleetforge.deploys import latest_deploys
    from fleetforge.lib_marker import MAGIC

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
                    Device(
                        device_id=f"a4cf12b3ee0{idx}",
                        project_id=proj_id,
                        platform_type="esp32c6",
                        fw_version="1.0.0",
                        partition_layout="ab-4m-v1",
                        ota_slot_size=1966080,
                        rollback_capable=True,
                        capabilities=["ota", "sig", "cancel"],
                        link_type="wifi",
                        power_class="always_on",
                        is_dev=(idx == 1),
                        presence_reported=True,
                        last_seen=now,
                    )
                    for idx in (1, 2, 3)
                ]
            )
            await s.commit()

        # 1. Mid-wave broker failure: batch fails, board 1 succeeds, boards 2 & 3 get publish_failed -> 503.
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

        # 2. Retry succeeds (server-authored `publish_failed` does NOT halt the cohort!).
        publisher.fail_publish_many_with = None
        publisher.fail_with = None
        publisher.fail_after_publishes = None
        publisher.published.clear()

        # Confirm board 1 (`esp32c6` canary) and seed two `esp32s3` boards (`ee04` canary, `ee05` fleet).
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
                    Device(
                        device_id="a4cf12b3ee04",
                        project_id=proj_id,
                        platform_type="esp32s3",
                        fw_version="1.0.0",
                        partition_layout="ab-4m-v1",
                        ota_slot_size=1966080,
                        rollback_capable=True,
                        capabilities=["ota", "sig", "cancel"],
                        link_type="wifi",
                        power_class="always_on",
                        is_dev=True,
                        presence_reported=True,
                        last_seen=now,
                    ),
                    Device(
                        device_id="a4cf12b3ee05",
                        project_id=proj_id,
                        platform_type="esp32s3",
                        fw_version="1.0.0",
                        partition_layout="ab-4m-v1",
                        ota_slot_size=1966080,
                        rollback_capable=True,
                        capabilities=["ota", "sig", "cancel"],
                        link_type="wifi",
                        power_class="always_on",
                        is_dev=False,
                        presence_reported=True,
                        last_seen=now,
                    ),
                ]
            )
            await s.commit()

        # 3. Per-target canary: `esp32c6` boards (`ee02`, `ee03`) both stage, while `esp32s3` stages ONLY `ee04`!
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

        # 4. Stalled offline board + cohort cancel (`POST /deploy/cancel`) halts auto-advancement.
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

        # Board `ee02` reports `cancelled`; subsequent confirm advance must stay halted!
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
