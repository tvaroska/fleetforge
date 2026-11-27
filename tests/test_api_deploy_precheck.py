"""`POST /v1/devices/{device_id}/deploy/precheck`, gate overrides, and deploy source tripwires (`T-10`)."""

from __future__ import annotations

import ast
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
from fleetforge.clock import now_utc
from fleetforge.db.models import DeployEvent, Device
from fleetforge.storage.blobs import blob_key
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)
from tests.support_deploy import (
    AB_SHA,
    ARDUINO_SHA,
    CONFIRM_TIMEOUT_S,
    DEVICE_ID,
    IMAGE,
    LAYOUT,
    OTHER_DEVICE_ID,
    OTHER_IMAGE,
    OTHER_SHA256,
    RELEASE_PRIV_1,
    SHA256,
    TARGET,
    TEST_RELEASE_PUBLIC_RING,
    VERSION,
    WRONG_SHA,
    add_artifact,
    add_device,
    deploy,
    events,
    precheck,
)

FIXTURES = Path(__file__).parent / "fixtures" / "firmware"
PLAIN_SKETCH = "esp32.nocall.app.bin"
LIBRARY_BUILD = "esp32.basic.app.head.bin"
AGENT_BUNDLE = "esp32.agent.app.head.bin"


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


async def upload_fixture(app: FastAPI, token: str, name: str) -> httpx.Response:
    """The real `POST /v1/artifact`, so the verdict under test is the one the upload stored."""
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            "/v1/artifact",
            params={"target": TARGET, "version": VERSION, "partition_layout": LAYOUT},
            content=(FIXTURES / name).read_bytes(),
            headers={"authorization": f"Bearer {token}"},
        )


