"""PostgreSQL integration & gate tests for `R11` Staged Cohort Deploy Policy Engine (`R11-1`).

Verifies:
- Multi-ring cohort promotion (`canary` -> `staging` -> `prod`): uploading a matching tagged
  artifact stages ONLY Ring 0 (`canary`); confirming Ring 0 via `handle_up_message` +
  `advance_project_rollout_on_confirm` automatically promotes to Ring 1 (`staging`) and then
  Ring 2 (`prod`).
- Automatic halt (`done when`): when a board in Ring 0 (`canary`) reports `rolled_back` (or
  enters `safe_mode` or drops below `min_free_heap_bytes`), promotion to the next wave halts
  automatically (`status == "halted"`, zero Stage commands published to `staging` or `prod`).
- `require_sim_passed` and `min_soak_s` promotion gates.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import (
    get_command_publisher,
    get_object_store,
    get_settings,
    get_sim_runner,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import DEFAULT_PROJECT_ID, Device, DeviceGroup
from fleetforge.ingestor.handlers import handle_up_message
from fleetforge.lib_marker import MAGIC
from fleetforge.rollout import advance_project_rollout_on_confirm
from fleetforge.signing import generate_keypair
from fleetforge.simulation import HostPreflightSimRunner
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)

_PRIV_A, _PUB_A = generate_keypair()
_PRIV_B, _PUB_B = generate_keypair()
_VALID_MARKER = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12


def _app_bin(label: str) -> bytes:
    return b"\xe9\x00\x02\x20" + _VALID_MARKER + label.encode("ascii") + b"\x00" * 64


def _make_board(
    device_id: str, group_id: Any, *, now: dt.datetime, free_heap: int = 140_000
) -> Device:
    return Device(
        device_id=device_id,
        project_id=DEFAULT_PROJECT_ID,
        group_id=group_id,
        platform_type="esp32s3",
        fw_version="1.0.0",
        partition_layout="ab-4m-v1",
        ota_slot_size=1966080,
        rollback_capable=True,
        capabilities=["ota", "sig", "selftest"],
        link_type="wifi",
        power_class="always_on",
        presence_reported=True,
        last_seen=now,
        health_at=now,
        free_heap=free_heap,
        min_free_heap=free_heap,
        boot_ok=True,
    )


class TestStagedCohortDeployPolicyEngine:
    @pytest.fixture
    async def env(
        self,
        admin_app: FastAPI,
        engine: AsyncEngine,
        clean_tables: None,
    ) -> dict[str, Any]:
        _ = clean_tables
        settings = settings_for_tests(
            artifact_signing_key=_PRIV_A,
            artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
        )
        store = MemoryObjectStore()
        publisher = FakeCommandPublisher()
        sim_runner = HostPreflightSimRunner()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        admin_app.dependency_overrides[get_settings] = lambda: settings
        admin_app.dependency_overrides[get_object_store] = lambda: store
        admin_app.dependency_overrides[get_command_publisher] = lambda: publisher
        admin_app.dependency_overrides[get_sim_runner] = lambda: sim_runner

        token = await login_admin(admin_app)
        now = now_utc()
        async with sessionmaker() as s:
            canary_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="canary")
            staging_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="staging")
            prod_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="prod")
            s.add_all([canary_grp, staging_grp, prod_grp])
            await s.flush()
            s.add_all(
                [
                    _make_board("a4cf12b3c001", canary_grp.id, now=now),
                    _make_board("a4cf12b3c002", staging_grp.id, now=now),
                    _make_board("a4cf12b3c003", prod_grp.id, now=now),
                ]
            )
            await s.commit()
            groups = {
                "canary": canary_grp.id,
                "staging": staging_grp.id,
                "prod": prod_grp.id,
            }

        return {
            "app": admin_app,
            "token": token,
            "sessionmaker": sessionmaker,
            "settings": settings,
            "store": store,
            "publisher": publisher,
            "groups": groups,
        }

    async def test_healthy_canary_promotes_sequentially_across_all_three_rings(
        self,
        env: dict[str, Any],
    ) -> None:
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        async with client_for(env["app"], base_url="https://testserver") as client:
            # Create a 3-ring tag policy: canary -> staging -> prod.
            pol_resp = await client.post(
                "/v1/projects/default/policies",
                headers=headers,
                json={
                    "name": "release-rings",
                    "match_mode": "tag",
                    "match_ref": "v*",
                    "ring_group_ids": [
                        str(groups["canary"]),
                        str(groups["staging"]),
                        str(groups["prod"]),
                    ],
                    "max_parallel": 5,
                    "require_sim_passed": True,
                },
            )
            assert pol_resp.status_code == 201, pol_resp.text
            policy_id = pol_resp.json()["id"]

            # Upload matching v1.2.0 with simulate=true -> stages ONLY Ring 0 (canary `a4cf12b3c001`)!
            image = _app_bin("v1.2.0-healthy")
            up = await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.2.0",
                    "partition_layout": "ab-4m-v1",
                    "tag": "v1.2.0",
                    "simulate": "true",
                },
                content=image,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(image)),
                },
            )
            assert up.status_code == 201, up.text
            assert up.json()["auto_deployed_devices"] == ["a4cf12b3c001"]
            assert [did for did, _ in publisher.published] == ["a4cf12b3c001"]
            canary_cmd_id = publisher.published[0][1]["id"]

            # Canary confirms 1.2.0 -> `advance_project_rollout_on_confirm` promotes Ring 1 (`staging`)!
            t1 = now_utc() + dt.timedelta(seconds=5)
            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps(
                        {"cmd_id": canary_cmd_id, "state": "confirmed", "detail": "selftest passed"}
                    ).encode(),
                    retained=False,
                    received_at=t1,
                    tolerance=2.5,
                )
                await s.commit()
                promoted_1 = await advance_project_rollout_on_confirm(
                    s,
                    cmd_id=canary_cmd_id,
                    device_id="a4cf12b3c001",
                    settings=env["settings"],
                    publisher=publisher,
                )
            assert [d.device_id for d in promoted_1] == ["a4cf12b3c002"]
            staging_cmd_id = publisher.published[1][1]["id"]

            # Staging confirms 1.2.0 -> promotes Ring 2 (`prod` `a4cf12b3c003`)!
            t2 = t1 + dt.timedelta(seconds=5)
            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c002/up/status",
                    payload=json.dumps(
                        {
                            "cmd_id": staging_cmd_id,
                            "state": "confirmed",
                            "detail": "selftest passed",
                        }
                    ).encode(),
                    retained=False,
                    received_at=t2,
                    tolerance=2.5,
                )
                await s.commit()
                promoted_2 = await advance_project_rollout_on_confirm(
                    s,
                    cmd_id=staging_cmd_id,
                    device_id="a4cf12b3c002",
                    settings=env["settings"],
                    publisher=publisher,
                )
            assert [d.device_id for d in promoted_2] == ["a4cf12b3c003"]
            prod_cmd_id = publisher.published[2][1]["id"]

            # Prod confirms 1.2.0 -> policy evaluation reaches `completed`!
            t3 = t2 + dt.timedelta(seconds=5)
            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c003/up/status",
                    payload=json.dumps(
                        {"cmd_id": prod_cmd_id, "state": "confirmed", "detail": "selftest passed"}
                    ).encode(),
                    retained=False,
                    received_at=t3,
                    tolerance=2.5,
                )
                await s.commit()

            eval_resp = await client.post(
                f"/v1/projects/default/policies/{policy_id}/evaluate",
                headers=headers,
                json={"version": "1.2.0"},
            )
            assert eval_resp.status_code == 200
            ev = eval_resp.json()
            assert ev["status"] == "completed"
            assert [r["status"] for r in ev["rings"]] == ["promoted", "promoted", "promoted"]

    async def test_failing_canary_cohort_halts_promotion_to_next_wave_automatically(
        self,
        env: dict[str, Any],
    ) -> None:
        """R11-1 done-when: a failing canary cohort halts promotion to the next wave automatically."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        async with client_for(env["app"], base_url="https://testserver") as client:
            pol_resp = await client.post(
                "/v1/projects/default/policies",
                headers=headers,
                json={
                    "name": "main-canary-guard",
                    "match_mode": "branch",
                    "match_ref": "main",
                    "ring_group_ids": [
                        str(groups["canary"]),
                        str(groups["staging"]),
                        str(groups["prod"]),
                    ],
                    "min_free_heap_bytes": 100_000,
                },
            )
            assert pol_resp.status_code == 201
            policy_id = pol_resp.json()["id"]

            # Upload 1.3.0-bad on branch `main` -> stages ONLY Ring 0 (`a4cf12b3c001`).
            bad_bin = _app_bin("v1.3.0-bad-build")
            up = await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.3.0-bad",
                    "partition_layout": "ab-4m-v1",
                    "branch": "main",
                },
                content=bad_bin,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(bad_bin)),
                },
            )
            assert up.status_code == 201
            assert [did for did, _ in publisher.published] == ["a4cf12b3c001"]
            canary_cmd_id = publisher.published[0][1]["id"]

            # Canary rolls back (`rolled_back` on `up/status`).
            t_rb = now_utc() + dt.timedelta(seconds=10)
            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps(
                        {
                            "cmd_id": canary_cmd_id,
                            "state": "rolled_back",
                            "detail": "selftest failed: sensor bus dead; returned to ota_0",
                        }
                    ).encode(),
                    retained=False,
                    received_at=t_rb,
                    tolerance=2.5,
                )
                await s.commit()

            # Evaluating (or attempting to advance) MUST halt on Ring 0 and stage NOTHING to staging/prod!
            eval_resp = await client.post(
                f"/v1/projects/default/policies/{policy_id}/evaluate",
                headers=headers,
                json={"version": "1.3.0-bad"},
            )
            assert eval_resp.status_code == 200
            ev = eval_resp.json()
            assert ev["status"] == "halted"
            assert (
                "ring 0 halted: device a4cf12b3c001 rolled_back on version 1.3.0-bad"
                == ev["halt_reason"]
            )
            assert ev["deployed"] == []
            assert [r["status"] for r in ev["rings"]] == ["halted", "pending", "pending"]
            assert len(publisher.published) == 1

    async def test_safe_mode_and_low_heap_on_confirmed_canary_also_halt_promotion(
        self,
        env: dict[str, Any],
    ) -> None:
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        async with client_for(env["app"], base_url="https://testserver") as client:
            pol_resp = await client.post(
                "/v1/projects/default/policies",
                headers=headers,
                json={
                    "name": "heap-and-safemode-guard",
                    "match_mode": "tag",
                    "match_ref": "rc-*",
                    "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                    "min_free_heap_bytes": 120_000,
                    "min_soak_s": 30,
                },
            )
            assert pol_resp.status_code == 201
            policy_id = pol_resp.json()["id"]

            rc_bin = _app_bin("rc-1.4.0")
            await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.4.0-rc1",
                    "partition_layout": "ab-4m-v1",
                    "tag": "rc-1",
                },
                content=rc_bin,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(rc_bin)),
                },
            )
            cmd_id = publisher.published[0][1]["id"]

            # Canary confirms, entering the 30s soak window -> status is `soaking`, prod untouched.
            t_conf = now_utc()
            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps({"cmd_id": cmd_id, "state": "confirmed"}).encode(),
                    retained=False,
                    received_at=t_conf,
                    tolerance=2.5,
                )
                await s.commit()

            soak_ev = (
                await client.post(
                    f"/v1/projects/default/policies/{policy_id}/evaluate",
                    headers=headers,
                    json={"version": "1.4.0-rc1"},
                )
            ).json()
            assert soak_ev["status"] == "soaking"
            assert len(publisher.published) == 1

            # During soak, canary leaks heap below 120,000 B -> policy halts before prod ever stages!
            async with env["sessionmaker"]() as s:
                dev = await s.get(Device, "a4cf12b3c001")
                assert dev is not None
                dev.free_heap = 80_000
                await s.commit()

            halt_ev = (
                await client.post(
                    f"/v1/projects/default/policies/{policy_id}/evaluate",
                    headers=headers,
                    json={"version": "1.4.0-rc1"},
                )
            ).json()
            assert halt_ev["status"] == "halted"
            assert "free_heap 80000 < 120000 bytes" in (halt_ev["halt_reason"] or "")
            assert len(publisher.published) == 1
