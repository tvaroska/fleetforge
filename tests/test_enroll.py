"""`POST /v1/enroll` over HTTP — the CRITICAL.md surface, against a real database.

The properties that matter here are the ones a mocked session would hide, so every
case runs the real ASGI app against the migrated test database with a **recording
fake provisioner** in place of the broker (no broker is running, and there is nothing
to provision against until `R0-sec-1`).

Four of these are security properties rather than plumbing, and each names the
disaster it prevents:

* `test_one_token_enrolls_exactly_one_board` — a 200 there is **fleet takeover**;
* `test_a_wrong_secret_does_not_burn_the_token` — a burn there is a fleet-wide
  denial of enrollment, triggerable by anyone who has read an api log line;
* `test_bad_identity_is_refused_before_the_burn` — a burn there is a token destroyed
  by a firmware typo, and a board that needs a re-flash to get another one;
* `test_an_admin_token_cannot_enroll` — the `ffe_` prefix check.

**Never assert on total row counts.** The app commits for real and every test in the
session shares one database, so assertions filter by the ids the test created.
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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fleetforge.api.deps import get_broker_provisioner
from fleetforge.auth.enrollment import EnrollmentTokenStatus, token_status
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.broker import BrokerProvisioningError
from fleetforge.clock import now_utc
from fleetforge.db.base import asyncpg_dsn
from fleetforge.db.models import Device, DeviceGroup, EnrollmentToken
from fleetforge.events import EVENTS_CHANNEL, DeviceEvent, EventType
from tests.conftest import (
    TEST_DB_NAME,
    capture_logs,
    client_for,
    database_url_for,
    login_admin,
)

BASE_URL = "https://testserver"


class RecordingProvisioner:
    """A `BrokerProvisioner` that remembers what it was asked to provision."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = True
        self.error: Exception | None = None

    async def ensure_client(self, device_id: str, password: str) -> bool:
        self.calls.append((device_id, password))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def provisioner() -> RecordingProvisioner:
    return RecordingProvisioner()


@pytest.fixture
def enroll_app(admin_app: FastAPI, provisioner: RecordingProvisioner) -> FastAPI:
    """`admin_app` with the broker replaced by the recording fake."""
    admin_app.dependency_overrides[get_broker_provisioner] = lambda: provisioner
    return admin_app


def _sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def issue_token(app: FastAPI, **body: object) -> dict[str, Any]:
    """Mint a token through the R0-be-2 endpoint — the way the field mints one."""
    admin = await login_admin(app)
    async with client_for(app, base_url=BASE_URL) as client:
        client.headers["Authorization"] = f"Bearer {admin}"
        response = await client.post("/v1/enrollment-tokens", json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


def enroll_body(token: str, device_id: str, **overrides: object) -> dict[str, Any]:
    """The flat body `spec/device-protocol.md` step 2 specifies."""
    body: dict[str, Any] = {
        "token": token,
        "device_id": device_id,
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
        "proto": 1,
        "fw_version": "1.4.2",
        "agent_version": "0.3.0",
        "partition_layout": "ab-4m-v1",
        "ota_slot_size": 1966080,
        "capabilities": ["ota", "selftest"],
    }
    body.update(overrides)
    return body


async def post_enroll(app: FastAPI, body: dict[str, Any]) -> httpx.Response:
    """No credential of any kind — the token in the body is the whole credential."""
    async with client_for(app, base_url=BASE_URL) as client:
        return await client.post("/v1/enroll", json=body)


async def fetch_device(engine: AsyncEngine, device_id: str) -> Device | None:
    async with _sessionmaker(engine)() as session:
        return await session.get(Device, device_id)


async def fetch_token(engine: AsyncEngine, token_id: str | uuid.UUID) -> EnrollmentToken:
    async with _sessionmaker(engine)() as session:
        row = await session.get(EnrollmentToken, uuid.UUID(str(token_id)))
    assert row is not None
    return row


async def count_devices(engine: AsyncEngine, device_id: str) -> int:
    async with _sessionmaker(engine)() as session:
        rows = (await session.scalars(select(Device).where(Device.device_id == device_id))).all()
    return len(rows)


async def execute(engine: AsyncEngine, statement: str, **params: object) -> None:
    async with _sessionmaker(engine)() as session:
        await session.execute(text(statement), params)
        await session.commit()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_enroll_returns_exactly_the_three_promised_fields(
    enroll_app: FastAPI, provisioner: RecordingProvisioner
) -> None:
    """`spec/device-protocol.md` step 4: `{device_id, mqtt_username, mqtt_password}`."""
    device_id = "a4cf12b3de01"
    issued = await issue_token(enroll_app)

    response = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"device_id", "mqtt_username", "mqtt_password"}
    assert body["device_id"] == device_id
    # The `%u` pattern ACLs bind to the MQTT username. Anything else breaks fleet authz.
    assert body["mqtt_username"] == device_id
    assert len(body["mqtt_password"]) >= 32
    assert provisioner.calls == [(device_id, body["mqtt_password"])]