class TestPrecheck:
    """R2b-be-2: a dry run that answers what `POST /deploy` would refuse, and sends nothing."""

    async def test_a_clean_board_is_deployable(
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
        assert body["device_partition_profile"] == LAYOUT
        assert body["confirm_timeout_s"] == CONFIRM_TIMEOUT_S
        assert body["device_online"] is True

    @pytest.mark.parametrize(
        ("overrides", "code", "deploy_status"),
        [
            ({"partition_layout": "ab-4m-arduino-v1"}, "layout_mismatch", 409),
            ({"partition_layout": "unknown"}, "unsupported_layout", 409),
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
        _ = (store, publisher)
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
        _ = (store, publisher)
        await add_device(
            db, partition_layout="ab-4m-arduino-v1", ota_slot_size=100, capabilities=["telemetry"]
        )
        await add_artifact(db)
        token = await login_admin(admin_app)

        body = (await precheck(admin_app, token)).json()

        assert [r["code"] for r in body["refusals"]] == [
            "layout_mismatch",
            "slot_too_small",
            "no_ota_capability",
        ]

    async def test_an_unknown_board_is_deployable_only_once_its_map_is_adopted(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """R3-be-2 end to end: detected unknown layout refused until adopted."""
        _ = store
        await add_device(
            db,
            partition_layout="unknown",
            ota_slot_size=1835008,
            partition_table_sha256=WRONG_SHA,
        )
        await db.execute(
            text(
                "INSERT INTO partition_profiles (partition_table_sha256, origin, ota_slot_size) "
                "VALUES (:sha, 'detected', 1835008)"
            ),
            {"sha": WRONG_SHA},
        )
        await db.commit()
        await add_artifact(db, version="be2-a")
        token = await login_admin(admin_app)

        refused = (await precheck(admin_app, token, version="be2-a")).json()
        assert refused["deployable"] is False
        assert [r["code"] for r in refused["refusals"]] == ["unsupported_layout"]
        message = refused["refusals"][0]["message"]
        assert message.startswith("this device's flash map is not a layout this server supports")
        assert "recorded as a detected profile" in message
        assert refused["device_partition_profile"] is None
        sent = await deploy(admin_app, token, version="be2-a")
        assert sent.status_code == 409
        assert sent.json()["detail"] == message
        assert await events(db) == []
        assert publisher.published == []

        async with client_for(admin_app, base_url="https://testserver") as client:
            adopted = await client.patch(
                f"/v1/partition-profiles/{WRONG_SHA}",
                json={"layout_id": "be2-map"},
                headers={"authorization": f"Bearer {token}"},
            )
        assert adopted.status_code == 200, adopted.text
        await add_artifact(db, sha256=OTHER_SHA256, version="be2-b", layout="be2-map")

        ok = (await precheck(admin_app, token, version="be2-b")).json()
        assert ok["deployable"] is True, ok["refusals"]
        assert ok["refusals"] == []
        assert ok["device_partition_profile"] == "be2-map"
        assert ok["device_partition_layout"] == "unknown"
        wrong = (await precheck(admin_app, token, version="be2-a")).json()
        assert [r["code"] for r in wrong["refusals"]] == ["layout_mismatch"]
        assert "runs partition layout be2-map" in wrong["refusals"][0]["message"]

        accepted = await deploy(admin_app, token, version="be2-b")
        assert accepted.status_code == 202, accepted.text
        assert [e["state"] for e in await events(db)] == ["requested"]
        assert publisher.last["artifact"]["sha256"] == OTHER_SHA256

    @pytest.mark.parametrize(
        ("overrides", "codes"),
        [
            ({"presence_reported": False}, ["offline"]),
            ({"presence_reported": None, "last_seen": None}, ["never_connected"]),
            (
                {"power_class": "sleepy", "expected_wake_interval_s": 60, "last_seen": "now"},
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
        _ = (store, publisher)
        resolved: dict[str, Any] = {
            k: (now_utc() if k == "last_seen" and v == "now" else v) for k, v in overrides.items()
        }
        await add_device(db, **resolved)
        await add_artifact(db)
        token = await login_admin(admin_app)

        body = (await precheck(admin_app, token)).json()

        assert body["deployable"] is True
        assert [w["code"] for w in body["warnings"]] == codes

    async def test_a_malformed_version_is_400_with_the_deploys_sentence(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        _ = publisher
        await add_device(db)
        token = await login_admin(admin_app)

        checked = await precheck(admin_app, token, version="../x")
        sent = await deploy(admin_app, token, version="../x")

        assert checked.status_code == 400
        assert checked.json()["detail"] == sent.json()["detail"]

    async def test_an_unknown_device_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        _ = (db, publisher)
        token = await login_admin(admin_app)

        response = await precheck(admin_app, token, device_id="ffffffffffff")

        assert response.status_code == 404
        assert "ffffffffffff" in response.json()["detail"]

    async def test_a_decommissioned_device_is_404(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        _ = publisher
        await add_device(db, decommissioned_at=now_utc())
        await add_artifact(db)
        token = await login_admin(admin_app)

        assert (await precheck(admin_app, token)).status_code == 404

    async def test_it_needs_an_admin(
        self, admin_app: FastAPI, db: AsyncSession, publisher: FakeCommandPublisher
    ) -> None:
        _ = (db, publisher)
        async with client_for(admin_app, base_url="https://testserver") as client:
            response = await client.post(
                f"/v1/devices/{DEVICE_ID}/deploy/precheck", json={"version": VERSION}
            )

        assert response.status_code == 401


class TestTheGate:
    """R2b-be-7: `rollback_incapable` gates `/deploy` unless `override` names it."""

    async def test_the_precheck_reports_the_gating_warning(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
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
        _ = store
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
        _ = store
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
        _ = store
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
        _ = store
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
        _ = store
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
        _ = (store, publisher)
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
        _ = store
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
        _ = (store, publisher)
        await add_device(db, presence_reported=False, rollback_capable=False)
        await add_artifact(db)
        token = await login_admin(admin_app)

        warned = (await precheck(admin_app, token)).json()["warnings"]

        assert [(w["code"], w["needs_override"]) for w in warned] == [
            ("offline", False),
            ("rollback_incapable", True),
        ]


class TestTheLibraryMarkerGate:
    """R3-be-1 acceptance, end to end: upload, pre-check, deploy."""

    async def test_a_plain_sketch_is_gated_until_the_code_is_named(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db)
        token = await login_admin(admin_app)
        uploaded = await upload_fixture(admin_app, token, PLAIN_SKETCH)
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["has_lib_marker"] is False

        checked = await precheck(admin_app, token)
        assert checked.status_code == 200
        body = checked.json()
        assert body["deployable"] is True
        assert body["refusals"] == []
        assert [(w["code"], w["needs_override"]) for w in body["warnings"]] == [
            ("no_library_marker", True)
        ]

        sent = await deploy(admin_app, token)
        assert sent.status_code == 409
        assert sent.json()["detail"] == body["warnings"][0]["message"]
        assert publisher.published == []
        assert await events(db) == []

        sent = await deploy(admin_app, token, override=["no_library_marker"])
        assert sent.status_code == 202, sent.text
        assert len(publisher.published) == 1
        assert [e["state"] for e in await events(db)] == ["requested"]

    @pytest.mark.parametrize("fixture", [LIBRARY_BUILD, AGENT_BUNDLE])
    async def test_a_library_build_and_an_agent_bundle_are_not_gated(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
        fixture: str,
    ) -> None:
        _ = store
        await add_device(db)
        token = await login_admin(admin_app)
        uploaded = await upload_fixture(admin_app, token, fixture)
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["has_lib_marker"] is True

        checked = (await precheck(admin_app, token)).json()
        sent = await deploy(admin_app, token)

        assert checked["refusals"] == [] and checked["warnings"] == []
        assert sent.status_code == 202, sent.text
        assert len(publisher.published) == 1

    async def test_no_verdict_never_warns(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
        await add_device(db)
        await add_artifact(db, has_lib_marker=None)
        token = await login_admin(admin_app)

        checked = (await precheck(admin_app, token)).json()
        sent = await deploy(admin_app, token)

        assert checked["warnings"] == []
        assert sent.status_code == 202

    async def test_a_refusal_beats_the_override(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = store
        await add_device(db, ota_slot_size=100)
        await add_artifact(db, has_lib_marker=False)
        token = await login_admin(admin_app)

        checked = (await precheck(admin_app, token)).json()
        sent = await deploy(admin_app, token, override=["no_library_marker"])

        assert [r["code"] for r in checked["refusals"]] == ["slot_too_small"]
        assert [w["code"] for w in checked["warnings"]] == ["no_library_marker"]
        assert sent.status_code == 409
        assert sent.json()["detail"] == checked["refusals"][0]["message"]
        assert publisher.published == []
        assert await events(db) == []

    async def test_both_gates_need_both_codes(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        _ = (store, publisher)
        await add_device(db, rollback_capable=False)
        await add_artifact(db, has_lib_marker=False)
        token = await login_admin(admin_app)

        warned = (await precheck(admin_app, token)).json()["warnings"]
        half = await deploy(admin_app, token, override=["rollback_incapable"])
        both = await deploy(admin_app, token, override=["rollback_incapable", "no_library_marker"])

        assert [w["code"] for w in warned] == ["rollback_incapable", "no_library_marker"]
        assert half.status_code == 409
        assert half.json()["detail"] == warned[1]["message"]
        assert both.status_code == 202

    async def test_unsigned_artifact_needs_an_override(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        """D-1 / A-8: unsigned image triggers `unsigned_artifact` (409 unless overridden),
        and is hard-refused (`signature_required_by_device`) on a board announcing `sig`."""
        _ = store
        await add_device(db)
        await add_artifact(db, sig=None)
        token = await login_admin(admin_app)

        checked = (await precheck(admin_app, token)).json()
        assert [w["code"] for w in checked["warnings"]] == ["unsigned_artifact"]
        assert checked["warnings"][0]["needs_override"] is True

        blocked = await deploy(admin_app, token)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == checked["warnings"][0]["message"]
        assert publisher.published == []

        allowed = await deploy(admin_app, token, override=["unsigned_artifact"])
        assert allowed.status_code == 202

        await add_device(db, device_id=OTHER_DEVICE_ID, capabilities=["ota", "sig"])
        enforced = await deploy(
            admin_app, token, device_id=OTHER_DEVICE_ID, override=["unsigned_artifact"]
        )
        assert enforced.status_code == 409
        assert "announces sig" in enforced.json()["detail"]


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
        _ = (store, publisher)
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
    """Source tripwires (`design/architecture.md` principle 5): the device owns the reboot."""

    @staticmethod
    def _source(name: str) -> str:
        return (Path(__file__).resolve().parent.parent / "src" / "fleetforge" / name).read_text()

    @classmethod
    def _code(cls, name: str) -> str:
        """The module's code, with docstrings and comments removed."""
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
        "module",
        [
            "deploys.py",
            "api/routers/deploys.py",
            "deploy_precheck.py",
            "api/routers/commands.py",
            "device_commands.py",
        ],
    )
    @pytest.mark.parametrize(
        "forbidden", ["asyncio.sleep", "create_task", "BackgroundTasks", "timedelta"]
    )
    def test_the_deploy_path_has_no_timer(self, module: str, forbidden: str) -> None:
        assert forbidden not in self._code(module), (
            f"{module} must not {forbidden}: awaiting_safe_window may last a week"
        )

    def test_only_publish_failed_authors_a_terminal_state(self) -> None:
        """The server authors exactly one terminal state, and it is `publish_failed`."""
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
        """`SignedUrlCache` holds the sole `store.signed_url(` call."""
        assert self._source("api/routers/deploys.py").count("store.signed_url(") == 0
        assert self._source("storage/urlcache.py").count("store.signed_url(") == 1

    def test_the_deploy_router_mints_exactly_one_url(self) -> None:
        """And the mint has one call site (`deploy_urls.py`), never duplicated in routers."""
        assert self._source("deploy_urls.py").count("mint_artifact_url(") == 1
        assert self._source("api/routers/deploys.py").count("mint_artifact_url(") == 0

    @pytest.mark.parametrize(
        "sentence",
        [
            "An image written into the wrong partition table",
            "The image must fit the slot",
            "did not announce the `ota` capability",
            "The target is this device's chip",
            "partition table fingerprint",
            "not a layout this server supports",
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
