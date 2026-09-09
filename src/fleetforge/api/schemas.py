"""Request/response models for the API.

Kept separate from the routers so R0-be-2/4/5 add theirs here rather than growing a
schema section inside each router.
"""

import datetime as dt
import uuid

from pydantic import BaseModel, Field


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
