"""`POST /v1/device-progress` over HTTP — S0-fw-1, against a real database.

The second unauthenticated write endpoint, so the same discipline as `test_enroll.py`:
the real ASGI app, the real migrated database, and the security properties named after
the disaster each one prevents.

Three of these are security properties rather than plumbing:

* `test_a_progress_report_never_burns_the_token` — a burn there is a board that can no
  longer enrol *because it said it was enrolling*, i.e. the feature bricking the boot
  it exists to observe;
* `test_another_devices_token_is_refused` — the endpoint's whole authorization rule is
  "a burned token reports only for the device it was burned for"; a 202 there lets
  anyone holding one spent token write arrivals for a device_id they invent;
* `test_an_admin_token_cannot_report` — the `ffe_` prefix check, so that a progress
  endpoint never becomes a second place to spend the credential that matters.

**Never assert on total row counts.** The app commits for real and the whole session
shares one database, so every assertion filters by the ids the test created.
"""

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fleetforge.api.deps import get_broker_provisioner
from fleetforge.db.base import asyncpg_dsn
from fleetforge.db.models import Device, DeviceProgress, EnrollmentToken, ProgressStage
from fleetforge.events import EVENTS_CHANNEL, EventType
from fleetforge.progress import has_already_arrived
from tests.conftest import (
    TEST_DB_NAME,
    capture_logs,
    client_for,
    database_url_for,
    login_admin,
    settings_for_tests,
)
from tests.test_enroll import (
    RecordingProvisioner,
    enroll_body,
    execute,
    issue_token,
    post_enroll,
)

BASE_URL = "https://testserver"


@pytest.fixture
def progress_app(admin_app: FastAPI) -> FastAPI:
    """`admin_app` with the broker faked, so a test can enrol before it reports."""
    admin_app.dependency_overrides[get_broker_provisioner] = lambda: RecordingProvisioner()
    return admin_app


def _sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def report_body(token: str, device_id: str, **overrides: object) -> dict[str, Any]:
    body: dict[str, Any] = {"token": token, "device_id": device_id, "stage": "link_up"}
    body.update(overrides)
    return body


async def post_progress(app: FastAPI, body: dict[str, Any]) -> httpx.Response:
    """No credential of any kind — the token in the body is the whole credential."""
    async with client_for(app, base_url=BASE_URL) as client:
        return await client.post("/v1/device-progress", json=body)


async def rows_for(engine: AsyncEngine, device_id: str) -> list[DeviceProgress]:
    async with _sessionmaker(engine)() as session:
        rows = (
            await session.scalars(
                select(DeviceProgress)
                .where(DeviceProgress.device_id == device_id)
                .order_by(DeviceProgress.id)
            )
        ).all()
    return list(rows)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_stage_is_accepted_and_recorded(progress_app: FastAPI, engine: AsyncEngine) -> None:
    """202 with an empty body — the board is told nothing it could act on."""
    device_id = "a4cf12b3df01"
    issued = await issue_token(progress_app)

    response = await post_progress(
        progress_app, report_body(issued["token"], device_id, detail="ethernet")
    )

    assert response.status_code == 202, response.text
    assert response.content == b""

    rows = await rows_for(engine, device_id)
    assert [(row.stage, row.detail) for row in rows] == [("link_up", "ethernet")]
    assert rows[0].token_id == uuid.UUID(issued["id"])
    assert rows[0].at is not None


async def test_the_stages_of_a_boot_are_kept_in_order(
    progress_app: FastAPI, engine: AsyncEngine
) -> None:
    """A board reports its whole arrival under one token; nothing overwrites anything."""
    device_id = "a4cf12b3df02"
    issued = await issue_token(progress_app)

    for stage in (ProgressStage.LINK_UP, ProgressStage.TIME_SYNCED, ProgressStage.ENROLLING):
        assert (
            await post_progress(progress_app, report_body(issued["token"], device_id, stage=stage))
        ).status_code == 202

    assert [row.stage for row in await rows_for(engine, device_id)] == [
        "link_up",
        "time_synced",
        "enrolling",
    ]


