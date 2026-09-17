"""The fleetforge registry schema — R0-db-1, plus `device_progress` (S0-fw-1), the
content-addressed artifact tables (S0-infra-4) and the version labels over them
(R1-be-1).

Nine tables: `device_groups`, `devices`, `enrollment_tokens`, `admin_tokens`,
`deploy_events`, `device_progress`, `artifacts`, `artifact_versions`, `builds`. Two of them
(`enrollment_tokens`, `admin_tokens`) are CRITICAL.md paths; read their docstrings
before changing anything.

Three conventions that hold across the whole file, each with a reason that is not
obvious from the code:

1. **No PostgreSQL ENUM types.** `spec/device-protocol.md` → *Evolution rules*: "the
   server must tolerate old agents forever" and payload changes are additive only. A
   PG enum needs a migration before it can accept a value a future agent invents, and
   an ingest that raises on an unknown `link_type` or `state` is a silent
   fleet-visibility outage. Vocabulary lives in the `StrEnum`s below; the columns are
   plain `TEXT`. The single exception is `devices.power_class`, which carries a DB
   `CHECK` because derived presence is only *defined* for `always_on` / `sleepy` — a
   third value would make presence undefined rather than merely unknown.

2. **Spelling is `enrollment` / `enroll` (US), everywhere.** `spec/device-protocol.md`
   is the near-frozen wire contract and it says `POST /v1/enroll`. `TODO.md` and
   `design/architecture.md` used to say `/v1/enrol`; R0-be-4 corrected both when it
   built the endpoint. There is one spelling, not two.

3. **`enrolled_at`, `last_seen`, `at`, … are server receipt times.** Never a device's
   own `ts` (`spec/device-protocol.md` → *Clock*: a board may boot with a 1970 clock
   and only fix it after SNTP).

Deliberately deferred, all additive — they are not oversights:
`boot_ok` and heartbeat health fields (`uptime_s`, `rssi`, `free_heap`) → R3
telemetry, which is where the hypertable lives; the current `up/status` transaction
state and the `deploys` table → R1; per-device deploy policy → R10. (`artifacts` is
no longer on that list — S0-infra-4 landed it early and empty, on purpose: freezing a
key scheme is free before the first object exists and a data migration afterwards.)
"""

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base

# `TIMESTAMPTZ`. Spelled once here so no column can accidentally be naive.
TimestampTZ = TIMESTAMP(timezone=True)


class PowerClass(StrEnum):
    """How a device is expected to be reachable. Enforced by a DB CHECK.

    The presence rule branches on this (`spec/device-protocol.md` → *Presence*), so a
    value outside this set has no defined meaning. R0-be-4 rejects an unknown
    `power_class` with 400 *before* burning the enrollment token.
    """

    ALWAYS_ON = "always_on"
    SLEEPY = "sleepy"


class LinkType(StrEnum):
    """Known link types. **Advisory** — the column is TEXT and accepts anything.

    A future agent on a link nobody has thought of yet must still show up in the
    dashboard.
    """

    WIFI = "wifi"
    ETHERNET = "ethernet"
    CELLULAR = "cellular"
    THREAD = "thread"