async def test_the_device_row_takes_its_group_from_the_token(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    """The group is the token's; `name` is the operator's and is never set here."""
    device_id = "a4cf12b3de02"
    async with _sessionmaker(engine)() as session:
        group = DeviceGroup(name=f"bench-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.commit()
        group_id = group.id

    issued = await issue_token(enroll_app, group_id=str(group_id))
    response = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    assert response.status_code == 200, response.text

    device = await fetch_device(engine, device_id)
    assert device is not None
    assert device.group_id == group_id
    assert device.platform_type == "esp32c6"
    assert device.link_type == "wifi"
    assert device.power_class == "always_on"
    assert device.fw_version == "1.4.2"
    assert device.capabilities == ["ota", "selftest"]
    assert device.enrolled_at is not None
    assert device.name is None
    assert device.decommissioned_at is None


async def test_the_token_is_burned_against_this_device(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    device_id = "a4cf12b3de03"
    issued = await issue_token(enroll_app)

    assert (await post_enroll(enroll_app, enroll_body(issued["token"], device_id))).status_code == (
        200
    )

    row = await fetch_token(engine, issued["id"])
    assert row.used_at is not None
    assert row.used_by_device_id == device_id
    assert token_status(row, now_utc()) is EnrollmentTokenStatus.USED


async def test_unknown_body_fields_are_ignored(enroll_app: FastAPI) -> None:
    """*Evolution rules*: both sides ignore what they do not know, forever."""
    issued = await issue_token(enroll_app)
    body = enroll_body(issued["token"], "a4cf12b3de04", unknown_future_field="ignored")

    assert (await post_enroll(enroll_app, body)).status_code == 200


async def test_the_endpoint_is_unauthenticated(enroll_app: FastAPI) -> None:
    """No cookie, no Authorization header — a board with no credential is the caller."""
    issued = await issue_token(enroll_app)
    async with client_for(enroll_app, base_url=BASE_URL) as client:
        response = await client.post(
            "/v1/enroll", json=enroll_body(issued["token"], "a4cf12b3de05")
        )

    assert response.status_code == 200, response.text
    assert "authorization" not in {k.lower() for k in client.headers}


# ---------------------------------------------------------------------------
# Single use — the CRITICAL.md property
# ---------------------------------------------------------------------------


async def test_one_token_enrolls_exactly_one_board(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    """**A 200 here is fleet takeover.** And the refused row must not survive."""
    first = "a4cf12b3de10"
    second = "b0b1b2b3b410"
    issued = await issue_token(enroll_app)

    assert (await post_enroll(enroll_app, enroll_body(issued["token"], first))).status_code == 200

    refused = await post_enroll(enroll_app, enroll_body(issued["token"], second))
    assert refused.status_code == 409, refused.text
    # The insert happens before the burn, so a refused burn MUST roll it back.
    assert await count_devices(engine, second) == 0


async def test_the_grace_window_recovers_a_lost_response(
    enroll_app: FastAPI, engine: AsyncEngine, provisioner: RecordingProvisioner
) -> None:
    """Same token, same device: a fresh password, one row, and no second burn."""
    device_id = "a4cf12b3de11"
    issued = await issue_token(enroll_app)

    first = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    assert first.status_code == 200
    burned_at = (await fetch_token(engine, issued["id"])).used_at

    second = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    assert second.status_code == 200, second.text
    assert second.json()["mqtt_password"] != first.json()["mqtt_password"]

    assert await count_devices(engine, device_id) == 1
    row = await fetch_token(engine, issued["id"])
    assert row.used_at == burned_at, "the retry is a lookup, never a second burn"
    assert row.used_by_device_id == device_id
    assert len(provisioner.calls) == 2


async def test_the_grace_window_closes(enroll_app: FastAPI, engine: AsyncEngine) -> None:
    """Past `enroll_retry_window_s`, a burned token is simply dead."""
    device_id = "a4cf12b3de12"
    issued = await issue_token(enroll_app)
    assert (
        await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    ).status_code == 200

    await execute(
        engine,
        "UPDATE enrollment_tokens SET used_at = now() - interval '2 hours' WHERE id = :id",
        id=uuid.UUID(issued["id"]),
    )

    late = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    assert late.status_code == 409, late.text


async def test_a_revoked_token_cannot_enroll(enroll_app: FastAPI, engine: AsyncEngine) -> None:
    device_id = "a4cf12b3de13"
    issued = await issue_token(enroll_app)
    await execute(
        engine,
        "UPDATE enrollment_tokens SET revoked_at = now() WHERE id = :id",
        id=uuid.UUID(issued["id"]),
    )

    response = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))

    assert response.status_code == 409, response.text
    assert await count_devices(engine, device_id) == 0


async def test_an_expired_token_cannot_enroll(enroll_app: FastAPI, engine: AsyncEngine) -> None:
    device_id = "a4cf12b3de14"
    issued = await issue_token(enroll_app)
    await execute(
        engine,
        "UPDATE enrollment_tokens SET expires_at = now() - interval '1 minute' WHERE id = :id",
        id=uuid.UUID(issued["id"]),
    )

    response = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))

    assert response.status_code == 409, response.text
    assert await count_devices(engine, device_id) == 0


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


