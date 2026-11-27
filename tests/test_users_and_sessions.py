"""Multi-user auth, Firebase invite-only flows, session revocation, and rate-limit tests (`T-10`, `T-11`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import AuthContext, get_firebase_auth
from fleetforge.api.schemas import MeResponse, UserCreate
from fleetforge.auth.ratelimit import FixedWindowLimiter
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
from tests.conftest import TEST_PASSWORD, client_for, login_admin
from tests.doubles import FakeFirebaseAuthClient

FB_SETTINGS = {
    "firebase_project_id": "sites-470716",
    "firebase_web_api_key": "fake-web-api-key",
}

FbEnv = tuple[FakeFirebaseAuthClient, Callable[..., None]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Clean up rows created via the app's own sessionmaker after each test."""
    yield
    async with AsyncSession(engine, expire_on_commit=False) as session:
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


@pytest.fixture
def fb_env(admin_app: FastAPI) -> Iterator[FbEnv]:
    """Install Port-checked `FakeFirebaseAuthClient` and Firebase settings; yield `(fake, reconfigure)`."""
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


def test_user_create_validation_accepts_firebase_email_without_password() -> None:
    """Invite-only Firebase users can be created with `email` and no local password."""
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


def test_auth_context_and_me_default_to_not_superadmin() -> None:
    """S0-sec-1: the fail-open `is_superadmin=True` defaults are gone."""
    ctx = AuthContext(token_id=uuid.uuid4(), subject="x", scopes=(), expires_at=None)
    assert ctx.is_superadmin is False
    me = MeResponse(token_id=uuid.uuid4(), subject="x", scopes=[], expires_at=None)
    assert me.is_superadmin is False


