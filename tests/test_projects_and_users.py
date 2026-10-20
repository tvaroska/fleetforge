"""End-to-end tests for Projects, Multi-User Auth & Scoped Tokens (Option A: P1-be-1..4)."""

import datetime as dt
import hashlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import (
    AuthContext,
    get_command_publisher,
    get_firebase_auth,
    get_object_store,
)
from fleetforge.api.eventstream import EventHub
from fleetforge.api.schemas import MeResponse, UserCreate
from fleetforge.auth.firebase import (
    FirebaseAuthError,
    FirebaseIdentity,
    FirebaseInvalidCredentials,
)
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, issue_token
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
        # Never the builtins: the deploy gate and upload ceiling elsewhere depend on them.
        await session.execute(text("DELETE FROM partition_profiles WHERE origin <> 'builtin'"))
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
        r = await client.put(
            "/v1/projects/default/members/alice",
            headers=headers,
            json={"role": "viewer"},
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
        assert publisher.last["artifact"]["sha256"] == SHA_B

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
        r = await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "alpha", "name": "Alpha"},
        )
        assert r.status_code in (200, 201), r.text
        r = await client.post(
            "/v1/projects",
            headers=headers,
            json={"slug": "beta", "name": "Beta"},
        )
        assert r.status_code in (200, 201), r.text
        r = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "bob", "password": "bob-password-123", "is_superadmin": False},
        )
        assert r.status_code in (200, 201), r.text
        r = await client.put(
            "/v1/projects/alpha/members/bob",
            headers=headers,
            json={"role": "viewer"},
        )
        assert r.status_code == 200, r.text

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
        r = await client.put(
            "/v1/projects/alpha/members/bob",
            headers=headers,
            json={"role": "operator"},
        )
        assert r.status_code == 200, r.text
        upload_op = await client.post(
            "/v1/artifact?target=esp32&version=1.0.0&partition_layout=ab-4m-v1&project=alpha",
            headers={**bob_headers, "Content-Type": "application/octet-stream"},
            content=IMAGE_A,
        )
        assert upload_op.status_code == 201

        # An operator cannot mint a project-pinned token: that needs owner.
        refused = await client.post(
            "/v1/auth/tokens",
            headers=bob_headers,
            json={"name": "alpha-reader", "project": "alpha", "scopes": ["read"]},
        )
        assert refused.status_code == 403, refused.text
        r = await client.put(
            "/v1/projects/alpha/members/bob",
            headers=headers,
            json={"role": "owner"},
        )
        assert r.status_code == 200, r.text

        # Bob, now owner, mints a read-only token pinned to 'alpha'
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
        self.accounts_created: list[str] = []
        self.fail_create = False
        self.fail_reset = False

    def register(
        self,
        *,
        email: str,
        password: str,
        uid: str,
        id_token: str,
        display_name: str | None = None,
        email_verified: bool = True,
    ) -> None:
        ident = FirebaseIdentity(
            uid=uid,
            email=email.lower(),
            email_verified=email_verified,
            display_name=display_name,
        )
        self.accounts[email.lower()] = (password, ident)
        self.tokens[id_token] = ident

    async def verify_id_token(
        self,
        token: str,
        project_id: str,
        *,
        api_key: str | None = None,
        check_revoked: bool = True,
    ) -> FirebaseIdentity:
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

    async def create_account(self, email: str, *, api_key: str) -> bool:
        if self.fail_create:
            raise FirebaseAuthError("signUp failed")
        key = email.lower()
        self.accounts_created.append(key)
        if key in self.accounts:
            return False
        placeholder = FirebaseIdentity(uid=f"fb-uid-{uuid.uuid4().hex}", email=key)
        self.accounts[key] = (uuid.uuid4().hex, placeholder)
        return True

    async def send_password_reset(self, email: str, *, api_key: str) -> None:
        if self.fail_reset:
            raise FirebaseAuthError("sendOobCode failed")
        if email.lower() not in self.accounts:
            # Mirrors Firebase: a reset for an address with no account is EMAIL_NOT_FOUND.
            raise FirebaseInvalidCredentials("EMAIL_NOT_FOUND")
        self.reset_emails_sent.append(email.lower())


FB_SETTINGS = {
    "firebase_project_id": "sites-470716",
    "firebase_web_api_key": "fake-web-api-key",
}