async def test_a_wrong_secret_does_not_burn_the_token(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    """§0.1 — the id half of an `ffe_` token is not a secret; it is in the api log.

    Burning before `averify_secret` would let anyone who has read one log line destroy
    every outstanding token: a bench full of boards that will not enroll, with the
    dashboard reporting them `used`.
    """
    device_id = "a4cf12b3de20"
    issued = await issue_token(enroll_app)
    token_id, _, _secret = issued["token"].partition(".")
    forged = f"{token_id}.WRONGSECRETWRONGSECRET"

    response = await post_enroll(enroll_app, enroll_body(forged, device_id))

    assert response.status_code == 401, response.text
    row = await fetch_token(engine, issued["id"])
    assert row.used_at is None
    assert token_status(row, now_utc()) is EnrollmentTokenStatus.ACTIVE
    assert await count_devices(engine, device_id) == 0


async def test_an_unknown_token_id_is_401(enroll_app: FastAPI) -> None:
    unknown = f"ffe_{uuid.uuid4().hex}.some-secret-value"
    response = await post_enroll(enroll_app, enroll_body(unknown, "a4cf12b3de21"))
    assert response.status_code == 401


async def test_an_admin_token_cannot_enroll(enroll_app: FastAPI) -> None:
    """The `ffe_` prefix is checked: an `ffa_` credential is not an enrollment token."""
    admin_token = await login_admin(enroll_app)
    assert admin_token.startswith("ffa_")

    response = await post_enroll(enroll_app, enroll_body(admin_token, "a4cf12b3de22"))

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Identity, validated before anything can burn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"device_id": "A4CF12B3DE90"}, "uppercase is not the canonical eFuse MAC"),
        ({"device_id": "zz"}, "not 12 hex digits"),
        ({"power_class": "hibernating"}, "presence is undefined for it"),
        ({"power_class": "sleepy"}, "sleepy with no wake interval violates the CHECK"),
    ],
)
async def test_bad_identity_is_refused_before_the_burn(
    enroll_app: FastAPI, engine: AsyncEngine, overrides: dict[str, Any], why: str
) -> None:
    """422 from pydantic, and — the point — the token is still `active` afterwards."""
    issued = await issue_token(enroll_app)
    body = enroll_body(issued["token"], "a4cf12b3de30")
    body.update(overrides)

    response = await post_enroll(enroll_app, body)

    assert response.status_code == 422, response.text
    row = await fetch_token(engine, issued["id"])
    assert token_status(row, now_utc()) is EnrollmentTokenStatus.ACTIVE, why


