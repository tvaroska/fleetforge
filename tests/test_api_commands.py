"""The four verbs and the command read — `api/routers/commands.py`. R5-be-2.

With the broker replaced by `FakeCommandPublisher`. The properties worth holding:

* **The command on the wire is Patch A2's**, key for key, and its `id` is the
  `command_id` the 202 names and the `device_commands` row's primary key.
* **A verb never writes a `deploy_events` row** — not `requested`, not `failed`, not on a
  publish failure. The deploy KPI table only records what the board reports.
* **The server gates only what it alone knows**: the board, its announced capability, and
  for apply/cancel the transaction it recorded for that board. Everything about the
  board's live state is the board's call, answered on `up/ack`.
* **Admin-only**, every route.
"""

import datetime as dt
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge import device_commands
from fleetforge.api.deps import get_command_publisher, get_object_store
from fleetforge.broker import CommandPublishError
from fleetforge.clock import now_utc
from fleetforge.db.models import TERMINAL_DEPLOY_STATES, DeployEvent, DeployState, Device
from fleetforge.storage.blobs import blob_key
from tests.conftest import FakeCommandPublisher, MemoryObjectStore, client_for, login_admin
from tests.test_api_deploy import (
    DEVICE_ID,
    IMAGE,
    OTHER_DEVICE_ID,
    SHA256,
    add_artifact,
    add_device,
    deploy,
    events,
)

ALL_VERBS = ["ota", "apply", "cancel", "rollback", "reboot"]


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    store = MemoryObjectStore()
    store.objects[blob_key(SHA256)] = IMAGE
    admin_app.dependency_overrides[get_object_store] = lambda: store
    yield store
    admin_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture
def publisher(admin_app: FastAPI) -> Iterator[FakeCommandPublisher]:
    publisher = FakeCommandPublisher()
    admin_app.dependency_overrides[get_command_publisher] = lambda: publisher
    yield publisher
    admin_app.dependency_overrides.pop(get_command_publisher, None)


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Committed writes, cleaned up afterwards (`device_commands` cascades with devices)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(DeployEvent))
            await session.execute(delete(Device))
            await session.execute(text("DELETE FROM artifact_versions"))
            await session.execute(text("DELETE FROM artifacts"))
            await session.commit()


async def post(
    app: FastAPI, path: str, token: str | None, device_id: str = DEVICE_ID
) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(f"/v1/devices/{device_id}{path}", headers=headers)


async def get_command(
    app: FastAPI, token: str, command_id: str, device_id: str = DEVICE_ID
) -> httpx.Response:
    async with client_for(app, base_url="https://testserver") as client:
        return await client.get(
            f"/v1/devices/{device_id}/commands/{command_id}",
            headers={"authorization": f"Bearer {token}"},
        )


async def command_rows(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT id, device_id, type, cmd_id, sent_by, publish_failed, result "
                "FROM device_commands ORDER BY sent_at, id"
            )
        )
    ).all()
    return [dict(row._mapping) for row in rows]  # noqa: SLF001 - the documented Row accessor


async def staged_deploy(
    app: FastAPI, db: AsyncSession, token: str, *, apply: str = "on_command"
) -> str:
    """A recorded deploy transaction for DEVICE_ID; returns its `cmd_id`."""
    await add_artifact(db)
    response = await deploy(app, token, apply=apply)
    assert response.status_code == 202, response.text
    return str(response.json()["cmd_id"])


def verb_path(verb: str, cmd_id: str | None) -> str:
    return f"/deploys/{cmd_id}/{verb}" if cmd_id is not None else f"/{verb}"


class TestEachVerbOnTheWire:
    @pytest.mark.parametrize("verb", ["rollback", "reboot"])
    async def test_rollback_and_reboot_publish_the_patch_a2_body(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher, verb: str
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)

        response = await post(admin_app, f"/{verb}", token)

        assert response.status_code == 202, response.text
        body = response.json()
        assert body == {
            "command_id": body["command_id"],
            "device_id": DEVICE_ID,
            "type": verb,
            "cmd_id": None,
            "device_online": True,
        }
        assert len(publisher.published) == 1
        device_id, payload = publisher.published[0]
        assert device_id == DEVICE_ID
        assert list(payload) == ["id", "type"]
        assert payload == {"id": body["command_id"], "type": verb}
        rows = await command_rows(db)
        assert [(r["id"], r["type"], r["cmd_id"], r["publish_failed"]) for r in rows] == [
            (body["command_id"], verb, None, False)
        ]
        assert rows[0]["sent_by"]["subject"] == "admin"
        assert rows[0]["sent_by"]["token_id"]
        assert await events(db) == []

    @pytest.mark.parametrize("verb", ["apply", "cancel"])
    async def test_apply_and_cancel_name_the_transaction(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        verb: str,
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token)
        before = await events(db)

        response = await post(admin_app, f"/deploys/{cmd_id}/{verb}", token)

        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["type"], body["cmd_id"]) == (verb, cmd_id)
        assert len(publisher.published) == 2  # the stage, then the verb
        payload = publisher.last
        assert list(payload) == ["id", "type", "cmd_id"]
        assert payload == {"id": body["command_id"], "type": verb, "cmd_id": cmd_id}
        assert body["command_id"] != cmd_id
        assert [(r["type"], r["cmd_id"]) for r in await command_rows(db)] == [(verb, cmd_id)]
        # Zero new deploy rows: the `requested` row of the stage is all there is.
        assert await events(db) == before
        assert [e["state"] for e in before] == ["requested"]

    async def test_cancel_works_on_an_auto_transaction_too(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """D4: an `auto` stage can still be cancelled before its reboot (the board decides)."""
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token, apply="auto")

        response = await post(admin_app, f"/deploys/{cmd_id}/cancel", token)

        assert response.status_code == 202, response.text


