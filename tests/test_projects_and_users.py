"""End-to-end tests for Projects, Multi-User Auth & Scoped Tokens (Option A: P1-be-1..4)."""

import datetime as dt
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import get_command_publisher, get_firebase_auth, get_object_store
from fleetforge.api.eventstream import EventHub
from fleetforge.api.schemas import UserCreate
from fleetforge.auth.firebase import FirebaseIdentity, FirebaseInvalidCredentials
from fleetforge.config import get_settings
from fleetforge.db.models import (
    DEFAULT_PROJECT_ID,
    AdminToken,
    DeployEvent,
    Device,
    DeviceGroup,
    EnrollmentToken,
    Project,
    ProjectMember,
    User,
)
from fleetforge.events import DeviceEvent, EventType
from tests.conftest import (
    TEST_PASSWORD,
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
)

IMAGE_A = b"\xe9\x06\x02\x20" + b"project-a firmware bytes" * 32
IMAGE_B = b"\xe9\x06\x02\x20" + b"project-b firmware bytes" * 32
SHA_A = hashlib.sha256(IMAGE_A).hexdigest()
SHA_B = hashlib.sha256(IMAGE_B).hexdigest()


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    mem = MemoryObjectStore()
    admin_app.dependency_overrides[get_object_store] = lambda: mem
    yield mem
    admin_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture
def publisher(admin_app: FastAPI) -> Iterator[FakeCommandPublisher]:
    pub = FakeCommandPublisher()
    admin_app.dependency_overrides[get_command_publisher] = lambda: pub
    yield pub
    admin_app.dependency_overrides.pop(get_command_publisher, None)


@pytest.fixture
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Clean up rows created via the app's own sessionmaker after each test."""
    yield
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(delete(DeployEvent))
        await session.execute(delete(EnrollmentToken))
        await session.execute(delete(Device))
        await session.execute(delete(DeviceGroup))
        await session.execute(text("DELETE FROM artifact_versions"))
        await session.execute(text("DELETE FROM artifacts"))
        await session.execute(delete(ProjectMember))
        await session.execute(delete(AdminToken))
        await session.execute(delete(User))
        await session.execute(delete(Project).where(Project.id != DEFAULT_PROJECT_ID))
        await session.commit()


