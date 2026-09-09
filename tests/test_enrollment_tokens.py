"""Enrollment token issuance over HTTP — the CRITICAL.md surface.

Run against the real ASGI app and the real (migrated) test database. The properties
that matter — single use, revocation beating the burn, the derived status agreeing
with the burn predicate — are exactly the ones a mocked session would hide.

`base_url="https://testserver"` throughout, because the login cookie is `Secure` and
`http.cookiejar` drops a `Secure` cookie on a plain-http origin.

**Never assert on the total number of tokens.** The `admin_app` fixture commits for
real and every test in the session shares one database, so assertions filter by the
ids the test created.
"""

import datetime as dt
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fleetforge.auth.enrollment import (
    BURN_SQL,
    EnrollmentTokenStatus,
    token_status,
)
from fleetforge.auth.hashing import verify_secret
from fleetforge.auth.tokens import ENROLLMENT_TOKEN_PREFIX, parse_token
from fleetforge.db.models import Device, DeviceGroup, EnrollmentToken
from tests.conftest import client_for, login_admin

BASE_URL = "https://testserver"

# Reused as the burn's `used_by_device_id`; the FK requires a real row.
BURN_DEVICE_ID = "aabbccdd0001"


def _client(app: FastAPI, token: str) -> httpx.AsyncClient:
    client = client_for(app, base_url=BASE_URL)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _ensure_burn_device(engine: AsyncEngine) -> str:
    """A committed device row the burn can point `used_by_device_id` at."""
    async with _sessionmaker(engine)() as session:
        if await session.get(Device, BURN_DEVICE_ID) is None:
            session.add(
                Device(
                    device_id=BURN_DEVICE_ID,
                    platform_type="esp32c6",
                    link_type="wifi",
                    power_class="always_on",
                )
            )
            await session.commit()
    return BURN_DEVICE_ID


async def _issue(client: httpx.AsyncClient, **body: object) -> dict:
    response = await client.post("/v1/enrollment-tokens", json=body)
    assert response.status_code == 201, response.text
    payload: dict = response.json()
    return payload


async def _burn(engine: AsyncEngine, token_id: uuid.UUID | str) -> int:
    """Run the shipped burn statement against a committed row; return rows affected."""
    device_id = await _ensure_burn_device(engine)
    async with _sessionmaker(engine)() as session:
        result = await session.execute(
            BURN_SQL, {"token_id": uuid.UUID(str(token_id)), "device_id": device_id}
        )
        await session.commit()
        return int(result.rowcount)


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


