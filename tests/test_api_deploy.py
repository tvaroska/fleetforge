"""`POST /v1/devices/{device_id}/deploy` — the orchestration endpoint (`R1-be-2`, `T-10`).

`spec/flows.md` Flow 2, with the broker and the object store replaced by the fakes in
`conftest.py`. Precheck, gate overrides, library-marker checks, and no-scheduler source
tripwires live in `tests/test_api_deploy_precheck.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import get_command_publisher, get_object_store, get_settings
from fleetforge.artifact_urls import verify_artifact_url
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, parse_token
from fleetforge.broker import CommandPublishError
from fleetforge.clock import now_utc
from fleetforge.db.models import TERMINAL_DEPLOY_STATES, DeployEvent, DeployState, Device
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from tests.conftest import (
    TEST_ARTIFACT_URL_SECRET,
    FakeCommandPublisher,
    MemoryObjectStore,
    capture_logs,
    client_for,
    login_admin,
    settings_for_tests,
)
from tests.support_deploy import (
    CONFIRM_TIMEOUT_S,
    DEVICE_ID,
    IMAGE,
    LAYOUT,
    OTHER_DEVICE_ID,
    OTHER_IMAGE,
    OTHER_SHA256,
    PUBLIC_BASE_URL,
    RELEASE_PRIV_1,
    SHA256,
    TARGET,
    TEST_RELEASE_PUBLIC_RING,
    TEST_SIG,
    TTL_S,
    VERSION,
    add_artifact,
    add_device,
    deploy,
    events,
    precheck,
)


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    store = MemoryObjectStore()
    store.objects[blob_key(SHA256)] = IMAGE
    store.objects[blob_key(OTHER_SHA256)] = OTHER_IMAGE
    release_settings = settings_for_tests(
        artifact_signing_key=RELEASE_PRIV_1,
        artifact_signing_public_keys=TEST_RELEASE_PUBLIC_RING,
    )
    admin_app.dependency_overrides[get_settings] = lambda: release_settings
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
    """Committed writes, cleaned up afterwards — the endpoint opens its own session."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(DeployEvent))
            await session.execute(delete(Device))
            await session.execute(text("DELETE FROM artifact_versions"))
            await session.execute(text("DELETE FROM artifacts"))
            await session.execute(text("DELETE FROM partition_profiles WHERE origin <> 'builtin'"))
            await session.commit()


def token_id_of(token: str) -> Any:
    parts = parse_token(token, ADMIN_TOKEN_PREFIX)
    assert parts is not None
    return parts.token_id