async def test_multi_user_login_and_disabled_user_rejection(
    admin_app: FastAPI,
    clean_tables: None,
) -> None:
    """P1-be-1: Superadmin creates a user, user logs in, and disabling immediately 401s."""
    admin_token = await login_admin(admin_app)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        # Create user 'alice'
        r = await client.post(
            "/v1/users",
            headers=headers,
            json={
                "username": "alice",
                "display_name": "Alice Smith",
                "password": "alice-secret-password",
                "is_superadmin": False,
            },
        )
        assert r.status_code == 201, r.text
        alice_id = r.json()["id"]

        # Grant alice viewer on 'default'
        r = await client.post(
            "/v1/projects/default/members",
            headers=headers,
            json={"username": "alice", "role": "viewer"},
        )
        assert r.status_code == 200, r.text

        # Alice logs in with her username and password
        r = await client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": "alice-secret-password"},
        )
        assert r.status_code == 200, r.text
        alice_cookie = r.cookies["ff_session"]
        alice_headers = {"Authorization": f"Bearer {alice_cookie}"}

        # /v1/auth/me reflects alice's identity and project membership
        me = (await client.get("/v1/auth/me", headers=alice_headers)).json()
        assert me["subject"] == "alice"
        assert me["is_superadmin"] is False
        assert [p["slug"] for p in me["projects"]] == ["default"]
        assert me["projects"][0]["role"] == "viewer"

        # Non-superadmin cannot list or create users
        assert (await client.get("/v1/users", headers=alice_headers)).status_code == 403

        # Superadmin disables alice -> alice's existing token immediately 401s
        r = await client.patch(
            "/v1/users/alice",
            headers=headers,
            json={"disabled": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["id"] == alice_id
        assert r.json()["disabled_at"] is not None

        assert (await client.get("/v1/auth/me", headers=alice_headers)).status_code == 401


async def test_project_isolation_for_artifacts_enrollment_devices_and_deploys(
    admin_app: FastAPI,
    store: MemoryObjectStore,
    publisher: FakeCommandPublisher,
    clean_tables: None,
) -> None:
    """P1-be-2 & P1-be-3: Projects isolate artifacts, enrollment tokens, devices, and deploys."""
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        # Create project 'robotics'
        r = await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "robotics", "name": "Robotics Fleet", "description": "Lab bots"},
        )
        assert r.status_code == 201, r.text

        # Upload 1.5.0 to 'default' (IMAGE_A) and 1.5.0 to 'robotics' (IMAGE_B)
        r_a = await client.post(
            "/v1/artifact?target=esp32&version=1.5.0&partition_layout=ab-4m-v1",
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=IMAGE_A,
        )
        assert r_a.status_code == 201, r_a.text
        assert r_a.json()["sha256"] == SHA_A

        r_b = await client.post(
            "/v1/artifact?target=esp32&version=1.5.0&partition_layout=ab-4m-v1&project=robotics",
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=IMAGE_B,
        )
        assert r_b.status_code == 201, r_b.text
        assert r_b.json()["sha256"] == SHA_B

        # Listing artifacts is isolated per project
        list_def = (await client.get("/v1/artifact", headers=headers)).json()["artifacts"]
        list_rob = (await client.get("/v1/artifact?project=robotics", headers=headers)).json()[
            "artifacts"
        ]
        assert [a["sha256"] for a in list_def] == [SHA_A]
        assert [a["sha256"] for a in list_rob] == [SHA_B]

        # Issue enrollment token in 'robotics' and enroll a board
        tok_rob = (
            await client.post(
                "/v1/enrollment-tokens?project=robotics",
                headers=headers,
                json={"group_id": None},
            )
        ).json()["token"]

        enroll_res = await client.post(
            "/v1/enroll",
            json={
                "token": tok_rob,
                "device_id": "a4cf12b3de90",
                "platform_type": "esp32",
                "proto": 1,
                "fw_version": "1.0.0",
                "agent_version": "0.5.0",
                "link_type": "wifi",
                "power_class": "always_on",
                "partition_layout": "ab-4m-v1",
                "ota_slot_size": 1966080,
                "rollback_capable": True,
                "capabilities": ["ota"],
            },
        )
        assert enroll_res.status_code == 200, enroll_res.text

        # Device appears in 'robotics' and NOT in 'default'
        devs_def = (await client.get("/v1/devices", headers=headers)).json()["devices"]
        devs_rob = (await client.get("/v1/devices?project=robotics", headers=headers)).json()[
            "devices"
        ]
        assert devs_def == []
        assert [d["device_id"] for d in devs_rob] == ["a4cf12b3de90"]

        # Cross-project re-enrollment while live in 'robotics' is refused with 409 Conflict
        tok_def = (
            await client.post(
                "/v1/enrollment-tokens",
                headers=headers,
                json={"group_id": None},
            )
        ).json()["token"]
        cross = await client.post(
            "/v1/enroll",
            json={
                "token": tok_def,
                "device_id": "a4cf12b3de90",
                "platform_type": "esp32",
                "proto": 1,
                "fw_version": "1.0.0",
                "agent_version": "0.5.0",
                "link_type": "wifi",
                "power_class": "always_on",
                "partition_layout": "ab-4m-v1",
                "ota_slot_size": 1966080,
                "rollback_capable": True,
                "capabilities": ["ota"],
            },
        )
        assert cross.status_code == 409, cross.text
        assert "another project" in cross.json()["detail"]

        # Deploying version 1.5.0 to a4cf12b3de90 resolves robotics' SHA_B, not default's SHA_A
        dep = await client.post(
            "/v1/devices/a4cf12b3de90/deploy",
            headers=headers,
            json={"version": "1.5.0", "apply": "auto", "override": ["no_library_marker"]},
        )
        assert dep.status_code == 202, dep.text
        assert dep.json()["sha256"] == SHA_B
        assert publisher.last["sha256"] == SHA_B

        # Transferring device from 'robotics' to 'default' via PATCH /v1/devices/{id}
        xfer = await client.patch(
            "/v1/devices/a4cf12b3de90",
            headers=headers,
            json={"project": "default"},
        )
        assert xfer.status_code == 200, xfer.text
        devs_def_after = (await client.get("/v1/devices", headers=headers)).json()["devices"]
        devs_rob_after = (await client.get("/v1/devices?project=robotics", headers=headers)).json()[
            "devices"
        ]
        assert [d["device_id"] for d in devs_def_after] == ["a4cf12b3de90"]
        assert devs_rob_after == []