async def test_issue_returns_plaintext_once(admin_app: FastAPI) -> None:
    """201 with an `ffe_` token whose embedded uuid is the row's id, valid for 24 h."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        body = await _issue(client)

    assert body["token"].startswith("ffe_")
    assert body["group_id"] is None

    parts = parse_token(body["token"], ENROLLMENT_TOKEN_PREFIX)
    assert parts is not None
    assert parts.token_id == uuid.UUID(body["id"])

    lifetime = dt.datetime.fromisoformat(body["expires_at"]) - dt.datetime.fromisoformat(
        body["created_at"]
    )
    assert abs(lifetime - dt.timedelta(hours=24)) < dt.timedelta(seconds=60)


async def test_issued_secret_verifies_against_stored_hash(
    admin_app: FastAPI, engine: AsyncEngine
) -> None:
    """The artifact actually works: the issued secret verifies against what was stored.

    Until `R0-be-4` exists there is nothing else that can prove the token is usable.
    """
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        body = await _issue(client)

    secret = body["token"].split(".", 1)[1]
    async with _sessionmaker(engine)() as session:
        row = await session.get(EnrollmentToken, uuid.UUID(body["id"]))

    assert row is not None
    assert row.secret_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert secret not in row.secret_hash
    assert verify_secret(secret, row.secret_hash) is True


async def test_issue_requires_admin(admin_app: FastAPI) -> None:
    """No credential is a 401; an `ffe_` token is not an admin credential either."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        issued = await _issue(client)

    async with client_for(admin_app, base_url=BASE_URL) as anonymous:
        response = await anonymous.post("/v1/enrollment-tokens", json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    async with _client(admin_app, issued["token"]) as impostor:
        as_enrollment = await impostor.get("/v1/enrollment-tokens")
    assert as_enrollment.status_code == 401


async def test_issue_unknown_group_404(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """An unknown group is a 404, not a 500 from an escaping FK violation.

    And nothing is inserted: a refused issuance that still leaves a row behind would
    put an unreachable credential in the table.
    """
    token = await login_admin(admin_app)

    async def count_tokens() -> int:
        async with _sessionmaker(engine)() as session:
            return len((await session.scalars(select(EnrollmentToken.id))).all())

    before = await count_tokens()
    async with _client(admin_app, token) as client:
        response = await client.post("/v1/enrollment-tokens", json={"group_id": str(uuid.uuid4())})

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown group"}
    assert await count_tokens() == before


async def test_issue_with_group(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """A real group is echoed back and persisted on the row."""
    async with _sessionmaker(engine)() as session:
        group = DeviceGroup(name=f"bench-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.commit()

    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        body = await _issue(client, group_id=str(group.id))

    assert body["group_id"] == str(group.id)

    async with _sessionmaker(engine)() as session:
        row = await session.get(EnrollmentToken, uuid.UUID(body["id"]))
    assert row is not None
    assert row.group_id == group.id


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def test_list_never_exposes_the_secret(admin_app: FastAPI) -> None:
    """No plaintext, no hash, and no field that could ever carry either."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        issued = await _issue(client)
        response = await client.get("/v1/enrollment-tokens")

    assert response.status_code == 200
    assert "ffe_" not in response.text
    assert "argon2" not in response.text

    summary = next(row for row in response.json()["tokens"] if row["id"] == issued["id"])
    assert "token" not in summary
    assert "secret_hash" not in summary


async def test_list_reports_status(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """Four seeded states map to four statuses, newest first."""
    device_id = await _ensure_burn_device(engine)
    now = dt.datetime.now(dt.UTC)
    seeds = {
        "active": {"expires_at": now + dt.timedelta(hours=24)},
        "used": {
            "expires_at": now + dt.timedelta(hours=24),
            "used_at": now,
            "used_by_device_id": device_id,
        },
        "revoked": {"expires_at": now + dt.timedelta(hours=24), "revoked_at": now},
        "expired": {"expires_at": now - dt.timedelta(seconds=1)},
    }

    created: dict[str, uuid.UUID] = {}
    async with _sessionmaker(engine)() as session:
        for offset, (expected, values) in enumerate(seeds.items()):
            row = EnrollmentToken(
                secret_hash="$argon2id$v=19$m=19456,t=2,p=1$seeded",
                created_at=now + dt.timedelta(seconds=offset),
                **values,
            )
            session.add(row)
            await session.flush()
            created[expected] = row.id
        await session.commit()

    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        response = await client.get("/v1/enrollment-tokens")

    rows = response.json()["tokens"]
    by_id = {row["id"]: row for row in rows}
    for expected, token_id in created.items():
        assert by_id[str(token_id)]["status"] == expected

    timestamps = [dt.datetime.fromisoformat(row["created_at"]) for row in rows]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("active", EnrollmentTokenStatus.ACTIVE),
        ("used", EnrollmentTokenStatus.USED),
        ("revoked", EnrollmentTokenStatus.REVOKED),
        ("expired", EnrollmentTokenStatus.EXPIRED),
    ],
)
async def test_status_matches_burn_predicate(
    session: AsyncSession, state: str, expected: EnrollmentTokenStatus
) -> None:
    """`token_status` says ACTIVE **iff** `BURN_SQL` would affect the row.

    This is the equivalence that stops the dashboard and the authorization decision
    drifting apart. Runs on the rolled-back `session` fixture, so the burn here leaves
    nothing behind.
    """
    now = dt.datetime.now(dt.UTC)
    session.add(
        Device(
            device_id="bbccddee0001",
            platform_type="esp32c6",
            link_type="wifi",
            power_class="always_on",
        )
    )
    await session.flush()

    values: dict[str, object] = {"expires_at": now + dt.timedelta(hours=24)}
    if state == "used":
        values |= {"used_at": now, "used_by_device_id": "bbccddee0001"}
    elif state == "revoked":
        values["revoked_at"] = now
    elif state == "expired":
        values["expires_at"] = now - dt.timedelta(seconds=1)

    row = EnrollmentToken(secret_hash="$argon2id$v=19$m=19456,t=2,p=1$seeded", **values)
    session.add(row)
    await session.flush()

    derived = token_status(row, now)
    assert derived == expected

    result = await session.execute(BURN_SQL, {"token_id": row.id, "device_id": "bbccddee0001"})
    burned = result.rowcount == 1

    assert burned == (derived == EnrollmentTokenStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Revocation and the burn — the CRITICAL.md properties
# ---------------------------------------------------------------------------


async def test_burn_after_issue_is_single_use(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """A token issued through the endpoint burns exactly once. Twice is fleet takeover."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        issued = await _issue(client)

    assert await _burn(engine, issued["id"]) == 1
    assert await _burn(engine, issued["id"]) == 0, "a second burn is fleet takeover"

    async with _client(admin_app, token) as client:
        rows = (await client.get("/v1/enrollment-tokens")).json()["tokens"]
    summary = next(row for row in rows if row["id"] == issued["id"])
    assert summary["status"] == "used"
    assert summary["used_by_device_id"] == BURN_DEVICE_ID


async def test_revoke_then_burn_fails(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """THE critical property: revocation is effective against the burn immediately."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        issued = await _issue(client)
        revoke = await client.post(f"/v1/enrollment-tokens/{issued['id']}/revoke")

    assert revoke.status_code == 204
    assert await _burn(engine, issued["id"]) == 0


async def test_revoke_is_idempotent(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """A second revoke is still 204 and does not move `revoked_at`."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        issued = await _issue(client)
        assert (
            await client.post(f"/v1/enrollment-tokens/{issued['id']}/revoke")
        ).status_code == 204

        async with _sessionmaker(engine)() as session:
            first = await session.get(EnrollmentToken, uuid.UUID(issued["id"]))
            assert first is not None
            revoked_at = first.revoked_at

        assert (
            await client.post(f"/v1/enrollment-tokens/{issued['id']}/revoke")
        ).status_code == 204

    async with _sessionmaker(engine)() as session:
        second = await session.get(EnrollmentToken, uuid.UUID(issued["id"]))
    assert second is not None
    assert second.revoked_at == revoked_at


async def test_revoke_unknown_404(admin_app: FastAPI) -> None:
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        response = await client.post(f"/v1/enrollment-tokens/{uuid.uuid4()}/revoke")

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown token"}


async def test_expired_token_status_and_burn(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """Back-dating `expires_at` makes the token expired in the list and dead to the burn."""
    token = await login_admin(admin_app)
    async with _client(admin_app, token) as client:
        issued = await _issue(client)

    async with _sessionmaker(engine)() as session:
        row = await session.get(EnrollmentToken, uuid.UUID(issued["id"]))
        assert row is not None
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    async with _client(admin_app, token) as client:
        rows = (await client.get("/v1/enrollment-tokens")).json()["tokens"]
    summary = next(item for item in rows if item["id"] == issued["id"])
    assert summary["status"] == "expired"

    assert await _burn(engine, issued["id"]) == 0


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


async def test_routes_are_versioned(admin_app: FastAPI) -> None:
    """All three routes exist, under `/v1`, in the published schema."""
    async with client_for(admin_app, base_url=BASE_URL) as client:
        response = await client.get("/v1/openapi.json")

    paths = response.json()["paths"]
    assert {"/v1/enrollment-tokens", "/v1/enrollment-tokens/{token_id}/revoke"} <= set(paths)
    assert all(path.startswith("/v1") for path in paths)