async def test_a_progress_report_never_burns_the_token(
    progress_app: FastAPI, engine: AsyncEngine
) -> None:
    """SECURITY: reporting is not enrolling.

    A burn here would mean a board that cannot join the fleet **because it announced
    that it was trying to** — the feature destroying the boot it exists to observe.
    """
    device_id = "a4cf12b3df03"
    issued = await issue_token(progress_app)

    for stage in ("link_up", "time_synced", "enrolling"):
        assert (
            await post_progress(progress_app, report_body(issued["token"], device_id, stage=stage))
        ).status_code == 202

    async with _sessionmaker(engine)() as session:
        row = await session.get(EnrollmentToken, uuid.UUID(issued["id"]))
    assert row is not None
    assert row.used_at is None
    assert row.used_by_device_id is None

    # And the proof that matters: the token still enrols.
    assert (
        await post_enroll(progress_app, enroll_body(issued["token"], device_id))
    ).status_code == 200


async def test_a_burned_token_still_reports_for_its_own_device(
    progress_app: FastAPI, engine: AsyncEngine
) -> None:
    """`enrolled` and `mqtt_connected` happen AFTER the burn, or they are unreportable."""
    device_id = "a4cf12b3df04"
    issued = await issue_token(progress_app)
    assert (
        await post_enroll(progress_app, enroll_body(issued["token"], device_id))
    ).status_code == 200

    response = await post_progress(
        progress_app,
        report_body(issued["token"], device_id, stage=ProgressStage.MQTT_CONNECTED),
    )

    assert response.status_code == 202, response.text
    assert [row.stage for row in await rows_for(engine, device_id)] == ["mqtt_connected"]


# ---------------------------------------------------------------------------
# The authorization matrix
# ---------------------------------------------------------------------------


async def test_another_devices_token_is_refused(progress_app: FastAPI, engine: AsyncEngine) -> None:
    """SECURITY: a burned token reports for the device it was burned for, and no other.

    A 202 here would let anyone holding one spent token write arrivals for any
    device_id they can invent, and the dashboard would show boards that do not exist.
    """
    enrolled_id = "a4cf12b3df05"
    other_id = "a4cf12b3df06"
    issued = await issue_token(progress_app)
    assert (
        await post_enroll(progress_app, enroll_body(issued["token"], enrolled_id))
    ).status_code == 200

    response = await post_progress(progress_app, report_body(issued["token"], other_id))

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid enrollment token"
    assert await rows_for(engine, other_id) == []


async def test_an_admin_token_cannot_report(progress_app: FastAPI, engine: AsyncEngine) -> None:
    """SECURITY: the `ffe_` prefix check, the same one `/v1/enroll` makes."""
    device_id = "a4cf12b3df07"
    admin = await login_admin(progress_app)

    response = await post_progress(progress_app, report_body(admin, device_id))

    assert response.status_code == 401
    assert await rows_for(engine, device_id) == []


async def test_a_bad_secret_is_refused(progress_app: FastAPI, engine: AsyncEngine) -> None:
    """The token id is public (it is in the issuance response and the api log)."""
    device_id = "a4cf12b3df08"
    issued = await issue_token(progress_app)
    tampered = f"ffe_{issued['id'].replace('-', '')}.{'A' * 32}"

    response = await post_progress(progress_app, report_body(tampered, device_id))

    assert response.status_code == 401
    assert await rows_for(engine, device_id) == []


async def test_an_unknown_token_id_is_refused(progress_app: FastAPI, engine: AsyncEngine) -> None:
    device_id = "a4cf12b3df09"
    unknown = f"ffe_{uuid.uuid4().hex}.{'A' * 32}"

    response = await post_progress(progress_app, report_body(unknown, device_id))

    assert response.status_code == 401
    assert await rows_for(engine, device_id) == []


async def test_a_revoked_token_is_refused(progress_app: FastAPI, engine: AsyncEngine) -> None:
    """Revocation is instant here too: an operator who kills a token kills its reports."""
    device_id = "a4cf12b3df0a"
    issued = await issue_token(progress_app)
    await execute(
        engine,
        "UPDATE enrollment_tokens SET revoked_at = now() WHERE id = :id",
        id=uuid.UUID(issued["id"]),
    )

    response = await post_progress(progress_app, report_body(issued["token"], device_id))

    assert response.status_code == 401
    assert await rows_for(engine, device_id) == []


async def test_an_expired_token_is_refused(progress_app: FastAPI, engine: AsyncEngine) -> None:
    """The 24 h TTL bounds how long a leaked token can write arrivals, burned or not."""
    device_id = "a4cf12b3df0b"
    issued = await issue_token(progress_app)
    await execute(
        engine,
        "UPDATE enrollment_tokens SET expires_at = now() - interval '1 minute' WHERE id = :id",
        id=uuid.UUID(issued["id"]),
    )

    response = await post_progress(progress_app, report_body(issued["token"], device_id))

    assert response.status_code == 401
    assert await rows_for(engine, device_id) == []