class TestTheCapabilityGate:
    @pytest.mark.parametrize("verb", ["apply", "cancel", "rollback", "reboot"])
    async def test_a_board_that_does_not_announce_the_verb_is_409(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        verb: str,
    ) -> None:
        """D9: an agent that predates R5 ignores a verb silently."""
        await add_device(db, capabilities=["ota"])
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token, apply="auto")
        published = len(publisher.published)

        response = await post(
            admin_app, verb_path(verb, cmd_id if verb in ("apply", "cancel") else None), token
        )

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == (
            f"this board's agent does not implement {verb} (it announces: ota), so it would "
            "ignore the command. Update the agent first."
        )
        assert len(publisher.published) == published
        assert await command_rows(db) == []

    async def test_a_board_announcing_nothing_says_so(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, capabilities=[])
        response = await post(admin_app, "/reboot", await login_admin(admin_app))
        assert response.status_code == 409
        assert "(it announces: nothing)" in response.json()["detail"]


class TestTheTransactionGates:
    async def test_an_unknown_cmd_id_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        response = await post(admin_app, "/deploys/nope/apply", await login_admin(admin_app))
        assert response.status_code == 404
        assert response.json()["detail"] == "no deploy nope on this board"
        assert publisher.published == []
        assert await command_rows(db) == []

    async def test_another_boards_cmd_id_is_404(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        await add_device(db, OTHER_DEVICE_ID, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token)

        response = await post(
            admin_app, f"/deploys/{cmd_id}/cancel", token, device_id=OTHER_DEVICE_ID
        )

        assert response.status_code == 404
        assert len(publisher.published) == 1

    @pytest.mark.parametrize(
        "state", [DeployState.CONFIRMED, DeployState.FAILED, DeployState.CANCELLED]
    )
    @pytest.mark.parametrize("verb", ["apply", "cancel"])
    async def test_an_ended_transaction_is_409_naming_its_state(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        state: DeployState,
        verb: str,
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token)
        db.add(
            DeployEvent(
                device_id=DEVICE_ID,
                cmd_id=cmd_id,
                state=state.value,
                is_terminal=state in TERMINAL_DEPLOY_STATES,
            )
        )
        await db.commit()

        response = await post(admin_app, f"/deploys/{cmd_id}/{verb}", token)

        assert response.status_code == 409
        assert response.json()["detail"] == (
            f"deploy {cmd_id} already ended ({state.value}); there is nothing to {verb}"
        )
        assert len(publisher.published) == 1

    async def test_apply_on_an_auto_transaction_is_409(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token, apply="auto")

        response = await post(admin_app, f"/deploys/{cmd_id}/apply", token)

        assert response.status_code == 409
        assert response.json()["detail"] == (
            f"deploy {cmd_id} was sent with apply: auto; the board applies it by itself"
        )

    async def test_a_staged_state_is_not_required_server_side(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """The server's view can lag: whether anything is staged is the board's answer."""
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        cmd_id = await staged_deploy(admin_app, db, token)  # only `requested` on record

        response = await post(admin_app, f"/deploys/{cmd_id}/apply", token)

        assert response.status_code == 202

    @pytest.mark.parametrize("bad", ["has.dot", "a" * 64, "sp%20ace"])
    async def test_a_cmd_id_of_the_wrong_shape_is_400(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher, bad: str
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        response = await post(admin_app, f"/deploys/{bad}/cancel", await login_admin(admin_app))
        assert response.status_code == 400, response.text
        assert publisher.published == []


class TestDeviceAndAuth:
    @pytest.mark.parametrize(
        "path", ["/deploys/x/apply", "/deploys/x/cancel", "/rollback", "/reboot"]
    )
    async def test_every_verb_needs_an_admin(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher, path: str
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        response = await post(admin_app, path, None)
        assert response.status_code == 401
        assert publisher.published == []

    async def test_the_command_read_needs_an_admin(self, admin_app: FastAPI) -> None:
        async with client_for(admin_app) as client:
            response = await client.get(f"/v1/devices/{DEVICE_ID}/commands/x")
        assert response.status_code == 401

    async def test_an_unknown_device_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        response = await post(admin_app, "/reboot", await login_admin(admin_app))
        assert response.status_code == 404
        assert response.json()["detail"] == f"no such device: {DEVICE_ID}"

    async def test_a_decommissioned_device_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS, decommissioned_at=now_utc())
        response = await post(admin_app, "/rollback", await login_admin(admin_app))
        assert response.status_code == 404
        assert publisher.published == []

    async def test_an_offline_board_is_still_sent_the_verb(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(
            db,
            capabilities=ALL_VERBS,
            presence_reported=False,
            last_seen=now_utc() - dt.timedelta(days=1),
        )
        response = await post(admin_app, "/reboot", await login_admin(admin_app))
        assert response.status_code == 202
        assert response.json()["device_online"] is False
        assert len(publisher.published) == 1


class TestAPublishFailure:
    async def test_is_503_marks_the_row_and_writes_no_deploy_row(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        publisher.fail_with = CommandPublishError("broker down")

        response = await post(admin_app, "/reboot", token)

        assert response.status_code == 503
        assert response.json()["detail"] == (
            "the broker could not accept the command; try again shortly"
        )
        rows = await command_rows(db)
        assert [(r["type"], r["publish_failed"]) for r in rows] == [("reboot", True)]
        assert await events(db) == []

        record = await get_command(admin_app, token, rows[0]["id"])
        assert record.status_code == 200
        assert record.json()["publish_failed"] is True
        assert record.json()["result"] is None

        async with client_for(admin_app) as client:
            listing = await client.get("/v1/devices", headers={"authorization": f"Bearer {token}"})
        assert listing.json()["devices"][0]["last_command"] is None


class TestTheCommandRead:
    async def test_shows_the_first_answer_once_recorded(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        await add_device(db, OTHER_DEVICE_ID, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        command_id = (await post(admin_app, "/reboot", token)).json()["command_id"]

        pending = await get_command(admin_app, token, command_id)
        assert pending.status_code == 200
        assert pending.json()["result"] is None
        assert pending.json()["answered_at"] is None

        at = now_utc()
        await device_commands.record_answer(
            db,
            device_id=DEVICE_ID,
            command_id=command_id,
            type="reboot",
            result="refused",
            cmd_id=None,
            detail="an update is in progress",
            at=at,
        )
        await db.commit()

        record = await get_command(admin_app, token, command_id)
        body = record.json()
        assert list(body) == [
            "command_id",
            "type",
            "cmd_id",
            "sent_at",
            "sent_by",
            "result",
            "detail",
            "answered_at",
            "device_id",
            "publish_failed",
        ]
        assert (body["result"], body["detail"], body["publish_failed"]) == (
            "refused",
            "an update is in progress",
            False,
        )
        assert dt.datetime.fromisoformat(body["answered_at"]) == at
        assert set(body["sent_by"]) == {"subject", "credential"}  # never the token id

        other = await get_command(admin_app, token, command_id, device_id=OTHER_DEVICE_ID)
        assert other.status_code == 404
        unknown = await get_command(admin_app, token, "nope")
        assert unknown.status_code == 404

    async def test_last_command_is_the_newest_with_its_answer(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, capabilities=ALL_VERBS)
        await add_device(db, OTHER_DEVICE_ID, capabilities=ALL_VERBS)
        token = await login_admin(admin_app)
        await post(admin_app, "/reboot", token)
        newest = (await post(admin_app, "/rollback", token)).json()["command_id"]
        await device_commands.record_answer(
            db,
            device_id=DEVICE_ID,
            command_id=newest,
            type="rollback",
            result="accepted",
            cmd_id=newest,
            detail=None,
            at=now_utc(),
        )
        await db.commit()

        async with client_for(admin_app) as client:
            listing = await client.get("/v1/devices", headers={"authorization": f"Bearer {token}"})
        by_id = {row["device_id"]: row for row in listing.json()["devices"]}
        last = by_id[DEVICE_ID]["last_command"]
        assert (last["command_id"], last["type"], last["result"], last["cmd_id"]) == (
            newest,
            "rollback",
            "accepted",
            None,
        )
        assert by_id[OTHER_DEVICE_ID]["last_command"] is None
        assert list(by_id[DEVICE_ID])[-2:] == ["last_command", "deploy"]