@pytest.fixture
def fb_env(admin_app: FastAPI) -> Iterator[tuple[FakeFirebaseAuthClient, Callable[..., None]]]:
    """Install a fake Firebase client and Firebase settings; yield (fake, reconfigure).

    `reconfigure(**overrides)` swaps the settings override (e.g. to drop the API key).
    Every override is restored afterwards: a leak breaks later tests.
    """
    fake_fb = FakeFirebaseAuthClient()
    orig_settings_fn = admin_app.dependency_overrides[get_settings]
    base_settings = orig_settings_fn()

    def reconfigure(**overrides: object) -> None:
        cfg = base_settings.model_copy(update={**FB_SETTINGS, **overrides})
        admin_app.dependency_overrides[get_settings] = lambda: cfg

    reconfigure()
    admin_app.dependency_overrides[get_firebase_auth] = lambda: fake_fb
    try:
        yield fake_fb, reconfigure
    finally:
        admin_app.dependency_overrides[get_settings] = orig_settings_fn
        admin_app.dependency_overrides.pop(get_firebase_auth, None)


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
        id_token="fb-id-token-carol-0001",
        display_name="Carol Danvers",
    )
    fake_fb.register(
        email="stranger@example.com",
        password="stranger-password",
        uid="fb-uid-stranger-999",
        id_token="fb-id-token-stranger-0001",
    )
    fake_fb.register(
        email="boris@tvaroska.sk",
        password="boris-firebase-password",
        uid="fb-uid-boris-001",
        id_token="fb-id-token-boris-0001",
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
                json={"id_token": "fb-id-token-stranger-0001"},
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
            assert invite.json()["email_delivery"] == "sent"

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
                json={"id_token": "fb-id-token-boris-0001"},
            )
            assert sa_res.status_code == 200, sa_res.text
            boris_headers = {"Authorization": f"Bearer {sa_res.cookies['ff_session']}"}
            me_boris = (await client.get("/v1/auth/me", headers=boris_headers)).json()
            assert me_boris["subject"] == "boris"
            assert me_boris["is_superadmin"] is True
    finally:
        admin_app.dependency_overrides[get_settings] = orig_settings_fn
        admin_app.dependency_overrides.pop(get_firebase_auth, None)


FbEnv = tuple[FakeFirebaseAuthClient, Callable[..., None]]


async def test_an_unverified_email_cannot_claim_an_invite(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
    engine: AsyncEngine,
) -> None:
    """S0-sec-1: a self-registered, unverified Firebase identity cannot take an invite."""
    fake_fb, _ = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        invite = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "dave", "email": "dave@example.com"},
        )
        assert invite.status_code == 201, invite.text
        dave_id = uuid.UUID(invite.json()["id"])

        # An attacker signs up dave@example.com with the public web API key.
        fake_fb.register(
            email="dave@example.com",
            password="squatter-password",
            uid="fb-uid-squatter-001",
            id_token="fb-id-token-squatter-0001",
            email_verified=False,
        )

        via_token = await client.post(
            "/v1/auth/firebase", json={"id_token": "fb-id-token-squatter-0001"}
        )
        assert via_token.status_code == 403, via_token.text
        assert "not been verified" in via_token.json()["detail"]

        via_password = await client.post(
            "/v1/auth/login",
            json={"username": "dave@example.com", "password": "squatter-password"},
        )
        assert via_password.status_code == 403, via_password.text
        assert "not been verified" in via_password.json()["detail"]
        assert "ff_session" not in via_password.cookies

        async with AsyncSession(engine) as session:
            dave = await session.get(User, dave_id)
            assert dave is not None
            assert dave.firebase_uid is None
            tokens = (
                await session.scalars(select(AdminToken).where(AdminToken.user_id == dave_id))
            ).all()
            assert tokens == []

        # The real owner completes the reset email: the account is now verified.
        fake_fb.register(
            email="dave@example.com",
            password="dave-real-password",
            uid="fb-uid-dave-001",
            id_token="fb-id-token-dave-00001",
            email_verified=True,
        )
        ok = await client.post(
            "/v1/auth/login",
            json={"username": "dave@example.com", "password": "dave-real-password"},
        )
        assert ok.status_code == 200, ok.text

        async with AsyncSession(engine) as session:
            dave = await session.get(User, dave_id)
            assert dave is not None
            assert dave.firebase_uid == "fb-uid-dave-001"


