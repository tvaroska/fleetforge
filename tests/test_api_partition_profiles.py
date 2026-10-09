"""`/v1/partition-profiles` — list, create, adopt, delete. R3-be-2.

These tests commit through the API, so `profiles_db` clears every non-builtin profile (and
the devices and artifacts they read) on entry and on exit. **It never deletes the builtins**:
the rest of the suite reaches them through the deploy gate and the upload ceiling.
"""

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.partition_profiles import MAX_PROFILE_SLOT_SIZE
from tests.conftest import capture_logs, client_for, login_admin

AB_SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
ARDUINO_SHA = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"
WRONG_SHA = "47db53920359cfb4581532a293d8e563401f3abe37f9b282cc713039ac937c4c"
USER_SHA = hashlib.sha256(b"be2").hexdigest()
SLOT = 1966080


async def _clear(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM partition_profiles WHERE origin <> 'builtin'"))
        await conn.execute(text("DELETE FROM deploy_events"))
        await conn.execute(text("DELETE FROM devices"))
        await conn.execute(text("DELETE FROM artifact_versions"))
        await conn.execute(text("DELETE FROM artifacts"))


@pytest.fixture
async def profiles_db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    await _clear(engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
    await _clear(engine)


async def add_pending(
    session: AsyncSession,
    sha: str = WRONG_SHA,
    *,
    slot: int | None = 1835008,
    device_id: str | None = "a4cf12b3de90",
) -> None:
    await session.execute(
        text(
            "INSERT INTO partition_profiles (partition_table_sha256, origin, ota_slot_size, "
            "flash_chip_size, detected_device_id) VALUES (:sha, 'detected', :slot, 4194304, :d)"
        ),
        {"sha": sha, "slot": slot, "d": device_id},
    )
    await session.commit()


async def add_device(session: AsyncSession, device_id: str, sha: str | None, **extra: Any) -> None:
    session.add(
        Device(
            device_id=device_id,
            platform_type="esp32",
            link_type="wifi",
            power_class="always_on",
            partition_layout="unknown",
            partition_table_sha256=sha,
            **extra,
        )
    )
    await session.commit()


async def add_artifact(session: AsyncSession, layout: str) -> None:
    await session.execute(
        text(
            "INSERT INTO artifacts (sha256, size_bytes, kind, target, partition_layout) "
            "VALUES (:sha, 10, 'user_firmware', 'esp32', :layout)"
        ),
        {"sha": hashlib.sha256(layout.encode()).hexdigest(), "layout": layout},
    )
    await session.commit()


async def call(
    app: FastAPI, method: str, path: str = "", token: str | None = None, **kwargs: Any
) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with client_for(app, base_url="https://testserver") as client:
        return await client.request(
            method, f"/v1/partition-profiles{path}", headers=headers, **kwargs
        )


async def row(session: AsyncSession, sha: str) -> tuple[Any, ...] | None:
    result = await session.execute(
        text(
            "SELECT layout_id, origin, ota_slot_size, adopted_at IS NOT NULL "
            "FROM partition_profiles WHERE partition_table_sha256 = :sha"
        ),
        {"sha": sha},
    )
    found = result.first()
    await session.commit()
    return tuple(found) if found is not None else None


CREATE = {"layout_id": "be2-user", "partition_table_sha256": USER_SHA, "ota_slot_size": SLOT}


class TestAuth:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "", None),
            ("POST", "", CREATE),
            ("PATCH", f"/{WRONG_SHA}", {"layout_id": "x"}),
            ("DELETE", f"/{WRONG_SHA}", None),
        ],
    )
    async def test_every_route_needs_an_admin(
        self,
        admin_app: FastAPI,
        profiles_db: AsyncSession,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        await add_pending(profiles_db)
        response = await call(admin_app, method, path, json=body)
        assert response.status_code == 401
        assert await row(profiles_db, WRONG_SHA) == (None, "detected", 1835008, False)
        assert await row(profiles_db, USER_SHA) is None


class TestList:
    async def test_the_builtins_are_listed_deployable(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        response = await call(admin_app, "GET", token=token)
        assert response.status_code == 200
        listed = response.json()["profiles"]
        assert [(p["layout_id"], p["origin"], p["deployable"]) for p in listed] == [
            ("ab-4m-v1", "builtin", True),
            ("ab-4m-arduino-v1", "builtin", True),
        ]
        assert [p["partition_table_sha256"] for p in listed] == [AB_SHA, ARDUINO_SHA]
        assert all(p["ota_slot_size"] == SLOT and p["adopted_at"] for p in listed)

    async def test_order_and_live_device_ids(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        await add_pending(profiles_db, "c" * 64, device_id=None)
        await add_device(profiles_db, "a4cf12b3de92", WRONG_SHA)
        await add_device(profiles_db, "a4cf12b3de91", WRONG_SHA)
        await add_device(profiles_db, "a4cf12b3de93", WRONG_SHA, decommissioned_at=now_utc())
        await add_device(profiles_db, "a4cf12b3de94", AB_SHA)
        token = await login_admin(admin_app)
        assert (await call(admin_app, "POST", token=token, json=CREATE)).status_code == 201

        listed = (await call(admin_app, "GET", token=token)).json()["profiles"]

        assert [(p["layout_id"], p["partition_table_sha256"]) for p in listed] == [
            ("ab-4m-v1", AB_SHA),
            ("ab-4m-arduino-v1", ARDUINO_SHA),
            ("be2-user", USER_SHA),
            (None, WRONG_SHA),
            (None, "c" * 64),
        ]
        by_sha = {p["partition_table_sha256"]: p for p in listed}
        assert by_sha[WRONG_SHA]["device_ids"] == ["a4cf12b3de91", "a4cf12b3de92"]
        assert by_sha[WRONG_SHA]["deployable"] is False
        assert by_sha[WRONG_SHA]["detected_device_id"] == "a4cf12b3de90"
        assert by_sha[WRONG_SHA]["flash_chip_size"] == 4194304
        assert by_sha[AB_SHA]["device_ids"] == ["a4cf12b3de94"]
        assert by_sha[USER_SHA]["device_ids"] == []


class TestCreate:
    async def test_a_user_profile_is_deployable_at_once(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        with capture_logs() as records:
            response = await call(admin_app, "POST", token=token, json=CREATE)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["layout_id"] == "be2-user"
        assert body["origin"] == "user"
        assert body["deployable"] is True
        assert body["ota_slot_size"] == SLOT
        assert body["adopted_at"] is not None
        assert await row(profiles_db, USER_SHA) == ("be2-user", "user", SLOT, True)
        assert any("created by" in r.getMessage() for r in records)

    async def test_a_duplicate_fingerprint_or_name_is_409(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        assert (await call(admin_app, "POST", token=token, json=CREATE)).status_code == 201

        again = await call(admin_app, "POST", token=token, json={**CREATE, "layout_id": "other"})
        assert again.status_code == 409
        assert "already the partition profile be2-user" in again.json()["detail"]

        taken = await call(
            admin_app, "POST", token=token, json={**CREATE, "partition_table_sha256": "d" * 64}
        )
        assert taken.status_code == 409
        assert "be2-user is already taken" in taken.json()["detail"]

        builtin = await call(
            admin_app, "POST", token=token, json={**CREATE, "partition_table_sha256": AB_SHA}
        )
        assert builtin.status_code == 409
        assert "ab-4m-v1" in builtin.json()["detail"]
        assert "`" not in builtin.json()["detail"]

    async def test_a_pending_fingerprint_points_to_adoption(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "POST", token=token, json={**CREATE, "partition_table_sha256": WRONG_SHA}
        )
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "this fingerprint is already a detected profile; adopt it by naming it"
        )
        assert await row(profiles_db, WRONG_SHA) == (None, "detected", 1835008, False)

    @pytest.mark.parametrize(
        "change",
        [
            {"layout_id": "unknown"},
            {"layout_id": "Be2"},
            {"layout_id": "be2/map"},
            {"layout_id": "be2\n"},
            {"partition_table_sha256": USER_SHA.upper()},
            {"partition_table_sha256": USER_SHA[:63]},
            {"ota_slot_size": 0},
            {"ota_slot_size": MAX_PROFILE_SLOT_SIZE + 1},
            {"extra": 1},
        ],
    )
    async def test_a_malformed_body_is_422(
        self, admin_app: FastAPI, profiles_db: AsyncSession, change: dict[str, Any]
    ) -> None:
        token = await login_admin(admin_app)
        response = await call(admin_app, "POST", token=token, json={**CREATE, **change})
        assert response.status_code == 422, response.text
        count = await profiles_db.scalar(
            text("SELECT count(*) FROM partition_profiles WHERE origin <> 'builtin'")
        )
        assert count == 0

    async def test_the_slot_ceiling_itself_is_accepted(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "POST", token=token, json={**CREATE, "ota_slot_size": MAX_PROFILE_SLOT_SIZE}
        )
        assert response.status_code == 201


class TestAdopt:
    async def test_naming_a_pending_profile_adopts_it_and_keeps_the_measured_slot(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "PATCH", f"/{WRONG_SHA}", token=token, json={"layout_id": "be2-map"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["layout_id"] == "be2-map"
        assert body["origin"] == "detected"
        assert body["deployable"] is True
        assert body["ota_slot_size"] == 1835008
        assert body["adopted_at"] is not None
        assert await row(profiles_db, WRONG_SHA) == ("be2-map", "detected", 1835008, True)

        # The same slot, stated, is not a disagreement.
        await add_pending(profiles_db, "c" * 64)
        same = await call(
            admin_app,
            "PATCH",
            f"/{'c' * 64}",
            token=token,
            json={"layout_id": "be2-c", "ota_slot_size": 1835008},
        )
        assert same.status_code == 200

    async def test_a_board_that_reported_no_slot_needs_one(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db, slot=None)
        token = await login_admin(admin_app)
        missing = await call(
            admin_app, "PATCH", f"/{WRONG_SHA}", token=token, json={"layout_id": "be2-map"}
        )
        assert missing.status_code == 422
        assert missing.json()["detail"] == (
            "this board did not report its OTA slot size; give ota_slot_size"
        )
        given = await call(
            admin_app,
            "PATCH",
            f"/{WRONG_SHA}",
            token=token,
            json={"layout_id": "be2-map", "ota_slot_size": 1835008},
        )
        assert given.status_code == 200
        assert await row(profiles_db, WRONG_SHA) == ("be2-map", "detected", 1835008, True)

    async def test_a_different_slot_is_409_the_measurement_wins(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        response = await call(
            admin_app,
            "PATCH",
            f"/{WRONG_SHA}",
            token=token,
            json={"layout_id": "be2-map", "ota_slot_size": SLOT},
        )
        assert response.status_code == 409
        assert "1835008" in response.json()["detail"] and str(SLOT) in response.json()["detail"]
        assert await row(profiles_db, WRONG_SHA) == (None, "detected", 1835008, False)

    async def test_a_measured_slot_above_the_ceiling_cannot_be_adopted(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db, slot=MAX_PROFILE_SLOT_SIZE + 1)
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "PATCH", f"/{WRONG_SHA}", token=token, json={"layout_id": "be2-map"}
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert str(MAX_PROFILE_SLOT_SIZE + 1) in detail and str(MAX_PROFILE_SLOT_SIZE) in detail

    async def test_a_builtin_never_changes(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "PATCH", f"/{AB_SHA}", token=token, json={"layout_id": "x"}
        )
        assert response.status_code == 409
        assert "builtin profiles are part of the firmware contract" in response.json()["detail"]
        assert await row(profiles_db, AB_SHA) == ("ab-4m-v1", "builtin", SLOT, True)

    async def test_a_name_never_changes_once_given(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        path = f"/{WRONG_SHA}"
        assert (
            await call(admin_app, "PATCH", path, token=token, json={"layout_id": "be2-map"})
        ).status_code == 200
        again = await call(admin_app, "PATCH", path, token=token, json={"layout_id": "other"})
        assert again.status_code == 409
        assert "never changes once given" in again.json()["detail"]
        assert (await call(admin_app, "POST", token=token, json=CREATE)).status_code == 201
        user = await call(admin_app, "PATCH", f"/{USER_SHA}", token=token, json={"layout_id": "y"})
        assert user.status_code == 409
        assert await row(profiles_db, WRONG_SHA) == ("be2-map", "detected", 1835008, True)

    async def test_a_taken_name_is_409(self, admin_app: FastAPI, profiles_db: AsyncSession) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "PATCH", f"/{WRONG_SHA}", token=token, json={"layout_id": "ab-4m-v1"}
        )
        assert response.status_code == 409
        assert "ab-4m-v1 is already taken" in response.json()["detail"]
        assert await row(profiles_db, WRONG_SHA) == (None, "detected", 1835008, False)

    @pytest.mark.parametrize("sha", ["d" * 64, "D" * 64, "not-a-fingerprint"])
    async def test_an_unknown_or_malformed_fingerprint_is_404(
        self, admin_app: FastAPI, profiles_db: AsyncSession, sha: str
    ) -> None:
        token = await login_admin(admin_app)
        response = await call(admin_app, "PATCH", f"/{sha}", token=token, json={"layout_id": "x"})
        assert response.status_code == 404
        assert "no partition profile" in response.json()["detail"]

    async def test_unknown_is_reserved(self, admin_app: FastAPI, profiles_db: AsyncSession) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        response = await call(
            admin_app, "PATCH", f"/{WRONG_SHA}", token=token, json={"layout_id": "unknown"}
        )
        assert response.status_code == 422


class TestDelete:
    async def test_a_builtin_is_409(self, admin_app: FastAPI, profiles_db: AsyncSession) -> None:
        token = await login_admin(admin_app)
        response = await call(admin_app, "DELETE", f"/{ARDUINO_SHA}", token=token)
        assert response.status_code == 409
        assert "builtin profiles are part of the firmware contract" in response.json()["detail"]
        assert await row(profiles_db, ARDUINO_SHA) == ("ab-4m-arduino-v1", "builtin", SLOT, True)

    async def test_a_pending_profile_is_deleted(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        response = await call(admin_app, "DELETE", f"/{WRONG_SHA}", token=token)
        assert response.status_code == 204
        assert response.content == b""
        assert await row(profiles_db, WRONG_SHA) is None

    async def test_a_profile_with_images_is_409(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        assert (await call(admin_app, "POST", token=token, json=CREATE)).status_code == 201
        await add_artifact(profiles_db, "be2-user")
        response = await call(admin_app, "DELETE", f"/{USER_SHA}", token=token)
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "1 artifact is labelled for be2-user; a profile with images cannot be removed"
        )
        assert await row(profiles_db, USER_SHA) == ("be2-user", "user", SLOT, True)

    async def test_an_adopted_profile_with_images_is_409_too(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        await add_pending(profiles_db)
        token = await login_admin(admin_app)
        path = f"/{WRONG_SHA}"
        await call(admin_app, "PATCH", path, token=token, json={"layout_id": "be2-map"})
        await add_artifact(profiles_db, "be2-map")
        assert (await call(admin_app, "DELETE", path, token=token)).status_code == 409

    async def test_a_user_profile_without_images_is_deleted(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        assert (await call(admin_app, "POST", token=token, json=CREATE)).status_code == 201
        response = await call(admin_app, "DELETE", f"/{USER_SHA}", token=token)
        assert response.status_code == 204
        assert await row(profiles_db, USER_SHA) is None

    async def test_an_unknown_fingerprint_is_404(
        self, admin_app: FastAPI, profiles_db: AsyncSession
    ) -> None:
        token = await login_admin(admin_app)
        assert (await call(admin_app, "DELETE", f"/{'d' * 64}", token=token)).status_code == 404
