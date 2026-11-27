"""Deploy steps, `PATCH /v1/devices/{id}`, `sent_by`, health/metrics, logs/telemetry, and decommission (`T-10`)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.clock import now_utc
from fleetforge.db.base import asyncpg_dsn
from fleetforge.db.models import (
    DeployEvent,
    Device,
    DeviceFault,
    DeviceGroup,
    DeviceLog,
    DeviceMetric,
    DeviceProgress,
    DeviceTelemetry,
    EnrollmentToken,
    Project,
)
from fleetforge.deploys import MAX_DEPLOY_STEPS
from fleetforge.events import EVENTS_CHANNEL, EventType
from tests.conftest import (
    TEST_DB_NAME,
    capture_logs,
    client_for,
    database_url_for,
    login_admin,
    settings_for_tests,
)

DEVICE_ID = "a4cf12b3de91"
OTHER_ID = "a4cf12b3de92"


async def _wipe_fleet(session: AsyncSession) -> None:
    await session.execute(delete(DeviceProgress))
    await session.execute(delete(EnrollmentToken))
    await session.execute(delete(Project).where(Project.slug != "default"))
    await session.execute(delete(DeployEvent))
    await session.execute(delete(Device))
    await session.execute(delete(DeviceGroup))
    await session.commit()


@pytest.fixture
async def fleet(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committed-writes session whose devices are deleted before and afterwards."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await _wipe_fleet(session)
        try:
            yield session
        finally:
            await session.rollback()
            await _wipe_fleet(session)


async def add_device(session: AsyncSession, device_id: str = DEVICE_ID, **overrides: Any) -> Device:
    values: dict[str, Any] = {
        "device_id": device_id,
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
    }
    values.update(overrides)
    device = Device(**values)
    session.add(device)
    await session.commit()
    return device


async def list_devices(app: FastAPI, token: str) -> Any:
    async with client_for(app) as client:
        response = await client.get("/v1/devices", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


async def add_deploy_event(
    session: AsyncSession,
    device_id: str,
    state: str,
    *,
    cmd_id: str | None,
    at: dt.datetime | None = None,
    is_terminal: bool = False,
    artifact_version: str | None = "1.5.0",
    from_version: str | None = "1.4.2",
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        DeployEvent(
            device_id=device_id,
            cmd_id=cmd_id,
            state=state,
            at=at if at is not None else now_utc(),
            is_terminal=is_terminal,
            artifact_version=artifact_version,
            from_version=from_version,
            detail=detail,
        )
    )
    await session.commit()


async def deploy_of(admin_app: FastAPI, device_id: str = DEVICE_ID) -> Any:
    body = await list_devices(admin_app, await login_admin(admin_app))
    row = next(row for row in body["devices"] if row["device_id"] == device_id)
    return row["deploy"]


async def patch_device(app: FastAPI, token: str, device_id: str, body: Any) -> httpx.Response:
    async with client_for(app) as client:
        return await client.patch(
            f"/v1/devices/{device_id}", json=body, headers={"Authorization": f"Bearer {token}"}
        )


async def row_of(app: FastAPI, token: str, device_id: str = DEVICE_ID) -> Any:
    body = await list_devices(app, token)
    return next(row for row in body["devices"] if row["device_id"] == device_id)


async def add_group(session: AsyncSession) -> uuid.UUID:
    group = DeviceGroup(name=f"bench-{uuid.uuid4().hex[:8]}")
    session.add(group)
    await session.commit()
    return group.id


async def test_steps_list_the_newest_transaction_only_oldest_first(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    now = now_utc()
    await add_deploy_event(
        fleet, DEVICE_ID, "confirmed", cmd_id="cmd-old", at=now - dt.timedelta(days=2)
    )
    for offset, state in [(30, "requested"), (28, "staging"), (25, "downloading")]:
        await add_deploy_event(
            fleet, DEVICE_ID, state, cmd_id="cmd-new", at=now - dt.timedelta(seconds=offset)
        )

    deploy = await deploy_of(admin_app)

    assert [step["state"] for step in deploy["steps"]] == ["requested", "staging", "downloading"]
    assert all(set(step) == {"state", "at"} for step in deploy["steps"])
    ats = [dt.datetime.fromisoformat(step["at"]) for step in deploy["steps"]]
    assert ats == sorted(ats)
    assert deploy["steps"][-1] == {"state": deploy["state"], "at": deploy["at"]}


async def test_steps_at_the_same_instant_keep_insertion_order(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """The `id ASC` tie-break: `requested` and `staging` share a millisecond."""
    await add_device(fleet)
    same_instant = now_utc()
    await add_deploy_event(fleet, DEVICE_ID, "requested", cmd_id="cmd-a", at=same_instant)
    await add_deploy_event(fleet, DEVICE_ID, "staging", cmd_id="cmd-a", at=same_instant)
    await add_deploy_event(fleet, DEVICE_ID, "downloading", cmd_id="cmd-a", at=same_instant)

    deploy = await deploy_of(admin_app)

    assert [step["state"] for step in deploy["steps"]] == ["requested", "staging", "downloading"]
    assert deploy["state"] == "downloading"


async def test_a_newest_row_without_a_cmd_id_is_a_transaction_of_one(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """NULL `cmd_id` rows are not one transaction; `steps` is never empty."""
    await add_device(fleet)
    now = now_utc()
    await add_deploy_event(
        fleet, DEVICE_ID, "staging", cmd_id=None, at=now - dt.timedelta(seconds=10)
    )
    await add_deploy_event(fleet, DEVICE_ID, "downloading", cmd_id=None, at=now)

    deploy = await deploy_of(admin_app)

    assert deploy["steps"] == [{"state": "downloading", "at": deploy["at"]}]


async def test_steps_are_capped_keeping_the_newest(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet)
    start = now_utc() - dt.timedelta(hours=1)
    total = MAX_DEPLOY_STEPS + 5
    for i in range(total):
        await add_deploy_event(
            fleet, DEVICE_ID, f"s{i}", cmd_id="cmd-a", at=start + dt.timedelta(seconds=i)
        )

    deploy = await deploy_of(admin_app)

    assert len(deploy["steps"]) == MAX_DEPLOY_STEPS
    assert [step["state"] for step in deploy["steps"]] == [
        f"s{i}" for i in range(total - MAX_DEPLOY_STEPS, total)
    ]
    assert deploy["steps"][-1]["state"] == deploy["state"] == f"s{total - 1}"


async def test_each_board_gets_its_own_steps(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet, "a4cf12b3de40")
    await add_device(fleet, "a4cf12b3de41")
    now = now_utc()
    for offset, (device, state) in enumerate(
        [
            ("a4cf12b3de40", "requested"),
            ("a4cf12b3de41", "requested"),
            ("a4cf12b3de40", "staging"),
            ("a4cf12b3de41", "staging"),
            ("a4cf12b3de41", "downloading"),
        ]
    ):
        await add_deploy_event(
            fleet, device, state, cmd_id=f"cmd-{device}", at=now + dt.timedelta(seconds=offset)
        )

    body = await list_devices(admin_app, await login_admin(admin_app))
    steps = {
        row["device_id"]: [step["state"] for step in row["deploy"]["steps"]]
        for row in body["devices"]
    }

    assert steps == {
        "a4cf12b3de40": ["requested", "staging"],
        "a4cf12b3de41": ["requested", "staging", "downloading"],
    }


async def test_the_deploy_carries_the_servers_confirm_window(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """The same number the pre-check shows, never a client-side guess."""
    await add_device(fleet)
    await add_deploy_event(fleet, DEVICE_ID, "rebooting", cmd_id="cmd-a")

    deploy = await deploy_of(admin_app)

    assert deploy["confirm_timeout_s"] == settings_for_tests().confirm_timeout_s


async def test_a_requested_rows_sha256_appears_nowhere_in_the_steps(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    digest = "b" * 64
    now = now_utc()
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "requested",
        cmd_id="cmd-a",
        at=now - dt.timedelta(seconds=3),
        detail={"sha256": digest, "size_bytes": 220000, "target": "esp32c6", "apply": "auto"},
    )
    await add_deploy_event(fleet, DEVICE_ID, "staging", cmd_id="cmd-a", at=now)

    async with client_for(admin_app) as client:
        response = await client.get(
            "/v1/devices", headers={"Authorization": f"Bearer {await login_admin(admin_app)}"}
        )

    assert response.status_code == 200
    assert digest not in response.text
    assert '"sha256"' not in response.text


async def test_patch_sets_a_name_that_the_fleet_list_then_shows(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    token = await login_admin(admin_app)

    response = await patch_device(admin_app, token, DEVICE_ID, {"name": "coop door"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "coop door"
    assert response.json()["device_id"] == DEVICE_ID
    assert (await row_of(admin_app, token))["name"] == "coop door"


async def test_patch_returns_the_same_row_shape_as_the_list(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    token = await login_admin(admin_app)

    response = await patch_device(admin_app, token, DEVICE_ID, {"name": "coop door"})

    assert response.json() == await row_of(admin_app, token)


async def test_surrounding_whitespace_is_trimmed(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet)
    response = await patch_device(
        admin_app, await login_admin(admin_app), DEVICE_ID, {"name": "  coop door  "}
    )
    assert response.json()["name"] == "coop door"


@pytest.mark.parametrize("cleared", ["", "   ", None])
async def test_a_blank_or_null_name_clears_it(
    admin_app: FastAPI, fleet: AsyncSession, cleared: str | None
) -> None:
    await add_device(fleet, name="coop door")
    token = await login_admin(admin_app)

    response = await patch_device(admin_app, token, DEVICE_ID, {"name": cleared})

    assert response.status_code == 200
    assert (await row_of(admin_app, token))["name"] is None


async def test_an_absent_key_leaves_its_field_alone(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    group_id = await add_group(fleet)
    await add_device(fleet, name="coop door", group_id=group_id)
    token = await login_admin(admin_app)

    await patch_device(admin_app, token, DEVICE_ID, {"group_id": None})
    row = await row_of(admin_app, token)
    assert (row["name"], row["group_id"]) == ("coop door", None)

    await patch_device(admin_app, token, DEVICE_ID, {"group_id": str(group_id)})
    await patch_device(admin_app, token, DEVICE_ID, {"name": "x"})
    row = await row_of(admin_app, token)
    assert (row["name"], row["group_id"]) == ("x", str(group_id))


async def test_an_empty_body_is_a_no_op(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet, name="coop door")
    token = await login_admin(admin_app)
    before = await row_of(admin_app, token)

    response = await patch_device(admin_app, token, DEVICE_ID, {})

    assert response.status_code == 200
    assert response.json() == before == await row_of(admin_app, token)


async def test_a_group_can_be_assigned_and_cleared(admin_app: FastAPI, fleet: AsyncSession) -> None:
    group_id = await add_group(fleet)
    await add_device(fleet)
    token = await login_admin(admin_app)

    assert (
        await patch_device(admin_app, token, DEVICE_ID, {"group_id": str(group_id)})
    ).status_code == 200
    assert (await row_of(admin_app, token))["group_id"] == str(group_id)

    await patch_device(admin_app, token, DEVICE_ID, {"group_id": None})
    assert (await row_of(admin_app, token))["group_id"] is None


async def test_an_unknown_group_is_404_and_changes_nothing(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    token = await login_admin(admin_app)
    missing = uuid.uuid4()

    response = await patch_device(
        admin_app, token, DEVICE_ID, {"group_id": str(missing), "name": "other"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == f"no such group: {missing}"
    assert (await row_of(admin_app, token))["name"] is None


async def test_an_unknown_device_is_404(admin_app: FastAPI, fleet: AsyncSession) -> None:
    _ = fleet
    response = await patch_device(
        admin_app, await login_admin(admin_app), "ffffffffffff", {"name": "x"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "no such device: ffffffffffff"


async def test_a_decommissioned_device_is_404(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet, decommissioned_at=now_utc())
    response = await patch_device(admin_app, await login_admin(admin_app), DEVICE_ID, {"name": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == f"no such device: {DEVICE_ID}"


async def test_unauthenticated_patch_is_401(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet)
    async with client_for(admin_app) as client:
        response = await client.patch(f"/v1/devices/{DEVICE_ID}", json={"name": "x"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x" * 65},
        {"name": "a\nb"},
        {"name": "a\u202eb"},
        {"name": "A4CF12B3DE91"},
        {"nmae": "x"},
        {"device_id": "ffffffffffff"},
        {"fw_version": "9"},
        {"name": 5},
    ],
)
async def test_a_bad_patch_body_is_422(
    admin_app: FastAPI, fleet: AsyncSession, body: dict[str, Any]
) -> None:
    await add_device(fleet)
    response = await patch_device(admin_app, await login_admin(admin_app), DEVICE_ID, body)
    assert response.status_code == 422


async def test_a_name_of_exactly_64_characters_passes(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    response = await patch_device(
        admin_app, await login_admin(admin_app), DEVICE_ID, {"name": "x" * 64}
    )
    assert response.status_code == 200


async def test_a_name_another_live_board_uses_is_409(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet, name="coop door")
    await add_device(fleet, OTHER_ID)

    response = await patch_device(
        admin_app, await login_admin(admin_app), OTHER_ID, {"name": "Coop Door"}
    )

    assert response.status_code == 409
    assert DEVICE_ID in response.json()["detail"]


async def test_re_sending_a_boards_own_name_is_fine(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet, name="coop door")
    response = await patch_device(
        admin_app, await login_admin(admin_app), DEVICE_ID, {"name": "coop door"}
    )
    assert response.status_code == 200


async def test_a_decommissioned_boards_name_does_not_block(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet, name="coop door", decommissioned_at=now_utc())
    await add_device(fleet, OTHER_ID)
    response = await patch_device(
        admin_app, await login_admin(admin_app), OTHER_ID, {"name": "coop door"}
    )
    assert response.status_code == 200


@pytest.fixture
async def listener() -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(asyncpg_dsn(database_url_for(TEST_DB_NAME)))
    try:
        yield connection
    finally:
        await connection.close()


async def test_a_rename_is_emitted_on_ff_events(
    admin_app: FastAPI, fleet: AsyncSession, listener: asyncpg.Connection
) -> None:
    await add_device(fleet)
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(EVENTS_CHANNEL, lambda *args: received.put_nowait(args[-1]))

    response = await patch_device(
        admin_app, await login_admin(admin_app), DEVICE_ID, {"name": "coop door"}
    )
    assert response.status_code == 200

    payload = json.loads(await asyncio.wait_for(received.get(), timeout=5))
    assert payload["type"] == EventType.DEVICE_UPDATED
    assert payload["device_id"] == DEVICE_ID
    assert "coop door" not in json.dumps(payload)


async def test_a_refused_patch_emits_nothing(
    admin_app: FastAPI, fleet: AsyncSession, listener: asyncpg.Connection
) -> None:
    await add_device(fleet, name="coop door")
    await add_device(fleet, OTHER_ID)
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(EVENTS_CHANNEL, lambda *args: received.put_nowait(args[-1]))

    response = await patch_device(
        admin_app, await login_admin(admin_app), OTHER_ID, {"name": "coop door"}
    )
    assert response.status_code == 409

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(received.get(), timeout=0.5)


async def test_the_name_is_not_logged(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet)
    token = await login_admin(admin_app)

    with capture_logs("fleetforge") as records:
        assert (
            await patch_device(admin_app, token, DEVICE_ID, {"name": "coop door"})
        ).status_code == 200

    messages = [record.getMessage() for record in records]
    assert any(f"device {DEVICE_ID} updated" in message for message in messages)
    assert not any("coop door" in message for message in messages)


SENDER_BLOB = {
    "subject": "admin",
    "token_id": "11111111-1111-1111-1111-111111111111",
    "credential": "dashboard session (1.2.3.4)",
}


async def test_sent_by_is_the_requested_rows_sender_without_the_token_id(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    now = now_utc()
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "requested",
        cmd_id="cmd-a",
        at=now - dt.timedelta(seconds=30),
        detail={"sha256": "c" * 64, "sent_by": SENDER_BLOB},
    )
    await add_deploy_event(fleet, DEVICE_ID, "confirmed", cmd_id="cmd-a", at=now, is_terminal=True)

    deploy = await deploy_of(admin_app)

    assert deploy["sent_by"] == {
        "subject": "admin",
        "credential": "dashboard session (1.2.3.4)",
    }
    assert set(deploy["sent_by"]) == {"subject", "credential"}
    assert deploy["detail"] is None
    assert [set(step) for step in deploy["steps"]] == [{"state", "at"}] * 2
    assert "token_id" not in str(deploy)


async def test_sent_by_is_null_without_a_requested_row_or_without_the_key(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet, "a4cf12b3de40")
    await add_device(fleet, "a4cf12b3de41")
    await add_deploy_event(fleet, "a4cf12b3de40", "staging", cmd_id="cmd-x")
    await add_deploy_event(
        fleet, "a4cf12b3de41", "requested", cmd_id="cmd-y", detail={"sha256": "c" * 64}
    )

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert {row["device_id"]: row["deploy"]["sent_by"] for row in body["devices"]} == {
        "a4cf12b3de40": None,
        "a4cf12b3de41": None,
    }


async def test_an_older_transactions_sender_does_not_bleed_into_a_newer_one(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    now = now_utc()
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "requested",
        cmd_id="cmd-a",
        at=now - dt.timedelta(hours=1),
        detail={"sent_by": SENDER_BLOB},
    )
    await add_deploy_event(fleet, DEVICE_ID, "requested", cmd_id="cmd-b", at=now)

    deploy = await deploy_of(admin_app)

    assert deploy["sent_by"] is None


@pytest.mark.parametrize("blob", ["admin", {"subject": 7}, {"subject": ""}, ["admin"], 3])
async def test_a_malformed_sender_blob_is_null_not_a_500(
    admin_app: FastAPI, fleet: AsyncSession, blob: Any
) -> None:
    await add_device(fleet)
    await add_deploy_event(fleet, DEVICE_ID, "requested", cmd_id="cmd-a", detail={"sent_by": blob})

    deploy = await deploy_of(admin_app)

    assert deploy["sent_by"] is None


async def test_a_non_string_credential_is_dropped_to_null(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "requested",
        cmd_id="cmd-a",
        detail={"sent_by": {"subject": "admin", "credential": 5}},
    )

    deploy = await deploy_of(admin_app)

    assert deploy["sent_by"] == {"subject": "admin", "credential": None}


async def test_each_board_gets_its_own_sender(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet, "a4cf12b3de40")
    await add_device(fleet, "a4cf12b3de41")
    for device, who in [("a4cf12b3de40", "dashboard session (a)"), ("a4cf12b3de41", None)]:
        await add_deploy_event(
            fleet,
            device,
            "requested",
            cmd_id=f"cmd-{device}",
            detail={"sent_by": {"subject": "admin", "credential": who}},
        )

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert {
        row["device_id"]: row["deploy"]["sent_by"]["credential"] for row in body["devices"]
    } == {
        "a4cf12b3de40": "dashboard session (a)",
        "a4cf12b3de41": None,
    }


async def test_the_health_fields_ride_on_the_list_before_deploy(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    now = now_utc()
    await add_device(
        fleet,
        last_reset_reason="panic",
        health_at=now - dt.timedelta(seconds=30),
        uptime_s=81234,
        rssi=-61,
        free_heap=142000,
        min_free_heap=98000,
        boot_ok=True,
    )
    await add_device(fleet, OTHER_ID)
    fleet.add_all(
        [
            DeviceFault(device_id=DEVICE_ID, at=now - dt.timedelta(hours=2), reason="panic"),
            DeviceFault(device_id=DEVICE_ID, at=now - dt.timedelta(hours=3), reason="brownout"),
            DeviceFault(device_id=DEVICE_ID, at=now - dt.timedelta(days=2), reason="panic"),
        ]
    )
    await fleet.commit()

    devices = (await list_devices(admin_app, await login_admin(admin_app)))["devices"]
    by_id = {row["device_id"]: row for row in devices}
    row, quiet = by_id[DEVICE_ID], by_id[OTHER_ID]

    assert row["last_reset_reason"] == "panic"
    assert row["fault_resets"] == {"window_s": 86400, "panic": 1, "task_wdt": 0, "brownout": 1}
    assert row["health"] == {
        "at": row["health"]["at"],
        "uptime_s": 81234,
        "rssi": -61,
        "free_heap": 142000,
        "min_free_heap": 98000,
        "boot_ok": True,
    }
    assert dt.datetime.fromisoformat(row["health"]["at"]) == now - dt.timedelta(seconds=30)
    assert row["metrics"] == []
    assert quiet["last_reset_reason"] is None
    assert quiet["health"] is None
    assert quiet["fault_resets"] == {"window_s": 86400, "panic": 0, "task_wdt": 0, "brownout": 0}
    keys = list(row)
    online = keys.index("online")
    assert keys[online + 1 :] == [
        "last_reset_reason",
        "fault_resets",
        "health",
        "metrics",
        "last_command",
        "is_dev",
        "stay_awake",
        "secrets_rev",
        "sub_versions",
        "safe_mode",
        "deploy",
    ], "frontend/src/api.ts mirrors this order; deploy stays last"


async def test_metrics_are_the_latest_per_name_sorted_and_capped(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    now = now_utc()
    await add_device(fleet)
    fleet.add_all(
        [
            DeviceMetric(
                device_id=DEVICE_ID,
                name=f"m{i:02d}",
                value=float(i),
                sample_at=now - dt.timedelta(seconds=i),
            )
            for i in range(40)
        ]
        + [
            DeviceMetric(
                device_id=DEVICE_ID,
                name="ancient",
                value=1.0,
                sample_at=now - dt.timedelta(days=31),
            )
        ]
    )
    await fleet.commit()

    row = await row_of(admin_app, await login_admin(admin_app))

    names = [metric["name"] for metric in row["metrics"]]
    assert names == [f"m{i:02d}" for i in range(32)], "the 32 newest, sorted by name"
    assert row["metrics"][3]["value"] == 3.0
    assert set(row["metrics"][0]) == {"name", "value", "at"}


async def test_the_patch_response_carries_the_health_fields_too(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    now = now_utc()
    await add_device(fleet, last_reset_reason="task_wdt", health_at=now, boot_ok=False)
    fleet.add(DeviceFault(device_id=DEVICE_ID, at=now, reason="task_wdt"))
    fleet.add(DeviceMetric(device_id=DEVICE_ID, name="temp_c", value=21.5, sample_at=now))
    await fleet.commit()
    token = await login_admin(admin_app)

    response = await patch_device(admin_app, token, DEVICE_ID, {"name": "coop door"})

    body = response.json()
    assert body == await row_of(admin_app, token)
    assert body["fault_resets"]["task_wdt"] == 1
    assert body["health"]["boot_ok"] is False
    assert body["metrics"][0]["name"] == "temp_c"


async def get_sub(app: FastAPI, path: str, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with client_for(app) as client:
        return await client.get(path, headers=headers)


async def test_the_log_tail_is_newest_first_limited_and_unexpired(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    now = now_utc()
    await add_device(fleet)
    fleet.add_all(
        [
            DeviceLog(
                device_id=DEVICE_ID,
                logged_at=now - dt.timedelta(seconds=10 - i),
                received_at=now,
                level="I",
                text=f"line {i}",
                dropped=3 if i == 0 else 0,
            )
            for i in range(5)
        ]
        + [
            DeviceLog(
                device_id=DEVICE_ID,
                logged_at=now - dt.timedelta(days=8),
                received_at=now - dt.timedelta(days=8),
                level="I",
                text="expired",
            ),
            DeviceLog(
                device_id=DEVICE_ID, logged_at=now, received_at=now, level="I", text=None, dropped=4
            ),
        ]
    )
    await fleet.commit()
    token = await login_admin(admin_app)

    response = await get_sub(admin_app, f"/v1/devices/{DEVICE_ID}/logs", token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == DEVICE_ID
    assert [line["text"] for line in body["lines"]] == [
        None,
        "line 4",
        "line 3",
        "line 2",
        "line 1",
        "line 0",
    ]
    assert body["lines"][0]["dropped"] == 4 and body["lines"][-1]["dropped"] == 3
    assert set(body["lines"][0]) == {
        "id",
        "logged_at",
        "received_at",
        "level",
        "text",
        "dropped",
        "context",
    }

    limited = await get_sub(admin_app, f"/v1/devices/{DEVICE_ID}/logs?limit=2", token)
    assert [line["text"] for line in limited.json()["lines"]] == [None, "line 4"]
    for bad in ("0", "5001", "x"):
        response = await get_sub(admin_app, f"/v1/devices/{DEVICE_ID}/logs?limit={bad}", token)
        assert response.status_code == 422, bad


async def test_the_sub_resources_are_404_for_unknown_and_decommissioned_devices(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet, OTHER_ID, decommissioned_at=now_utc())
    token = await login_admin(admin_app)
    for device_id in ("a4cf12b3de00", OTHER_ID):
        for path in ("logs", "telemetry?name=temp_c"):
            response = await get_sub(admin_app, f"/v1/devices/{device_id}/{path}", token)
            assert response.status_code == 404, (device_id, path)
            assert response.json()["detail"] == f"no such device: {device_id}"


async def test_the_sub_resources_need_a_credential(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_device(fleet)
    for path in ("logs", "telemetry?name=temp_c"):
        response = await get_sub(admin_app, f"/v1/devices/{DEVICE_ID}/{path}", None)
        assert response.status_code == 401, path


async def test_a_telemetry_series_is_newest_first_limited_and_validated(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    now = now_utc()
    await add_device(fleet)
    fleet.add_all(
        [
            DeviceTelemetry(
                device_id=DEVICE_ID,
                name="temp_c",
                value=float(i),
                sample_at=now - dt.timedelta(seconds=10 - i),
            )
            for i in range(5)
        ]
        + [
            DeviceTelemetry(device_id=DEVICE_ID, name="other", value=9.0, sample_at=now),
            DeviceTelemetry(
                device_id=DEVICE_ID,
                name="temp_c",
                value=-1.0,
                sample_at=now - dt.timedelta(days=31),
            ),
        ]
    )
    await fleet.commit()
    token = await login_admin(admin_app)

    response = await get_sub(admin_app, f"/v1/devices/{DEVICE_ID}/telemetry?name=temp_c", token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["device_id"], body["name"]) == (DEVICE_ID, "temp_c")
    assert [point["value"] for point in body["samples"]] == [4.0, 3.0, 2.0, 1.0, 0.0]
    limited = await get_sub(
        admin_app, f"/v1/devices/{DEVICE_ID}/telemetry?name=temp_c&limit=2", token
    )
    assert [point["value"] for point in limited.json()["samples"]] == [4.0, 3.0]
    for query in ("name=Temp", "name=", "name=a%0Ab", "", "name=temp_c&limit=0"):
        response = await get_sub(admin_app, f"/v1/devices/{DEVICE_ID}/telemetry?{query}", token)
        assert response.status_code == 422, query
