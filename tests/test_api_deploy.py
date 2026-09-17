"""`POST /v1/devices/{device_id}/deploy` — the orchestration endpoint. R1-be-2.

`spec/flows.md` Flow 2, with the broker and the object store replaced by the fakes in
`conftest.py`. The properties worth holding, one class each:

* **The command on the wire is the spec's, and the signed URL lives only there.** The
  URL is a bearer credential: it must not appear in `deploy_events.detail`, in a log
  record, or in the response body. Those three assertions are the point of this suite.
* **Record the intent, then act.** A `requested` row exists before the publish, and a
  publish that fails closes the transaction with the one terminal state the server may
  author — so a broker outage is never reported as a deploy still in flight.
* **A retry is the same transaction.** Same device, same bytes, inside the signed-URL
  window: same `cmd_id`, `reused: true`, and **no second row** — the board deduplicates
  on the id and downloads once.
* **A board that cannot take the image is refused before anything is signed.** Wrong
  chip is a 404 (the label exists, just not for this target); layout, slot size and the
  missing `ota` capability are 409s, all of them naming the value that did not match.
* **`device_online` is reported, never enforced.** The QoS-1 command waits in the
  device's persistent session; refusing to deploy to a sleeping board would be wrong.
"""

import ast
import hashlib
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import get_command_publisher, get_object_store
from fleetforge.broker import CommandPublishError
from fleetforge.clock import now_utc
from fleetforge.db.models import TERMINAL_DEPLOY_STATES, DeployEvent, DeployState, Device
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    capture_logs,
    client_for,
    login_admin,
    settings_for_tests,
)

DEVICE_ID = "a4cf12b3de90"
OTHER_DEVICE_ID = "a4cf12b3de91"
IMAGE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the server" * 37
OTHER_IMAGE = IMAGE + b"!"
SHA256 = hashlib.sha256(IMAGE).hexdigest()
OTHER_SHA256 = hashlib.sha256(OTHER_IMAGE).hexdigest()
VERSION = "1.5.0"
TARGET = "esp32"
LAYOUT = "ab-4m-v1"
# Read from the defaults rather than retyped: the URL's lifetime is also the reuse
# window, and a literal here would keep passing while the setting drifted.
TTL_S = settings_for_tests().signed_url_ttl_s
CONFIRM_TIMEOUT_S = settings_for_tests().confirm_timeout_s


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    store = MemoryObjectStore()
    store.objects[blob_key(SHA256)] = IMAGE
    store.objects[blob_key(OTHER_SHA256)] = OTHER_IMAGE
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
    """Committed writes, cleaned up afterwards — the endpoint opens its own session.

    `deploy_events` first: its FK to `devices` is `ON DELETE RESTRICT`, which is the
    whole point of it.
    """
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


async def add_device(session: AsyncSession, device_id: str = DEVICE_ID, **overrides: Any) -> Device:
    values: dict[str, Any] = {
        "device_id": device_id,
        "platform_type": TARGET,
        "link_type": "wifi",
        "power_class": "always_on",
        "fw_version": "1.4.2",
        "partition_layout": LAYOUT,
        "ota_slot_size": 1966080,
        "capabilities": ["ota"],
        "presence_reported": True,
        "last_seen": now_utc(),
    }
    values.update(overrides)
    device = Device(**values)
    session.add(device)
    await session.commit()
    return device


async def add_artifact(
    session: AsyncSession,
    *,
    sha256: str = SHA256,
    size_bytes: int = len(IMAGE),
    target: str = TARGET,
    version: str = VERSION,
    layout: str | None = LAYOUT,
) -> None:
    """Insert the artifact and its label directly — `test_api_artifact_upload.py` owns POST."""
    await session.execute(
        text(
            "INSERT INTO artifacts (sha256, size_bytes, kind, target, partition_layout) "
            "VALUES (:sha256, :size_bytes, 'user_firmware', :target, :layout) "
            "ON CONFLICT (sha256) DO NOTHING"
        ),
        {"sha256": sha256, "size_bytes": size_bytes, "target": target, "layout": layout},
    )
    await session.execute(
        text(
            "INSERT INTO artifact_versions (target, version, sha256) "
            "VALUES (:target, :version, :sha256)"
        ),
        {"target": target, "version": version, "sha256": sha256},
    )
    await session.commit()


