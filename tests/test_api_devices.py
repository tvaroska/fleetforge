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
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from tests.conftest import client_for, login_admin

DEVICE_ID = "a4cf12b3de91"


@pytest.fixture
async def fleet(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committed-writes session whose devices are deleted afterwards."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
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
    assert await list_devices(admin_app, await login_admin(admin_app)) == {"devices": []}


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
    }
    assert row["fw_version"] == "1.4.2"
    assert row["capabilities"] == ["ota", "selftest"]
    # NULL until the broker credential exists — R0-sec-1's reconcile list.
    assert row["broker_provisioned_at"] is None
    assert row["online"] is True
