"""Firebase Authentication integration (Invite-Only Email + Password & ID Token Exchange).

Uses:
1. `google.oauth2.id_token.verify_firebase_token` (bundled with `google-auth` via
   `google-cloud-storage`) to verify RS256 Firebase ID tokens against Google's public
   x509 certificates without requiring a service-account private key.
2. Firebase Identity Toolkit REST (`accounts:signInWithPassword` and
   `accounts:sendOobCode`) over HTTPS using `FIREBASE_WEB_API_KEY` so operators can
   sign in with email + password directly on the Fleetforge login screen and receive
   Firebase password-setup / reset emails when invited.
"""

from __future__ import annotations

import asyncio
import json
import logging
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


class FirebaseAuthError(Exception):
    """Unrecoverable or transient upstream error talking to Firebase Auth."""


class FirebaseInvalidCredentials(FirebaseAuthError):
    """The email/password or Firebase ID token was rejected by Firebase."""


@dataclass(frozen=True, slots=True)
class FirebaseIdentity:
    """Verified identity extracted from a Firebase Auth response or ID token."""

    uid: str
    email: str
    email_verified: bool = True
    display_name: str | None = None


class FirebaseAuthClient(Protocol):
    """Injectable interface for Firebase Auth operations (faked in unit tests)."""

    async def verify_id_token(self, token: str, project_id: str) -> FirebaseIdentity:
        """Verify an RS256 Firebase ID token for `project_id`."""
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
        if exc.code in (400, 401, 403) and any(
            code in msg
            for code in (
                "INVALID_LOGIN_CREDENTIALS",
                "INVALID_PASSWORD",
                "EMAIL_NOT_FOUND",
                "USER_DISABLED",
                "INVALID_EMAIL",
                "MISSING_PASSWORD",
            )
        ):
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

    uid = str(claims.get("sub") or claims.get("user_id") or "").strip()
    email = str(claims.get("email") or "").strip().lower()
    if not uid or not email:
        raise FirebaseInvalidCredentials("Firebase ID token is missing uid or email claim")
    display_name = claims.get("name")
    return FirebaseIdentity(
        uid=uid,
        email=email,
        email_verified=bool(claims.get("email_verified", False)),
        display_name=str(display_name).strip() if display_name else None,
    )


class LiveFirebaseAuthClient:
    """Production Firebase Auth client backed by `google-auth` + Identity Toolkit REST."""

    async def verify_id_token(self, token: str, project_id: str) -> FirebaseIdentity:
        return await asyncio.to_thread(_verify_token_sync, token, project_id)

    async def sign_in_with_password(
        self,
        email: str,
        password: str,
        *,
        api_key: str,
        project_id: str | None = None,
    ) -> FirebaseIdentity:
        data = await asyncio.to_thread(
            _post_identity_toolkit,
            "accounts:signInWithPassword",
            api_key,
            {
                "email": email.strip().lower(),
                "password": password,
                "returnSecureToken": True,
            },
        )
        id_tok = str(data.get("idToken") or "")
        if project_id and id_tok:
            return await self.verify_id_token(id_tok, project_id)
        uid = str(data.get("localId") or "").strip()
        resolved_email = str(data.get("email") or email).strip().lower()
        if not uid or not resolved_email:
            raise FirebaseInvalidCredentials("Firebase sign-in returned no uid")
        display_name = str(data.get("displayName") or "").strip() or None
        return FirebaseIdentity(
            uid=uid,
            email=resolved_email,
            email_verified=True,
            display_name=display_name,
        )

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
