"""Unit tests for `fleetforge.auth.firebase` (S0-sec-1): token hardening, revocation,
fail-closed `email_verified`, server-side account creation and error mapping.

Pure unit tests: no database, no network. Module attributes are monkeypatched.
"""

import io
import time
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from fleetforge.auth import firebase as fb
from fleetforge.auth.firebase import (
    FirebaseAuthError,
    FirebaseEmailExists,
    FirebaseIdentity,
    FirebaseInvalidCredentials,
    LiveFirebaseAuthClient,
)

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


def _patch_google(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]) -> None:
    def fake_verify(token: str, request: object, audience: str | None = None) -> dict[str, Any]:
        assert audience == PROJECT
        return claims

    monkeypatch.setattr(fb.google_id_token, "verify_firebase_token", fake_verify)


class FakeToolkit:
    """Stands in for `_post_identity_toolkit`; dispatches on the endpoint name."""

    def __init__(self, responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((endpoint, payload))
        resp = self.responses[endpoint]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _verified_identity(**overrides: Any) -> FirebaseIdentity:
    values: dict[str, Any] = {
        "uid": "fb-uid-001",
        "email": "carol@example.com",
        "email_verified": True,
        "auth_time": int(time.time()) - 30,
    }
    values.update(overrides)
    return FirebaseIdentity(**values)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_identity_email_verified_defaults_to_false() -> None:
    assert FirebaseIdentity(uid="u", email="e@example.com").email_verified is False


# ---------------------------------------------------------------------------
# _verify_token_sync: claims google-auth does not check
# ---------------------------------------------------------------------------


def test_good_claims_yield_identity_with_auth_time(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims()
    _patch_google(monkeypatch, claims)
    ident = fb._verify_token_sync(TOKEN, PROJECT)
    assert ident.uid == "fb-uid-001"
    assert ident.email == "carol@example.com"
    assert ident.email_verified is True
    assert ident.auth_time == claims["auth_time"]
    assert ident.display_name == "Carol"


def test_unverified_claim_is_carried_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_google(monkeypatch, _claims(email_verified=False))
    assert fb._verify_token_sync(TOKEN, PROJECT).email_verified is False


def test_missing_email_verified_claim_is_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims()
    del claims["email_verified"]
    _patch_google(monkeypatch, claims)
    assert fb._verify_token_sync(TOKEN, PROJECT).email_verified is False


@pytest.mark.parametrize(
    "iss",
    [
        "https://securetoken.google.com/some-other-project",
        "https://accounts.google.com",
        None,
    ],
)
def test_wrong_issuer_is_refused(monkeypatch: pytest.MonkeyPatch, iss: str | None) -> None:
    _patch_google(monkeypatch, _claims(iss=iss))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


def test_missing_auth_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims()
    del claims["auth_time"]
    _patch_google(monkeypatch, claims)
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


def test_future_auth_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_google(monkeypatch, _claims(auth_time=int(time.time()) + 3600))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


@pytest.mark.parametrize("sub", ["", "   ", "x" * 129, None, 12345])
def test_bad_sub_is_refused(monkeypatch: pytest.MonkeyPatch, sub: object) -> None:
    _patch_google(monkeypatch, _claims(sub=sub))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


def test_google_verification_failure_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(token: str, request: object, audience: str | None = None) -> dict[str, Any]:
        raise ValueError("Token expired")

    monkeypatch.setattr(fb.google_id_token, "verify_firebase_token", boom)
    with pytest.raises(FirebaseInvalidCredentials):
        fb._verify_token_sync(TOKEN, PROJECT)


# ---------------------------------------------------------------------------
# LiveFirebaseAuthClient.verify_id_token: revocation via accounts:lookup
# ---------------------------------------------------------------------------


async def test_check_revoked_without_api_key_fails_closed_as_upstream_error() -> None:
    client = LiveFirebaseAuthClient()
    with pytest.raises(FirebaseAuthError) as excinfo:
        await client.verify_id_token(TOKEN, PROJECT, api_key=None, check_revoked=True)
    # Not "invalid credentials" (401): a configuration gap must become a 503.
    assert type(excinfo.value) is FirebaseAuthError


async def test_check_revoked_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    with pytest.raises(FirebaseAuthError) as excinfo:
        await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT)
    assert type(excinfo.value) is FirebaseAuthError


async def test_lookup_is_called_with_the_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    toolkit = FakeToolkit(
        {"accounts:lookup": {"users": [{"localId": "fb-uid-001", "emailVerified": True}]}}
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    ident = await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
    assert ident.email_verified is True
    assert toolkit.calls == [("accounts:lookup", {"idToken": TOKEN})]


async def test_disabled_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    toolkit = FakeToolkit(
        {
            "accounts:lookup": {
                "users": [{"localId": "fb-uid-001", "emailVerified": True, "disabled": True}]
            }
        }
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    with pytest.raises(FirebaseInvalidCredentials):
        await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)


async def test_token_older_than_valid_since_is_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_time = int(time.time()) - 600
    monkeypatch.setattr(
        fb, "_verify_token_sync", lambda t, p: _verified_identity(auth_time=auth_time)
    )
    toolkit = FakeToolkit(
        {
            "accounts:lookup": {
                "users": [
                    {
                        "localId": "fb-uid-001",
                        "emailVerified": True,
                        "validSince": str(auth_time + 60),
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    with pytest.raises(FirebaseInvalidCredentials):
        await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)


async def test_token_newer_than_valid_since_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_time = int(time.time()) - 600
    monkeypatch.setattr(
        fb, "_verify_token_sync", lambda t, p: _verified_identity(auth_time=auth_time)
    )
    toolkit = FakeToolkit(
        {
            "accounts:lookup": {
                "users": [
                    {
                        "localId": "fb-uid-001",
                        "emailVerified": True,
                        "validSince": str(auth_time - 60),
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    ident = await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
    assert ident.auth_time == auth_time


async def test_garbage_valid_since_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    toolkit = FakeToolkit(
        {"accounts:lookup": {"users": [{"localId": "fb-uid-001", "validSince": "soon"}]}}
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    with pytest.raises(FirebaseInvalidCredentials):
        await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)


async def test_lookup_uid_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    toolkit = FakeToolkit(
        {"accounts:lookup": {"users": [{"localId": "someone-else", "emailVerified": True}]}}
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    with pytest.raises(FirebaseInvalidCredentials):
        await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)


async def test_empty_lookup_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    monkeypatch.setattr(fb, "_post_identity_toolkit", FakeToolkit({"accounts:lookup": {}}))
    with pytest.raises(FirebaseInvalidCredentials):
        await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)


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
    monkeypatch: pytest.MonkeyPatch, claim: bool, lookup: bool | None, expected: bool
) -> None:
    monkeypatch.setattr(
        fb, "_verify_token_sync", lambda t, p: _verified_identity(email_verified=claim)
    )
    account: dict[str, Any] = {"localId": "fb-uid-001"}
    if lookup is not None:
        account["emailVerified"] = lookup
    monkeypatch.setattr(
        fb, "_post_identity_toolkit", FakeToolkit({"accounts:lookup": {"users": [account]}})
    )
    ident = await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, api_key=API_KEY)
    assert ident.email_verified is expected


async def test_check_revoked_false_skips_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_verify_token_sync", lambda t, p: _verified_identity())
    toolkit = FakeToolkit({})
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    await LiveFirebaseAuthClient().verify_id_token(TOKEN, PROJECT, check_revoked=False)
    assert toolkit.calls == []


# ---------------------------------------------------------------------------
# sign_in_with_password never invents email_verified
# ---------------------------------------------------------------------------


async def test_sign_in_without_project_id_reads_email_verified_from_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolkit = FakeToolkit(
        {
            "accounts:signInWithPassword": {
                "idToken": TOKEN,
                "localId": "fb-uid-001",
                "email": "carol@example.com",
            },
            # emailVerified absent -> unverified (regression: was hard-coded True).
            "accounts:lookup": {"users": [{"localId": "fb-uid-001", "email": "carol@example.com"}]},
        }
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    ident = await LiveFirebaseAuthClient().sign_in_with_password(
        "carol@example.com", "pw-123456", api_key=API_KEY, project_id=None
    )
    assert ident.email_verified is False
    assert ident.uid == "fb-uid-001"
    assert [c[0] for c in toolkit.calls] == ["accounts:signInWithPassword", "accounts:lookup"]


async def test_sign_in_without_project_id_verified_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolkit = FakeToolkit(
        {
            "accounts:signInWithPassword": {"idToken": TOKEN, "localId": "fb-uid-001"},
            "accounts:lookup": {
                "users": [
                    {
                        "localId": "fb-uid-001",
                        "email": "carol@example.com",
                        "emailVerified": True,
                        "displayName": "Carol",
                    }
                ]
            },
        }
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    ident = await LiveFirebaseAuthClient().sign_in_with_password(
        "carol@example.com", "pw-123456", api_key=API_KEY
    )
    assert ident.email_verified is True
    assert ident.display_name == "Carol"


async def test_sign_in_with_project_id_verifies_and_checks_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def fake_verify(token: str, project_id: str) -> FirebaseIdentity:
        seen.append((token, project_id))
        return _verified_identity()

    monkeypatch.setattr(fb, "_verify_token_sync", fake_verify)
    toolkit = FakeToolkit(
        {
            "accounts:signInWithPassword": {"idToken": TOKEN, "localId": "fb-uid-001"},
            "accounts:lookup": {"users": [{"localId": "fb-uid-001", "emailVerified": False}]},
        }
    )
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    ident = await LiveFirebaseAuthClient().sign_in_with_password(
        "carol@example.com", "pw-123456", api_key=API_KEY, project_id=PROJECT
    )
    assert seen == [(TOKEN, PROJECT)]
    assert ident.email_verified is False
    assert [c[0] for c in toolkit.calls] == ["accounts:signInWithPassword", "accounts:lookup"]


async def test_sign_in_without_id_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    toolkit = FakeToolkit({"accounts:signInWithPassword": {"localId": "fb-uid-001"}})
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    with pytest.raises(FirebaseInvalidCredentials):
        await LiveFirebaseAuthClient().sign_in_with_password(
            "carol@example.com", "pw-123456", api_key=API_KEY
        )


# ---------------------------------------------------------------------------
# create_account
# ---------------------------------------------------------------------------


async def test_create_account_success_returns_true(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    toolkit = FakeToolkit({"accounts:signUp": {"localId": "new-uid"}})
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    assert await LiveFirebaseAuthClient().create_account("Erin@Example.com", api_key=API_KEY)
    endpoint, payload = toolkit.calls[0]
    assert endpoint == "accounts:signUp"
    assert payload["email"] == "erin@example.com"
    assert payload["returnSecureToken"] is False
    password = payload["password"]
    assert isinstance(password, str) and len(password) >= 32
    assert password not in caplog.text


async def test_create_account_passwords_are_random(monkeypatch: pytest.MonkeyPatch) -> None:
    toolkit = FakeToolkit({"accounts:signUp": {}})
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    client = LiveFirebaseAuthClient()
    await client.create_account("a@example.com", api_key=API_KEY)
    await client.create_account("a@example.com", api_key=API_KEY)
    assert toolkit.calls[0][1]["password"] != toolkit.calls[1][1]["password"]


async def test_create_account_existing_email_returns_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    toolkit = FakeToolkit({"accounts:signUp": FirebaseEmailExists("exists")})
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    assert await LiveFirebaseAuthClient().create_account("e@example.com", api_key=API_KEY) is False
    assert toolkit.calls[0][1]["password"] not in caplog.text


async def test_create_account_other_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    toolkit = FakeToolkit({"accounts:signUp": FirebaseAuthError("HTTP 400")})
    monkeypatch.setattr(fb, "_post_identity_toolkit", toolkit)
    with pytest.raises(FirebaseAuthError):
        await LiveFirebaseAuthClient().create_account("e@example.com", api_key=API_KEY)
    assert toolkit.calls[0][1]["password"] not in caplog.text


# ---------------------------------------------------------------------------
# _post_identity_toolkit error mapping
# ---------------------------------------------------------------------------


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = io.BytesIO(('{"error":{"message":"' + message + '"}}').encode())
    return urllib.error.HTTPError(
        "https://identitytoolkit.googleapis.com/v1/x", code, "x", Message(), body
    )


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fake_urlopen(req: object, timeout: float | None = None) -> object:
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_email_exists_maps_to_firebase_email_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _http_error(400, "EMAIL_EXISTS"))
    with pytest.raises(FirebaseEmailExists):
        fb._post_identity_toolkit("accounts:signUp", API_KEY, {"email": "e@example.com"})


@pytest.mark.parametrize(
    "message",
    [
        "INVALID_ID_TOKEN",
        "USER_NOT_FOUND",
        "TOKEN_EXPIRED",
        "EMAIL_NOT_FOUND",
        "INVALID_LOGIN_CREDENTIALS",
    ],
)
def test_invalid_codes_map_to_invalid_credentials(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    _patch_urlopen(monkeypatch, _http_error(400, message))
    with pytest.raises(FirebaseInvalidCredentials):
        fb._post_identity_toolkit("accounts:lookup", API_KEY, {"idToken": TOKEN})


@pytest.mark.parametrize("message", ["OPERATION_NOT_ALLOWED", "ADMIN_ONLY_OPERATION", ""])
def test_other_errors_stay_upstream_errors(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    _patch_urlopen(monkeypatch, _http_error(400, message))
    with pytest.raises(FirebaseAuthError) as excinfo:
        fb._post_identity_toolkit("accounts:signUp", API_KEY, {})
    assert type(excinfo.value) is FirebaseAuthError


def test_unreachable_is_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, OSError("connection refused"))
    with pytest.raises(FirebaseAuthError) as excinfo:
        fb._post_identity_toolkit("accounts:lookup", API_KEY, {})
    assert type(excinfo.value) is FirebaseAuthError