async def test_an_unverified_bootstrap_superadmin_email_is_refused(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
    engine: AsyncEngine,
) -> None:
    """S0-sec-1: an unverified FIREBASE_SUPERADMIN_EMAILS address never bootstraps a row."""
    fake_fb, reconfigure = fb_env
    reconfigure(firebase_superadmin_emails="boss@example.com")
    fake_fb.register(
        email="boss@example.com",
        password="squatter-password",
        uid="fb-uid-boss-squatter",
        id_token="fb-id-token-boss-squat-01",
        email_verified=False,
    )

    async with client_for(admin_app, base_url="https://testserver") as client:
        via_token = await client.post(
            "/v1/auth/firebase", json={"id_token": "fb-id-token-boss-squat-01"}
        )
        assert via_token.status_code == 403, via_token.text
        assert "not been verified" in via_token.json()["detail"]

        via_password = await client.post(
            "/v1/auth/login",
            json={"username": "boss@example.com", "password": "squatter-password"},
        )
        assert via_password.status_code == 403, via_password.text
        assert "not been verified" in via_password.json()["detail"]

    async with AsyncSession(engine) as session:
        rows = (await session.scalars(select(User).where(User.email == "boss@example.com"))).all()
        assert rows == []


async def test_an_invite_to_a_new_address_creates_the_account_and_sends_the_email(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
) -> None:
    """S0-sec-1: an invite to an address with no Firebase account still delivers."""
    fake_fb, _ = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        r = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "erin", "email": "erin@example.com", "send_invite_email": True},
        )
        assert r.status_code == 201, r.text
        assert r.json()["email_delivery"] == "sent"
    assert fake_fb.accounts_created == ["erin@example.com"]
    assert fake_fb.reset_emails_sent == ["erin@example.com"]


async def test_an_invite_reports_when_the_email_cannot_be_sent(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
) -> None:
    """S0-sec-1: the 201 body says whether the setup email went out."""
    fake_fb, reconfigure = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        fake_fb.fail_reset = True
        failed = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "fred", "email": "fred@example.com", "send_invite_email": True},
        )
        assert failed.status_code == 201, failed.text
        assert failed.json()["email_delivery"] == "failed"
        fake_fb.fail_reset = False

        none_requested = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "hank", "email": "hank@example.com", "send_invite_email": False},
        )
        assert none_requested.status_code == 201, none_requested.text
        assert none_requested.json()["email_delivery"] is None

        reconfigure(firebase_web_api_key=None)
        unconfigured = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "gina", "email": "gina@example.com", "send_invite_email": True},
        )
        assert unconfigured.status_code == 201, unconfigured.text
        assert unconfigured.json()["email_delivery"] == "not_configured"
    assert "hank@example.com" not in fake_fb.accounts_created
    assert "gina@example.com" not in fake_fb.accounts_created


async def test_a_password_reset_for_a_pre_fix_invitee_creates_the_account(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
) -> None:
    """S0-sec-1: an invitee from before the fix (no Firebase account) can still be reached."""
    fake_fb, reconfigure = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        reconfigure(firebase_web_api_key=None)
        created = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "ivan", "email": "ivan@example.com", "send_invite_email": True},
        )
        assert created.status_code == 201, created.text
        assert created.json()["email_delivery"] == "not_configured"
        assert fake_fb.accounts_created == []

        reconfigure()
        r = await client.patch(
            "/v1/users/ivan",
            headers=headers,
            json={"send_password_reset": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["email_delivery"] == "sent"
    assert fake_fb.accounts_created == ["ivan@example.com"]
    assert fake_fb.reset_emails_sent == ["ivan@example.com"]


def test_auth_context_and_me_default_to_not_superadmin() -> None:
    """S0-sec-1: the fail-open `is_superadmin=True` defaults are gone."""
    ctx = AuthContext(token_id=uuid.uuid4(), subject="x", scopes=(), expires_at=None)
    assert ctx.is_superadmin is False
    me = MeResponse(token_id=uuid.uuid4(), subject="x", scopes=[], expires_at=None)
    assert me.is_superadmin is False


# --- S0-sec-2: partition profiles, token minting, hidden device ids ----------------------

AB_SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"  # ab-4m-v1
DETECTED_SHA = hashlib.sha256(b"s0-sec-2 detected").hexdigest()
USER_PROFILE_SHA = hashlib.sha256(b"s0-sec-2 user").hexdigest()
ALPHA_DEVICE = "aaaaaaaaaaa1"
BETA_DEVICE = "bbbbbbbbbbb1"
UNKNOWN_DEVICE = "ccccccccccc1"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str], slug: str) -> None:
    r = await client.post("/v1/projects", headers=headers, json={"slug": slug, "name": slug})
    assert r.status_code == 201, r.text