async def test_a_refusal_never_says_why_on_the_wire(progress_app: FastAPI) -> None:
    """One body for every refusal; the distinguishing detail goes to the log."""
    device_id = "a4cf12b3df0c"
    issued = await issue_token(progress_app)

    unknown = f"ffe_{uuid.uuid4().hex}.{'A' * 32}"
    bad_secret = f"ffe_{issued['id'].replace('-', '')}.{'A' * 32}"

    bodies = {
        (await post_progress(progress_app, report_body(unknown, device_id))).text,
        (await post_progress(progress_app, report_body(bad_secret, device_id))).text,
    }
    assert len(bodies) == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device_id",
    ["A4CF12B3DF10", "a4cf12b3df1", "a4:cf:12:b3:df:10", "", "a4cf12b3df10ff"],
)
async def test_a_non_canonical_device_id_is_refused(progress_app: FastAPI, device_id: str) -> None:
    """Same rule as `/v1/enroll`: reject, never normalise — `%u` binds to this string."""
    issued = await issue_token(progress_app)
    response = await post_progress(progress_app, report_body(issued["token"], device_id))
    assert response.status_code == 422


@pytest.mark.parametrize("stage", ["Link_Up", "link up", "link-up", "", "x" * 33, "1st"])
async def test_a_malformed_stage_is_refused(progress_app: FastAPI, stage: str) -> None:
    """The SHAPE is bounded even though the vocabulary is not."""
    issued = await issue_token(progress_app)
    response = await post_progress(
        progress_app, report_body(issued["token"], "a4cf12b3df11", stage=stage)
    )
    assert response.status_code == 422


async def test_a_stage_this_server_has_never_heard_of_is_accepted(
    progress_app: FastAPI, engine: AsyncEngine
) -> None:
    """The additive rule: an agent the server cannot update must be able to say something new."""
    device_id = "a4cf12b3df12"
    issued = await issue_token(progress_app)

    response = await post_progress(
        progress_app, report_body(issued["token"], device_id, stage="calibrating_radio")
    )

    assert response.status_code == 202, response.text
    assert [row.stage for row in await rows_for(engine, device_id)] == ["calibrating_radio"]


async def test_an_overlong_detail_is_refused(progress_app: FastAPI) -> None:
    issued = await issue_token(progress_app)
    response = await post_progress(
        progress_app, report_body(issued["token"], "a4cf12b3df13", detail="x" * 201)
    )
    assert response.status_code == 422