async def test_rbac_roles_and_scoped_tokens(
    admin_app: FastAPI,
    store: MemoryObjectStore,
    clean_tables: None,
) -> None:
    """P1-be-1 & P1-be-2: Viewer vs Operator roles and scoped/pinned API tokens."""
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "alpha", "name": "Alpha"},
        )
        await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "beta", "name": "Beta"},
        )
        await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "bob", "password": "bob-password-123", "is_superadmin": False},
        )
        await client.post(
            "/v1/projects/alpha/members",
            headers=headers,
            json={"username": "bob", "role": "viewer"},
        )

        # Bob logs in
        r = await client.post(
            "/v1/auth/login",
            json={"username": "bob", "password": "bob-password-123"},
        )
        bob_headers = {"Authorization": f"Bearer {r.cookies['ff_session']}"}

        # Bob can read 'alpha' devices, cannot read 'beta' devices (403), cannot upload to 'alpha' (403)
        assert (
            await client.get("/v1/devices?project=alpha", headers=bob_headers)
        ).status_code == 200
        assert (
            await client.get("/v1/devices?project=beta", headers=bob_headers)
        ).status_code == 403
        upload_viewer = await client.post(
            "/v1/artifact?target=esp32&version=1.0.0&partition_layout=ab-4m-v1&project=alpha",
            headers={**bob_headers, "Content-Type": "application/octet-stream"},
            content=IMAGE_A,
        )
        assert upload_viewer.status_code == 403

        # Promote Bob to operator on 'alpha' -> upload now succeeds
        await client.post(
            "/v1/projects/alpha/members",
            headers=headers,
            json={"username": "bob", "role": "operator"},
        )
        upload_op = await client.post(
            "/v1/artifact?target=esp32&version=1.0.0&partition_layout=ab-4m-v1&project=alpha",
            headers={**bob_headers, "Content-Type": "application/octet-stream"},
            content=IMAGE_A,
        )
        assert upload_op.status_code == 201

        # Bob mints a read-only token pinned to 'alpha'
        tok_res = await client.post(
            "/v1/auth/tokens",
            headers=bob_headers,
            json={"name": "alpha-reader", "project": "alpha", "scopes": ["read"]},
        )
        assert tok_res.status_code == 201, tok_res.text
        scoped_token = tok_res.json()["token"]
        scoped_id = tok_res.json()["id"]
        scoped_headers = {"Authorization": f"Bearer {scoped_token}"}

        # Pinned token defaults to 'alpha' even without ?project=alpha
        assert (await client.get("/v1/devices", headers=scoped_headers)).status_code == 200
        # Pinned token cannot access another project
        assert (
            await client.get("/v1/devices?project=beta", headers=scoped_headers)
        ).status_code == 403
        # Read-only token cannot upload artifacts (missing write:artifacts scope)
        upload_ro = await client.post(
            "/v1/artifact?target=esp32&version=1.0.1&partition_layout=ab-4m-v1&project=alpha",
            headers={**scoped_headers, "Content-Type": "application/octet-stream"},
            content=IMAGE_A,
        )
        assert upload_ro.status_code == 403
        # Non-admin token cannot mint another token
        assert (
            await client.post(
                "/v1/auth/tokens",
                headers=scoped_headers,
                json={"name": "nested", "scopes": ["read"]},
            )
        ).status_code == 403

        # Revoke the token
        rev = await client.post(f"/v1/auth/tokens/{scoped_id}/revoke", headers=bob_headers)
        assert rev.status_code == 204
        assert (await client.get("/v1/devices", headers=scoped_headers)).status_code == 401


