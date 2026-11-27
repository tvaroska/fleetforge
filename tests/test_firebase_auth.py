"""Unit and Port-conformance tests for `fleetforge.auth.firebase` (`S0-sec-1`, `T-11`).

Exercises `LiveFirebaseAuthClient` against an injectable `httpx.MockTransport` Identity
Toolkit bridge (zero private `_post_identity_toolkit` monkeypatches) and verifies behavioral
Port parity between `LiveFirebaseAuthClient` and `tests.doubles.FakeFirebaseAuthClient`.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from typing import Any

import httpx
import pytest

from fleetforge.auth import firebase as fb
from fleetforge.auth.firebase import (
    FirebaseAuthClient,
    FirebaseAuthError,
    FirebaseEmailExists,
    FirebaseIdentity,
    FirebaseInvalidCredentials,
    LiveFirebaseAuthClient,
)
from tests.doubles import FakeFirebaseAuthClient

PROJECT = "sites-470716"
API_KEY = "fake-web-api-key"
TOKEN = "fake-id-token-0123456789"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "fb-uid-001",
        "email": "Carol@Example.com",
        "email_verified": True,
        "name": "Carol",
        "auth_time": now - 30,
        "iat": now - 10,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


class MockIdentityToolkit:
    """Drives `LiveFirebaseAuthClient`'s HTTP transport via `httpx.MockTransport`."""

    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[int, dict[str, Any]] | Exception]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def enqueue(
        self,
        endpoint: str,
        body: dict[str, Any] | Exception,
        *,
        status_code: int = 200,
    ) -> None:
        item: tuple[int, dict[str, Any]] | Exception = (
            body if isinstance(body, Exception) else (status_code, body)
        )
        self.routes.setdefault(endpoint, []).append(item)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        self.calls.append((endpoint, payload))
        queue = self.routes.get(endpoint) or []
        if not queue:
            raise httpx.ConnectError(f"unexpected endpoint {endpoint!r}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        code, body = item
        return httpx.Response(code, json=body)

    def urlopen(self, req: urllib.request.Request, timeout: float | None = None) -> io.BytesIO:
        _ = timeout
        raw_body = bytes(req.data) if isinstance(req.data, (bytes, bytearray, memoryview)) else b""
        try:
            resp = self._client.request(
                req.get_method(),
                req.full_url,
                content=raw_body,
                headers=dict(req.header_items()),
            )
        except httpx.HTTPError as exc:
            raise OSError(str(exc)) from exc
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                resp.status_code,
                "HTTP Error",
                Message(),
                io.BytesIO(resp.content),
            )
        return io.BytesIO(resp.content)


@pytest.fixture
def toolkit(monkeypatch: pytest.MonkeyPatch) -> MockIdentityToolkit:
    bridge = MockIdentityToolkit()
    monkeypatch.setattr(urllib.request, "urlopen", bridge.urlopen)
    return bridge


@pytest.fixture
def set_token_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]]:
    seen: list[tuple[str, str | None]] = []
    state: dict[str, Any] = {"value": _claims()}

    def _verify(token: str, _request: object, audience: str | None = None) -> dict[str, Any]:
        seen.append((token, audience))
        current = state["value"]
        if isinstance(current, Exception):
            raise current
        return dict(current)

    monkeypatch.setattr(fb.google_id_token, "verify_firebase_token", _verify)

    def _configure(claims_or_exc: dict[str, Any] | Exception) -> list[tuple[str, str | None]]:
        state["value"] = claims_or_exc
        return seen

    return _configure


def test_identity_email_verified_defaults_to_false() -> None:
    assert FirebaseIdentity(uid="u", email="e@example.com").email_verified is False