async def _user_session(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    username: str,
    memberships: dict[str, str],
) -> dict[str, str]:
    """Create `username` with `memberships` (slug -> role), log in, return its headers."""
    password = f"{username}-password-123"
    r = await client.post(
        "/v1/users",
        headers=headers,
        json={"username": username, "password": password, "is_superadmin": False},
    )
    assert r.status_code == 201, r.text
    for slug, role in memberships.items():
        r = await client.put(
            f"/v1/projects/{slug}/members/{username}", headers=headers, json={"role": role}
        )
        assert r.status_code == 200, r.text
    r = await client.post("/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return _bearer(r.cookies["ff_session"])


async def _project_id(engine: AsyncEngine, slug: str) -> uuid.UUID:
    async with AsyncSession(engine) as session:
        found = await session.scalar(select(Project.id).where(Project.slug == slug))
    assert found is not None
    return found


async def _userless_token(
    engine: AsyncEngine, project_id: uuid.UUID | None, scopes: list[str], name: str
) -> tuple[str, uuid.UUID]:
    """A machine token with no user (CLI `issue-token`), optionally project-pinned."""
    issued = issue_token(ADMIN_TOKEN_PREFIX)
    async with AsyncSession(engine) as session:
        session.add(
            AdminToken(
                id=issued.token_id,
                name=name,
                secret_hash=issued.secret_hash,
                subject="cli",
                scopes=scopes,
                user_id=None,
                project_id=project_id,
            )
        )
        await session.commit()
    return issued.token, issued.token_id


async def _add_device(engine: AsyncEngine, device_id: str, project_id: uuid.UUID) -> None:
    async with AsyncSession(engine) as session:
        session.add(
            Device(
                device_id=device_id,
                project_id=project_id,
                platform_type="esp32",
                link_type="wifi",
                power_class="always_on",
                partition_layout="ab-4m-v1",
                partition_table_sha256=AB_SHA,
                capabilities=["ota", "reboot", "rollback"],
            )
        )
        await session.commit()


async def _add_pending_profile(engine: AsyncEngine, detected_device_id: str) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(
            text(
                "INSERT INTO partition_profiles (partition_table_sha256, origin, ota_slot_size, "
                "flash_chip_size, detected_device_id) "
                "VALUES (:sha, 'detected', 1835008, 4194304, :d)"
            ),
            {"sha": DETECTED_SHA, "d": detected_device_id},
        )
        await session.commit()


async def _profile_row(engine: AsyncEngine, sha: str) -> tuple[object, ...] | None:
    async with AsyncSession(engine) as session:
        found = (
            await session.execute(
                text(
                    "SELECT layout_id, origin, ota_slot_size, adopted_at IS NOT NULL, "
                    "detected_device_id FROM partition_profiles "
                    "WHERE partition_table_sha256 = :sha"
                ),
                {"sha": sha},
            )
        ).first()
    return tuple(found) if found is not None else None


async def _token_count(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as session:
        return len((await session.scalars(select(AdminToken.id))).all())


async def test_a_viewer_or_read_token_cannot_write_partition_profiles(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """S0-sec-2: profiles are global, so writing one is a superadmin action only."""
    admin_headers = _bearer(await login_admin(admin_app))
    await _add_pending_profile(engine, ALPHA_DEVICE)
    pending_before = await _profile_row(engine, DETECTED_SHA)
    assert pending_before == (None, "detected", 1835008, False, ALPHA_DEVICE)
    create = {
        "layout_id": "s0sec2-user",
        "partition_table_sha256": USER_PROFILE_SHA,
        "ota_slot_size": 1966080,
    }

    async with client_for(admin_app, base_url="https://testserver") as client:
        callers = {
            "viewer": await _user_session(client, admin_headers, "pviewer", {"default": "viewer"}),
            "operator": await _user_session(
                client, admin_headers, "poperator", {"default": "operator"}
            ),
            "owner": await _user_session(client, admin_headers, "powner", {"default": "owner"}),
            "pinned read": _bearer(
                (await _userless_token(engine, DEFAULT_PROJECT_ID, ["read"], "p-read"))[0]
            ),
            "pinned admin": _bearer(
                (await _userless_token(engine, DEFAULT_PROJECT_ID, ["admin"], "p-admin"))[0]
            ),
        }
        for who, headers in callers.items():
            r = await client.post("/v1/partition-profiles", headers=headers, json=create)
            assert r.status_code == 403, (who, r.text)
            r = await client.patch(
                f"/v1/partition-profiles/{DETECTED_SHA}",
                headers=headers,
                json={"layout_id": "s0sec2-adopted"},
            )
            assert r.status_code == 403, (who, r.text)
            r = await client.delete(f"/v1/partition-profiles/{DETECTED_SHA}", headers=headers)
            assert r.status_code == 403, (who, r.text)
            # A malformed fingerprint is refused before it is looked at: no 404 oracle.
            r = await client.patch(
                "/v1/partition-profiles/not-a-sha", headers=headers, json={"layout_id": "x"}
            )
            assert r.status_code == 403, (who, r.text)
            r = await client.delete("/v1/partition-profiles/not-a-sha", headers=headers)
            assert r.status_code == 403, (who, r.text)
            # Reading is still allowed.
            r = await client.get("/v1/partition-profiles", headers=headers)
            assert r.status_code == 200, (who, r.text)

        assert await _profile_row(engine, DETECTED_SHA) == pending_before
        assert await _profile_row(engine, USER_PROFILE_SHA) is None

        # The superadmin still writes.
        r = await client.post("/v1/partition-profiles", headers=admin_headers, json=create)
        assert r.status_code == 201, r.text
        r = await client.patch(
            f"/v1/partition-profiles/{DETECTED_SHA}",
            headers=admin_headers,
            json={"layout_id": "s0sec2-adopted"},
        )
        assert r.status_code == 200, r.text
        r = await client.delete(f"/v1/partition-profiles/{USER_PROFILE_SHA}", headers=admin_headers)
        assert r.status_code == 204, r.text


async def test_the_profile_list_shows_only_the_callers_projects_devices(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """S0-sec-2: every profile is listed, but device ids only from the caller's projects."""
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        await _create_project(client, admin_headers, "alpha")
        await _create_project(client, admin_headers, "beta")
        alpha_id = await _project_id(engine, "alpha")
        beta_id = await _project_id(engine, "beta")
        await _add_device(engine, ALPHA_DEVICE, alpha_id)
        await _add_device(engine, BETA_DEVICE, beta_id)
        await _add_pending_profile(engine, BETA_DEVICE)

        viewer = await _user_session(client, admin_headers, "lviewer", {"alpha": "viewer"})
        loner = await _user_session(client, admin_headers, "lloner", {})
        beta_reader = _bearer((await _userless_token(engine, beta_id, ["read"], "beta-read"))[0])

        async def listing(headers: dict[str, str]) -> dict[str, dict[str, Any]]:
            r = await client.get("/v1/partition-profiles", headers=headers)
            assert r.status_code == 200, r.text
            return {p["partition_table_sha256"]: p for p in r.json()["profiles"]}

        everything = await listing(admin_headers)
        assert {ALPHA_DEVICE, BETA_DEVICE} <= set(everything[AB_SHA]["device_ids"])
        assert everything[DETECTED_SHA]["detected_device_id"] == BETA_DEVICE

        alpha_view = await listing(viewer)
        assert alpha_view.keys() == everything.keys(), "the profiles themselves are global"
        assert alpha_view[AB_SHA]["device_ids"] == [ALPHA_DEVICE]
        assert alpha_view[DETECTED_SHA]["detected_device_id"] is None

        beta_view = await listing(beta_reader)
        assert beta_view[AB_SHA]["device_ids"] == [BETA_DEVICE]
        assert beta_view[DETECTED_SHA]["detected_device_id"] == BETA_DEVICE

        lonely = await listing(loner)
        assert lonely.keys() == everything.keys()
        assert all(p["device_ids"] == [] for p in lonely.values())
        assert lonely[DETECTED_SHA]["detected_device_id"] is None


async def test_a_project_pinned_token_cannot_mint_tokens(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """S0-sec-2: only an unpinned session mints, so no CI token outlives its revocation."""
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        await _create_project(client, admin_headers, "alpha")
        alpha_id = await _project_id(engine, "alpha")
        owner = await _user_session(client, admin_headers, "mowner", {"alpha": "owner"})

        # The owner's session still mints, including a pinned admin token.
        r = await client.post(
            "/v1/auth/tokens",
            headers=owner,
            json={"name": "ci", "project": "alpha", "scopes": ["admin"]},
        )
        assert r.status_code == 201, r.text
        user_pinned = _bearer(r.json()["token"])
        userless_pinned = _bearer(
            (await _userless_token(engine, alpha_id, ["admin"], "userless-alpha"))[0]
        )

        before = await _token_count(engine)
        for who, headers in {"userless": userless_pinned, "user-owned": user_pinned}.items():
            for body in (
                {"name": "child", "project": "alpha", "scopes": ["admin"]},
                {"name": "child", "scopes": ["read"]},
            ):
                r = await client.post("/v1/auth/tokens", headers=headers, json=body)
                assert r.status_code == 403, (who, body, r.text)
        assert await _token_count(engine) == before, "a refused mint wrote a row"


async def test_a_userless_project_token_cannot_revoke_other_userless_tokens(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """S0-sec-2: two userless tokens are not each other's (`None == None` was "own")."""
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        await _create_project(client, admin_headers, "alpha")
        alpha_id = await _project_id(engine, "alpha")
        target, target_id = await _userless_token(engine, None, ["admin"], "operator-cli")
        sibling, sibling_id = await _userless_token(engine, alpha_id, ["read"], "alpha-sibling")
        caller, caller_id = await _userless_token(engine, alpha_id, ["admin"], "alpha-ci")

        r = await client.post(f"/v1/auth/tokens/{target_id}/revoke", headers=_bearer(caller))
        assert r.status_code == 403, r.text
        assert (await client.get("/v1/auth/me", headers=_bearer(target))).status_code == 200

        # Same project, still not its own: only a user who owns alpha may revoke it.
        r = await client.post(f"/v1/auth/tokens/{sibling_id}/revoke", headers=_bearer(caller))
        assert r.status_code == 403, r.text
        assert (await client.get("/v1/auth/me", headers=_bearer(sibling))).status_code == 200

        # Itself, yes.
        r = await client.post(f"/v1/auth/tokens/{caller_id}/revoke", headers=_bearer(caller))
        assert r.status_code == 204, r.text
        assert (await client.get("/v1/auth/me", headers=_bearer(caller))).status_code == 401


async def test_a_device_in_another_project_is_indistinguishable_from_an_unknown_one(
    admin_app: FastAPI,
    engine: AsyncEngine,
    store: MemoryObjectStore,
    publisher: FakeCommandPublisher,
    clean_tables: None,
) -> None:
    """S0-sec-2: visibility is checked before permission, and hidden looks like unknown."""
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        await _create_project(client, admin_headers, "alpha")
        await _create_project(client, admin_headers, "beta")
        alpha_id = await _project_id(engine, "alpha")
        beta_id = await _project_id(engine, "beta")
        await _add_device(engine, ALPHA_DEVICE, alpha_id)
        await _add_device(engine, BETA_DEVICE, beta_id)

        callers = {
            "alpha viewer": await _user_session(
                client, admin_headers, "dviewer", {"alpha": "viewer"}
            ),
            "alpha operator": await _user_session(
                client, admin_headers, "doperator", {"alpha": "operator"}
            ),
            "alpha pinned read": _bearer(
                (await _userless_token(engine, alpha_id, ["read"], "alpha-read"))[0]
            ),
        }

        def requests(device_id: str) -> list[tuple[str, str, dict[str, object] | None]]:
            deploy = {"version": "1.0.0", "apply": "auto"}
            return [
                ("POST", f"/v1/devices/{device_id}/reboot", None),
                ("POST", f"/v1/devices/{device_id}/rollback", None),
                ("GET", f"/v1/devices/{device_id}/commands/x", None),
                ("POST", f"/v1/devices/{device_id}/deploy", deploy),
                ("POST", f"/v1/devices/{device_id}/deploy/precheck", deploy),
                ("PATCH", f"/v1/devices/{device_id}", {"name": "x"}),
                ("GET", f"/v1/devices/{device_id}/logs", None),
                ("GET", f"/v1/devices/{device_id}/telemetry?name=temp", None),
            ]

        for who, headers in callers.items():
            for (method, hidden_path, body), (_, unknown_path, _) in zip(
                requests(BETA_DEVICE), requests(UNKNOWN_DEVICE), strict=True
            ):
                hidden = await client.request(method, hidden_path, headers=headers, json=body)
                unknown = await client.request(method, unknown_path, headers=headers, json=body)
                assert unknown.status_code == 404, (who, unknown_path, unknown.text)
                assert unknown.json() == {"detail": f"no such device: {UNKNOWN_DEVICE}"}
                assert hidden.status_code == 404, (who, hidden_path, hidden.text)
                assert hidden.json() == {"detail": f"no such device: {BETA_DEVICE}"}

        # Inside the caller's own project, too little role is still a 403.
        r = await client.post(f"/v1/devices/{ALPHA_DEVICE}/reboot", headers=callers["alpha viewer"])
        assert r.status_code == 403, r.text
        r = await client.patch(
            f"/v1/devices/{ALPHA_DEVICE}", headers=callers["alpha pinned read"], json={"name": "x"}
        )
        assert r.status_code == 403, r.text
        assert publisher.published == [], "nothing reached a board"


async def test_an_enrollment_token_in_another_project_is_unknown(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """S0-sec-2: revoking another project's enrollment token answers like a random id."""
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        await _create_project(client, admin_headers, "alpha")
        await _create_project(client, admin_headers, "beta")
        r = await client.post(
            "/v1/enrollment-tokens?project=beta", headers=admin_headers, json={"group_id": None}
        )
        assert r.status_code == 201, r.text
        beta_token_id = r.json()["id"]
        operator = await _user_session(client, admin_headers, "eoperator", {"alpha": "operator"})

        hidden = await client.post(
            f"/v1/enrollment-tokens/{beta_token_id}/revoke", headers=operator
        )
        unknown = await client.post(
            f"/v1/enrollment-tokens/{uuid.uuid4()}/revoke", headers=operator
        )
        assert unknown.status_code == 404, unknown.text
        assert (hidden.status_code, hidden.json()) == (404, unknown.json())
        assert hidden.json() == {"detail": "unknown token"}

        async with AsyncSession(engine) as session:
            row = await session.get(EnrollmentToken, uuid.UUID(beta_token_id))
        assert row is not None and row.revoked_at is None


async def test_a_member_added_with_put_is_listed_and_post_is_not_a_route(
    admin_app: FastAPI, clean_tables: None
) -> None:
    """S0-bug-7: member add is `PUT /members/{username}` with `{"role"}`, never POST."""
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        r = await client.post(
            "/v1/users",
            headers=admin_headers,
            json={"username": "carol", "password": "carol-password-123", "is_superadmin": False},
        )
        assert r.status_code == 201, r.text

        url = "/v1/projects/default/members/carol"
        r = await client.put(url, headers=admin_headers, json={"role": "operator"})
        assert r.status_code == 200, r.text
        assert (r.json()["username"], r.json()["role"]) == ("carol", "operator")

        r = await client.get("/v1/projects/default/members", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert [(m["username"], m["role"]) for m in r.json()["members"]] == [("carol", "operator")]

        # A second PUT upserts the role; it does not add a row.
        r = await client.put(url, headers=admin_headers, json={"role": "viewer"})
        assert r.status_code == 200, r.text
        r = await client.get("/v1/projects/default/members", headers=admin_headers)
        assert [(m["username"], m["role"]) for m in r.json()["members"]] == [("carol", "viewer")]

        # The username lives in the path; the schema forbids it in the body.
        r = await client.put(
            url, headers=admin_headers, json={"username": "carol", "role": "viewer"}
        )
        assert r.status_code == 422, r.text

        # The dashboard's old shape is not a route.
        r = await client.post(
            "/v1/projects/default/members",
            headers=admin_headers,
            json={"username": "carol", "role": "viewer"},
        )
        assert r.status_code == 405, r.text

        r = await client.put(
            "/v1/projects/default/members/nobody", headers=admin_headers, json={"role": "viewer"}
        )
        assert r.status_code == 404, r.text
