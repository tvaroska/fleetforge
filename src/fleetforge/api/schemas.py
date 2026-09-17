"""Request/response models for the API.

Kept separate from the routers so R0-be-2/4/5 add theirs here rather than growing a
schema section inside each router.
"""

import datetime as dt
import re
import uuid
from typing import Literal, Self

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
    # The newest deploy transaction's current state, or `None` for a board that has
    # never been deployed to. Rides on this response for the same reason `arrivals`
    # does — see `DeploySummary` and `routers/devices.py`.
    #
    # **Last, and defaulted**: `frontend/src/api.ts` mirrors this model "field for field
    # and in its order", and *Evolution rules* say an added field must not break a
    # client built before it existed.
    deploy: "DeploySummary | None" = None


class DeploySummary(BaseModel):
    """A board's newest deploy state, as the fleet view sees it. R1-fe-1.

    Built from `fleetforge.deploys.latest_deploys` — the module that owns
    `deploy_events` — and carried on `GET /v1/devices` rather than on an endpoint of
    its own, because the dashboard already re-reads that envelope on every `ff_events`
    hint (`DeviceList.arrivals`, same argument).

    `state` and `detail` are **device-controlled strings**. `DeployState` is advisory —
    `deploy_events.state` is TEXT with no CHECK, so an agent newer than this server can
    report a state nobody here has heard of and it is recorded and returned as itself.
    A renderer treats both as text and never switches exhaustively on `state`.

    `is_terminal` is the **server's** answer, stored at write time from
    `TERMINAL_DEPLOY_STATES`. It is sent precisely so no client keeps a second copy of
    that vocabulary; two definitions of "terminal" is the failure `deploys.py` warns
    about, and it would surface as a KPI that disagrees with the dashboard.

    `pct` is **not a progress feed.** `deploy_events` is a log of transitions and the
    writer keeps the first `pct` it saw for a repeated state, so an agent that publishes
    `downloading` once (ours does) reports one number for the whole download. Render it
    as text; a bar driven by it would sit still and read as a hang.
    """

    # NULL only for a row written before `cmd_id` was known — the column is nullable, so
    # the field is too rather than the API pretending otherwise.
    cmd_id: str | None
    state: str
    at: dt.datetime
    is_terminal: bool
    # The version this transaction intends to install, and the one it replaces. Copied
    # off the `requested` row by the writer; both NULL for a transaction this server has
    # no `requested` row for (a board replaying a command from before a DB rebuild).
    artifact_version: str | None
    from_version: str | None
    pct: int | None
    detail: str | None


class ArrivalSummary(BaseModel):
    """A board that is *arriving* — it has reported a boot stage but is not in the fleet yet.

    `device_id` here may name a board that has no `devices` row at all, which is the
    whole point: this is what the dashboard shows instead of a gap between "flashed"
    and "online" (S0-fw-1).

    `stalled` is derived on read by `fleetforge.progress.latest_progress` from the age
    of `at`, exactly as `DeviceSummary.online` is derived by `presence.is_online` — and
    for the same reason, `progress_stall_s` is not sent so no client can re-derive it.

    `stage` and `detail` are **device-controlled strings**, bounded in length by
    `ProgressReport` and by nothing else. A renderer must treat them as text.
    """

    device_id: str
    stage: str
    detail: str | None
    at: dt.datetime
    stalled: bool


class DeviceList(BaseModel):
    """An envelope, not a bare array, so a cursor can be added without a break."""

    devices: list[DeviceSummary]
    # Rides on the fleet read rather than getting its own endpoint: the dashboard's
    # hint→re-read engine already re-reads this on every `ff_events` frame, so
    # arrivals need no second fetch and no poll of their own. Defaulted, because a
    # client built against R0 must not break on the added field (*Evolution rules*).
    arrivals: list[ArrivalSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Device boot/enrol progress (S0-fw-1)
# ---------------------------------------------------------------------------

# Device-controlled free text. Long enough for a real reason ("esp-tls handshake
# failed: 0x8010"), short enough that 20 rows per board is not a storage question.
MAX_PROGRESS_DETAIL = 200

# The shape of a stage, and deliberately NOT its vocabulary: `ProgressStage` is
# advisory and the column has no CHECK, because a flash-baked agent the server can
# never update must still be able to say something the server has not heard of.
STAGE_PATTERN = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")


class ProgressReport(BaseModel):
    """The body of `POST /v1/device-progress`. Flat, and it stays flat, like `EnrollRequest`.

    The credential is the `ffe_` enrollment token the board already holds — a
    pre-enrolment board has no MQTT credential and nothing else to prove itself with.
    The endpoint **verifies it and never burns it** (`routers/progress.py`).
    """

    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)
    device_id: str
    stage: str = Field(min_length=1, max_length=32)
    detail: str | None = Field(default=None, max_length=MAX_PROGRESS_DETAIL)

    @field_validator("device_id")
    @classmethod
    def _canonical_device_id(cls, value: str) -> str:
        """Same rule as `EnrollRequest`: reject, never normalise."""
        if is_valid_device_id(value):
            return value
        raise ValueError("device_id must be 12 lowercase hex digits (the eFuse MAC)")

    @field_validator("stage")
    @classmethod
    def _stage_shape(cls, value: str) -> str:
        """Bound the shape so an unknown stage is storable but a hostile one is not."""
        if STAGE_PATTERN.match(value):
            return value
        raise ValueError("stage must match [a-z][a-z0-9_]{0,31}")

    @field_validator("detail")
    @classmethod
    def _printable_detail(cls, value: str | None) -> str | None:
        """No control characters.

        This string is written to the API log and rendered in the dashboard. A device
        that can inject a newline can forge a log line, and this is the one field on
        the endpoint that an unauthenticated-until-verified caller fully controls.
        """
        if value is None:
            return value
        if any(ch < " " or ch == "\x7f" for ch in value):
            raise ValueError("detail must not contain control characters")
        return value


