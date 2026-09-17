"""`GET /v1/devices` — the read model the SSE contract tells every client to re-read.

Rows are inserted directly: this suite is not testing enrollment (`test_enroll.py`
does), it is testing what the fleet view reports — above all that presence is
**derived on read** through `fleetforge.presence.is_online` and not re-implemented
here with a literal `2.5`.

Every test runs inside the rolled-back `session` fixture's transaction... except it
cannot: the endpoint opens its own session against the same engine and would not see
uncommitted rows. So rows are committed and cleaned up by the `fleet` fixture.
"""

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.clock import now_utc
from fleetforge.db.models import DeployEvent, Device, DeviceProgress
from tests.conftest import client_for, login_admin, settings_for_tests

DEVICE_ID = "a4cf12b3de91"


@pytest.fixture
async def fleet(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committed-writes session whose devices are deleted afterwards."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(DeviceProgress))
            # Before the devices: the FK is `ON DELETE RESTRICT`, which is the point.
            await session.execute(delete(DeployEvent))
            await session.execute(delete(Device))
            await session.commit()


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


async def test_unauthenticated_is_401(admin_app: FastAPI) -> None:
    async with client_for(admin_app) as client:
        response = await client.get("/v1/devices")
    assert response.status_code == 401


async def test_an_empty_fleet_is_an_envelope_not_an_error(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    assert await list_devices(admin_app, await login_admin(admin_app)) == {
        "devices": [],
        "arrivals": [],
    }


@pytest.mark.parametrize(
    ("presence_reported", "expected"),
    [(True, True), (False, False), (None, False)],
)
async def test_always_on_presence_is_the_reported_value(
    admin_app: FastAPI, fleet: AsyncSession, presence_reported: bool | None, expected: bool
) -> None:
    """`always_on` is the retained `up/presence` value — `last_seen` does not enter into it."""
    await add_device(fleet, presence_reported=presence_reported, last_seen=now_utc())

    body = await list_devices(admin_app, await login_admin(admin_app))
    assert body["devices"][0]["online"] is expected


@pytest.mark.parametrize(
    ("age_s", "expected"),
    # `presence_tolerance` is 2.5 and the wake interval is 60 s, so the boundary is
    # 150 s. The far side of it is the case that proves the API calls
    # `presence.is_online` rather than carrying its own number.
    [(100, True), (200, False)],
)
async def test_sleepy_presence_comes_from_the_wake_interval(
    admin_app: FastAPI, fleet: AsyncSession, age_s: int, expected: bool
) -> None:
    await add_device(
        fleet,
        power_class="sleepy",
        expected_wake_interval_s=60,
        last_seen=now_utc() - dt.timedelta(seconds=age_s),
        presence_reported=True,  # ignored for sleepy boards, and it must stay ignored
    )

    body = await list_devices(admin_app, await login_admin(admin_app))
    assert body["devices"][0]["online"] is expected


async def test_a_decommissioned_device_is_gone_from_the_fleet_view(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet, "a4cf12b3de01")
    await add_device(fleet, "a4cf12b3de02", decommissioned_at=now_utc())

    body = await list_devices(admin_app, await login_admin(admin_app))
    assert [row["device_id"] for row in body["devices"]] == ["a4cf12b3de01"]


async def test_newest_enrollment_first(admin_app: FastAPI, fleet: AsyncSession) -> None:
    now = now_utc()
    await add_device(fleet, "a4cf12b3de01", enrolled_at=now - dt.timedelta(hours=2))
    await add_device(fleet, "a4cf12b3de02", enrolled_at=now)
    await add_device(fleet, "a4cf12b3de03", enrolled_at=now - dt.timedelta(hours=1))

    body = await list_devices(admin_app, await login_admin(admin_app))
    assert [row["device_id"] for row in body["devices"]] == [
        "a4cf12b3de02",
        "a4cf12b3de03",
        "a4cf12b3de01",
    ]


async def test_the_summary_carries_the_fleet_view_and_no_presence_ingredients(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """`presence_reported` must never ship: one implementation of the presence rule."""
    await add_device(
        fleet,
        name="bench board",
        fw_version="1.4.2",
        agent_version="0.3.0",
        partition_layout="ab-4m-v1",
        ota_slot_size=1966080,
        capabilities=["ota", "selftest"],
        presence_reported=True,
    )

    row = (await list_devices(admin_app, await login_admin(admin_app)))["devices"][0]

    assert set(row) == {
        "device_id",
        "name",
        "group_id",
        "platform_type",
        "fw_version",
        "agent_version",
        "link_type",
        "power_class",
        "expected_wake_interval_s",
        "parent_device_id",
        "partition_layout",
        "ota_slot_size",
        "capabilities",
        "last_seen",
        "enrolled_at",
        "broker_provisioned_at",
        "online",
        "deploy",
    }
    assert row["fw_version"] == "1.4.2"
    assert row["capabilities"] == ["ota", "selftest"]
    # NULL until the broker credential exists — R0-sec-1's reconcile list.
    assert row["broker_provisioned_at"] is None
    assert row["online"] is True


# ---------------------------------------------------------------------------
# Arrivals (S0-fw-1) — boards between "flashed" and "online"
# ---------------------------------------------------------------------------
#
# `stalled` is the point of these: it is derived on read from the age of the newest
# stage, in `fleetforge.progress`, exactly as `online` is derived by `presence`. If a
# `stalled` column ever appears in `device_progress`, these tests are why it must not.


async def add_progress(
    session: AsyncSession,
    device_id: str,
    stage: str,
    *,
    age_s: float = 0.0,
    detail: str | None = None,
) -> None:
    session.add(
        DeviceProgress(
            device_id=device_id,
            token_id=uuid.uuid4(),
            stage=stage,
            detail=detail,
            at=now_utc() - dt.timedelta(seconds=age_s),
        )
    )
    await session.commit()


async def test_a_board_that_is_only_arriving_has_no_device_row(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """The whole feature in one assertion: something is shown where nothing was."""
    await add_progress(fleet, "a4cf12b3de10", "enrolling", detail="attempt 3")

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert body["devices"] == []
    assert body["arrivals"] == [
        {
            "device_id": "a4cf12b3de10",
            "stage": "enrolling",
            "detail": "attempt 3",
            "at": body["arrivals"][0]["at"],
            "stalled": False,
        }
    ]


async def test_only_the_newest_stage_of_a_board_is_shown(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """An arrival is a position, not a history: the dashboard shows where it got to."""
    await add_progress(fleet, "a4cf12b3de11", "link_up", age_s=30)
    await add_progress(fleet, "a4cf12b3de11", "time_synced", age_s=20)
    await add_progress(fleet, "a4cf12b3de11", "enrolling", age_s=10)

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert [(row["device_id"], row["stage"]) for row in body["arrivals"]] == [
        ("a4cf12b3de11", "enrolling")
    ]


async def test_a_board_whose_stage_has_gone_quiet_reads_stalled(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """The acceptance criterion: a board that stops mid-arrival must LOOK stopped."""
    stall_s = settings_for_tests().progress_stall_s
    await add_progress(fleet, "a4cf12b3de12", "enrolling", age_s=stall_s + 30)
    await add_progress(fleet, "a4cf12b3de13", "enrolling", age_s=1)

    body = await list_devices(admin_app, await login_admin(admin_app))

    stalled = {row["device_id"]: row["stalled"] for row in body["arrivals"]}
    assert stalled == {"a4cf12b3de12": True, "a4cf12b3de13": False}


async def test_an_old_arrival_falls_out_of_the_window(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """Yesterday's failed board is not still on today's dashboard."""
    window_s = settings_for_tests().progress_window_s
    await add_progress(fleet, "a4cf12b3de14", "halted", age_s=window_s + 60)

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert body["arrivals"] == []


async def test_an_online_board_is_in_the_fleet_and_not_arriving(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """A board stops *arriving* the moment it has really arrived — one truth, not two."""
    await add_device(fleet, "a4cf12b3de15", presence_reported=True)
    await add_progress(fleet, "a4cf12b3de15", "mqtt_connected")

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert [row["device_id"] for row in body["devices"]] == ["a4cf12b3de15"]
    assert body["devices"][0]["online"] is True
    assert body["arrivals"] == []


async def test_an_enrolled_but_offline_board_is_still_arriving(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """The case that earns the feature: the row exists, the board never came up.

    Without this, a board that enrolled and then failed at the broker reads exactly
    like one that has merely gone to sleep, and the stage is the only thing that says
    which.
    """
    await add_device(fleet, "a4cf12b3de16", presence_reported=False)
    await add_progress(fleet, "a4cf12b3de16", "mqtt_refused", detail="broker connack 5")

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert body["devices"][0]["online"] is False
    assert [(row["device_id"], row["stage"]) for row in body["arrivals"]] == [
        ("a4cf12b3de16", "mqtt_refused")
    ]


async def test_a_board_that_arrived_and_then_died_is_offline_not_arriving(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """S0-fe-3, the criterion: a completed arrival must not come back as a stuck one.

    Before this rule, presence decaying put a board that reached `mqtt_connected`
    days ago straight back into the arriving list — as "stalled at `mqtt_connected`",
    duplicating the offline row directly above it — for the rest of the window.
    """
    await add_device(
        fleet,
        "a4cf12b3de20",
        presence_reported=False,
        broker_provisioned_at=now_utc() - dt.timedelta(seconds=600),
        last_seen=now_utc() - dt.timedelta(seconds=300),
    )
    # Its arrival: reported before the heartbeats that later advanced `last_seen`.
    await add_progress(fleet, "a4cf12b3de20", "mqtt_connected", age_s=590)

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert [row["device_id"] for row in body["devices"]] == ["a4cf12b3de20"]
    assert body["devices"][0]["online"] is False
    assert body["arrivals"] == []


async def test_a_re_flashed_board_arrives_again(admin_app: FastAPI, fleet: AsyncSession) -> None:
    """The case the S0-fw-1 rule was written to catch, and which S0-fe-3 must not break.

    Same board as above — provisioned, with a `last_seen` from its previous life — but
    now reporting a stage *newer* than that `last_seen`. Re-enrolment deliberately
    leaves `last_seen` alone (`registry.py`), so this is what a re-flash really looks
    like in the database, not a contrived timestamp.
    """
    await add_device(
        fleet,
        "a4cf12b3de21",
        presence_reported=False,
        broker_provisioned_at=now_utc() - dt.timedelta(seconds=600),
        last_seen=now_utc() - dt.timedelta(seconds=300),
    )
    await add_progress(fleet, "a4cf12b3de21", "mqtt_connected", age_s=590)
    await add_progress(fleet, "a4cf12b3de21", "link_up", age_s=5)

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert body["devices"][0]["online"] is False
    assert [(row["device_id"], row["stage"]) for row in body["arrivals"]] == [
        ("a4cf12b3de21", "link_up")
    ]


async def test_a_board_that_enrolled_but_never_connected_still_arrives(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """Provisioned but never `last_seen` — the S0-fe-3 rule must not swallow this one.

    Distinct from `test_an_enrolled_but_offline_board_is_still_arriving` above, which
    has no `broker_provisioned_at` either: here the credential exists and only the
    connection never happened, so `last_seen` is the sole conjunct doing the work.
    """
    await add_device(
        fleet,
        "a4cf12b3de22",
        presence_reported=False,
        broker_provisioned_at=now_utc() - dt.timedelta(seconds=60),
        last_seen=None,
    )
    await add_progress(fleet, "a4cf12b3de22", "mqtt_refused", detail="broker connack 5")

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert [(row["device_id"], row["stage"]) for row in body["arrivals"]] == [
        ("a4cf12b3de22", "mqtt_refused")
    ]


async def test_arrivals_are_newest_first(admin_app: FastAPI, fleet: AsyncSession) -> None:
    await add_progress(fleet, "a4cf12b3de17", "enrolling", age_s=90)
    await add_progress(fleet, "a4cf12b3de18", "link_up", age_s=5)
    await add_progress(fleet, "a4cf12b3de19", "time_synced", age_s=45)

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert [row["device_id"] for row in body["arrivals"]] == [
        "a4cf12b3de18",
        "a4cf12b3de19",
        "a4cf12b3de17",
    ]


# ---------------------------------------------------------------------------
# Deploy state (R1-fe-1) — the newest transaction, carried on the fleet read
# ---------------------------------------------------------------------------
#
# `deploy` rides on this response for the same reason `arrivals` does: the dashboard's
# one refresh engine is hint→re-read-this-endpoint. The SQL lives in `deploys.py`, the
# module that owns `deploy_events`, so these tests seed rows and assert what the fleet
# read reports — above all the ordering, which is the part a plausible implementation
# gets wrong silently.


async def add_deploy_event(
    session: AsyncSession,
    device_id: str,
    state: str,
    *,
    cmd_id: str,
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


async def test_a_board_never_deployed_to_has_a_present_null_deploy(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """Present and null, not absent: the client renders "never deployed", not undefined."""
    await add_device(fleet)

    row = (await list_devices(admin_app, await login_admin(admin_app)))["devices"][0]

    assert "deploy" in row
    assert row["deploy"] is None


async def test_a_board_mid_transaction_reports_where_it_got_to(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    await add_device(fleet)
    now = now_utc()
    await add_deploy_event(
        fleet, DEVICE_ID, "requested", cmd_id="cmd-a", at=now - dt.timedelta(seconds=20)
    )
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "downloading",
        cmd_id="cmd-a",
        at=now - dt.timedelta(seconds=5),
        detail={"pct": 42, "detail": "12 of 28 blocks"},
    )

    deploy = await deploy_of(admin_app)

    assert deploy["state"] == "downloading"
    assert deploy["cmd_id"] == "cmd-a"
    assert deploy["is_terminal"] is False
    assert deploy["artifact_version"] == "1.5.0"
    assert deploy["from_version"] == "1.4.2"
    assert deploy["pct"] == 42
    assert deploy["detail"] == "12 of 28 blocks"


async def test_the_newest_transaction_wins_over_an_older_terminal_one(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """Last week's `confirmed` must not outrank today's in-flight deploy."""
    await add_device(fleet)
    now = now_utc()
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "confirmed",
        cmd_id="cmd-a",
        at=now - dt.timedelta(days=7),
        is_terminal=True,
        artifact_version="1.4.2",
    )
    await add_deploy_event(
        fleet, DEVICE_ID, "requested", cmd_id="cmd-b", at=now - dt.timedelta(seconds=10)
    )
    await add_deploy_event(
        fleet, DEVICE_ID, "staging", cmd_id="cmd-b", at=now - dt.timedelta(seconds=9)
    )

    deploy = await deploy_of(admin_app)

    assert deploy["state"] == "staging"
    assert deploy["cmd_id"] == "cmd-b"
    assert deploy["is_terminal"] is False


async def test_an_old_terminal_state_is_still_the_honest_answer(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """No time window: "the last thing that happened" has no expiry date."""
    await add_device(fleet)
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "failed",
        cmd_id="cmd-a",
        at=now_utc() - dt.timedelta(days=30),
        is_terminal=True,
    )

    deploy = await deploy_of(admin_app)

    assert deploy["state"] == "failed"
    assert deploy["is_terminal"] is True


async def test_two_rows_at_the_same_instant_resolve_by_insertion_order(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """`ORDER BY at DESC, id DESC` — and the `id` half is what this pins.

    `record_requested` and the board's first `up/status` routinely land inside one
    millisecond. On `at DESC` alone the `requested` row can sort above the `staging`
    that followed it and the dashboard walks backwards.
    """
    await add_device(fleet)
    same_instant = now_utc()
    await add_deploy_event(fleet, DEVICE_ID, "requested", cmd_id="cmd-a", at=same_instant)
    await add_deploy_event(fleet, DEVICE_ID, "staging", cmd_id="cmd-a", at=same_instant)

    assert (await deploy_of(admin_app))["state"] == "staging"


async def test_a_requested_rows_sha256_does_not_leak_into_the_api_detail(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """`requested.detail` carries the digest and the size. That is not what `detail` means."""
    await add_device(fleet)
    await add_deploy_event(
        fleet,
        DEVICE_ID,
        "requested",
        cmd_id="cmd-a",
        detail={"sha256": "a" * 64, "size_bytes": 220000, "target": "esp32c6", "apply": "auto"},
    )

    deploy = await deploy_of(admin_app)

    assert deploy["state"] == "requested"
    assert deploy["detail"] is None
    assert deploy["pct"] is None


async def test_a_state_this_server_has_never_heard_of_is_reported_as_itself(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """`DeployState` is advisory; the column is TEXT with no CHECK, and so is this field."""
    await add_device(fleet)
    await add_deploy_event(fleet, DEVICE_ID, "defragmenting", cmd_id="cmd-a")

    deploy = await deploy_of(admin_app)

    assert deploy["state"] == "defragmenting"
    assert deploy["is_terminal"] is False


async def test_each_board_gets_its_own_newest_state(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """`DISTINCT ON (device_id)` — one row per board, never another board's."""
    await add_device(fleet, "a4cf12b3de30")
    await add_device(fleet, "a4cf12b3de31")
    await add_device(fleet, "a4cf12b3de32")
    await add_deploy_event(fleet, "a4cf12b3de30", "confirmed", cmd_id="cmd-a", is_terminal=True)
    await add_deploy_event(fleet, "a4cf12b3de31", "awaiting_safe_window", cmd_id="cmd-b")

    body = await list_devices(admin_app, await login_admin(admin_app))
    states = {
        row["device_id"]: None if row["deploy"] is None else row["deploy"]["state"]
        for row in body["devices"]
    }

    assert states == {
        "a4cf12b3de30": "confirmed",
        "a4cf12b3de31": "awaiting_safe_window",
        "a4cf12b3de32": None,
    }


async def test_a_decommissioned_board_with_deploy_history_stays_out_of_the_list(
    admin_app: FastAPI, fleet: AsyncSession
) -> None:
    """Regression: the deploy read must not resurrect a soft-deleted board."""
    await add_device(fleet, "a4cf12b3de33")
    await add_device(fleet, "a4cf12b3de34", decommissioned_at=now_utc())
    await add_deploy_event(fleet, "a4cf12b3de34", "confirmed", cmd_id="cmd-a", is_terminal=True)

    body = await list_devices(admin_app, await login_admin(admin_app))

    assert [row["device_id"] for row in body["devices"]] == ["a4cf12b3de33"]
