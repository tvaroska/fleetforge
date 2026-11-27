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
        sim_runner = StubSimRunner()
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

    async def test_halted_ring_stages_no_further_board_in_that_ring(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-22(1): when Ring 0's canary confirms in safe_mode / low heap, remaining boards
        inside Ring 0 MUST NOT be staged before policy gates run."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        # Add 3 sibling boards to Ring 0 (`canary`) alongside `a4cf12b3c001`.
        now = now_utc()
        async with env["sessionmaker"]() as s:
            s.add_all(
                [
                    _make_board("a4cf12b3c011", groups["canary"], now=now),
                    _make_board("a4cf12b3c012", groups["canary"], now=now),
                    _make_board("a4cf12b3c013", groups["canary"], now=now),
                ]
            )
            await s.commit()

        async with client_for(env["app"], base_url="https://testserver") as client:
            pol = await client.post(
                "/v1/projects/default/policies",
                headers=headers,
                json={
                    "name": "ring0-inner-gate",
                    "match_mode": "tag",
                    "match_ref": "inner-*",
                    "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                    "min_free_heap_bytes": 100_000,
                },
            )
            assert pol.status_code == 201, pol.text
            policy_id = pol.json()["id"]

            img = _app_bin("v1.5.0-inner")
            up = await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.5.0",
                    "partition_layout": "ab-4m-v1",
                    "tag": "inner-1",
                },
                content=img,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(img)),
                },
            )
            assert up.status_code == 201, up.text
            assert [did for did, _ in publisher.published] == ["a4cf12b3c001"]
            canary_cmd_id = publisher.published[0][1]["id"]

            # Canary confirms in safe_mode with 1 byte of free heap.
            async with env["sessionmaker"]() as s:
                dev = await s.get(Device, "a4cf12b3c001")
                assert dev is not None
                dev.safe_mode = True
                dev.free_heap = 1
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps({"cmd_id": canary_cmd_id, "state": "confirmed"}).encode(),
                    retained=False,
                    received_at=now_utc(),
                    tolerance=2.5,
                )
                await s.commit()

            publisher.published.clear()
            async with env["sessionmaker"]() as s:
                advanced = await advance_project_rollout_on_confirm(
                    s,
                    cmd_id=canary_cmd_id,
                    device_id="a4cf12b3c001",
                    settings=env["settings"],
                    publisher=publisher,
                )
            assert advanced == []
            assert publisher.published == []

            ev = (
                await client.post(
                    f"/v1/projects/default/policies/{policy_id}/evaluate",
                    headers=headers,
                    json={"version": "1.5.0"},
                )
            ).json()
            assert ev["status"] == "halted"
            assert ev["deployed"] == []

    async def test_unknown_free_heap_does_not_pass_the_heap_gate(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-22(2): a board with `free_heap = NULL` must not pass `min_free_heap_bytes`."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        async with env["sessionmaker"]() as s:
            dev = await s.get(Device, "a4cf12b3c001")
            assert dev is not None
            dev.free_heap = None
            await s.commit()

        async with client_for(env["app"], base_url="https://testserver") as client:
            pol = await client.post(
                "/v1/projects/default/policies",
                headers=headers,
                json={
                    "name": "heap-required",
                    "match_mode": "tag",
                    "match_ref": "heap-*",
                    "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                    "min_free_heap_bytes": 100_000,
                    "min_soak_s": 0,
                },
            )
            assert pol.status_code == 201
            policy_id = pol.json()["id"]

            img = _app_bin("v1.6.0-heap")
            await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.6.0",
                    "partition_layout": "ab-4m-v1",
                    "tag": "heap-1",
                },
                content=img,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(img)),
                },
            )
            canary_cmd_id = publisher.published[0][1]["id"]

            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps({"cmd_id": canary_cmd_id, "state": "confirmed"}).encode(),
                    retained=False,
                    received_at=now_utc(),
                    tolerance=2.5,
                )
                await s.commit()

            publisher.published.clear()
            ev = (
                await client.post(
                    f"/v1/projects/default/policies/{policy_id}/evaluate",
                    headers=headers,
                    json={"version": "1.6.0"},
                )
            ).json()
            assert ev["status"] == "soaking"
            assert ev["deployed"] == []
            assert ev["rings"][0]["status"] != "promoted"
            assert publisher.published == []

            # Once healthy free_heap is reported, Ring 0 promotes and stages Ring 1.
            async with env["sessionmaker"]() as s:
                dev = await s.get(Device, "a4cf12b3c001")
                assert dev is not None
                dev.free_heap = 140_000
                await s.commit()

            ev_ok = (
                await client.post(
                    f"/v1/projects/default/policies/{policy_id}/evaluate",
                    headers=headers,
                    json={"version": "1.6.0"},
                )
            ).json()
            assert ev_ok["status"] == "in_progress"
            assert [d["device_id"] for d in ev_ok["deployed"]] == ["a4cf12b3c003"]

    async def test_empty_or_deleted_ring_halts_instead_of_promoting(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-22(4): a ring with no compatible board or a deleted group halts instead of skipping."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        async with env["sessionmaker"]() as s:
            empty_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="empty-canary")
            doomed_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="doomed-canary")
            s.add_all([empty_grp, doomed_grp])
            await s.commit()
            empty_id, doomed_id = empty_grp.id, doomed_grp.id

        async with client_for(env["app"], base_url="https://testserver") as client:
            img = _app_bin("v1.7.0-empty-ring")
            up = await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.7.0",
                    "partition_layout": "ab-4m-v1",
                },
                content=img,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(img)),
                },
            )
            assert up.status_code == 201

            # Case 1: Empty Ring 0 must halt rather than promoting directly to prod.
            pol_empty = (
                await client.post(
                    "/v1/projects/default/policies",
                    headers=headers,
                    json={
                        "name": "empty-ring-guard",
                        "match_mode": "tag",
                        "match_ref": "never-*",
                        "ring_group_ids": [str(empty_id), str(groups["prod"])],
                    },
                )
            ).json()
            ev_empty = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_empty['id']}/evaluate",
                    headers=headers,
                    json={"version": "1.7.0"},
                )
            ).json()
            assert ev_empty["status"] == "halted"
            assert ev_empty["deployed"] == []
            assert ev_empty["rings"][0]["status"] == "halted"
            assert "no compatible devices" in (ev_empty["halt_reason"] or "")
            assert publisher.published == []

            # Case 2: Deleted Ring 0 group must also halt rather than promoting to prod.
            pol_del = (
                await client.post(
                    "/v1/projects/default/policies",
                    headers=headers,
                    json={
                        "name": "deleted-ring-guard",
                        "match_mode": "tag",
                        "match_ref": "never2-*",
                        "ring_group_ids": [str(doomed_id), str(groups["prod"])],
                    },
                )
            ).json()
            del_resp = await client.delete(f"/v1/groups/{doomed_id}", headers=headers)
            assert del_resp.status_code == 204
            ev_del = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_del['id']}/evaluate",
                    headers=headers,
                    json={"version": "1.7.0"},
                )
            ).json()
            assert ev_del["status"] == "halted"
            assert ev_del["deployed"] == []
            assert "was deleted" in (ev_del["halt_reason"] or "")
            assert publisher.published == []

    async def test_halt_survives_a_newer_event_on_the_failed_board(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-22(5): persisted halt survives newer deploy events on the failed board until explicit resume."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        groups = env["groups"]

        async with client_for(env["app"], base_url="https://testserver") as client:
            pol_id = (
                await client.post(
                    "/v1/projects/default/policies",
                    headers=headers,
                    json={
                        "name": "halt-memory",
                        "match_mode": "tag",
                        "match_ref": "mem-*",
                        "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                    },
                )
            ).json()["id"]

            bad_img = _app_bin("v1.8.0-bad")
            await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.8.0",
                    "partition_layout": "ab-4m-v1",
                    "tag": "mem-1",
                },
                content=bad_img,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(bad_img)),
                },
            )
            bad_cmd = publisher.published[0][1]["id"]

            # Canary rolls back on 1.8.0, then receives a newer deploy event on another command.
            t1 = now_utc() + dt.timedelta(seconds=5)
            t2 = t1 + dt.timedelta(seconds=5)
            async with env["sessionmaker"]() as s:
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps({"cmd_id": bad_cmd, "state": "rolled_back"}).encode(),
                    retained=False,
                    received_at=t1,
                    tolerance=2.5,
                )
                await handle_up_message(
                    s,
                    topic="ff/v1/d/a4cf12b3c001/up/status",
                    payload=json.dumps({"cmd_id": "cmd-other-ver", "state": "confirmed"}).encode(),
                    retained=False,
                    received_at=t2,
                    tolerance=2.5,
                )
                await s.commit()

            publisher.published.clear()
            # Even though `latest_deploys` is now `cmd-other-ver`, the policy MUST halt on 1.8.0.
            ev1 = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_id}/evaluate",
                    headers=headers,
                    json={"version": "1.8.0"},
                )
            ).json()
            assert ev1["status"] == "halted"
            assert ev1["deployed"] == []
            assert publisher.published == []

            # A second evaluation without `resume=true` stays halted.
            ev2 = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_id}/evaluate",
                    headers=headers,
                    json={"version": "1.8.0"},
                )
            ).json()
            assert ev2["status"] == "halted"
            assert ev2["deployed"] == []

            # Explicit `resume=true` clears the persisted halt and allows re-staging.
            ev_resumed = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_id}/evaluate",
                    headers=headers,
                    json={"version": "1.8.0", "resume": True},
                )
            ).json()
            assert ev_resumed["status"] == "in_progress"
            assert [d["device_id"] for d in ev_resumed["deployed"]] == ["a4cf12b3c001"]

    async def test_soak_elapses_for_boards_confirmed_by_announce(
        self,
        env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A-22(6): soak counts from first completeness even when confirmed by `up/announce` only."""
        import fleetforge.deploy_policies as dp_mod

        headers = {"Authorization": f"Bearer {env['token']}"}
        groups = env["groups"]

        # Canary announces `fw_version = 1.9.0` without any `confirmed` DeployEvent row.
        async with env["sessionmaker"]() as s:
            dev = await s.get(Device, "a4cf12b3c001")
            assert dev is not None
            dev.fw_version = "1.9.0"
            await s.commit()

        async with client_for(env["app"], base_url="https://testserver") as client:
            img = _app_bin("v1.9.0-announce")
            await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "1.9.0",
                    "partition_layout": "ab-4m-v1",
                },
                content=img,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(img)),
                },
            )
            pol_id = (
                await client.post(
                    "/v1/projects/default/policies",
                    headers=headers,
                    json={
                        "name": "announce-soak",
                        "match_mode": "tag",
                        "match_ref": "soak-*",
                        "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                        "min_soak_s": 30,
                    },
                )
            ).json()["id"]

            t0 = now_utc()
            monkeypatch.setattr(dp_mod, "now_utc", lambda: t0)
            ev1 = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_id}/evaluate",
                    headers=headers,
                    json={"version": "1.9.0"},
                )
            ).json()
            assert ev1["status"] == "soaking"
            assert ev1["deployed"] == []

            # 35 s later, soak elapses from the recorded first-completeness timestamp and stages prod.
            monkeypatch.setattr(dp_mod, "now_utc", lambda: t0 + dt.timedelta(seconds=35))
            ev2 = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_id}/evaluate",
                    headers=headers,
                    json={"version": "1.9.0"},
                )
            ).json()
            assert ev2["status"] == "in_progress"
            assert ev2["rings"][0]["status"] == "promoted"
            assert [d["device_id"] for d in ev2["deployed"]] == ["a4cf12b3c003"]

    async def test_scoped_token_matrix_on_policy_routes(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-26: `deploy`-scoped tokens manage/evaluate policies; `viewer`/`read`/`write:artifacts` cannot write."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        groups = env["groups"]

        async with client_for(env["app"], base_url="https://testserver") as client:
            assert (
                await client.post(
                    "/v1/users",
                    headers=headers,
                    json={
                        "username": "policyviewer",
                        "password": "viewer-password-1234",
                        "project": "default",
                        "role": "viewer",
                    },
                )
            ).status_code == 201
            v_login = await client.post(
                "/v1/auth/login",
                json={"username": "policyviewer", "password": "viewer-password-1234"},
            )
            viewer_hdrs = {"Authorization": f"Bearer {v_login.cookies['ff_session']}"}

            async def _mint(name: str, scopes: list[str]) -> dict[str, str]:
                r = await client.post(
                    "/v1/auth/tokens",
                    headers=headers,
                    json={"name": name, "project": "default", "scopes": scopes},
                )
                assert r.status_code == 201, r.text
                return {"Authorization": f"Bearer {r.json()['token']}"}

            read_hdrs = await _mint("pol-read", ["read"])
            wa_hdrs = await _mint("pol-wa", ["write:artifacts"])
            deploy_hdrs = await _mint("pol-deploy", ["deploy"])

            pol_body = {
                "name": "scoped-policy",
                "match_mode": "tag",
                "match_ref": "scoped-*",
                "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
            }
            for forbidden in (viewer_hdrs, read_hdrs, wa_hdrs):
                assert (
                    await client.post(
                        "/v1/projects/default/policies",
                        headers=forbidden,
                        json=pol_body,
                    )
                ).status_code == 403

            created = await client.post(
                "/v1/projects/default/policies",
                headers=deploy_hdrs,
                json=pol_body,
            )
            assert created.status_code == 201, created.text
            pol_id = created.json()["id"]

            assert (
                await client.get("/v1/projects/default/policies", headers=viewer_hdrs)
            ).status_code == 200
            assert (
                await client.get("/v1/projects/default/policies", headers=read_hdrs)
            ).status_code == 200
            assert (
                await client.patch(
                    f"/v1/projects/default/policies/{pol_id}",
                    headers=deploy_hdrs,
                    json={"min_soak_s": 10},
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/v1/projects/default/policies/{pol_id}/evaluate",
                    headers=read_hdrs,
                    json={"version": "1.0.0"},
                )
            ).status_code == 403
            assert (
                await client.delete(
                    f"/v1/projects/default/policies/{pol_id}",
                    headers=viewer_hdrs,
                )
            ).status_code == 403
            assert (
                await client.delete(
                    f"/v1/projects/default/policies/{pol_id}",
                    headers=deploy_hdrs,
                )
            ).status_code == 204

    async def test_each_halt_condition_names_its_device_and_reason(
        self,
        env: dict[str, Any],
    ) -> None:
        """T-19: each halt condition (`rolled_back`, `cancelled`, `failed`, `safe_mode`, low heap,
        fault reset, and `require_sim_passed` on failed sim) names the offending device & reason."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        groups = env["groups"]

        async with client_for(env["app"], base_url="https://testserver") as client:
            # 1. `require_sim_passed` halts when the artifact's simulation verdict is `failed`.
            bad_sim_img = (
                b"\xe9\x00\x02\x20"
                + _VALID_MARKER
                + b"FF_SIM_BOOT_CRASH: Guru Meditation"
                + b"\x00" * 64
            )
            up_sim = await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32s3",
                    "version": "2.0.0-simfail",
                    "partition_layout": "ab-4m-v1",
                    "simulate": "true",
                },
                content=bad_sim_img,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(bad_sim_img)),
                },
            )
            assert up_sim.status_code == 201
            assert up_sim.json()["sim_status"] == "failed"

            pol_sim = (
                await client.post(
                    "/v1/projects/default/policies",
                    headers=headers,
                    json={
                        "name": "require-sim-gate",
                        "match_mode": "tag",
                        "match_ref": "sim-*",
                        "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                        "require_sim_passed": True,
                    },
                )
            ).json()
            ev_sim = (
                await client.post(
                    f"/v1/projects/default/policies/{pol_sim['id']}/evaluate",
                    headers=headers,
                    json={"version": "2.0.0-simfail"},
                )
            ).json()
            assert ev_sim["status"] == "halted"
            assert "sim_status='passed'" in (ev_sim["halt_reason"] or "")

            # 2. Device-level halt reasons each name the exact device and cause.
            cases = [
                ("2.0.1-rb", "rolled_back", None, "rolled_back"),
                ("2.0.2-cancel", "cancelled", None, "cancelled"),
                ("2.0.3-fail", "failed", None, "failed"),
                ("2.0.4-safe", "confirmed", "safe_mode", "safe_mode"),
                ("2.0.5-heap", "confirmed", "low_heap", "free_heap 40000 < 100000 bytes"),
                ("2.0.6-fault", "confirmed", "fault_reset", "fault reset"),
            ]
            for ver, ev_state, mutation, expected_substr in cases:
                img = _app_bin(f"img-{ver}")
                up = await client.post(
                    "/v1/artifact",
                    params={
                        "target": "esp32s3",
                        "version": ver,
                        "partition_layout": "ab-4m-v1",
                    },
                    content=img,
                    headers={
                        **headers,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(img)),
                    },
                )
                assert up.status_code == 201, up.text

                # Reset canary state cleanly before each sub-case.
                async with env["sessionmaker"]() as s:
                    dev = await s.get(Device, "a4cf12b3c001")
                    assert dev is not None
                    dev.safe_mode = False
                    dev.free_heap = 140_000
                    dev.last_reset_reason = "power_on"
                    dev.fw_version = ver if ev_state == "confirmed" else "1.0.0"
                    await s.commit()

                pol = (
                    await client.post(
                        "/v1/projects/default/policies",
                        headers=headers,
                        json={
                            "name": f"halt-{ver}",
                            "match_mode": "tag",
                            "match_ref": f"never-{ver}",
                            "ring_group_ids": [str(groups["canary"]), str(groups["prod"])],
                            "min_free_heap_bytes": 100_000,
                        },
                    )
                ).json()
                env["publisher"].published.clear()
                stage_ev = (
                    await client.post(
                        f"/v1/projects/default/policies/{pol['id']}/evaluate",
                        headers=headers,
                        json={"version": ver},
                    )
                ).json()
                if env["publisher"].published:
                    cmd_id = env["publisher"].published[0][1]["id"]
                else:
                    cmd_id = f"cmd-{ver}"
                async with env["sessionmaker"]() as s:
                    if ev_state != "confirmed":
                        await handle_up_message(
                            s,
                            topic="ff/v1/d/a4cf12b3c001/up/status",
                            payload=json.dumps({"cmd_id": cmd_id, "state": ev_state}).encode(),
                            retained=False,
                            received_at=now_utc() + dt.timedelta(seconds=2),
                            tolerance=2.5,
                        )
                    dev = await s.get(Device, "a4cf12b3c001")
                    assert dev is not None
                    if mutation == "safe_mode":
                        dev.safe_mode = True
                    elif mutation == "low_heap":
                        dev.free_heap = 40_000
                    elif mutation == "fault_reset":
                        dev.last_reset_reason = "panic"
                        dev.last_seen = now_utc() + dt.timedelta(seconds=3)
                    await s.commit()

                res = (
                    await client.post(
                        f"/v1/projects/default/policies/{pol['id']}/evaluate",
                        headers=headers,
                        json={"version": ver},
                    )
                ).json()
                assert res["status"] == "halted", (ver, stage_ev, res)
                reason = res["halt_reason"] or ""
                assert "a4cf12b3c001" in reason, (ver, reason)
                assert expected_substr in reason, (ver, reason)