class DeployState(StrEnum):
    """The `up/status` state machine, verbatim from `spec/device-protocol.md`.

    **Advisory** — `deploy_events.state` is TEXT with no CHECK, for the same reason
    as `LinkType`.

    One member is **not** in the spec's machine: `REQUESTED`. `deploy_events.state`
    records what the *server* knows as well as what the device reported, and "we
    published a command" is a server-side fact with no device state to match it. See
    its comment below and `deploys.py`, the table's single writer.
    """

    # Server-authored, and it never arrives on the wire: no device publishes
    # `up/status {"state":"requested"}`, and `ingestor/handlers.py` must never accept
    # one. Written by `deploys.py::record_requested` the moment a `stage` command is
    # about to be published, so that an abandoned deploy is visible to the KPIs and
    # R1-be-4 can map a `cmd_id` back to the version that was intended.
    REQUESTED = "requested"
    IDLE = "idle"
    STAGING = "staging"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    STAGED = "staged"
    AWAITING_SAFE_WINDOW = "awaiting_safe_window"
    APPLYING = "applying"
    REBOOTING = "rebooting"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class ProgressStage(StrEnum):
    """The boot/enrolment stages an agent reports over HTTPS (S0-fw-1).

    **Advisory** — `device_progress.stage` is TEXT with no CHECK, for the same reason
    as `LinkType`: the R0 agent is flash-baked and the server must tolerate an agent
    it can never update, including one that invents a stage. The API bounds the
    *shape* of the string (`^[a-z_]{1,32}$`), never its membership here.

    The order below is the order a healthy board walks, up to `halted`; `brownout`
    sits outside it and is documented where it is defined. There is deliberately no
    stage before `link_up`: a board with no link cannot report anything at all, which
    is this feature's honest limit (`docs/features/enrollment.md`).
    """

    LINK_UP = "link_up"
    TIME_SYNCED = "time_synced"
    ENROLLING = "enrolling"
    ENROLLED = "enrolled"
    MQTT_CONNECTED = "mqtt_connected"
    # The broker refused the stored credential — the board is up and talking to the
    # API but will never appear in the fleet until it re-enrolls.
    MQTT_REFUSED = "mqtt_refused"
    # `agent_main.c::park()`: a failure no retry can fix. `detail` carries the reason.
    HALTED = "halted"
    # Out of the walk, and retrospective: the agent saw `ESP_RST_BROWNOUT` and is
    # reporting that the PREVIOUS boot died on a power fault (S0-fw-3). It arrives just
    # after `link_up` from a board that is, right now, working — which is the point. A
    # board still stuck in the brownout loop has no link and reports nothing at all.
    BROWNOUT = "brownout"


class ArtifactKind(StrEnum):
    """What a stored blob is. **Advisory** — `artifacts.kind` is TEXT with no CHECK.

    Same reasoning as `LinkType` (convention 1 above): a kind nobody has thought of yet
    — a Pi image, a delta patch, an LVGL asset bundle — must be storable without a
    migration. The four `agent_*` values are the parts of an agent bundle
    (`agent/dist/<target>/manifest.json`); `user_firmware` is what R1's upload endpoint
    will write.
    """

    AGENT_APP = "agent_app"
    AGENT_BOOTLOADER = "agent_bootloader"
    AGENT_PARTITION_TABLE = "agent_partition_table"
    AGENT_OTA_DATA = "agent_ota_data"
    USER_FIRMWARE = "user_firmware"


TERMINAL_DEPLOY_STATES = frozenset(
    {DeployState.CONFIRMED, DeployState.ROLLED_BACK, DeployState.FAILED}
)
"""States that end a deploy transaction. The writer sets `deploy_events.is_terminal`
from this set — see `DeployEvent` for why the flag is stored rather than derived."""


class DeviceGroup(Base):
    """A named set of devices.

    Groups exist in R0 only because enrollment tokens are group-scoped
    (`spec/prd.md` → *Security & data posture*); bulk deploy is V3. No rows are
    seeded: an ungrouped token or device is `group_id IS NULL`, never a magic
    "default" row.

    Named `device_groups` rather than `groups` because `GROUPS` is a SQL keyword.
    """

    __tablename__ = "device_groups"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