def test_good_claims_yield_identity_with_auth_time(
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    claims = _claims()
    seen = set_token_claims(claims)
    ident = fb._verify_token_sync(TOKEN, PROJECT)
    assert seen == [(TOKEN, PROJECT)]
    assert ident.uid == "fb-uid-001"
    assert ident.email == "carol@example.com"
    assert ident.email_verified is True
    assert ident.auth_time == claims["auth_time"]
    assert ident.display_name == "Carol"


def test_unverified_and_missing_email_verified_claims_stay_false(
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    set_token_claims(_claims(email_verified=False))
    assert fb._verify_token_sync(TOKEN, PROJECT).email_verified is False

    missing = _claims()
    del missing["email_verified"]
    set_token_claims(missing)
    assert fb._verify_token_sync(TOKEN, PROJECT).email_verified is False


@pytest.mark.parametrize(
    "iss",
    [
        "https://securetoken.google.com/some-other-project",
        "https://accounts.google.com",
        None,
    ],
)
def test_wrong_issuer_is_refused(
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
    iss: str | None,
) -> None:
    set_token_claims(_claims(iss=iss))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


def test_missing_or_future_auth_time_is_refused(
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    no_auth_time = _claims()
    del no_auth_time["auth_time"]
    set_token_claims(no_auth_time)
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)

    set_token_claims(_claims(auth_time=int(time.time()) + 3600))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


@pytest.mark.parametrize("sub", ["", "   ", "x" * 129, None, 12345])
def test_bad_sub_is_refused(
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
    sub: object,
) -> None:
    set_token_claims(_claims(sub=sub))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


def test_google_verification_failure_is_invalid(
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    set_token_claims(ValueError("Token expired"))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


@pytest.mark.port
@pytest.mark.parametrize("kind", ["fake", "live"])
async def test_port_check_revoked_without_api_key_fails_closed(
    kind: str,
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    set_token_claims(_claims())
    client: FirebaseAuthClient = (
        FakeFirebaseAuthClient() if kind == "fake" else LiveFirebaseAuthClient()
    )
    with pytest.raises(FirebaseAuthError) as excinfo:
        await client.verify_id_token(TOKEN, PROJECT, api_key=None, check_revoked=True)
    assert type(excinfo.value) is FirebaseAuthError

    with pytest.raises(FirebaseAuthError) as default_exc:
        await client.verify_id_token(TOKEN, PROJECT)
    assert type(default_exc.value) is FirebaseAuthError


@pytest.mark.port
@pytest.mark.parametrize("kind", ["fake", "live"])
async def test_port_verify_id_token_disabled_and_revocation_parity(
    kind: str,
    toolkit: MockIdentityToolkit,
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    auth_time = int(time.time()) - 600
    set_token_claims(_claims(auth_time=auth_time))

    if kind == "fake":
        fake = FakeFirebaseAuthClient()
        fake.register(
            email="carol@example.com",
            password="pw",
            uid="fb-uid-001",
            id_token=TOKEN,
            auth_time=auth_time,
            valid_since=auth_time - 60,
        )
        ok_ident = await fake.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
        assert ok_ident.email_verified is True

        fake.valid_since["carol@example.com"] = auth_time + 60
        with pytest.raises(FirebaseInvalidCredentials):
            await fake.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)

        fake.valid_since.pop("carol@example.com", None)
        fake.disabled_emails.add("carol@example.com")
        with pytest.raises(FirebaseInvalidCredentials):
            await fake.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
        return

    live = LiveFirebaseAuthClient()
    toolkit.enqueue(
        "accounts:lookup",
        {
            "users": [
                {
                    "localId": "fb-uid-001",
                    "emailVerified": True,
                    "validSince": str(auth_time - 60),
                }
            ]
        },
    )
    ok_ident = await live.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
    assert ok_ident.email_verified is True
    assert toolkit.calls == [("accounts:lookup", {"idToken": TOKEN})]

    toolkit.routes.clear()
    toolkit.enqueue(
        "accounts:lookup",
        {
            "users": [
                {
                    "localId": "fb-uid-001",
                    "emailVerified": True,
                    "validSince": str(auth_time + 60),
                }
            ]
        },
    )
    with pytest.raises(FirebaseInvalidCredentials):
        await live.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)

    toolkit.routes.clear()
    toolkit.enqueue(
        "accounts:lookup",
        {"users": [{"localId": "fb-uid-001", "emailVerified": True, "disabled": True}]},
    )
    with pytest.raises(FirebaseInvalidCredentials):
        await live.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)


async def test_live_lookup_edge_cases_and_check_revoked_false(
    toolkit: MockIdentityToolkit,
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    set_token_claims(_claims())
    client = LiveFirebaseAuthClient()

    for bad_lookup in (
        {"users": [{"localId": "fb-uid-001", "validSince": "soon"}]},
        {"users": [{"localId": "someone-else", "emailVerified": True}]},
        {},
    ):
        toolkit.routes.clear()
        toolkit.enqueue("accounts:lookup", bad_lookup)
        with pytest.raises(FirebaseInvalidCredentials):
            await client.verify_id_token(TOKEN, PROJECT, api_key=API_KEY)

    toolkit.calls.clear()
    toolkit.routes.clear()
    ident = await client.verify_id_token(TOKEN, PROJECT, check_revoked=False)
    assert ident.uid == "fb-uid-001"
    assert toolkit.calls == []


@pytest.mark.parametrize(
    ("claim", "lookup", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (True, None, False),
        (False, True, False),
    ],
)
async def test_email_verified_needs_claim_and_lookup(
    toolkit: MockIdentityToolkit,
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
    claim: bool,
    lookup: bool | None,
    expected: bool,
) -> None:
    set_token_claims(_claims(email_verified=claim))
    account: dict[str, Any] = {"localId": "fb-uid-001"}
    if lookup is not None:
        account["emailVerified"] = lookup
    toolkit.enqueue("accounts:lookup", {"users": [account]})
    ident = await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
    assert ident.email_verified is expected


async def test_sign_in_with_and_without_project_id_reads_verified_state(
    toolkit: MockIdentityToolkit,
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
) -> None:
    seen = set_token_claims(_claims())
    client = LiveFirebaseAuthClient()

    toolkit.enqueue(
        "accounts:signInWithPassword",
        {"idToken": TOKEN, "localId": "fb-uid-001", "email": "carol@example.com"},
    )
    toolkit.enqueue("accounts:lookup", {"users": [{"localId": "fb-uid-001"}]})
    unverified = await client.sign_in_with_password(
        "carol@example.com", "pw-123456", api_key=API_KEY, project_id=None
    )
    assert unverified.email_verified is False
    assert unverified.uid == "fb-uid-001"

    toolkit.routes.clear()
    toolkit.enqueue("accounts:signInWithPassword", {"idToken": TOKEN, "localId": "fb-uid-001"})
    toolkit.enqueue(
        "accounts:lookup",
        {
            "users": [
                {
                    "localId": "fb-uid-001",
                    "email": "carol@example.com",
                    "emailVerified": True,
                    "displayName": "Carol",
                }
            ]
        },
    )
    verified = await client.sign_in_with_password("carol@example.com", "pw-123456", api_key=API_KEY)
    assert verified.email_verified is True
    assert verified.display_name == "Carol"

    toolkit.routes.clear()
    toolkit.enqueue("accounts:signInWithPassword", {"idToken": TOKEN, "localId": "fb-uid-001"})
    toolkit.enqueue(
        "accounts:lookup",
        {"users": [{"localId": "fb-uid-001", "emailVerified": False}]},
    )
    with_proj = await client.sign_in_with_password(
        "carol@example.com", "pw-123456", api_key=API_KEY, project_id=PROJECT
    )
    assert seen == [(TOKEN, PROJECT)]
    assert with_proj.email_verified is False

    toolkit.routes.clear()
    toolkit.enqueue("accounts:signInWithPassword", {"localId": "fb-uid-001"})
    with pytest.raises(FirebaseInvalidCredentials):
        await client.sign_in_with_password("carol@example.com", "pw-123456", api_key=API_KEY)


@pytest.mark.port
@pytest.mark.parametrize("kind", ["fake", "live"])
async def test_port_create_account_and_password_reset_lifecycle(
    kind: str,
    toolkit: MockIdentityToolkit,
    set_token_claims: Callable[[dict[str, Any] | Exception], list[tuple[str, str | None]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    set_token_claims(_claims(email="erin@example.com"))

    if kind == "fake":
        fake = FakeFirebaseAuthClient()
        assert await fake.create_account("Erin@Example.com", api_key=API_KEY) is True
        assert await fake.create_account("erin@example.com", api_key=API_KEY) is False
        await fake.send_password_reset(
            "Erin@Example.com",
            api_key=API_KEY,
            continue_url="https://bingo.tvaroska.sk",
        )
        assert fake.reset_emails_sent == ["erin@example.com"]
        assert fake.continue_urls == ["https://bingo.tvaroska.sk"]
        ident = await fake.reset_password(
            "oob-for-erin@example.com",
            "new-strong-password-123",
            api_key=API_KEY,
            project_id=PROJECT,
        )
        assert ident.email == "erin@example.com"
        assert ident.email_verified is True
        with pytest.raises(FirebaseInvalidCredentials):
            await fake.reset_password(
                "oob-for-erin@example.com",
                "another-password-123",
                api_key=API_KEY,
            )
        return

    live = LiveFirebaseAuthClient()
    toolkit.enqueue("accounts:signUp", {"localId": "new-uid"})
    toolkit.enqueue(
        "accounts:signUp",
        {"error": {"message": "EMAIL_EXISTS"}},
        status_code=400,
    )
    assert await live.create_account("Erin@Example.com", api_key=API_KEY) is True
    assert await live.create_account("Erin@Example.com", api_key=API_KEY) is False

    signup_payload_1 = toolkit.calls[0][1]
    signup_payload_2 = toolkit.calls[1][1]
    assert signup_payload_1["email"] == "erin@example.com"
    assert signup_payload_1["returnSecureToken"] is False
    assert len(signup_payload_1["password"]) >= 32
    assert signup_payload_1["password"] != signup_payload_2["password"]
    assert signup_payload_1["password"] not in caplog.text

    toolkit.calls.clear()
    toolkit.routes.clear()
    toolkit.enqueue("accounts:sendOobCode", {"email": "erin@example.com"})
    toolkit.enqueue(
        "accounts:resetPassword",
        {"email": "Erin@Example.com", "requestType": "PASSWORD_RESET"},
    )
    toolkit.enqueue(
        "accounts:signInWithPassword",
        {"idToken": TOKEN, "localId": "fb-uid-001", "email": "erin@example.com"},
    )
    toolkit.enqueue(
        "accounts:lookup",
        {"users": [{"localId": "fb-uid-001", "email": "erin@example.com", "emailVerified": True}]},
    )
    await live.send_password_reset(
        "Erin@Example.com",
        api_key=API_KEY,
        continue_url="https://bingo.tvaroska.sk",
    )
    ident = await live.reset_password(
        "oob-secret-12345",
        "new-strong-password-123",
        api_key=API_KEY,
        project_id=PROJECT,
    )
    assert ident.email == "erin@example.com"
    assert ident.email_verified is True
    assert toolkit.calls[0] == (
        "accounts:sendOobCode",
        {
            "requestType": "PASSWORD_RESET",
            "email": "erin@example.com",
            "continueUrl": "https://bingo.tvaroska.sk",
        },
    )
    assert toolkit.calls[1] == (
        "accounts:resetPassword",
        {"oobCode": "oob-secret-12345", "newPassword": "new-strong-password-123"},
    )


def test_identity_toolkit_http_and_transport_error_mapping(
    toolkit: MockIdentityToolkit,
) -> None:
    toolkit.enqueue(
        "accounts:signUp",
        {"error": {"message": "EMAIL_EXISTS"}},
        status_code=400,
    )
    with pytest.raises(FirebaseEmailExists):
        fb._post_identity_toolkit("accounts:signUp", API_KEY, {"email": "e@example.com"})

    for code in (
        "INVALID_ID_TOKEN",
        "USER_NOT_FOUND",
        "TOKEN_EXPIRED",
        "EMAIL_NOT_FOUND",
        "INVALID_LOGIN_CREDENTIALS",
        "EXPIRED_OOB_CODE",
        "INVALID_OOB_CODE",
        "WEAK_PASSWORD",
    ):
        toolkit.routes.clear()
        toolkit.enqueue("accounts:lookup", {"error": {"message": code}}, status_code=400)
        with pytest.raises(FirebaseInvalidCredentials):
            fb._post_identity_toolkit("accounts:lookup", API_KEY, {"idToken": TOKEN})

    for upstream_code in ("OPERATION_NOT_ALLOWED", "ADMIN_ONLY_OPERATION", ""):
        toolkit.routes.clear()
        toolkit.enqueue(
            "accounts:signUp",
            {"error": {"message": upstream_code}},
            status_code=400,
        )
        with pytest.raises(FirebaseAuthError) as excinfo:
            fb._post_identity_toolkit("accounts:signUp", API_KEY, {})
        assert type(excinfo.value) is FirebaseAuthError

    toolkit.routes.clear()
    toolkit.enqueue("accounts:lookup", httpx.ConnectError("connection refused"))
    with pytest.raises(FirebaseAuthError) as net_exc:
        fb._post_identity_toolkit("accounts:lookup", API_KEY, {})
    assert type(net_exc.value) is FirebaseAuthError


def test_user_and_self_profile_schemas_normalize_fields() -> None:
    from fleetforge.api.schemas import SelfProfileUpdate, UserCreate, UserUpdate
    from fleetforge.db.models import ProjectRole

    created = UserCreate(
        username="boris",
        email="  Boris@Tvaroska.com ",
        display_name="  Boris Tvaroska  ",
        password="secret-password",
        is_superadmin=True,
        project="eink-todo",
        role=ProjectRole.OWNER,
    )
    assert created.email == "boris@tvaroska.com"
    assert created.display_name == "Boris Tvaroska"
    assert created.project == "eink-todo"
    assert created.role == ProjectRole.OWNER

    updated = UserUpdate(email="   ", display_name="   ", is_superadmin=True)
    assert updated.email is None
    assert updated.display_name is None
    assert updated.is_superadmin is True

    self_patch = SelfProfileUpdate(
        email="  Boris@Tvaroska.com ",
        display_name="  Boris Tvaroska ",
        password="new-password-123",
    )
    assert self_patch.email == "boris@tvaroska.com"
    assert self_patch.display_name == "Boris Tvaroska"
    assert self_patch.password == "new-password-123"