async def deploy(
    app: FastAPI,
    token: str,
    device_id: str = DEVICE_ID,
    **body: Any,
) -> httpx.Response:
    payload: dict[str, Any] = {"version": VERSION}
    payload.update(body)
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            f"/v1/devices/{device_id}/deploy",
            json=payload,
            headers={"authorization": f"Bearer {token}"},
        )


async def events(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT cmd_id, state, is_terminal, from_version, artifact_version, detail "
                "FROM deploy_events ORDER BY id"
            )
        )
    ).all()
    return [dict(row._mapping) for row in rows]  # noqa: SLF001 - the documented Row accessor


class TestAuth:
    async def test_unauthenticated_is_401_and_publishes_nothing(
        self, admin_app: FastAPI, db: AsyncSession, store: MemoryObjectStore, publisher: Any
    ) -> None:
        await add_device(db)
        await add_artifact(db)

        async with client_for(admin_app) as client:
            response = await client.post(
                f"/v1/devices/{DEVICE_ID}/deploy", json={"version": VERSION}
            )

        assert response.status_code == 401
        assert publisher.published == []


class TestTheCommandOnTheWire:
    async def test_202_publishes_the_spec_payload_to_the_devices_topic(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["device_id"] == DEVICE_ID
        assert body["version"] == VERSION
        assert body["sha256"] == SHA256
        assert body["size_bytes"] == len(IMAGE)
        assert body["apply"] == "auto"
        assert body["reused"] is False
        assert body["device_online"] is True

        (device_id, payload) = publisher.published[0]
        assert device_id == DEVICE_ID
        # Exactly the spec's keys — an extra one reaches a flash-baked agent.
        assert payload == {
            "id": body["cmd_id"],
            "type": "stage",
            "artifact": {
                "url": f"https://memory.invalid/{blob_key(SHA256)}?ttl={TTL_S}",
                "sha256": SHA256,
                "size": len(IMAGE),
                "version": VERSION,
            },
            "apply": "auto",
            "confirm_timeout_s": CONFIRM_TIMEOUT_S,
        }

    async def test_apply_on_command_is_passed_through(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, apply="on_command")

        assert response.status_code == 202, response.text
        assert publisher.last["apply"] == "on_command"

    async def test_an_unknown_apply_mode_is_422(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, apply="whenever")

        assert response.status_code == 422
        assert publisher.published == []

    async def test_the_url_is_signed_once_from_the_blob_key(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """One signing call site, and the key is a function of the bytes."""
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        await deploy(admin_app, token)

        assert blob_key(SHA256) in publisher.last["artifact"]["url"]


class TestTheUrlIsACredential:
    async def test_it_is_not_in_the_response_body(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert "http" not in response.text
        assert "url" not in response.json()

    async def test_it_is_not_in_the_recorded_event(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        await deploy(admin_app, token)

        [row] = await events(db)
        assert "http" not in str(row["detail"])
        assert row["detail"] == {
            "sha256": SHA256,
            "size_bytes": len(IMAGE),
            "target": TARGET,
            "apply": "auto",
        }

    async def test_it_is_not_in_the_logs(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        with capture_logs() as records:
            await deploy(admin_app, token)

        messages = [record.getMessage() for record in records]
        assert messages, "capture_logs caught nothing — the assertion below would be vacuous"
        assert not any("memory.invalid" in message for message in messages)


class TestRecordsTheIntent:
    async def test_a_requested_row_is_written_and_is_not_terminal(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        [row] = await events(db)
        assert row["state"] == DeployState.REQUESTED.value
        assert row["is_terminal"] is False
        assert row["cmd_id"] == response.json()["cmd_id"]
        assert row["from_version"] == "1.4.2"
        assert row["artifact_version"] == VERSION

    async def test_requested_is_never_a_terminal_state(self) -> None:
        """A `requested` row must not close a transaction — both KPIs read the terminal one."""
        assert DeployState.REQUESTED not in TERMINAL_DEPLOY_STATES


class TestARetryIsTheSameTransaction:
    async def test_the_same_artifact_reuses_the_cmd_id_and_writes_no_second_row(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        first = await deploy(admin_app, token)
        second = await deploy(admin_app, token)

        assert second.status_code == 202, second.text
        assert second.json()["cmd_id"] == first.json()["cmd_id"]
        assert first.json()["reused"] is False
        assert second.json()["reused"] is True
        assert len(await events(db)) == 1, "a retry is the same intent, not a second one"
        # Republished, though: the first URL may never have arrived.
        assert len(publisher.published) == 2

    async def test_a_different_artifact_is_a_new_transaction(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        await add_artifact(db, sha256=OTHER_SHA256, size_bytes=len(OTHER_IMAGE), version="1.6.0")
        token = await login_admin(admin_app)

        first = await deploy(admin_app, token)
        second = await deploy(admin_app, token, version="1.6.0")

        assert second.json()["reused"] is False
        assert second.json()["cmd_id"] != first.json()["cmd_id"]
        assert len(await events(db)) == 2

    async def test_a_closed_transaction_is_not_reused(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """Once the device reports a terminal state, the next POST is a fresh deploy."""
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        first = await deploy(admin_app, token)

        db.add(
            DeployEvent(
                device_id=DEVICE_ID,
                cmd_id=first.json()["cmd_id"],
                state=DeployState.CONFIRMED.value,
                is_terminal=True,
                artifact_version=VERSION,
            )
        )
        await db.commit()

        second = await deploy(admin_app, token)
        assert second.json()["reused"] is False
        assert second.json()["cmd_id"] != first.json()["cmd_id"]

    async def test_the_window_is_the_signed_url_lifetime(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """Past the TTL the first URL has expired, so the old id would be a lie."""
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        first = await deploy(admin_app, token)

        await db.execute(
            text("UPDATE deploy_events SET at = now() - make_interval(secs => 100000)")
        )
        await db.commit()

        second = await deploy(admin_app, token)
        assert second.json()["reused"] is False
        assert second.json()["cmd_id"] != first.json()["cmd_id"]

    async def test_another_devices_open_transaction_is_not_reused(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_device(db, OTHER_DEVICE_ID)
        await add_artifact(db)
        token = await login_admin(admin_app)

        first = await deploy(admin_app, token)
        second = await deploy(admin_app, token, device_id=OTHER_DEVICE_ID)

        assert second.json()["cmd_id"] != first.json()["cmd_id"]
        assert second.json()["reused"] is False


class TestRefusals:
    async def test_a_malformed_version_is_400_before_anything_else(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, version="../../etc/passwd")

        assert response.status_code == 400
        assert publisher.published == []

    async def test_an_unknown_device_is_404(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, device_id="ffffffffffff")

        assert response.status_code == 404
        assert publisher.published == []

    async def test_a_decommissioned_device_is_404(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, decommissioned_at=now_utc())
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 404
        assert publisher.published == []

    async def test_an_artifact_built_for_another_chip_is_404(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """The label exists — just not for this board's chip. `target` is never a request field."""
        await add_device(db, platform_type="esp32c6")
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 404
        assert "esp32c6" in response.json()["detail"]
        assert publisher.published == []

    async def test_a_partition_layout_mismatch_is_409_naming_both(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, partition_layout="single-2m-v1")
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "single-2m-v1" in detail and LAYOUT in detail
        assert publisher.published == []

    async def test_an_image_larger_than_the_ota_slot_is_409(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, ota_slot_size=100)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 409
        assert "100" in response.json()["detail"]
        assert publisher.published == []

    async def test_a_device_without_the_ota_capability_is_409(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, capabilities=["telemetry"])
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 409
        assert "telemetry" in response.json()["detail"]
        assert publisher.published == []

    async def test_an_r0_board_that_announced_neither_is_not_refused(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """Unknown layout and unknown slot size are not a mismatch — only a *conflict* is."""
        await add_device(db, partition_layout=None, ota_slot_size=None)
        await add_artifact(db)
        token = await login_admin(admin_app)

        assert (await deploy(admin_app, token)).status_code == 202

    async def test_a_refusal_writes_no_deploy_event(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, capabilities=[])
        await add_artifact(db)
        token = await login_admin(admin_app)

        await deploy(admin_app, token)

        assert await events(db) == [], "a refused deploy never happened"


class TestFailuresDownstream:
    async def test_a_store_that_cannot_sign_is_503_and_costs_no_row(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        store.fail_with = ObjectStoreError("the bucket is on fire")

        response = await deploy(admin_app, token)

        assert response.status_code == 503
        assert await events(db) == []
        assert publisher.published == []
        # Never the bucket, the key or the exception text: `detail` is banner text.
        assert "bucket" not in response.json()["detail"]

    async def test_a_broker_that_refuses_is_503_and_closes_the_transaction(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        publisher.fail_with = CommandPublishError("no broker")

        response = await deploy(admin_app, token)

        assert response.status_code == 503
        requested, failed = await events(db)
        assert requested["state"] == DeployState.REQUESTED.value
        assert failed["state"] == DeployState.FAILED.value
        assert failed["is_terminal"] is True
        assert failed["cmd_id"] == requested["cmd_id"]
        assert failed["detail"] == {"reason": "publish_failed"}

    async def test_a_publish_failure_is_the_one_terminal_state_the_server_authors(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """And it counts as a *loss* for fleet safety, which is why nothing else may use it."""
        assert DeployState.FAILED in TERMINAL_DEPLOY_STATES


class TestPresenceIsReportedNotEnforced:
    @pytest.mark.parametrize("presence_reported", [True, False])
    async def test_an_offline_board_is_still_deployed_to(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        presence_reported: bool,
    ) -> None:
        """The QoS-1 command waits in the device's persistent session."""
        await add_device(db, presence_reported=presence_reported)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 202
        assert response.json()["device_online"] is presence_reported
        assert len(publisher.published) == 1


class TestNoSchedulerLivesHere:
    """Source tripwires. `design/architecture.md` principle 5: the device owns the reboot.

    A sleeper, a background task or a second writer of terminal states would each turn
    "the board is still deciding" into "the server gave up", silently and only in
    production. These are cheap to keep and expensive to rediscover.
    """

    @staticmethod
    def _source(name: str) -> str:
        return (Path(__file__).resolve().parent.parent / "src" / "fleetforge" / name).read_text()

    @classmethod
    def _code(cls, name: str) -> str:
        """The module's **code**, with docstrings and comments removed.

        Both files explain at length that they contain no sleeper; a substring search
        over the raw text would therefore fail on the prose that documents the rule.
        `ast.unparse` drops comments, and the loop below drops the docstrings.
        """
        tree = ast.parse(cls._source(name))
        for node in ast.walk(tree):
            if not isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    @pytest.mark.parametrize("module", ["deploys.py", "api/routers/deploys.py"])
    @pytest.mark.parametrize(
        "forbidden", ["asyncio.sleep", "create_task", "BackgroundTasks", "timedelta"]
    )
    def test_the_deploy_path_has_no_timer(self, module: str, forbidden: str) -> None:
        assert forbidden not in self._code(module), (
            f"{module} must not {forbidden}: awaiting_safe_window may last a week"
        )

    def test_only_publish_failed_authors_a_terminal_state(self) -> None:
        """One `is_terminal` in `deploys.py`, in `record_publish_failure`."""
        source = self._source("deploys.py")
        assignments = re.findall(r"is_terminal=(.+?),", source)
        assert assignments == ["False", "DeployState.FAILED in TERMINAL_DEPLOY_STATES"]

    def test_the_router_never_builds_a_deploy_event_itself(self) -> None:
        """`deploys.py` is the single writer — `test_invariants.py` holds the estate-wide rule."""
        assert "DeployEvent(" not in self._source("api/routers/deploys.py")

    def test_the_router_signs_exactly_one_url(self) -> None:
        assert self._source("api/routers/deploys.py").count("store.signed_url(") == 1