class Device(Base):
    """An enrolled board.

    The primary key is the natural key `device_id`: the eFuse MAC, lowercase hex, no
    separators. It is also the MQTT username (`spec/device-protocol.md`), which is
    what makes the whole fleet authorization two `%u` pattern ACLs — a surrogate PK
    would add a join and a chance of the two identities drifting apart. The format
    CHECK is therefore a security control, not tidiness: a `device_id` that does not
    match `^[0-9a-f]{12}$` is a username that the ACL patterns may not constrain the
    way they are read to.

    **Presence is derived, never stored.** `spec/device-protocol.md` → *Presence*:
    an `always_on` device is online iff the retained `up/presence` value says so
    (`presence_reported`); a `sleepy` device is online iff
    `now() - last_seen < 2.5 * expected_wake_interval_s`. Both ingredients are
    columns; the answer is computed in the API layer. Do not add an `online` column —
    it would be wrong the moment nothing writes to it.

    Removal is a **soft delete** (`decommissioned_at`). `deploy_events` is kept
    forever (`spec/prd.md` → *Retention*) while every enrolled device must be
    removable (`docs/features/enrollment.md`); soft delete is what satisfies both.
    Decommissioning also has to clear the device's retained MQTT topics, or the
    registry resurrects ghosts on the next broker restart. That is **not** R0-be-4 —
    that task only enrolls; it belongs to the decommissioning task filed in
    `docs/features/enrollment.md` → *Post-v1*, together with
    `BrokerProvisioner.delete_client`.

    There is deliberately no `devices.enrollment_token_id`: provenance is
    `enrollment_tokens.used_by_device_id`, one direction only, so the two tables are
    not mutually dependent.
    """

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("device_id ~ '^[0-9a-f]{12}$'", name="device_id_format"),
        CheckConstraint("power_class IN ('always_on', 'sleepy')", name="power_class"),
        # A sleepy board with no wake interval makes `2.5 * expected_wake_interval_s`
        # undefined, so it would never appear offline in the dashboard. This is a v1
        # case (the e-paper frame), not a hypothetical.
        CheckConstraint(
            "power_class <> 'sleepy' "
            "OR (expected_wake_interval_s IS NOT NULL AND expected_wake_interval_s > 0)",
            name="sleepy_wake_interval",
        ),
    )

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_type: Mapped[str] = mapped_column(Text, nullable=False)
    proto: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    fw_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_type: Mapped[str] = mapped_column(Text, nullable=False)
    power_class: Mapped[str] = mapped_column(Text, nullable=False)
    expected_wake_interval_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_device_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("devices.device_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    partition_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    ota_slot_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("device_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The server's own receipt time for the last message from this device. NEVER the
    # device's `ts` field — see the module docstring.
    last_seen: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True, index=True)
    # Last value seen on the retained `up/presence` topic. NULL = never heard from.
    presence_reported: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Set once the Mosquitto dynsec client exists, so provisioning can be reconciled
    # and retried idempotently after a partial enrollment.
    broker_provisioned_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    enrolled_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    decommissioned_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


class EnrollmentToken(Base):
    """A short-lived, group-scoped, single-use enrollment credential. **CRITICAL.**

    `CRITICAL.md`: "A token that fails to burn lets anyone with one board enrol
    arbitrary devices into the fleet." v1 has no LAN perimeter — the broker is on the
    public internet from R0 — so this is the whole gate.

    **Wire format:** `ffe_{uuid-hex}.{secret-b64url}`. The UUID half is this row's
    `id`; the secret half is verified against `secret_hash` (argon2id, stored as a
    self-describing PHC string, so the column outlives the parameter choice). The
    UUID is in the token because argon2 hashes are salted and therefore not
    searchable — `WHERE secret_hash = argon2(input)` cannot work, and scanning every
    row costs one argon2 verification per row per request. **The plaintext secret is
    never stored, logged, or returned twice** (issuance shows it once — R0-be-2).

    **The burn is one statement, and it is shipped, not copied.** R0-be-2 moved it
    into `fleetforge.auth.enrollment.BURN_SQL`; R0-be-4 **imports** that constant and
    never writes its own. For reference only, it is:

    ```sql
    UPDATE enrollment_tokens
       SET used_at = now(), used_by_device_id = $2
     WHERE id = $1 AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()
    RETURNING id;
    ```

    Zero rows back = already burned, revoked, or expired; reject the enrollment.
    Under PostgreSQL's default READ COMMITTED the loser of a race re-evaluates the
    predicate after the winner commits and correctly gets zero rows. Two rules:
    never implement this as SELECT → check → UPDATE (the classic TOCTOU that hands
    over the fleet), and if the connection is ever REPEATABLE READ or SERIALIZABLE,
    handle serialization failures (SQLSTATE 40001) instead of assuming success.

    `expires_at` has no DB default on purpose: the 24-hour window is a spec number
    (`spec/prd.md`), applied by R0-be-2, and duplicating it in DDL would create two
    places to change it.
    """

    __tablename__ = "enrollment_tokens"
    __table_args__ = (
        # A device recorded against a token that was never burned means the burn path
        # was bypassed. Make that unrepresentable rather than merely unlikely.
        CheckConstraint(
            "used_by_device_id IS NULL OR used_at IS NOT NULL",
            name="used_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    # Indexed for R1's purge of expired/revoked tokens.
    expires_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    used_by_device_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)


class AdminToken(Base):
    """An operator credential for the control plane. **CRITICAL.**

    `CRITICAL.md`: "Single admin credential on a public-facing API. Bypass = full
    fleet control."

    **Wire format:** `ffa_{uuid-hex}.{secret-b64url}`, same construction and same
    reason as `EnrollmentToken`. Two notes for R0-be-1:

    * `/v1/auth/login` returns **this same token type** in an HttpOnly, Secure,
      SameSite=Strict cookie. There is no second kind of credential and no session
      table — which is precisely why `revoked_at` exists and why this is not a JWT:
      revocation must be instant, not "when the token expires".
    * Writing `last_used_at` on every authenticated request turns every read into a
      write. Throttle it — at most one update per token per ~60 s.

    `subject` and `scopes` are reserved extension points, always `admin` /
    `{admin}` in v1 (`TODO.md` R0-be-1). They exist now so that adding `read` /
    `deploy` scopes later is not a migration on a table holding live credentials.
    """

    __tablename__ = "admin_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'admin'"))
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{admin}'::text[]")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    # NULL = never expires.
    expires_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)