class TestAuth:
    async def test_unauthenticated_is_401_and_publishes_nothing(
        self, admin_app: FastAPI, db: AsyncSession, store: MemoryObjectStore, publisher: Any
    ) -> None:
        _ = store
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
        _ = store
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
        url = payload["artifact"].pop("url")
        assert payload == {
            "id": body["cmd_id"],
            "type": "stage",
            "artifact": {
                "sha256": SHA256,
                "size": len(IMAGE),
                "version": VERSION,
                "sig": TEST_SIG,
            },
            "apply": "auto",
            "confirm_timeout_s": CONFIRM_TIMEOUT_S,
        }
        assert url.startswith(f"{PUBLIC_BASE_URL}/v1/artifact/{SHA256}/bin?")
        assert "exp=" in url
        assert "sig=" in url

    async def test_apply_on_command_is_passed_through(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db, capabilities=["ota", "apply"])
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, apply="on_command")

        assert response.status_code == 202, response.text
        assert publisher.last["apply"] == "on_command"

    async def test_on_command_to_a_board_without_apply_is_409_and_sends_nothing(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R5-be-2 / R5-spec-1 D9: a pre-R5 agent ignores `apply`; refused before mint/write."""
        _ = store
        await add_device(db, capabilities=["ota"])
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, apply="on_command")

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == (
            "this board's agent cannot be told to apply (it announces: ota), so an on_command "
            "deploy would wait at staged until the board restarts. Deploy with apply: auto, or "
            "update the agent first."
        )
        assert publisher.published == []
        assert await events(db) == []

    async def test_auto_to_a_board_without_apply_is_unaffected(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db, capabilities=["ota"])
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, apply="auto")

        assert response.status_code == 202, response.text
        assert publisher.last["apply"] == "auto"

    async def test_the_precheck_lists_no_apply_capability(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db, capabilities=["ota"])
        await add_artifact(db)
        token = await login_admin(admin_app)

        on_command = await precheck(admin_app, token, apply="on_command")
        auto = await precheck(admin_app, token, apply="auto")

        assert on_command.status_code == 200, on_command.text
        assert on_command.json()["deployable"] is False
        assert [f["code"] for f in on_command.json()["refusals"]] == ["no_apply_capability"]
        assert auto.json()["deployable"] is True
        assert auto.json()["refusals"] == []
        assert publisher.published == []

    async def test_an_unknown_apply_mode_is_422(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token, apply="whenever")

        assert response.status_code == 422
        assert publisher.published == []

    async def test_the_url_is_ours_and_verifies_against_our_secret(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R1-be-3: the deploy path mints the link; it does not ask the store for one."""
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        await deploy(admin_app, token)

        url = publisher.last["artifact"]["url"]
        assert url.startswith(f"{PUBLIC_BASE_URL}/v1/artifact/{SHA256}/bin?")
        fields = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
        assert verify_artifact_url(
            SHA256, exp=fields["exp"], sig=fields["sig"], secret=TEST_ARTIFACT_URL_SECRET
        ) == pytest.approx(int(now_utc().timestamp()) + TTL_S, abs=5)
        assert store.signed_urls == []
        assert blob_key(SHA256) not in url

    async def test_a_deploy_without_a_url_secret_is_503_and_costs_no_row(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R1-be-3: missing URL secret refuses before any row is written."""
        _ = store
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        admin_app.dependency_overrides[get_settings] = lambda: settings_for_tests(
            artifact_url_secret=None
        )

        response = await deploy(admin_app, token)

        assert response.status_code == 503
        assert publisher.published == []
        assert await events(db) == [], "a deploy that could not mint a URL never happened"
        assert "ARTIFACT_URL_SECRET" not in response.text


class TestTheUrlIsACredential:
    async def test_it_is_not_in_the_response_body(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
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
        _ = (store, publisher)
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
            "sent_by": {
                "subject": "admin",
                "token_id": str(token_id_of(token)),
                "credential": row["detail"]["sent_by"]["credential"],
            },
        }
        assert row["detail"]["sent_by"]["credential"].startswith("dashboard session (")

    async def test_it_is_not_in_the_logs(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        with capture_logs() as records:
            await deploy(admin_app, token)

        messages = [record.getMessage() for record in records]
        assert messages, "capture_logs caught nothing — the assertions below would be vacuous"
        url = publisher.last["artifact"]["url"]
        sig = url.split("sig=", 1)[1]
        assert not any(url in message or sig in message for message in messages)
        assert not any("/v1/artifact/" in message for message in messages)


class TestRecordsTheIntent:
    async def test_a_requested_row_is_written_and_is_not_terminal(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
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


class TestRecordsTheSender:
    """R2b-be-4: who sent it is `detail.sent_by` on the `requested` row. No column."""

    async def test_the_requested_row_carries_the_sender(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        await deploy(admin_app, token)

        [row] = await events(db)
        sent_by = row["detail"]["sent_by"]
        assert sent_by["subject"] == "admin"
        assert sent_by["token_id"] == str(token_id_of(token))
        assert sent_by["credential"].startswith("dashboard session (")

    async def test_a_reuse_by_a_second_session_keeps_the_first_sender(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
        await add_device(db)
        await add_artifact(db)
        first_token = await login_admin(admin_app)
        second_token = await login_admin(admin_app)
        assert token_id_of(first_token) != token_id_of(second_token)

        await deploy(admin_app, first_token)
        second = await deploy(admin_app, second_token)

        assert second.json()["reused"] is True
        [row] = await events(db)
        assert row["detail"]["sent_by"]["token_id"] == str(token_id_of(first_token))

    async def test_the_publish_failure_detail_is_unchanged(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        publisher.fail_with = CommandPublishError("no broker")

        await deploy(admin_app, token)

        _requested, failed = await events(db)
        assert failed["detail"] == {"reason": "publish_failed"}


class TestARetryIsTheSameTransaction:
    async def test_the_same_artifact_reuses_the_cmd_id_and_writes_no_second_row(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
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
        assert len(publisher.published) == 2

    async def test_a_different_artifact_is_a_new_transaction(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
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
        _ = (store, publisher)
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

    async def test_a_cancelled_transaction_is_not_reused(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R5-be-2 closes R5-fw-4 gap (c): `cancelled` is terminal, so re-deploying mints a new id."""
        _ = (store, publisher)
        await add_device(db, capabilities=["ota", "apply", "cancel"])
        await add_artifact(db)
        token = await login_admin(admin_app)
        first = await deploy(admin_app, token, apply="on_command")

        db.add(
            DeployEvent(
                device_id=DEVICE_ID,
                cmd_id=first.json()["cmd_id"],
                state=DeployState.CANCELLED.value,
                is_terminal=DeployState.CANCELLED in TERMINAL_DEPLOY_STATES,
            )
        )
        await db.commit()

        second = await deploy(admin_app, token, apply="on_command")
        assert second.status_code == 202, second.text
        assert second.json()["reused"] is False
        assert second.json()["cmd_id"] != first.json()["cmd_id"]
        requested = [e for e in await events(db) if e["state"] == "requested"]
        assert [e["cmd_id"] for e in requested] == [first.json()["cmd_id"], second.json()["cmd_id"]]

    async def test_the_window_is_the_signed_url_lifetime(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """Past the TTL the first URL has expired, so the old id would be a lie."""
        _ = (store, publisher)
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
        _ = (store, publisher)
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
        _ = (db, store)
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
        _ = store
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
        _ = store
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
        """The label exists — just not for this board's chip."""
        _ = store
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
        _ = store
        await add_device(db, partition_layout="ab-4m-arduino-v1")
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "ab-4m-arduino-v1" in detail and LAYOUT in detail
        assert publisher.published == []

    async def test_an_image_larger_than_the_ota_slot_is_409(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
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
        _ = store
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
        _ = (store, publisher)
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
        _ = (store, publisher)
        await add_device(db, capabilities=[])
        await add_artifact(db)
        token = await login_admin(admin_app)

        await deploy(admin_app, token)

        assert await events(db) == [], "a refused deploy never happened"


class TestFailuresDownstream:
    async def test_an_unreachable_store_no_longer_blocks_a_deploy(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R1-be-3: the deploy path no longer talks to the four-verb object store Port."""
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        store.fail_with = ObjectStoreError("the bucket is on fire")

        response = await deploy(admin_app, token)

        assert response.status_code == 202
        assert publisher.published, "the command still goes out"
        assert store.signed_urls == []

    async def test_a_broker_that_refuses_is_503_and_closes_the_transaction(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
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
        _ = (admin_app, db, store, publisher)
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
        _ = store
        await add_device(db, presence_reported=presence_reported)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await deploy(admin_app, token)

        assert response.status_code == 202
        assert response.json()["device_online"] is presence_reported
        assert len(publisher.published) == 1
