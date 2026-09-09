"""Request/response models for the API.

Kept separate from the routers so R0-be-2/4/5 add theirs here rather than growing a
schema section inside each router.
"""

import datetime as dt
import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fleetforge.auth.enrollment import EnrollmentTokenStatus
from fleetforge.auth.tokens import MAX_TOKEN_LENGTH
from fleetforge.db.models import PowerClass
from fleetforge.identity import is_valid_device_id


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


# ---------------------------------------------------------------------------
# Device enrollment (R0-be-4)
# ---------------------------------------------------------------------------


class EnrollRequest(BaseModel):
    """The device-facing enrollment body. **Flat, and it stays flat forever.**

    `spec/device-protocol.md` → *Enrolment happens over HTTPS, not MQTT*, step 2:
    `POST https://…/v1/enroll { token, <the announce identity payload> }` — so
    `{"token": "ffe_…", "device_id": "…", "platform_type": "…", …}`, **not**
    `{"token": …, "identity": {…}}`. The R0 agent is flash-baked and speaks this
    until someone physically retrieves the board; a nested body is a recall.

    `extra="ignore"` and **no field added later may ever be required**
    (*Evolution rules*: additive changes only, both sides ignore what they do not
    know).

    Every check here runs *before* anything touches the database, which is the point:
    `db/models.py::PowerClass` — "R0-be-4 rejects an unknown `power_class` with 400
    before burning the enrollment token." A burn followed by a CHECK violation is a
    token destroyed by a firmware typo, and the board then needs a re-flash to get a
    new one. The DB CHECKs stay the backstop, never the gate.
    """

    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)

    device_id: str
    platform_type: str = Field(min_length=1, max_length=64)
    # TEXT column, no PG enum: a board on a link nobody has invented yet still enrolls.
    link_type: str = Field(min_length=1, max_length=32)
    power_class: str
    # Stored, never rejected — "the server must tolerate agents it cannot update".
    proto: int = 1
    fw_version: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)
    expected_wake_interval_s: int | None = Field(default=None, gt=0)
    parent_device_id: str | None = None
    partition_layout: str | None = Field(default=None, max_length=64)
    ota_slot_size: int | None = Field(default=None, gt=0)
    capabilities: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("device_id", "parent_device_id")
    @classmethod
    def _canonical_device_id(cls, value: str | None) -> str | None:
        """Reject a non-canonical id; never normalise one.

        `mqtt_username` **is** `device_id`, and the two `%u` pattern ACLs are the
        entire fleet authz. Lowercasing here would mean the string the caller sent and
        the string the ACL binds to are not obviously the same string.
        """
        if value is None or is_valid_device_id(value):
            return value
        raise ValueError("device_id must be 12 lowercase hex digits (the eFuse MAC)")

    @field_validator("power_class")
    @classmethod
    def _known_power_class(cls, value: str) -> str:
        """Derived presence is only *defined* for `always_on` / `sleepy` (DB CHECK)."""
        if value not in set(PowerClass):
            raise ValueError(f"power_class must be one of {sorted(PowerClass)}")
        return value

    @model_validator(mode="after")
    def _sleepy_needs_an_interval(self) -> Self:
        """The `sleepy_wake_interval` CHECK, applied before the burn rather than after.

        A sleepy board with no wake interval makes `2.5 × expected_wake_interval_s`
        undefined, so it would never appear offline in the dashboard.
        """
        if self.power_class == PowerClass.SLEEPY and self.expected_wake_interval_s is None:
            raise ValueError("power_class=sleepy requires a positive expected_wake_interval_s")
        return self


class EnrollResponse(BaseModel):
    """Exactly the three fields `spec/device-protocol.md` step 4 promises.

    `mqtt_password` exists in this object and nowhere else: not in Postgres, not in a
    log line, not in the dynsec store (which keeps a hash). Losing it means
    re-enrolling — see `config.enroll_retry_window_s`.

    Broker host and port are deliberately absent: they are baked at flash time
    (`spec/flows.md` Flow 1 step 4). Adding them later would be additive.
    """

    device_id: str
    # == device_id. The `%u` pattern ACLs bind to the MQTT username, so this is a
    # security control and not a convenience — see `broker/provisioner.py`.
    mqtt_username: str
    mqtt_password: str


# ---------------------------------------------------------------------------
# Devices (R0-be-5)
# ---------------------------------------------------------------------------


class DeviceSummary(BaseModel):
    """One enrolled board, as the fleet view sees it.

    **`online` is computed on read** by `fleetforge.presence.is_online`, never stored
    and never sent as ingredients: `presence_reported` is deliberately absent, so no
    client can re-derive the rule and disagree with the server about a sleepy board
    that has simply stopped waking up.

    Built field by field with `from_attributes` off, same as `EnrollmentTokenSummary`
    — the guard that stops a future ORM column leaking into a response.
    """

    device_id: str
    name: str | None
    group_id: uuid.UUID | None
    platform_type: str
    fw_version: str | None
    agent_version: str | None
    link_type: str
    power_class: str
    expected_wake_interval_s: int | None
    parent_device_id: str | None
    partition_layout: str | None
    ota_slot_size: int | None
    capabilities: list[str]
    last_seen: dt.datetime | None
    enrolled_at: dt.datetime
    # NULL until the broker credential exists — the honest reconcile list R0-sec-1
    # works from (`WHERE broker_provisioned_at IS NULL`).
    broker_provisioned_at: dt.datetime | None
    online: bool


class DeviceList(BaseModel):
    """An envelope, not a bare array, so a cursor can be added without a break."""

    devices: list[DeviceSummary]