class DeployEvent(Base):
    """Append-only log of observed deploy state transitions — the KPI source.

    `spec/prd.md` → *Retention*: kept **forever**; "the metric history is the
    product's evidence". `TODO.md` R0-db-1: "KPI-ready from R1 — R5 needs the
    history, not just the table." So rows are inserted, never updated in place, and
    `device_id` is a real FK with **ON DELETE RESTRICT**: no future code can destroy
    KPI history by removing a device. The supported removal path is
    `devices.decommissioned_at`.

    The two KPIs this table has to answer (`spec/prd.md` → *Success criteria*),
    both computed over the terminal event of each `(device_id, cmd_id)` transaction:

    * **delivery success** — terminal state is `confirmed` on the intended
      `artifact_version`; a `rolled_back` is a *miss*. Target >= 90%.
    * **fleet safety** — terminal state is `confirmed` **or** `rolled_back`; a
      `rolled_back` is a *save*, only `failed` (or no terminal event at all) is a
      loss. Target 100%.

    `is_terminal` is redundant with `state`, on purpose: it keeps the terminal-state
    vocabulary in Python (`TERMINAL_DEPLOY_STATES`) instead of the database, so a
    protocol addition is a code change rather than a migration plus an index rebuild,
    while still giving both KPI queries a direct partial index.

    This is **not** a Timescale hypertable and must not become one: forever
    retention, tiny volume (<= 25 devices in v1), and an outgoing FK that hypertables
    only complicate. R3's telemetry table is the hypertable case.
    """

    __tablename__ = "deploy_events"
    __table_args__ = (
        Index("ix_deploy_events_device_id_at", "device_id", text("at DESC")),
        Index("ix_deploy_events_cmd_id", "cmd_id"),
        Index(
            "ix_deploy_events_terminal",
            "device_id",
            text("at DESC"),
            postgresql_where=text("is_terminal"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    # Server receipt time, never the device's `ts`.
    at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False
    )
    # The `dn/cmd` id echoed back by `up/status`; groups one device's transaction.
    cmd_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    from_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The *intended* new version: "delivery success" means the device is running it.
    artifact_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DeviceProgress(Base):
    """Append-only boot/enrolment stage reports from an agent — S0-fw-1.

    A board is invisible between "flashed" and "online": the dashboard has nothing
    to show until the first retained `up/announce` reaches the ingestor, which is
    exactly the window where a first-hardware attempt fails. These rows are what the
    fleet view shows instead of that gap.

    Three deliberate shapes:

    * **No FK to `devices`.** The interesting rows are the ones written *before* the
      device exists — `link_up` and `enrolling` from a board that never enrols. An FK
      would make the failure case unrecordable, which is the whole feature.
    * **Not KPI history.** Unlike `DeployEvent` this is a debugging aid with a short
      useful life, so `fleetforge.progress` caps it at `progress_max_rows_per_device`
      rows per device on insert: a board retrying enrolment every 60 s must not grow
      the table without bound. Nothing outside that module may rely on a row's
      survival.
    * **`stage` is TEXT with no CHECK** (see `ProgressStage`), and `detail` is
      device-controlled free text — bounded by the API, never trusted by a reader.

    There is no `stalled` stage. Staleness is derived on read from the age of the
    newest row, in one place, exactly as `online` is derived by `presence.is_online`.
    """

    __tablename__ = "device_progress"
    __table_args__ = (Index("ix_device_progress_device_id_at", "device_id", text("at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    # Server receipt time. The reporting board may not have run SNTP yet — before
    # `time_synced` its own clock is worthless, so it never sends one.
    at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The enrollment token this was reported under. Not a FK either: the token row
    # may be deleted by a future retention sweep long before these rows age out.
    token_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Artifact(Base):
    """One row per content digest — the metadata the bytes cannot carry. S0-infra-4.

    `DECISIONS.md` 2026-09-14 → *one storage model for every image*: artifact bytes are
    content-addressed at `fleetforge/blobs/sha256/<hex>`, write-once, and Postgres holds
    "an `artifacts` row per digest (size, kind, target, partition_layout, provenance)".
    The key rules are `fleetforge.storage.blobs`; this table is their metadata half.

    **Lands empty, with no readers.** R1's upload endpoint is the first writer and
    S0-infra-6 the first agent-bundle one. Freezing the scheme is free while zero objects
    exist and is a migration over live bytes in a shared bucket afterwards — the same
    shipping posture `fleetforge.storage` itself took at R0-be-6.

    **There is deliberately no `storage_key` column.** The key is
    `blobs.blob_key(sha256)`, a pure function of the PK; storing it creates a second
    spelling that can disagree with the first, which is the failure the whole
    reject-never-normalise rule exists to prevent.

    **There is deliberately no `deleted_at` and no refcount.** Nothing deletes blobs
    until R2 pruning, and a refcount with no decrementer is a lie that a future pruner
    would believe.

    The `sha256` CHECK is a security control rather than tidiness, exactly as
    `devices.device_id_format` is: the digest *is* the object key, so a row whose digest
    is not `^[0-9a-f]{64}$` names a key that `blobs.blob_key` will refuse to build — and
    the disagreement would surface at download time, not at write time.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        # A zero-byte artifact is a deploy that ships nothing; it is never a real image.
        CheckConstraint("size_bytes > 0", name="size_positive"),
        # How "the current app image for this chip" gets found.
        Index("ix_artifacts_kind_target", "kind", "target"),
    )

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Plain TEXT with no CHECK — see `ArtifactKind`, and convention 1 above.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Chip target (`esp32c6`). NULL where the bytes are target-independent, or where a
    # user upload simply did not say.
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Mirrors `devices.partition_layout` (`ab-4m-v1`).
    partition_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `source_commit`, `idf_version`, `idf_image`, `agent_version`, `config_sha256`,
    # `build_digest` — i.e. the S0-infra-3 manifest identity, **copied, never
    # recomputed**. JSONB rather than columns because the shape belongs to the producer
    # and a user upload has none of it; the precedent is `deploy_events.detail`.
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Server receipt time — see the module docstring.
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class ArtifactVersion(Base):
    """A human version label pointing at a digest — `(target, version) → sha256`. R1-be-1.

    **Why this is a table and not a column on `artifacts`.** `artifacts` is
    content-addressed: the digest is the primary key, so a `version` column would make
    one digest carry exactly one label, and re-tagging byte-identical firmware would be
    a primary-key collision rather than the ordinary thing it is. Labels are mutable
    pointers over immutable bytes — the same split S0-infra-6 chose for the agent
    catalog, where `agent/index.json` is the one mutable key over `blobs/sha256/…`.
    `artifacts` therefore stays exactly as S0-infra-4 froze it.

    **The label has a real FK to `artifacts.sha256`.** Migration `0003` has no foreign
    keys, but that was forced rather than chosen: `builds.outputs` names artifacts
    inside JSONB and PostgreSQL cannot FK into JSONB (see `Build`). Here it can, so it
    does — and the constraint is what stops R2's pruner deleting bytes a label still
    points at. Deletes are `RESTRICT`: the pruner must drop the label first, which is
    the order that leaves the fleet able to explain itself.

    **`version` is TEXT with no DB CHECK, and validated at the edge instead.** The
    vocabulary is the user's — semver, a date, a CI build number — so a CHECK here
    would be this project deciding what a customer may call their firmware. What the
    API *does* enforce is that the label is safe to carry: it reaches a `dn/cmd` JSON
    payload (`spec/device-protocol.md`) and a dashboard list, and it is **rejected,
    never normalised** (`identity.py`'s rule) so that `1.5.0` and `1.5.0 ` cannot
    become two spellings of one release.

    `(target, version)` is the PK rather than a surrogate id: "which bytes is
    `esp32`/`1.5.0`?" is the only question this table answers, and the uniqueness it
    needs is exactly the uniqueness the PK gives. `spec/prd.md` → *Retention* ("20
    stored versions per platform, oldest pruned") is then one indexed scan of
    `(target, created_at)`, read backwards.
    """

    __tablename__ = "artifact_versions"
    __table_args__ = (
        # Mirrors `artifacts.sha256_format`. The FK already guarantees the row exists;
        # this guarantees the value names a key `blobs.blob_key` will agree to build.
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        # "The 20 newest versions for this platform" — the R2 pruner's only query.
        # Ascending: PostgreSQL scans a b-tree backwards at the same cost, so a DESC
        # index would buy nothing and be one more thing for the migration to match.
        Index("ix_artifact_versions_target_created_at", "target", "created_at"),
    )

    # Chip target (`esp32`), `releases.md`'s "platform_type". NOT NULL here even though
    # `artifacts.target` is nullable: a label with no platform cannot answer "20 versions
    # per platform", and every upload knows its target.
    target: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(
        Text, ForeignKey("artifacts.sha256", ondelete="RESTRICT"), nullable=False
    )
    # Server receipt time — convention 3 above. Also the prune order.
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class Build(Base):
    """A build-cache entry: cache key → the artifacts that build produced. S0-infra-4.

    `design/artifacts.md` → *Dynamic build: a cache miss, not a mode*. Tier 1 of that
    document's three tiers is exactly one lookup in this table; tiers 2 and 3 are the R9
    build runner and are not here. Lands empty with no readers, like `artifacts`.

    `cache_key` is `H(idf_image_digest, target, partition_layout, source_tree_digest,
    config_digest)` — source **tree**, not commit, so a dirty tree or a config-only
    change cannot hit a stale entry. `docs/features/build-pipeline.md`'s
    `(repo, ref, toolchain)` is wrong as written and `DECISIONS.md` 2026-09-14 corrects
    it.

    **Why `outputs` is JSONB and not a `build_outputs` join table** — the decision a
    reviewer will question. A build of an agent bundle produces four parts, so a single
    `artifact_sha256` column cannot represent it, and a per-part row would collide on
    the cache key as PK. The output set is consumed as a unit (it *is* the manifest
    view) and is never queried part-wise across builds. The cost is real and has to be
    stated: **PostgreSQL cannot FK into JSONB, so a future pruner must treat
    `builds.outputs` as a GC root** rather than relying on referential integrity to keep
    a referenced blob alive. A join table is the additive migration the day part-wise
    queries appear.

    `key_inputs` exists because a cache key nobody can explain settles no argument —
    the same S0-fw-3 lesson that produced `config_sha256`.
    """

    __tablename__ = "builds"
    __table_args__ = (
        CheckConstraint("cache_key ~ '^[0-9a-f]{64}$'", name="cache_key_format"),
        # `jsonb_typeof` because JSONB accepts `[]` and `"x"` as happily as an object,
        # and a reader that does `outputs["app"]` on a list gets a 500, not a miss.
        CheckConstraint("jsonb_typeof(key_inputs) = 'object'", name="key_inputs_object"),
        CheckConstraint("jsonb_typeof(outputs) = 'object'", name="outputs_object"),
        Index("ix_builds_target_partition_layout", "target", "partition_layout"),
    )

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    # The values that were hashed into `cache_key`.
    key_inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # `{"<part>": {"sha256": "<hex>", "offset": 65536}}`; `offset` may be absent for a
    # single-image build. Each `sha256` names an `artifacts` row — by convention, not by
    # FK; see the class docstring.
    outputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Denormalised from `key_inputs` for lookup. The key stays the authority.
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    partition_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