async def test_a_sleepy_board_with_an_interval_enrolls(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    device_id = "a4cf12b3de31"
    issued = await issue_token(enroll_app)
    body = enroll_body(
        issued["token"], device_id, power_class="sleepy", expected_wake_interval_s=3600
    )

    assert (await post_enroll(enroll_app, body)).status_code == 200

    device = await fetch_device(engine, device_id)
    assert device is not None
    assert device.power_class == "sleepy"
    assert device.expected_wake_interval_s == 3600


async def test_an_unknown_parent_device_is_400_not_500(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    device_id = "a4cf12b3de32"
    issued = await issue_token(enroll_app)
    body = enroll_body(issued["token"], device_id, parent_device_id="ffffffffff99")

    response = await post_enroll(enroll_app, body)

    assert response.status_code == 400, response.text
    assert await count_devices(engine, device_id) == 0
    row = await fetch_token(engine, issued["id"])
    assert row.used_at is None


# ---------------------------------------------------------------------------
# Re-enrollment
# ---------------------------------------------------------------------------


async def test_re_enrollment_upserts_and_keeps_the_operator_s_name(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    """A re-flashed board re-enrolls with a NEW token; the operator's name survives."""
    device_id = "a4cf12b3de40"
    first = await issue_token(enroll_app)
    assert (await post_enroll(enroll_app, enroll_body(first["token"], device_id))).status_code == (
        200
    )

    await execute(
        engine,
        "UPDATE devices SET name = 'bench frame', decommissioned_at = now(), "
        "presence_reported = true, broker_provisioned_at = now() WHERE device_id = :d",
        d=device_id,
    )
    enrolled_at_before = (await fetch_device(engine, device_id)).enrolled_at  # type: ignore[union-attr]

    second = await issue_token(enroll_app)
    body = enroll_body(second["token"], device_id, fw_version="2.0.0")
    assert (await post_enroll(enroll_app, body)).status_code == 200

    assert await count_devices(engine, device_id) == 1
    device = await fetch_device(engine, device_id)
    assert device is not None
    assert device.name == "bench frame", "name is operator-set and never written here"
    assert device.fw_version == "2.0.0"
    assert device.enrolled_at > enrolled_at_before
    # A retired board that re-enrolls is revived, or the ingestor drops it forever.
    assert device.decommissioned_at is None
    # The old retained presence describes a session that no longer exists.
    assert device.presence_reported is None
    assert device.broker_provisioned_at is not None, "the fake provisioner returned True"

    for issued in (first, second):
        row = await fetch_token(engine, issued["id"])
        assert row.used_by_device_id == device_id


async def test_an_ungrouped_token_does_not_ungroup_a_placed_device(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    """`COALESCE(:group_id, devices.group_id)` — R0 tokens are ungrouped in practice."""
    device_id = "a4cf12b3de41"
    async with _sessionmaker(engine)() as session:
        group = DeviceGroup(name=f"bench-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.commit()
        group_id = group.id

    grouped = await issue_token(enroll_app, group_id=str(group_id))
    assert (
        await post_enroll(enroll_app, enroll_body(grouped["token"], device_id))
    ).status_code == 200

    ungrouped = await issue_token(enroll_app)
    assert (
        await post_enroll(enroll_app, enroll_body(ungrouped["token"], device_id))
    ).status_code == 200

    device = await fetch_device(engine, device_id)
    assert device is not None
    assert device.group_id == group_id


# ---------------------------------------------------------------------------
# The broker seam
# ---------------------------------------------------------------------------


async def test_broker_provisioned_at_is_stamped_only_when_something_was_provisioned(
    enroll_app: FastAPI, engine: AsyncEngine, provisioner: RecordingProvisioner
) -> None:
    """`NullProvisioner` returns False and the column stays NULL — R0-sec-1's reconcile list."""
    provisioner.result = True
    provisioned_id = "a4cf12b3de50"
    issued = await issue_token(enroll_app)
    assert (
        await post_enroll(enroll_app, enroll_body(issued["token"], provisioned_id))
    ).status_code == 200
    device = await fetch_device(engine, provisioned_id)
    assert device is not None and device.broker_provisioned_at is not None

    provisioner.result = False
    skipped_id = "a4cf12b3de51"
    issued = await issue_token(enroll_app)
    assert (
        await post_enroll(enroll_app, enroll_body(issued["token"], skipped_id))
    ).status_code == 200
    device = await fetch_device(engine, skipped_id)
    assert device is not None
    assert device.broker_provisioned_at is None


async def test_a_provisioning_failure_is_503_and_the_enrollment_survives(
    enroll_app: FastAPI, engine: AsyncEngine, provisioner: RecordingProvisioner
) -> None:
    """Commit-before-provision, and the grace window is what makes it recoverable."""
    device_id = "a4cf12b3de52"
    issued = await issue_token(enroll_app)
    provisioner.error = BrokerProvisioningError("broker down")

    failed = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))

    assert failed.status_code == 503, failed.text
    assert failed.headers["Retry-After"] == "5"
    device = await fetch_device(engine, device_id)
    assert device is not None, "the enrollment committed before the broker was touched"
    assert device.broker_provisioned_at is None
    assert (await fetch_token(engine, issued["id"])).used_by_device_id == device_id

    # The board retries; the grace window lets it through and it gets a credential.
    provisioner.error = None
    retried = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    assert retried.status_code == 200, retried.text
    device = await fetch_device(engine, device_id)
    assert device is not None and device.broker_provisioned_at is not None


# ---------------------------------------------------------------------------
# Rate limiting, leakage, routing
# ---------------------------------------------------------------------------


async def test_failures_are_rate_limited_and_successes_are_not(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    """Both buckets are consulted before any argon2; only failures count."""
    enroll_app.state.enroll_limiter = FixedWindowLimiter(per_key=2, per_global=60, window_s=60)
    unknown = f"ffe_{uuid.uuid4().hex}.nope"

    codes = [
        (await post_enroll(enroll_app, enroll_body(unknown, "a4cf12b3de60"))).status_code
        for _ in range(3)
    ]
    assert codes == [401, 401, 429]

    limited = await post_enroll(enroll_app, enroll_body(unknown, "a4cf12b3de60"))
    assert int(limited.headers["Retry-After"]) >= 1

    # A successful enrollment must not consume the bucket.
    enroll_app.state.enroll_limiter = FixedWindowLimiter(per_key=2, per_global=60, window_s=60)
    for suffix in ("61", "62", "63"):
        issued = await issue_token(enroll_app)
        response = await post_enroll(
            enroll_app, enroll_body(issued["token"], f"a4cf12b3de{suffix}")
        )
        assert response.status_code == 200, response.text


async def test_the_broker_password_is_never_logged(enroll_app: FastAPI) -> None:
    """It exists in the response body and nowhere else — not Postgres, not the log.

    `capture_logs` rather than `caplog`: the app's `basicConfig(force=True)` unhooks
    caplog's root handler, and this assertion would then pass with nothing captured.
    """
    issued = await issue_token(enroll_app)
    with capture_logs() as records:
        response = await post_enroll(enroll_app, enroll_body(issued["token"], "a4cf12b3de70"))
    assert response.status_code == 200

    password = response.json()["mqtt_password"]
    logged = "\n".join(record.getMessage() for record in records)
    assert records, "nothing was captured — the assertion below would be vacuous"
    assert password not in logged
    assert issued["token"] not in logged


async def test_the_broker_password_is_not_stored_anywhere(
    enroll_app: FastAPI, engine: AsyncEngine
) -> None:
    device_id = "a4cf12b3de71"
    issued = await issue_token(enroll_app)
    response = await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    assert response.status_code == 200
    password = response.json()["mqtt_password"]

    async with _sessionmaker(engine)() as session:
        hit = await session.scalar(
            text("SELECT count(*) FROM devices WHERE devices::text LIKE :needle"),
            {"needle": f"%{password}%"},
        )
    assert hit == 0


async def test_the_enrollment_token_list_never_shows_a_broker_credential(
    enroll_app: FastAPI,
) -> None:
    issued = await issue_token(enroll_app)
    response = await post_enroll(enroll_app, enroll_body(issued["token"], "a4cf12b3de72"))
    assert response.status_code == 200
    password = response.json()["mqtt_password"]

    admin = await login_admin(enroll_app)
    async with client_for(enroll_app, base_url=BASE_URL) as client:
        client.headers["Authorization"] = f"Bearer {admin}"
        listing = await client.get("/v1/enrollment-tokens")

    assert listing.status_code == 200
    assert password not in listing.text
    assert issued["token"] not in listing.text


async def test_enroll_is_versioned_and_documented(enroll_app: FastAPI) -> None:
    async with client_for(enroll_app, base_url=BASE_URL) as client:
        schema = (await client.get("/v1/openapi.json")).json()

    assert "/v1/enroll" in schema["paths"]
    assert all(path.startswith("/v1") for path in schema["paths"])


# ---------------------------------------------------------------------------
# The SSE seam
# ---------------------------------------------------------------------------


@pytest.fixture
async def listener() -> AsyncIterator[asyncpg.Connection]:
    """A raw asyncpg connection LISTENing on `ff_events` (as R0-be-5 will)."""
    dsn = asyncpg_dsn(database_url_for(TEST_DB_NAME))
    connection = await asyncpg.connect(dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def test_device_enrolled_is_emitted_on_ff_events(
    enroll_app: FastAPI, listener: asyncpg.Connection
) -> None:
    """The dashboard learns about a board before it has ever connected; `online` is false."""
    device_id = "a4cf12b3de80"
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(EVENTS_CHANNEL, lambda *args: received.put_nowait(args[-1]))

    issued = await issue_token(enroll_app)
    assert (
        await post_enroll(enroll_app, enroll_body(issued["token"], device_id))
    ).status_code == 200

    payload = json.loads(await asyncio.wait_for(received.get(), timeout=5))
    assert payload["type"] == EventType.DEVICE_ENROLLED
    assert payload["device_id"] == device_id
    assert payload["online"] is False, "the board has not connected yet"
    assert payload["fw_version"] == "1.4.2"
    event = DeviceEvent.model_validate(payload)
    assert event.at <= now_utc() + dt.timedelta(seconds=5)