class TestDeployPolicyAndWaveOfflineUnits:
    """T-18 (`M5d`) & T-19: fast offline unit tests with zero database dependency."""

    def test_matches_deploy_policy_rules(self) -> None:
        from fleetforge.db.models import DeployPolicy
        from fleetforge.deploy_policies import matches_deploy_policy

        branch_pol = DeployPolicy(
            project_id=DEFAULT_PROJECT_ID,
            name="main-pol",
            enabled=True,
            match_mode="branch",
            match_ref="main",
            ring_group_ids=[],
            require_sim_passed=True,
        )
        assert matches_deploy_policy(
            branch_pol, branch="main", tag=None, test_passed=True, sim_status="passed"
        )
        assert not matches_deploy_policy(
            branch_pol, branch="main", tag=None, test_passed=True, sim_status="failed"
        )
        assert not matches_deploy_policy(
            branch_pol, branch="main", tag=None, test_passed=False, sim_status="passed"
        )
        assert not matches_deploy_policy(
            branch_pol, branch="dev", tag=None, test_passed=True, sim_status="passed"
        )
        branch_pol.enabled = False
        assert not matches_deploy_policy(
            branch_pol, branch="main", tag=None, test_passed=True, sim_status="passed"
        )

        tag_pol = DeployPolicy(
            project_id=DEFAULT_PROJECT_ID,
            name="tag-pol",
            enabled=True,
            match_mode="tag",
            match_ref="v2.*",
            ring_group_ids=[],
            require_sim_passed=False,
        )
        assert matches_deploy_policy(
            tag_pol, branch=None, tag="v2.1.0", test_passed=None, sim_status=None
        )
        assert not matches_deploy_policy(
            tag_pol, branch=None, tag="v3.0.0", test_passed=True, sim_status="passed"
        )

    async def test_detect_ring_halt_offline_conditions(self) -> None:
        from unittest.mock import AsyncMock

        from fleetforge.deploy_policy_gates import detect_ring_halt
        from fleetforge.deploys import DeploySnapshot

        now = now_utc()
        dev = _make_board("a4cf12b3f001", None, now=now, free_heap=120_000)
        fake_session = AsyncMock()
        fake_session.execute = AsyncMock()

        # publish_failed is transient broker failure, not a firmware halt.
        pub_fail = DeploySnapshot(
            cmd_id="c1",
            state="failed",
            at=now,
            is_terminal=True,
            artifact_version="2.0.0",
            from_version="1.0.0",
            pct=None,
            detail="broker publish failed",
            steps=(),
            sent_by=None,
            selftest=None,
            reason="publish_failed",
        )
        assert (
            await detect_ring_halt(
                fake_session,
                ring_idx=0,
                ring_devices=[dev],
                snapshots={dev.device_id: pub_fail},
                version="2.0.0",
                min_free_heap_bytes=100_000,
            )
            is None
        )

        for bad_state in ("rolled_back", "cancelled", "failed"):
            snap = DeploySnapshot(
                cmd_id="c2",
                state=bad_state,
                at=now,
                is_terminal=True,
                artifact_version="2.0.0",
                from_version="1.0.0",
                pct=None,
                detail="selftest timeout",
                steps=(),
                sent_by=None,
                selftest=None,
                reason="selftest_timeout",
            )
            reason = await detect_ring_halt(
                fake_session,
                ring_idx=0,
                ring_devices=[dev],
                snapshots={dev.device_id: snap},
                version="2.0.0",
                min_free_heap_bytes=100_000,
            )
            assert reason is not None
            assert dev.device_id in reason and bad_state in reason

        # Confirmed board with safe_mode or low free_heap or fault reset halts.
        dev_safe = _make_board("a4cf12b3f002", None, now=now, free_heap=150_000)
        dev_safe.fw_version = "2.0.0"
        dev_safe.safe_mode = True
        r_safe = await detect_ring_halt(
            fake_session,
            ring_idx=1,
            ring_devices=[dev_safe],
            snapshots={},
            version="2.0.0",
            min_free_heap_bytes=100_000,
        )
        assert r_safe is not None and "safe_mode" in r_safe and dev_safe.device_id in r_safe

        dev_heap = _make_board("a4cf12b3f003", None, now=now, free_heap=50_000)
        dev_heap.fw_version = "2.0.0"
        r_heap = await detect_ring_halt(
            fake_session,
            ring_idx=1,
            ring_devices=[dev_heap],
            snapshots={},
            version="2.0.0",
            min_free_heap_bytes=100_000,
        )
        assert r_heap is not None and "50000 < 100000" in r_heap

    def test_select_rollout_wave_enforces_link_and_gateway_airtime_caps(self) -> None:
        """T-18 (`M5d`) & T-19: verifies LoRa/BLE/Zigbee=1, Thread/Cellular=2, and gateway=3 caps."""
        from fleetforge.deploy_precheck import ResolvedArtifact
        from fleetforge.rollout_state import (
            LINK_AIRTIME_MAX_PARALLEL,
            PARENT_GATEWAY_MAX_PARALLEL,
            select_rollout_wave,
        )

        now = now_utc()
        settings = settings_for_tests()
        art = ResolvedArtifact(
            sha256="a" * 64,
            size_bytes=1024,
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
            artifact_type="app",
            sim_status="passed",
        )

        expected_link_caps = {
            "lora": 1,
            "ble": 1,
            "zigbee": 1,
            "thread": 2,
            "cellular": 2,
        }
        assert LINK_AIRTIME_MAX_PARALLEL == expected_link_caps
        assert PARENT_GATEWAY_MAX_PARALLEL == 3

        for link_type, expected_cap in expected_link_caps.items():
            devs = []
            for idx in range(expected_cap + 2):
                d = _make_board(f"a4cf12{link_type[:2]}{idx:04x}", None, now=now)
                d.link_type = link_type
                devs.append(d)
            skipped: dict[str, str] = {}
            wave = select_rollout_wave(
                eligible=[(d, art, True) for d in devs],
                all_project_devices=devs,
                snapshots={},
                confirmed_targets={"esp32s3"},
                version="2.0.0",
                canary=False,
                max_parallel=10,
                now=now,
                settings=settings,
                skipped=skipped,
            )
            assert len(wave) == expected_cap, (link_type, len(wave))
            assert len(skipped) == 2
            assert all(f"airtime cap={expected_cap} on {link_type}" in v for v in skipped.values())

        # Parent gateway cap (=3) across 5 wifi children under one gateway.
        gw_devs = []
        for idx in range(5):
            d = _make_board(f"a4cf12aa{idx:04x}", None, now=now)
            d.link_type = "wifi"
            d.parent_device_id = "a4cf12000001"
            gw_devs.append(d)
        gw_skipped: dict[str, str] = {}
        gw_wave = select_rollout_wave(
            eligible=[(d, art, True) for d in gw_devs],
            all_project_devices=gw_devs,
            snapshots={},
            confirmed_targets={"esp32s3"},
            version="2.0.0",
            canary=False,
            max_parallel=10,
            now=now,
            settings=settings,
            skipped=gw_skipped,
        )
        assert len(gw_wave) == 3
        assert len(gw_skipped) == 2
        assert all("gateway a4cf12000001 airtime cap=3" in v for v in gw_skipped.values())

    def test_supersession_guard_blocks_downgrade_over_newer_confirmed_or_in_flight(self) -> None:
        from fleetforge.deploys import DeploySnapshot
        from fleetforge.rollout_state import has_newer_confirmed_or_in_flight, scope_covers

        t0 = now_utc()
        t1 = t0 + dt.timedelta(seconds=30)
        newer_confirmed = DeploySnapshot(
            cmd_id="cmd-new",
            state="confirmed",
            at=t1,
            is_terminal=True,
            artifact_version="2.1.0",
            from_version="2.0.0",
            pct=100,
            detail=None,
            steps=(),
            sent_by=None,
            selftest=None,
        )
        assert has_newer_confirmed_or_in_flight(
            newer_confirmed, version="2.0.0", rollout_started_at=t0
        )
        assert not has_newer_confirmed_or_in_flight(
            newer_confirmed, version="2.1.0", rollout_started_at=t0
        )
        assert not has_newer_confirmed_or_in_flight(
            newer_confirmed, version="2.0.0", rollout_started_at=t1 + dt.timedelta(seconds=10)
        )
        assert scope_covers("all", None, None, "dev", "esp32s3", None)
        assert not scope_covers("dev", None, None, "all", None, None)