# ---------------------------------------------------------------------------
# Artifact upload (R1-be-1)
# ---------------------------------------------------------------------------


class ArtifactUploaded(BaseModel):
    """What `POST /v1/artifact` returns — the digest is the artifact's identity.

    `sha256` is what the caller needs and the one field it cannot compute a second
    spelling of: R1-be-2 puts it in the `stage` payload and R1-be-3 serves the bytes
    from it. There is deliberately **no `url` and no `storage_key`** here. The key is
    `blob_key(sha256)`, a pure function of this field (`storage/blobs.py`), and a URL
    is a short-lived signed credential that belongs to the deploy that needs it, not
    to a successful upload.

    `created` distinguishes the two success cases a content-addressed store collapses:
    `True` is a new label, `False` is an idempotent re-upload of bytes already stored
    under this exact `(target, version)`. The status code says the same thing (201 vs
    200); the field is here so a client does not have to parse it out of one.
    """

    sha256: str
    size_bytes: int
    target: str
    version: str
    partition_layout: str
    created: bool


class ArtifactSummary(BaseModel):
    """One deployable label — a row of `GET /v1/artifact`. R1-fe-1.

    A label over a digest, joined to the digest's metadata: `(target, version)` comes
    from `artifact_versions`, `size_bytes`/`partition_layout`/`kind` from `artifacts`.
    Two labels over one blob are two rows with one `sha256`, which is the shape a
    content-addressed store makes ordinary and a `version` column would have made a
    primary-key collision (`db/models.py::ArtifactVersion`).

    **There is no `url`, for the same reason `ArtifactUploaded` has none.** A download
    link is a short-lived bearer credential minted per deploy, and a list endpoint that
    handed one out would mint credentials nobody asked for.

    `created_at` is the label's, not the blob's: re-tagging old bytes under a new
    version is a new thing to deploy, and the list is ordered by it.
    """

    target: str
    version: str
    sha256: str
    size_bytes: int
    # Both NULL-able on `artifacts`: bytes can be target-independent, and an old row
    # may predate the layout column. A user upload always has both.
    partition_layout: str | None
    kind: str
    created_at: dt.datetime


class ArtifactList(BaseModel):
    """An envelope, not a bare array — the `DeviceList` rule, so a cursor can be added."""

    artifacts: list[ArtifactSummary]


# ---------------------------------------------------------------------------
# Deploy orchestration (R1-be-2)
# ---------------------------------------------------------------------------


class DeployRequest(BaseModel):
    """Deploy one labelled version to one device. Two fields, and both are choices.

    There is deliberately **no `target`**: the chip is the device's own
    `platform_type` (`spec/flows.md` Flow 2 → "reject on chip mismatch"). Making it a
    request field would let an operator flash an esp32 image onto an esp32c6, which is
    a brick, not a typo.

    There is also no `url`, no `sha256` and no `force`. The artifact is resolved from
    `(platform_type, version)` server-side, so the command can never carry bytes the
    server has not stored and checked.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64, description="the label, e.g. 1.5.0")
    # `spec/device-protocol.md`: `auto` lets the device apply as soon as *it* judges the
    # window safe; `on_command` stages and waits. R1 ships no `apply` command, so an
    # `on_command` deploy parks the board in `staged` until R2 — accepted on purpose,
    # because the wire field exists and refusing it would be inventing a restriction.
    apply: Literal["auto", "on_command"] = "auto"


class DeployAccepted(BaseModel):
    """What `POST /v1/devices/{device_id}/deploy` returns with 202.

    202 and not 201: the command was handed to the broker, and under MQTT 3.1.1 that is
    the *most* the server can honestly claim (`broker/commands.py` — a denied publish is
    invisible). Whether the device staged it arrives later, as `up/status`.

    **No URL here, ever.** The signed URL is a bearer credential; it goes to the device
    in the command and nowhere else — not into this body, not into the log, not into
    `deploy_events.detail`.

    `reused` is `True` when this POST re-published an in-flight intent instead of
    minting a new one, which is how a caller can tell a retry from a fresh deploy: the
    device deduplicates on `cmd_id`, so a reused id means "no second download".

    `device_online` is reported and **never** blocking: the broker queues the QoS-1
    command in the device's persistent session, which is the entire point of
    `clean_session=false` for a sleepy board.
    """

    cmd_id: str
    device_id: str
    version: str
    sha256: str
    size_bytes: int
    apply: str
    reused: bool
    device_online: bool