def test_event_hub_filters_by_project_id() -> None:
    """P1-be-4: Project-scoped SSE subscribers only receive events for their project."""
    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    hub = EventHub(queue_size=8, max_subscribers=8)

    event_a = DeviceEvent(
        type=EventType.DEVICE_HEARTBEAT,
        device_id="a4cf12b3de90",
        project_id=proj_a,
        at=dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC),
        online=True,
    ).model_dump_json()

    event_b = DeviceEvent(
        type=EventType.DEVICE_HEARTBEAT,
        device_id="b4cf12b3de91",
        project_id=proj_b,
        at=dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC),
        online=True,
    ).model_dump_json()

    with (
        hub.subscribe(project_id=proj_a) as sub_a,
        hub.subscribe(project_id=proj_b) as sub_b,
        hub.subscribe() as sub_all,
    ):
        hub.publish(event_a, project_id=proj_a)
        hub.publish(event_b, project_id=proj_b)

        assert sub_a.queue.get_nowait() == event_a
        assert sub_a.queue.empty()

        assert sub_b.queue.get_nowait() == event_b
        assert sub_b.queue.empty()

        assert sub_all.queue.get_nowait() == event_a
        assert sub_all.queue.get_nowait() == event_b


def test_openapi_schema_includes_projects_users_and_tokens() -> None:
    """P1-be-1..4: All new multi-project/multi-user routes and query params exist in OpenAPI."""
    from fleetforge.api.main import create_app

    schema = create_app().openapi()
    paths = schema["paths"]

    expected_paths = {
        "/v1/auth/firebase",
        "/v1/auth/me",
        "/v1/auth/tokens",
        "/v1/auth/tokens/{token_id}/revoke",
        "/v1/projects",
        "/v1/projects/{slug}",
        "/v1/projects/{slug}/members",
        "/v1/projects/{slug}/members/{username}",
        "/v1/users",
        "/v1/users/{username}",
    }
    assert expected_paths.issubset(paths.keys())

    # Verify `project` query parameter is exposed on fleet-scoped endpoints
    for route, method in (
        ("/v1/devices", "get"),
        ("/v1/artifact", "get"),
        ("/v1/artifact", "post"),
        ("/v1/enrollment-tokens", "get"),
        ("/v1/enrollment-tokens", "post"),
        ("/v1/events", "get"),
    ):
        params = {p["name"] for p in paths[route][method].get("parameters", [])}
        assert "project" in params, f"{method.upper()} {route} missing ?project= parameter"


def test_user_create_validation_accepts_firebase_email_without_password() -> None:
    """Invite-only Firebase users can be created with `email` and no local password."""
    from pydantic import ValidationError

    fb_user = UserCreate(username="carol", email="Carol@Example.com", send_invite_email=True)
    assert fb_user.email == "carol@example.com"
    assert fb_user.password is None

    local_user = UserCreate(username="dave", password="secret-password-123")
    assert local_user.email is None
    assert local_user.password == "secret-password-123"

    with pytest.raises(ValidationError):
        UserCreate(username="nobody")

    with pytest.raises(ValidationError):
        UserCreate(username="bad-email", email="not-an-email")


class FakeFirebaseAuthClient:
    """In-memory Firebase Auth verifier/client for testing invite-only auth."""

    def __init__(self) -> None:
        self.accounts: dict[str, tuple[str, FirebaseIdentity]] = {}
        self.tokens: dict[str, FirebaseIdentity] = {}
        self.reset_emails_sent: list[str] = []

    def register(
        self,
        *,
        email: str,
        password: str,
        uid: str,
        id_token: str,
        display_name: str | None = None,
    ) -> None:
        ident = FirebaseIdentity(
            uid=uid,
            email=email.lower(),
            email_verified=True,
            display_name=display_name,
        )
        self.accounts[email.lower()] = (password, ident)
        self.tokens[id_token] = ident

    async def verify_id_token(self, token: str, project_id: str) -> FirebaseIdentity:
        if token not in self.tokens:
            raise FirebaseInvalidCredentials("bad token")
        return self.tokens[token]

    async def sign_in_with_password(
        self,
        email: str,
        password: str,
        *,
        api_key: str,
        project_id: str | None = None,
    ) -> FirebaseIdentity:
        entry = self.accounts.get(email.lower())
        if entry is None or entry[0] != password:
            raise FirebaseInvalidCredentials("bad email or password")
        return entry[1]

    async def send_password_reset(self, email: str, *, api_key: str) -> None:
        self.reset_emails_sent.append(email.lower())


