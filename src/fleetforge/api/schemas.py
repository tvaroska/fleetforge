"""Request/response models for the API.

Kept separate from the routers so R0-be-2/4/5 add theirs here rather than growing a
schema section inside each router.
"""

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from fleetforge.auth.enrollment import EnrollmentTokenStatus


class LoginRequest(BaseModel):
    """The admin password. Never logged, never echoed."""

    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    """What login returns — deliberately **not** the token.

    `design/architecture.md`: the token is "returned in an HttpOnly / Secure /
    SameSite=Strict cookie *instead of the response body*", so browser XSS cannot
    read it. The expiry is here so the dashboard can schedule a re-login.
    """

    expires_at: dt.datetime


class MeResponse(BaseModel):
    """The identity behind the presented credential."""

    token_id: uuid.UUID
    subject: str
    scopes: list[str]
    expires_at: dt.datetime | None


# ---------------------------------------------------------------------------
# Enrollment tokens (R0-be-2)
# ---------------------------------------------------------------------------


class EnrollmentTokenCreate(BaseModel):
    """What an operator may choose when issuing a token.

    Only the group. The 24 h lifetime is a spec number applied from
    `config.enrollment_token_ttl_hours` and is deliberately not overridable per
    request — two places to violate one rule is one too many.
    """

    group_id: uuid.UUID | None = None  # None = ungrouped, the normal R0 case


class EnrollmentTokenIssued(BaseModel):
    """The issuance response — the **only** place the plaintext ever exists.

    Unlike the admin login token (which stays out of the body because it is a browser
    credential), this one must be in the body: a human copies it into the flasher's
    baked config (`spec/flows.md` Flow 1, steps 1 → 4). It is not stored, not logged
    and not re-derivable, so there is no endpoint that can ever show it again.
    """

    id: uuid.UUID
    token: str
    group_id: uuid.UUID | None
    expires_at: dt.datetime
    created_at: dt.datetime


class EnrollmentTokenSummary(BaseModel):
    """One row of the issuance history.

    **No `token` field and no `secret_hash` field, ever.** This response model is the
    last line of defence against a future `from_attributes=True` sweep that would
    happily serialize the hash straight out of the ORM row — which is why summaries
    are constructed field by field and `from_attributes` stays off.
    """

    id: uuid.UUID
    group_id: uuid.UUID | None
    status: EnrollmentTokenStatus
    created_at: dt.datetime
    expires_at: dt.datetime
    used_at: dt.datetime | None
    used_by_device_id: str | None
    revoked_at: dt.datetime | None


class EnrollmentTokenList(BaseModel):
    """An envelope, not a bare array, so a cursor can be added without a break."""

    tokens: list[EnrollmentTokenSummary]