@pytest.mark.parametrize("detail", ["one\ntwo", "one\rtwo", "one\ttwo", "nul\x00byte"])
async def test_a_detail_with_control_characters_is_refused(
    progress_app: FastAPI, detail: str
) -> None:
    """A device that can inject a newline into `detail` can forge an api log line."""
    issued = await issue_token(progress_app)
    response = await post_progress(
        progress_app, report_body(issued["token"], "a4cf12b3df14", detail=detail)
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The row cap
# ---------------------------------------------------------------------------


async def test_a_device_cannot_grow_the_table_without_bound(
    progress_app: FastAPI, engine: AsyncEngine
) -> None:
    """A board retrying enrolment every 60 s reports forever; the table must not.

    The cap is per device, so one chatty board cannot evict another board's history.
    """
    chatty = "a4cf12b3df15"
    quiet = "a4cf12b3df16"
    cap = settings_for_tests().progress_max_rows_per_device
    issued = await issue_token(progress_app)

    assert (
        await post_progress(progress_app, report_body(issued["token"], quiet))
    ).status_code == 202

    for n in range(cap + 5):
        assert (
            await post_progress(
                progress_app, report_body(issued["token"], chatty, detail=f"attempt {n}")
            )
        ).status_code == 202

    rows = await rows_for(engine, chatty)
    assert len(rows) == cap
    # The NEWEST are what survive: the last report is the one that explains the stall.
    assert rows[-1].detail == f"attempt {cap + 4}"
    assert len(await rows_for(engine, quiet)) == 1


# ---------------------------------------------------------------------------
# The event, and the log
# ---------------------------------------------------------------------------


@pytest.fixture
async def listener() -> AsyncIterator[asyncpg.Connection]:
    """A raw asyncpg connection LISTENing on `ff_events` (as the API workers do)."""
    dsn = asyncpg_dsn(database_url_for(TEST_DB_NAME))
    connection = await asyncpg.connect(dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def test_device_progress_is_emitted_on_ff_events(
    progress_app: FastAPI, listener: asyncpg.Connection
) -> None:
    """The dashboard is nudged to re-read; the stage itself is NOT in the frame."""
    device_id = "a4cf12b3df17"
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(EVENTS_CHANNEL, lambda *args: received.put_nowait(args[-1]))

    issued = await issue_token(progress_app)
    assert (
        await post_progress(
            progress_app, report_body(issued["token"], device_id, detail="secret-ish")
        )
    ).status_code == 202

    payload = json.loads(await asyncio.wait_for(received.get(), timeout=5))
    assert payload["type"] == EventType.DEVICE_PROGRESS
    assert payload["device_id"] == device_id
    assert payload["online"] is False, "a board reporting a boot stage is not in the fleet"
    # Device-controlled text does not belong in a fan-out payload; the client re-reads.
    assert "secret-ish" not in json.dumps(payload)


async def test_a_refused_report_writes_nothing_and_emits_nothing(
    progress_app: FastAPI, engine: AsyncEngine, listener: asyncpg.Connection
) -> None:
    """`emit()` runs inside the request's transaction, so a 401 must leave no trace."""
    device_id = "a4cf12b3df18"
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(EVENTS_CHANNEL, lambda *args: received.put_nowait(args[-1]))

    unknown = f"ffe_{uuid.uuid4().hex}.{'A' * 32}"
    assert (await post_progress(progress_app, report_body(unknown, device_id))).status_code == 401

    assert await rows_for(engine, device_id) == []
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(received.get(), timeout=0.5)


async def test_the_token_plaintext_is_never_logged(progress_app: FastAPI) -> None:
    """The one rule the whole `auth` package keeps: ids in the log, never secrets."""
    device_id = "a4cf12b3df19"
    issued = await issue_token(progress_app)

    with capture_logs("fleetforge") as records:
        assert (
            await post_progress(progress_app, report_body(issued["token"], device_id))
        ).status_code == 202

    logged = "\n".join(record.getMessage() for record in records)
    assert issued["token"] not in logged
    assert issued["token"].split(".", 1)[1] not in logged


# ---------------------------------------------------------------------------
# `has_already_arrived` — the S0-fe-3 rule, in memory
# ---------------------------------------------------------------------------
#
# No database and no HTTP: this is a pure predicate over four values, and the whole
# point of putting it in `fleetforge.progress` rather than inline in the router was
# that each conjunct could be pinned separately. `test_api_devices.py` covers what it
# means for the response; this covers what it means.

LAST_SEEN = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.UTC)


def device_with(**overrides: Any) -> Device:
    """An in-memory `Device` — never added to a session, never committed."""
    values: dict[str, Any] = {
        "device_id": "a4cf12b3df20",
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
        "broker_provisioned_at": LAST_SEEN - dt.timedelta(seconds=30),
        "last_seen": LAST_SEEN,
    }
    values.update(overrides)
    return Device(**values)


def test_a_board_with_no_row_at_all_has_not_arrived() -> None:
    """Mid-arrival boards have no `devices` row; that is the feature, not an edge case."""
    assert has_already_arrived(None, stage_at=LAST_SEEN) is False


def test_a_board_with_no_broker_credential_has_not_arrived() -> None:
    """Enrolled but never provisioned — it never reached the last step of the sequence."""
    assert has_already_arrived(device_with(broker_provisioned_at=None), stage_at=LAST_SEEN) is False


def test_a_board_that_never_spoke_has_not_arrived() -> None:
    """Credential issued, connection never made — the `mqtt_refused` case must stay visible."""
    assert has_already_arrived(device_with(last_seen=None), stage_at=LAST_SEEN) is False


@pytest.mark.parametrize(
    ("offset_s", "expected"),
    # The boundary is `<=`: a stage report landing in the same instant as `last_seen`
    # belongs to the arrival that produced it, not to a new one.
    [(-60, True), (0, True), (60, False)],
)
def test_the_boundary_is_the_stage_against_last_seen(offset_s: int, expected: bool) -> None:
    stage_at = LAST_SEEN + dt.timedelta(seconds=offset_s)
    assert has_already_arrived(device_with(), stage_at=stage_at) is expected
