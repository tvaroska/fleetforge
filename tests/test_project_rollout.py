"""Database-backed staged project cohort rollout and RBAC integration tests (`T-4`).

Drives `POST /v1/projects/{slug}/deploy`, `handle_up_message`, and `advance_project_rollout_on_confirm`
against a real migrated PostgreSQL database (`0 skipped`), exercising `SELECT ... FOR UPDATE`,
per-target canary gating, bounded wave concurrency, `rolled_back` auto-halt, and project RBAC.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import get_command_publisher, get_object_store, get_settings
from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.ingestor.handlers import handle_up_message
from fleetforge.rollout import advance_project_rollout_on_confirm
from fleetforge.signing import generate_keypair, sign_digest
from tests.conftest import MemoryObjectStore, client_for, login_admin, settings_for_tests

_PRIV_A, _PUB_A = generate_keypair()
_PRIV_B, _PUB_B = generate_keypair()


class RecordingCommandPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, device_id: str, payload: dict[str, Any]) -> None:
        self.published.append((device_id, payload))

    async def publish_many(self, items: Any) -> None:
        self.published.extend(items)

    async def publish_cfg(self, device_id: str, payload: dict[str, Any]) -> None:
        self.published.append((device_id, payload))

    async def publish_cfg_many(self, items: Any) -> None:
        self.published.extend(items)

    async def clear_cfg(self, device_id: str) -> None:
        self.published.append((device_id, {}))


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
    publisher = RecordingCommandPublisher()
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
