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
import inspect
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
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

DEVICE_ID = "a4cf12b3de90"
OTHER_DEVICE_ID = "a4cf12b3de91"
IMAGE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the server" * 37
OTHER_IMAGE = IMAGE + b"!"
SHA256 = hashlib.sha256(IMAGE).hexdigest()
OTHER_SHA256 = hashlib.sha256(OTHER_IMAGE).hexdigest()
VERSION = "1.5.0"
TARGET = "esp32"
LAYOUT = "ab-4m-v1"
# spec/device-protocol.md → Partition layouts, retyped (R2b-be-7).
AB_SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
ARDUINO_SHA = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"
# Read from the defaults rather than retyped: the URL's lifetime is also the reuse
# window, and a literal here would keep passing while the setting drifted.
TTL_S = settings_for_tests().signed_url_ttl_s
CONFIRM_TIMEOUT_S = settings_for_tests().confirm_timeout_s
# R1-be-3: the origin a device is told to fetch from. Ours now, not the store's.
PUBLIC_BASE_URL = settings_for_tests().public_base_url


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


async def precheck(
    app: FastAPI,
    token: str,
    device_id: str = DEVICE_ID,
    **body: Any,
) -> httpx.Response:
    payload: dict[str, Any] = {"version": VERSION}
    payload.update(body)
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            f"/v1/devices/{device_id}/deploy/precheck",
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
        # The URL is minted here and varies by `exp`, so it is checked by shape below
        # and compared out of the payload equality.
        url = payload["artifact"].pop("url")
        # Exactly the spec's keys — an extra one reaches a flash-baked agent.
        assert payload == {
            "id": body["cmd_id"],
            "type": "stage",
            "artifact": {
                "sha256": SHA256,
                "size": len(IMAGE),
                "version": VERSION,
            },
            "apply": "auto",
            "confirm_timeout_s": CONFIRM_TIMEOUT_S,
        }
        # R1-be-3: our origin and our signature, not the store's presigned URL.
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

    async def test_the_url_is_ours_and_verifies_against_our_secret(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R1-be-3: the deploy path mints the link; it does not ask the store for one.

        Verified rather than pattern-matched — a URL the download endpoint would refuse
        is a board that reports `download_failed` an hour after the deploy looked fine.
        """
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

        # The store is never asked to sign anything on the deploy path any more.
        assert store.signed_urls == []
        # And the blob key stays an implementation detail of the download side.
        assert blob_key(SHA256) not in url

    async def test_a_deploy_without_a_url_secret_is_503_and_costs_no_row(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R1-be-3. Replaces "the store cannot sign" — the mint is local now.

        A 202 whose URL no board can redeem is the lie `NullCommandPublisher` refuses to
        tell, so the refusal happens before any row exists.
        """
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
        # Neither the missing setting nor the secret is named to the caller.
        assert "ARTIFACT_URL_SECRET" not in response.text


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
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        with capture_logs() as records:
            await deploy(admin_app, token)

        messages = [record.getMessage() for record in records]
        assert messages, "capture_logs caught nothing — the assertions below would be vacuous"
        # The whole URL, and the signature on its own: R1-be-3 made the URL ours, so a
        # search for the *store's* hostname would now pass while leaking everything.
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


def token_id_of(token: str) -> Any:
    parts = parse_token(token, ADMIN_TOKEN_PREFIX)
    assert parts is not None
    return parts.token_id


class TestRecordsTheSender:
    """R2b-be-4: who sent it is `detail.sent_by` on the `requested` row. No column."""

    async def test_the_requested_row_carries_the_sender(
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
    async def test_an_unreachable_store_no_longer_blocks_a_deploy(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R1-be-3 changed this, deliberately — it used to be a 503. DECISIONS.md.

        The deploy path no longer talks to the object store at all: the URL is minted
        locally and signed upstream only when a board actually asks for the bytes. So a
        store outage is no longer detected here. That is the honest trade: nothing in
        the four-verb seam can check existence cheaply (`get` downloads 1.9 MB), the
        `artifacts` row is already the evidence the bytes were stored, and
        `GET /v1/artifact/{sha}/bin` answers the outage with its own 503 at the moment
        it is true rather than at the moment the operator clicked deploy.
        """
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


class TestPrecheck:
    """R2b-be-2: a dry run that answers what `POST /deploy` would refuse, and sends nothing."""

    async def test_a_clean_board_is_deployable(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await precheck(admin_app, token)

        assert response.status_code == 200
        body = response.json()
        assert body["deployable"] is True
        assert body["refusals"] == []
        assert body["warnings"] == []
        assert body["from_version"] == "1.4.2"
        assert body["sha256"] == SHA256
        assert body["size_bytes"] == len(IMAGE)
        assert body["artifact_partition_layout"] == LAYOUT
        assert body["device_partition_layout"] == LAYOUT
        assert body["confirm_timeout_s"] == CONFIRM_TIMEOUT_S
        assert body["device_online"] is True

    @pytest.mark.parametrize(
        ("overrides", "code", "deploy_status"),
        [
            ({"partition_layout": "single-2m-v1"}, "layout_mismatch", 409),
            ({"ota_slot_size": 100}, "slot_too_small", 409),
            ({"capabilities": ["telemetry"]}, "no_ota_capability", 409),
            ({"platform_type": "esp32c6"}, "no_artifact_for_target", 404),
            ({"partition_table_sha256": ARDUINO_SHA}, "partition_table_mismatch", 409),
        ],
    )
    async def test_every_refusal_matches_the_deploy_word_for_word(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        overrides: dict[str, Any],
        code: str,
        deploy_status: int,
    ) -> None:
        await add_device(db, **overrides)
        await add_artifact(db)
        token = await login_admin(admin_app)

        checked = await precheck(admin_app, token)
        sent = await deploy(admin_app, token)

        assert checked.status_code == 200
        body = checked.json()
        assert body["deployable"] is False
        assert body["refusals"][0]["code"] == code
        assert sent.status_code == deploy_status
        assert sent.json()["detail"] == body["refusals"][0]["message"]

    async def test_all_reasons_arrive_at_once_in_order(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(
            db, partition_layout="single-2m-v1", ota_slot_size=100, capabilities=["telemetry"]
        )
        await add_artifact(db)
        token = await login_admin(admin_app)

        body = (await precheck(admin_app, token)).json()

        assert [r["code"] for r in body["refusals"]] == [
            "layout_mismatch",
            "slot_too_small",
            "no_ota_capability",
        ]

    @pytest.mark.parametrize(
        ("overrides", "codes"),
        [
            ({"presence_reported": False}, ["offline"]),
            ({"presence_reported": None, "last_seen": None}, ["never_connected"]),
            (
                {"power_class": "sleepy", "expected_wake_interval_s": 60, "last_seen": now_utc()},
                ["sleepy"],
            ),
        ],
    )
    async def test_warnings_are_reported_and_do_not_block(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        overrides: dict[str, Any],
        codes: list[str],
    ) -> None:
        await add_device(db, **overrides)
        await add_artifact(db)
        token = await login_admin(admin_app)

        body = (await precheck(admin_app, token)).json()

        assert body["deployable"] is True
        assert [w["code"] for w in body["warnings"]] == codes

    async def test_a_malformed_version_is_400_with_the_deploys_sentence(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db)
        token = await login_admin(admin_app)

        checked = await precheck(admin_app, token, version="../x")
        sent = await deploy(admin_app, token, version="../x")

        assert checked.status_code == 400
        assert checked.json()["detail"] == sent.json()["detail"]

    async def test_an_unknown_device_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        token = await login_admin(admin_app)

        response = await precheck(admin_app, token, device_id="ffffffffffff")

        assert response.status_code == 404
        assert "ffffffffffff" in response.json()["detail"]

    async def test_a_decommissioned_device_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        await add_device(db, decommissioned_at=now_utc())
        await add_artifact(db)
        token = await login_admin(admin_app)

        assert (await precheck(admin_app, token)).status_code == 404

    async def test_it_needs_an_admin(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        async with client_for(admin_app, base_url="https://testserver") as client:
            response = await client.post(
                f"/v1/devices/{DEVICE_ID}/deploy/precheck", json={"version": VERSION}
            )

        assert response.status_code == 401


class TestTheGate:
    """R2b-be-7: `rollback_incapable` gates `/deploy` unless `override` names it.

    Refusals are never overridable; a gated deploy writes no row and publishes nothing.
    """

    async def test_the_precheck_reports_the_gating_warning(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await precheck(admin_app, token)

        assert response.status_code == 200
        body = response.json()
        assert body["deployable"] is True
        assert body["refusals"] == []
        assert [(w["code"], w["needs_override"]) for w in body["warnings"]] == [
            ("rollback_incapable", True)
        ]
        assert "cannot roll back" in body["warnings"][0]["message"]

    async def test_a_deploy_without_the_override_is_409_with_the_warnings_sentence(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        warned = (await precheck(admin_app, token)).json()["warnings"][0]
        sent = await deploy(admin_app, token)

        assert sent.status_code == 409
        assert sent.json()["detail"] == warned["message"]
        assert publisher.published == []
        assert await events(db) == []

    async def test_naming_the_code_opens_the_gate(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        sent = await deploy(admin_app, token, override=["rollback_incapable"])

        assert sent.status_code == 202
        assert len(publisher.published) == 1
        assert [e["state"] for e in await events(db)] == ["requested"]

    @pytest.mark.parametrize("override", [["rollback_incapable"], []])
    async def test_an_override_with_nothing_to_override_is_a_no_op(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        override: list[str],
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)

        sent = await deploy(admin_app, token, override=override)

        assert sent.status_code == 202
        assert len(publisher.published) == 1

    @pytest.mark.parametrize("override", [["force"], "rollback_incapable", ["x"] * 9])
    async def test_an_unknown_code_or_shape_is_422(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        override: Any,
    ) -> None:
        await add_device(db, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        sent = await deploy(admin_app, token, override=override)

        assert sent.status_code == 422
        assert publisher.published == []
        assert await events(db) == []

    async def test_a_refusal_beats_an_override(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, partition_table_sha256=ARDUINO_SHA, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        checked = (await precheck(admin_app, token)).json()
        sent = await deploy(admin_app, token, override=["rollback_incapable"])

        assert [r["code"] for r in checked["refusals"]] == ["partition_table_mismatch"]
        assert [w["code"] for w in checked["warnings"]] == ["rollback_incapable"]
        assert sent.status_code == 409
        assert sent.json()["detail"] == checked["refusals"][0]["message"]
        assert "partition table fingerprint" in sent.json()["detail"]
        assert publisher.published == []
        assert await events(db) == []

    async def test_a_board_that_matches_its_profile_and_can_roll_back_is_clean(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, partition_table_sha256=AB_SHA, rollback_capable=True)
        await add_artifact(db)
        token = await login_admin(admin_app)

        checked = (await precheck(admin_app, token)).json()
        sent = await deploy(admin_app, token)

        assert checked["deployable"] is True
        assert checked["refusals"] == [] and checked["warnings"] == []
        assert sent.status_code == 202

    async def test_the_precheck_ignores_override(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await precheck(admin_app, token, override=["rollback_incapable"])

        assert response.status_code == 200
        assert [w["code"] for w in response.json()["warnings"]] == ["rollback_incapable"]
        assert publisher.published == []

    async def test_plain_warnings_do_not_need_an_override(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db, presence_reported=False, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        warned = (await precheck(admin_app, token)).json()["warnings"]

        assert [(w["code"], w["needs_override"]) for w in warned] == [
            ("offline", False),
            ("rollback_incapable", True),
        ]


class TestPrecheckSendsNothing:
    @pytest.mark.parametrize("overrides", [{}, {"partition_layout": "single-2m-v1"}])
    async def test_nothing_is_minted_recorded_or_published(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        overrides: dict[str, Any],
    ) -> None:
        await add_device(db, **overrides)
        await add_artifact(db)
        token = await login_admin(admin_app)

        response = await precheck(admin_app, token)

        assert response.status_code == 200
        assert publisher.published == []
        assert await events(db) == []
        assert store.signed_urls == []
        assert "http" not in response.text
        assert "sig=" not in response.text

    async def test_it_answers_without_any_url_configuration(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        await add_device(db)
        await add_artifact(db)
        token = await login_admin(admin_app)
        previous = admin_app.dependency_overrides.get(get_settings)
        admin_app.dependency_overrides[get_settings] = lambda: settings_for_tests(
            artifact_url_secret=None, public_base_url=None
        )
        try:
            checked = await precheck(admin_app, token)
            sent = await deploy(admin_app, token)
        finally:
            if previous is None:
                admin_app.dependency_overrides.pop(get_settings, None)
            else:
                admin_app.dependency_overrides[get_settings] = previous

        assert checked.status_code == 200
        assert sent.status_code == 503


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

    @pytest.mark.parametrize(
        "module", ["deploys.py", "api/routers/deploys.py", "deploy_precheck.py"]
    )
    @pytest.mark.parametrize(
        "forbidden", ["asyncio.sleep", "create_task", "BackgroundTasks", "timedelta"]
    )
    def test_the_deploy_path_has_no_timer(self, module: str, forbidden: str) -> None:
        assert forbidden not in self._code(module), (
            f"{module} must not {forbidden}: awaiting_safe_window may last a week"
        )

    def test_only_publish_failed_authors_a_terminal_state(self) -> None:
        """The **server** authors exactly one terminal state, and it is `publish_failed`.

        Three WRITE sites in `deploys.py`, in writer order: `record_requested` (never
        terminal), `record_publish_failure` (the one server-authored terminal state),
        and R1-be-4's `record_observed_status` — which is the **device's** reported
        state, so its flag is derived from `TERMINAL_DEPLOY_STATES` and is never a
        literal `True`. A fourth write, or any literal `True`, means someone taught the
        server to author an outcome for a command the device provably received.

        The fourth entry is R1-fe-1's `latest_deploys`, and it is a READ: it copies the
        flag the row was written with into a `DeploySnapshot` so the dashboard never
        re-derives "terminal" from a hardcoded list of states. `row.is_terminal` authors
        nothing, which is exactly why it is spelled that way and not recomputed here.
        """
        source = self._source("deploys.py")
        assignments = re.findall(r"is_terminal=(.+?),", source)
        assert assignments == [
            "False",
            "DeployState.FAILED in TERMINAL_DEPLOY_STATES",
            "state in TERMINAL_DEPLOY_STATES",
            "row.is_terminal",
        ]

    def test_the_router_never_builds_a_deploy_event_itself(self) -> None:
        """`deploys.py` is the single writer — `test_invariants.py` holds the estate-wide rule."""
        assert "DeployEvent(" not in self._source("api/routers/deploys.py")

    def test_upstream_urls_are_signed_in_exactly_one_place(self) -> None:
        """R1-be-3 moved the call; the invariant it protects is unchanged.

        `CRITICAL.md` → *Artifact signing keys & `signed_url` generation*. The deploy
        router used to hold the only `store.signed_url(` call; now `SignedUrlCache` does,
        and the deploy path has none at all. A second call site anywhere is a second
        place where a URL's lifetime, its caching and its logging can drift apart — and,
        on GCS, a second source of `signBlob` traffic nobody is counting.
        """
        assert self._source("api/routers/deploys.py").count("store.signed_url(") == 0
        assert self._source("storage/urlcache.py").count("store.signed_url(") == 1

    def test_the_deploy_router_mints_exactly_one_url(self) -> None:
        """And the mint has one call site too, for the same reason."""
        assert self._source("api/routers/deploys.py").count("mint_artifact_url(") == 1

    @pytest.mark.parametrize(
        "sentence",
        [
            "An image written into the wrong partition table",
            "The image must fit the slot",
            "did not announce the `ota` capability",
            "The target is this device's chip",
            "partition table fingerprint",
            "cannot roll back",
        ],
    )
    def test_every_refusal_sentence_lives_in_one_place(self, sentence: str) -> None:
        assert sentence in self._source("deploy_precheck.py")
        assert sentence not in self._source("api/routers/deploys.py")

    def test_the_precheck_route_has_no_side_effects(self) -> None:
        from fleetforge.api.routers import deploys

        source = inspect.getsource(deploys.precheck_deploy)
        for forbidden in (
            "_artifact_url",
            "record_requested",
            "latest_open_transaction",
            "publish",
            "commit",
        ):
            assert forbidden not in source
