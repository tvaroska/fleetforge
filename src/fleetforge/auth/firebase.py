"""Firebase Authentication integration (Invite-Only Email + Password & ID Token Exchange).

Uses:
1. `google.oauth2.id_token.verify_firebase_token` (bundled with `google-auth` via
   `google-cloud-storage`) to verify RS256 Firebase ID tokens against Google's public
   x509 certificates without requiring a service-account private key. That call checks
   the signature, `exp`/`iat` and `aud`; `_verify_token_sync` adds the `iss`, `sub` and
   `auth_time` checks it leaves out.
2. Firebase Identity Toolkit REST over HTTPS using `FIREBASE_WEB_API_KEY`:
   `accounts:signInWithPassword` (email + password on the Fleetforge login screen),
   `accounts:lookup` (current account state: disabled, `validSince`, `emailVerified`,
   i.e. the revocation check `firebase_admin` would do with a service account),
   `accounts:signUp` (an invite creates the Firebase account server-side with a random
   password nobody knows) and `accounts:sendOobCode PASSWORD_RESET` (the setup email).

**The email must be verified.** Self sign-up is callable by anyone holding the public
web API key and cannot be disabled (the invite path above uses it), so an identity whose
email is not verified proves nothing about mailbox ownership and is refused at login
(`api/routers/auth.py::_resolve_invited_firebase_user`). `email_verified` is therefore
fail-closed: it defaults to `False` and is `True` only when both the token claim and the
live `accounts:lookup` say so (DECISIONS.md, 2026-10-19 S0-sec-1).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

_IDENTITY_TOOLKIT_BASE = "https://identitytoolkit.googleapis.com/v1"
_HTTP_TIMEOUT_S = 10.0
_ISSUER_PREFIX = "https://securetoken.google.com/"
_MAX_UID_LENGTH = 128
# Tolerance for `auth_time` slightly ahead of our clock (Google's clock vs ours).
_AUTH_TIME_SKEW_S = 60

_INVALID_CREDENTIAL_CODES = (
    "INVALID_LOGIN_CREDENTIALS",
    "INVALID_PASSWORD",
    "EMAIL_NOT_FOUND",
    "USER_DISABLED",
    "INVALID_EMAIL",
    "MISSING_PASSWORD",
    "INVALID_ID_TOKEN",
    "USER_NOT_FOUND",
    "TOKEN_EXPIRED",
)


class FirebaseAuthError(Exception):
    """Unrecoverable or transient upstream error talking to Firebase Auth."""


class FirebaseInvalidCredentials(FirebaseAuthError):
    """The email/password or Firebase ID token was rejected by Firebase."""


class FirebaseEmailExists(FirebaseAuthError):
    """`accounts:signUp` was refused because a Firebase account already has this email."""


@dataclass(frozen=True, slots=True)
class FirebaseIdentity:
    """Verified identity extracted from a Firebase Auth response or ID token.

    `email_verified` defaults to `False` on purpose: an identity whose mailbox
    ownership was not positively confirmed must never be treated as verified.
    """

    uid: str
    email: str
    email_verified: bool = False
    display_name: str | None = None
    auth_time: int | None = None


class FirebaseAuthClient(Protocol):
    """Injectable interface for Firebase Auth operations (faked in unit tests)."""

    async def verify_id_token(
        self,
        token: str,
        project_id: str,
        *,
        api_key: str | None = None,
        check_revoked: bool = True,
    ) -> FirebaseIdentity:
        """Verify an RS256 Firebase ID token for `project_id`.

        With `check_revoked` (the default) the account is also looked up live and the
        token refused if the account is disabled or the token predates `validSince`;
        that needs `api_key`, and its absence raises `FirebaseAuthError` (fail closed).
        """
        ...

    async def sign_in_with_password(
        self,
        email: str,
        password: str,
        *,
        api_key: str,
        project_id: str | None = None,
    ) -> FirebaseIdentity:
        """Verify `email` + `password` against Firebase Identity Toolkit."""
        ...

    async def create_account(self, email: str, *, api_key: str) -> bool:
        """Create a Firebase account for `email` with a random, discarded password.

        Returns `True` if created, `False` if an account with that email already exists.
        """
        ...

    async def send_password_reset(self, email: str, *, api_key: str) -> None:
        """Trigger Firebase's `PASSWORD_RESET` email for an invited user."""
        ...