async def test_firebase_invite_only_auth_and_superadmin_bootstrap(
    admin_app: FastAPI,
    clean_tables: None,
) -> None:
    """Invite-only Firebase email+password and ID token login, plus superadmin bootstrap."""
    fake_fb = FakeFirebaseAuthClient()
    fake_fb.register(
        email="carol@example.com",
        password="carol-firebase-password",
        uid="fb-uid-carol-001",
        id_token="fb-jwt-carol",
        display_name="Carol Danvers",
    )
    fake_fb.register(
        email="stranger@example.com",
        password="stranger-password",
        uid="fb-uid-stranger-999",
        id_token="fb-jwt-stranger",
    )
    fake_fb.register(
        email="boris@tvaroska.sk",
        password="boris-firebase-password",
        uid="fb-uid-boris-001",
        id_token="fb-jwt-boris",
        display_name="Boris Tvaroska",
    )

    orig_settings_fn = admin_app.dependency_overrides[get_settings]
    base_settings = orig_settings_fn()
    fb_settings = base_settings.model_copy(
        update={
            "firebase_project_id": "sites-470716",
            "firebase_web_api_key": "fake-web-api-key",
            "firebase_superadmin_emails": "boris@tvaroska.sk",
        }
    )
    admin_app.dependency_overrides[get_settings] = lambda: fb_settings
    admin_app.dependency_overrides[get_firebase_auth] = lambda: fake_fb

    try:
        admin_token = await login_admin(admin_app, TEST_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}"}

        async with client_for(admin_app, base_url="https://testserver") as client:
            # 1. Uninvited user is rejected with 403 even with valid Firebase credentials
            uninvited = await client.post(
                "/v1/auth/firebase",
                json={"id_token": "fb-jwt-stranger"},
            )
            assert uninvited.status_code == 403, uninvited.text

            # 2. Superadmin invites carol@example.com (no local password, sends Firebase reset email)
            invite = await client.post(
                "/v1/users",
                headers=headers,
                json={
                    "username": "carol",
                    "email": "carol@example.com",
                    "send_invite_email": True,
                },
            )
            assert invite.status_code == 201, invite.text
            assert invite.json()["email"] == "carol@example.com"
            assert invite.json()["has_local_password"] is False
            assert fake_fb.reset_emails_sent == ["carol@example.com"]

            # 3. Carol signs in with email + password at POST /v1/auth/login -> binds firebase_uid
            login_res = await client.post(
                "/v1/auth/login",
                json={"username": "carol@example.com", "password": "carol-firebase-password"},
            )
            assert login_res.status_code == 200, login_res.text
            carol_headers = {"Authorization": f"Bearer {login_res.cookies['ff_session']}"}
            me_carol = (await client.get("/v1/auth/me", headers=carol_headers)).json()
            assert me_carol["subject"] == "carol"
            assert me_carol["is_superadmin"] is False

            # 4. Configured FIREBASE_SUPERADMIN_EMAILS auto-provisions as superadmin on first login
            sa_res = await client.post(
                "/v1/auth/firebase",
                json={"id_token": "fb-jwt-boris"},
            )
            assert sa_res.status_code == 200, sa_res.text
            boris_headers = {"Authorization": f"Bearer {sa_res.cookies['ff_session']}"}
            me_boris = (await client.get("/v1/auth/me", headers=boris_headers)).json()
            assert me_boris["subject"] == "boris"
            assert me_boris["is_superadmin"] is True
    finally:
        admin_app.dependency_overrides[get_settings] = orig_settings_fn
        admin_app.dependency_overrides.pop(get_firebase_auth, None)