async def test_multi_user_login_and_disabled_user_rejection(
    admin_app: FastAPI,
    clean_tables: None,
) -> None:
    """P1-be-1: Superadmin creates a user, user logs in, and disabling immediately 401s."""
    _ = clean_tables
    admin_token = await login_admin(admin_app)
    headers = _bearer(admin_token)

    async with client_for(admin_app, base_url="https://testserver") as client:
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

        r = await client.put(
            "/v1/projects/default/members/alice",
            headers=headers,
            json={"role": "viewer"},
        )
        assert r.status_code == 200, r.text

        r = await client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": "alice-secret-password"},
        )
        assert r.status_code == 200, r.text
        alice_headers = _bearer(r.cookies["ff_session"])

        me = (await client.get("/v1/auth/me", headers=alice_headers)).json()
        assert me["subject"] == "alice"
        assert me["is_superadmin"] is False
        assert [p["slug"] for p in me["projects"]] == ["default"]
        assert me["projects"][0]["role"] == "viewer"

        assert (await client.get("/v1/users", headers=alice_headers)).status_code == 403

        r = await client.patch(
            "/v1/users/alice",
            headers=headers,
            json={"disabled": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["id"] == alice_id
        assert r.json()["disabled_at"] is not None

        assert (await client.get("/v1/auth/me", headers=alice_headers)).status_code == 401


async def test_firebase_invite_only_auth_and_superadmin_bootstrap(
    admin_app: FastAPI,
    clean_tables: None,
) -> None:
    """Invite-only Firebase email+password and ID token login, plus superadmin bootstrap."""
    _ = clean_tables
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
        headers = _bearer(admin_token)

        async with client_for(admin_app, base_url="https://testserver") as client:
            uninvited = await client.post(
                "/v1/auth/firebase",
                json={"id_token": "fb-id-token-stranger-0001"},
            )
            assert uninvited.status_code == 403, uninvited.text

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

            login_res = await client.post(
                "/v1/auth/login",
                json={"username": "carol@example.com", "password": "carol-firebase-password"},
            )
            assert login_res.status_code == 200, login_res.text
            carol_headers = _bearer(login_res.cookies["ff_session"])
            me_carol = (await client.get("/v1/auth/me", headers=carol_headers)).json()
            assert me_carol["subject"] == "carol"
            assert me_carol["is_superadmin"] is False

            sa_res = await client.post(
                "/v1/auth/firebase",
                json={"id_token": "fb-id-token-boris-0001"},
            )
            assert sa_res.status_code == 200, sa_res.text
            boris_headers = _bearer(sa_res.cookies["ff_session"])
            me_boris = (await client.get("/v1/auth/me", headers=boris_headers)).json()
            assert me_boris["subject"] == "boris"
            assert me_boris["is_superadmin"] is True
    finally:
        admin_app.dependency_overrides[get_settings] = orig_settings_fn
        admin_app.dependency_overrides.pop(get_firebase_auth, None)


async def test_an_unverified_email_cannot_claim_an_invite(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
    engine: AsyncEngine,
) -> None:
    """S0-sec-1: a self-registered, unverified Firebase identity cannot take an invite."""
    _ = clean_tables
    fake_fb, _ = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = _bearer(admin_token)

    async with client_for(admin_app, base_url="https://testserver") as client:
        invite = await client.post(
            "/v1/users",
            headers=headers,
            json={"username": "dave", "email": "dave@example.com"},
        )
        assert invite.status_code == 201, invite.text
        dave_id = uuid.UUID(invite.json()["id"])

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
    _ = clean_tables
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
    """S0-sec-1: an invite to an address with no Firebase account still delivers and can reset."""
    _ = clean_tables
    fake_fb, reconfigure = fb_env
    reconfigure(public_base_url="https://bingo.tvaroska.sk")
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = _bearer(admin_token)

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
        assert fake_fb.continue_urls == ["https://bingo.tvaroska.sk"]

        reset_res = await client.post(
            "/v1/auth/reset-password",
            json={
                "oob_code": "oob-for-erin@example.com",
                "new_password": "erin-new-password-88",
            },
        )
        assert reset_res.status_code == 200, reset_res.text
        erin_headers = _bearer(reset_res.cookies["ff_session"])
        me_erin = (await client.get("/v1/auth/me", headers=erin_headers)).json()
        assert me_erin["subject"] == "erin"

        reuse_res = await client.post(
            "/v1/auth/reset-password",
            json={
                "oob_code": "oob-for-erin@example.com",
                "new_password": "erin-new-password-99",
            },
        )
        assert reuse_res.status_code == 401, reuse_res.text


async def test_an_invite_reports_when_the_email_cannot_be_sent(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
) -> None:
    """S0-sec-1: the 201 body says whether the setup email went out."""
    _ = clean_tables
    fake_fb, reconfigure = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = _bearer(admin_token)

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
    _ = clean_tables
    fake_fb, reconfigure = fb_env
    admin_token = await login_admin(admin_app, TEST_PASSWORD)
    headers = _bearer(admin_token)

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


async def test_user_lifecycle_edit_delete_and_self_profile(
    admin_app: FastAPI, clean_tables: None
) -> None:
    """Superadmin can invite with project role, edit fields, and delete; users can PATCH `/v1/auth/me`."""
    _ = clean_tables
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        r = await client.post(
            "/v1/users",
            headers=admin_headers,
            json={
                "username": "boris",
                "email": "boris@tvaroska.com",
                "display_name": "Boris",
                "password": "initial-password-123",
                "is_superadmin": False,
                "project": "default",
                "role": "owner",
            },
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["display_name"] == "Boris"
        assert created["has_local_password"] is True
        assert created["projects"] == [{"project_slug": "default", "role": "owner"}]

        r = await client.patch(
            "/v1/users/boris",
            headers=admin_headers,
            json={
                "display_name": "Boris Tvaroska",
                "password": "reset-password-456",
                "is_superadmin": True,
            },
        )
        assert r.status_code == 200, r.text
        patched = r.json()
        assert patched["display_name"] == "Boris Tvaroska"
        assert patched["is_superadmin"] is True

        r = await client.post(
            "/v1/auth/login",
            json={"username": "boris", "password": "reset-password-456"},
        )
        assert r.status_code == 200, r.text
        boris_cookie = r.cookies.get("ff_session")
        assert boris_cookie
        boris_headers = _bearer(boris_cookie)

        me_res = await client.get("/v1/auth/me", headers=boris_headers)
        assert me_res.status_code == 200
        assert me_res.json()["display_name"] == "Boris Tvaroska"
        assert me_res.json()["email"] == "boris@tvaroska.com"
        assert me_res.json()["has_local_password"] is True
        assert me_res.json()["is_superadmin"] is True

        me_patch = await client.patch(
            "/v1/auth/me",
            headers=boris_headers,
            json={
                "display_name": "Boris T.",
                "current_password": "reset-password-456",
                "password": "self-changed-password-789",
            },
        )
        assert me_patch.status_code == 200, me_patch.text
        assert me_patch.json()["display_name"] == "Boris T."

        self_del = await client.delete("/v1/users/boris", headers=boris_headers)
        assert self_del.status_code == 409, self_del.text

        del_res = await client.delete("/v1/users/boris", headers=admin_headers)
        assert del_res.status_code == 204, del_res.text
        assert (await client.get("/v1/auth/me", headers=boris_headers)).status_code == 401


async def test_pinned_admin_token_cannot_patch_its_own_user(
    admin_app: FastAPI, engine: AsyncEngine, clean_tables: None
) -> None:
    """A-2: a project-pinned token with `admin` scope cannot PATCH `/v1/users/{username}`."""
    _ = clean_tables
    admin_headers = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        r = await client.post(
            "/v1/users",
            headers=admin_headers,
            json={
                "username": "alice",
                "email": "alice@example.com",
                "password": "alice-initial-password-123",
                "is_superadmin": True,
                "project": "default",
                "role": "owner",
            },
        )
        assert r.status_code == 201, r.text

        login_res = await client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": "alice-initial-password-123"},
        )
        assert login_res.status_code == 200, login_res.text
        alice_session = _bearer(login_res.cookies["ff_session"])

        tok_res = await client.post(
            "/v1/auth/tokens",
            headers=alice_session,
            json={"name": "alice-ci-default", "project": "default", "scopes": ["admin"]},
        )
        assert tok_res.status_code == 201, tok_res.text
        pinned_headers = _bearer(tok_res.json()["token"])

        async with AsyncSession(engine) as session:
            before = await session.scalar(select(User).where(User.username == "alice"))
            assert before is not None
            before_hash = before.password_hash
            before_email = before.email

        patch_res = await client.patch(
            "/v1/users/alice",
            headers=pinned_headers,
            json={"password": "attacker-password-999", "email": "attacker@evil.example"},
        )
        assert patch_res.status_code == 403, patch_res.text

        async with AsyncSession(engine) as session:
            after = await session.scalar(select(User).where(User.username == "alice"))
            assert after is not None
            assert after.password_hash == before_hash
            assert after.email == before_email


async def test_update_me_requires_current_password_and_revokes_other_sessions(
    admin_app: FastAPI, clean_tables: None
) -> None:
    """A-35: `PATCH /v1/auth/me` requires `current_password` and revokes sibling `admin_tokens`."""
    _ = clean_tables
    admin_headers_1 = _bearer(await login_admin(admin_app))
    admin_headers_2 = _bearer(await login_admin(admin_app))

    async with client_for(admin_app, base_url="https://testserver") as client:
        assert (await client.get("/v1/auth/me", headers=admin_headers_1)).status_code == 200
        assert (await client.get("/v1/auth/me", headers=admin_headers_2)).status_code == 200

        created = await client.post(
            "/v1/users",
            headers=admin_headers_1,
            json={
                "username": "maya",
                "email": "maya@example.com",
                "password": "maya-password-111",
                "project": "default",
                "role": "operator",
            },
        )
        assert created.status_code == 201, created.text

        m1 = await client.post(
            "/v1/auth/login", json={"username": "maya", "password": "maya-password-111"}
        )
        m2 = await client.post(
            "/v1/auth/login", json={"username": "maya", "password": "maya-password-111"}
        )
        assert m1.status_code == 200 and m2.status_code == 200
        maya_s1 = _bearer(m1.cookies["ff_session"])
        maya_s2 = _bearer(m2.cookies["ff_session"])
        assert (await client.get("/v1/auth/me", headers=maya_s2)).status_code == 200

        no_curr = await client.patch(
            "/v1/auth/me",
            headers=maya_s1,
            json={"password": "attacker-password-999"},
        )
        assert no_curr.status_code == 400, no_curr.text
        assert (await client.get("/v1/auth/me", headers=maya_s2)).status_code == 200

        bypass_users = await client.patch(
            "/v1/users/maya",
            headers=maya_s1,
            json={"password": "attacker-password-999"},
        )
        assert bypass_users.status_code == 400, bypass_users.text
        assert (await client.get("/v1/auth/me", headers=maya_s2)).status_code == 200

        bad_curr = await client.patch(
            "/v1/auth/me",
            headers=maya_s1,
            json={
                "current_password": "wrong-password-000",
                "password": "attacker-password-999",
            },
        )
        assert bad_curr.status_code == 401, bad_curr.text
        assert (await client.get("/v1/auth/me", headers=maya_s2)).status_code == 200

        ok_change = await client.patch(
            "/v1/auth/me",
            headers=maya_s1,
            json={
                "current_password": "maya-password-111",
                "password": "maya-password-222",
            },
        )
        assert ok_change.status_code == 200, ok_change.text
        assert (await client.get("/v1/auth/me", headers=maya_s1)).status_code == 200
        assert (await client.get("/v1/auth/me", headers=maya_s2)).status_code == 401
        assert (
            await client.post(
                "/v1/auth/login", json={"username": "maya", "password": "maya-password-111"}
            )
        ).status_code == 401
        assert (
            await client.post(
                "/v1/auth/login", json={"username": "maya", "password": "maya-password-222"}
            )
        ).status_code == 200

        admin_no_curr = await client.patch(
            "/v1/auth/me",
            headers=admin_headers_1,
            json={"password": "attacker-admin-pw-999"},
        )
        assert admin_no_curr.status_code == 400, admin_no_curr.text
        assert (await client.get("/v1/auth/me", headers=admin_headers_2)).status_code == 200

        admin_ok = await client.patch(
            "/v1/auth/me",
            headers=admin_headers_1,
            json={"current_password": TEST_PASSWORD, "password": "new-admin-password-789"},
        )
        assert admin_ok.status_code == 200, admin_ok.text
        assert (await client.get("/v1/auth/me", headers=admin_headers_1)).status_code == 200
        assert (await client.get("/v1/auth/me", headers=admin_headers_2)).status_code == 401

        admin_users_bypass = await client.patch(
            "/v1/users/admin",
            headers=admin_headers_1,
            json={"password": "bypassed-admin-pw-999"},
        )
        assert admin_users_bypass.status_code == 400, admin_users_bypass.text


async def test_firebase_and_reset_password_rate_limiting_is_atomic(
    admin_app: FastAPI,
    clean_tables: None,
    fb_env: FbEnv,
) -> None:
    """A-35: `/v1/auth/firebase` and `/v1/auth/reset-password` use atomic limiter reservations."""
    _ = (clean_tables, fb_env)
    admin_app.state.login_limiter = FixedWindowLimiter(per_key=2, per_global=10, window_s=60)

    async with client_for(admin_app, base_url="https://testserver") as client:
        r1 = await client.post("/v1/auth/firebase", json={"id_token": "x" * 32})
        r2 = await client.post(
            "/v1/auth/reset-password",
            json={"oob_code": "bad-oob-code-1234", "new_password": "new-password-1234"},
        )
        assert r1.status_code == 401, r1.text
        assert r2.status_code == 401, r2.text

        r3 = await client.post("/v1/auth/firebase", json={"id_token": "x" * 32})
        r4 = await client.post(
            "/v1/auth/reset-password",
            json={"oob_code": "bad-oob-code-1234", "new_password": "new-password-1234"},
        )
        assert r3.status_code == 429, r3.text
        assert r4.status_code == 429, r4.text