def _post_identity_toolkit(endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{_IDENTITY_TOOLKIT_BASE}/{endpoint}?key={urllib.parse.quote(api_key, safe='')}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - fixed HTTPS Google Identity Toolkit endpoint
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310 - fixed HTTPS Google endpoint
            raw = resp.read().decode("utf-8")
            result: dict[str, Any] = json.loads(raw)
            return result
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8", errors="replace"))
            msg = str(err_body.get("error", {}).get("message", ""))
        except Exception:  # noqa: BLE001
            msg = ""
        if exc.code in (400, 401, 403) and "EMAIL_EXISTS" in msg:
            raise FirebaseEmailExists("a Firebase account with this email already exists") from None
        if exc.code in (400, 401, 403) and any(code in msg for code in _INVALID_CREDENTIAL_CODES):
            raise FirebaseInvalidCredentials("invalid email or password") from None
        raise FirebaseAuthError(f"Firebase Identity Toolkit returned HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001
        raise FirebaseAuthError("could not reach Firebase Identity Toolkit") from exc


def _verify_token_sync(token: str, project_id: str) -> FirebaseIdentity:
    try:
        claims: dict[str, Any] = google_id_token.verify_firebase_token(
            token,
            google_requests.Request(),
            audience=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise FirebaseInvalidCredentials("invalid or expired Firebase ID token") from exc

    # google-auth checks signature, exp/iat and aud only; the rest is ours.
    if claims.get("iss") != f"{_ISSUER_PREFIX}{project_id}":
        raise FirebaseInvalidCredentials("Firebase ID token has the wrong issuer")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip() or len(sub) > _MAX_UID_LENGTH:
        raise FirebaseInvalidCredentials("Firebase ID token has an invalid sub claim")
    auth_time = claims.get("auth_time")
    if not isinstance(auth_time, int) or isinstance(auth_time, bool):
        raise FirebaseInvalidCredentials("Firebase ID token is missing auth_time")
    if auth_time > int(time.time()) + _AUTH_TIME_SKEW_S:
        raise FirebaseInvalidCredentials("Firebase ID token auth_time is in the future")

    uid = sub.strip()
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise FirebaseInvalidCredentials("Firebase ID token is missing the email claim")
    display_name = claims.get("name")
    return FirebaseIdentity(
        uid=uid,
        email=email,
        email_verified=claims.get("email_verified") is True,
        display_name=str(display_name).strip() if display_name else None,
        auth_time=auth_time,
    )


def _lookup_account_sync(id_token: str, api_key: str) -> dict[str, Any]:
    """Return the live Firebase account (`users[0]` of `accounts:lookup`) for `id_token`."""
    data = _post_identity_toolkit("accounts:lookup", api_key, {"idToken": id_token})
    users = data.get("users")
    if not isinstance(users, list) or not users or not isinstance(users[0], dict):
        raise FirebaseInvalidCredentials("Firebase account lookup returned no user")
    account: dict[str, Any] = users[0]
    return account


def _apply_lookup(identity: FirebaseIdentity, account: dict[str, Any]) -> FirebaseIdentity:
    """Refuse a disabled or revoked account; `email_verified` = token claim AND live account."""
    if account.get("disabled"):
        raise FirebaseInvalidCredentials("Firebase account is disabled")
    local_id = account.get("localId")
    if local_id is not None and str(local_id) != identity.uid:
        raise FirebaseInvalidCredentials("Firebase account lookup returned a different uid")
    valid_since_raw = account.get("validSince")
    if valid_since_raw is not None:
        try:
            valid_since = int(str(valid_since_raw))
        except ValueError:
            raise FirebaseInvalidCredentials(
                "Firebase account has an unreadable validSince"
            ) from None
        if identity.auth_time is None or identity.auth_time < valid_since:
            raise FirebaseInvalidCredentials("Firebase ID token has been revoked")
    return dataclasses.replace(
        identity,
        email_verified=identity.email_verified and account.get("emailVerified") is True,
    )


def _identity_from_account(account: dict[str, Any], fallback_email: str) -> FirebaseIdentity:
    """Build an identity from a live `accounts:lookup` entry (no ID-token claims available)."""
    if account.get("disabled"):
        raise FirebaseInvalidCredentials("Firebase account is disabled")
    uid = str(account.get("localId") or "").strip()
    email = str(account.get("email") or fallback_email).strip().lower()
    if not uid or not email:
        raise FirebaseInvalidCredentials("Firebase account lookup returned no uid")
    display_name = str(account.get("displayName") or "").strip() or None
    return FirebaseIdentity(
        uid=uid,
        email=email,
        email_verified=account.get("emailVerified") is True,
        display_name=display_name,
    )


class LiveFirebaseAuthClient:
    """Production Firebase Auth client backed by `google-auth` + Identity Toolkit REST."""

    async def verify_id_token(
        self,
        token: str,
        project_id: str,
        *,
        api_key: str | None = None,
        check_revoked: bool = True,
    ) -> FirebaseIdentity:
        if check_revoked and not api_key:
            # Fail closed: never silently skip the revocation check.
            raise FirebaseAuthError("revocation check needs FIREBASE_WEB_API_KEY")
        identity = await asyncio.to_thread(_verify_token_sync, token, project_id)
        if not check_revoked or api_key is None:
            return identity
        account = await asyncio.to_thread(_lookup_account_sync, token, api_key)
        return _apply_lookup(identity, account)

    async def sign_in_with_password(
        self,
        email: str,
        password: str,
        *,
        api_key: str,
        project_id: str | None = None,
    ) -> FirebaseIdentity:
        normalized = email.strip().lower()
        data = await asyncio.to_thread(
            _post_identity_toolkit,
            "accounts:signInWithPassword",
            api_key,
            {
                "email": normalized,
                "password": password,
                "returnSecureToken": True,
            },
        )
        id_tok = str(data.get("idToken") or "")
        if not id_tok:
            raise FirebaseInvalidCredentials("Firebase sign-in returned no ID token")
        if project_id:
            return await self.verify_id_token(
                id_tok, project_id, api_key=api_key, check_revoked=True
            )
        # No project id to verify the token against: ask Firebase for the account
        # instead of inventing `email_verified`.
        account = await asyncio.to_thread(_lookup_account_sync, id_tok, api_key)
        return _identity_from_account(account, str(data.get("email") or normalized))

    async def create_account(self, email: str, *, api_key: str) -> bool:
        try:
            await asyncio.to_thread(
                _post_identity_toolkit,
                "accounts:signUp",
                api_key,
                {
                    "email": email.strip().lower(),
                    # Never stored or logged: the invitee sets their own via the reset email.
                    "password": secrets.token_urlsafe(32),
                    "returnSecureToken": False,
                },
            )
        except FirebaseEmailExists:
            return False
        return True

    async def send_password_reset(self, email: str, *, api_key: str) -> None:
        await asyncio.to_thread(
            _post_identity_toolkit,
            "accounts:sendOobCode",
            api_key,
            {
                "requestType": "PASSWORD_RESET",
                "email": email.strip().lower(),
            },
        )
