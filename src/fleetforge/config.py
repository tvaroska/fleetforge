"""Application settings.

Deliberately tiny. `alembic/env.py` reads `DATABASE_URL` straight from the
environment and never imports this module: `content` learned the hard way that
importing app config into Alembic forces every unrelated setting to validate
before `alembic upgrade head` can run, which turns a missing OAuth secret into a
failed migration.

`database_url` must stay required — a silently-defaulted database URL is how a
service ends up writing to the wrong database.

Environment names are the field names uppercased and unprefixed (`DATABASE_URL`,
`MQTT_HOST`, `ADMIN_PASSWORD_HASH`). The `FF_` prefix seen in `.env` belongs to
*compose* knobs (ports, domain) and is deliberately not used here.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (or a local `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

    # Broker coordinates. The defaults are the Compose service name, so the
    # containerised ingestor needs no configuration; on the host, export
    # MQTT_HOST=localhost only if you have published the broker port (the
    # standalone stack deliberately does not — see docs/runbooks/dev-stack.md).
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883

    # This process's OWN broker credential — the ingestor's. The API's control
    # credential is `mqtt_dynsec_*` below: a different privilege that rotates
    # separately, so do not reuse one for the other. Both unset means anonymous,
    # which the broker refuses from R0-sec-1 on (`allow_anonymous false`) — the
    # ingestor then reconnect-loops with "Not authorized" and its healthcheck goes
    # stale after 120 s.
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    # --- Admin auth (R0-be-1) ------------------------------------------------
    # The argon2id PHC string for the admin password — never the password itself.
    # Optional here but MANDATORY in docker-compose.yml (`${ADMIN_PASSWORD_HASH:?}`):
    # `create_app()` must stay constructible with no environment at all (the health
    # tests depend on it), while a deployed stack must refuse to start unconfigured.
    # When it is None, `/v1/auth/login` answers 503 and startup logs one WARNING.
    # `/v1/readyz` deliberately ignores it — readiness is about the database, and a
    # login-config problem must not turn into a container restart loop.
    #
    # In `.env` this value MUST be single-quoted: a PHC string is full of `$` and
    # docker compose would otherwise interpolate `$argon2id`/`$v`/`$m` away.
    admin_password_hash: str | None = None

    # How long a login cookie / session token lives. 7 days.
    session_ttl_hours: int = 168

    # Login rate limiting: failures per window, per client IP and globally. The
    # global bucket is the backstop, because the client key comes from a
    # client-spoofable `X-Forwarded-For` (see `api/deps.py::client_key`).
    login_rate_limit_per_ip: int = 5
    login_rate_limit_global: int = 30
    login_rate_limit_window_s: int = 60

    # `AdminToken` docstring: writing `last_used_at` on every authenticated request
    # turns every read into a write. At most one update per token per ~60 s.
    last_used_throttle_s: int = 60

    # How long a successful argon2 verification is memoized. This memoizes the hash
    # comparison ONLY — see `auth/cache.py`; the row is still read every request.
    verify_cache_ttl_s: int = 60

    # --- Enrollment tokens (R0-be-2) -----------------------------------------
    # How long an enrollment token stays usable. 24 h, from spec/prd.md → Security &
    # data posture (PROPOSED). Deliberately NOT a DB default: one number, one place
    # (see db/models.py::EnrollmentToken).
    enrollment_token_ttl_hours: int = 24

    # --- Ingestor / derived presence (R0-be-3) --------------------------------
    # A sleepy device is offline once `now - last_seen > tolerance *
    # expected_wake_interval_s`. 2.5 comes from spec/prd.md → Requirements &
    # targets → Timing ("Offline shows in dashboard — sleepy: 2.5 ×
    # expected_wake_interval_s since last_seen"). One number, one place: the API's
    # device list (R0-be-5/R0-fe-2) reads the same setting through
    # `fleetforge.presence.is_online`, never its own literal. `always_on` presence
    # needs no number — it is the retained LWT value.
    presence_tolerance: float = 2.5

    # --- Device enrollment (R0-be-4) -----------------------------------------
    # How long after a burn the SAME device_id may re-present the SAME token and get
    # a freshly provisioned credential. Recovers a lost enrollment response (the
    # device writes NVS only after it reads the body); it can never enroll a second
    # board, because the lookup matches on `used_by_device_id`.
    # See DECISIONS.md 2026-09-08 -> "Enrollment: commit, then provision".
    enroll_retry_window_s: int = 600

    # `/v1/enroll` is unauthenticated, public, and does one argon2 verification per
    # request. Same two-bucket shape and the same reason as the login limiter
    # (auth/ratelimit.py); the per-IP bucket is generous because a fleet behind one
    # NAT may reboot together, and only FAILURES are counted. The window is shared
    # with login (`login_rate_limit_window_s`) — one window, one place.
    enroll_rate_limit_per_ip: int = 10
    enroll_rate_limit_global: int = 60

    # --- Broker credential provisioning (R0-be-4 / R0-sec-1) ------------------
    # The Mosquitto dynamic-security ADMIN credential — broker-root, and a
    # different privilege from `mqtt_username`/`mqtt_password` above. BOTH unset
    # selects `broker.NullProvisioner`, which provisions nothing: that is the
    # tests' and a bare `create_app()`'s path, never the dev stack, where
    # docker-compose.yml makes both mandatory. Never a real value in a tracked
    # file, and never logged.
    mqtt_dynsec_username: str | None = None
    mqtt_dynsec_password: str | None = None
    # The dynsec role a device's client is created with. `mosquitto/bootstrap.sh`
    # creates a role with THIS EXACT NAME; if the two disagree, `createClient`
    # fails and every enrollment answers 503. The role is deliberately EMPTY — the
    # two `%u` pattern ACLs live in `mosquitto/acl`, because Mosquitto 2.0's dynsec
    # plugin has no `%u` substitution (DECISIONS.md 2026-09-08, R0-sec-1).
    mqtt_dynsec_role: str = "device"
    # One dynsec round-trip must not hold an API worker forever.
    broker_command_timeout_s: float = 5.0

    # --- SSE event stream (R0-be-5) ------------------------------------------
    # A comment frame every 15 s. Idle proxies and load balancers close silent
    # connections, and a client with no traffic cannot tell "quiet fleet" from
    # "dead socket".
    sse_keepalive_s: float = 15.0
    # A stream is closed after 15 min and the client reconnects. Auth is checked
    # once, at connect (api/deps.py::require_admin), so this is the bound on how
    # long a REVOKED admin token can keep reading events — instant revocation is the
    # reason JWT was rejected (DECISIONS.md 2026-09-08, R0-be-1). Deliberately well
    # under nginx's `proxy_read_timeout 3600s` in frontend/nginx.conf.
    sse_max_stream_s: float = 900.0
    # Per-client buffer. A client that falls this far behind is disconnected rather
    # than buffered — see api/eventstream.py::EventHub.publish.
    sse_queue_size: int = 200
    # Concurrent SSE streams per API worker. spec/prd.md → Capacity sizes v1 at 25
    # devices and one operator; this is a memory guard on a 256 M container, not a
    # product limit.
    sse_max_clients: int = 20
    # How often the LISTEN connection proves it is still alive with `SELECT 1`. A
    # silently dead TCP socket fires no termination callback, and the symptom is an
    # SSE stream that is connected and permanently empty.
    events_listener_ping_s: float = 30.0

    # --- Object store (R0-be-6) ----------------------------------------------
    # None means "infer from what is configured" — see storage/factory.py. Set it
    # explicitly only to be unambiguous; BOTH groups configured is always an error.
    object_store_backend: Literal["s3", "gcs"] | None = None

    # S3 / MinIO. THESE FOUR NAMES ARE FIXED by the `api` service block in
    # docker-compose.yml, which R0-infra-1 pre-wired for this task. Renaming one
    # silently unconfigures the container.
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    # The endpoint a DEVICE can reach. A presigned URL signs the Host header, so a URL
    # generated against the container-internal endpoint (`http://minio:9000`) cannot be
    # rewritten afterwards — the signature would no longer match. Defaults to
    # `s3_endpoint_url`, which is right only when the two are the same host.
    s3_public_endpoint_url: str | None = None
    # Mandatory for botocore's SigV4 even against MinIO, which accepts any value.
    s3_region: str = "us-east-1"
    # The dev MinIO bucket is dedicated to fleetforge; there is nothing to share it
    # with, so no prefix. GCS is the opposite case — see `gcs_prefix`.
    s3_prefix: str = ""

    # GCS. gs://btvaroska is SHARED (secrets/, podcasts/, audio/, backup/ …), so the
    # prefix is a containment boundary and defaults to the right thing.
    gcs_bucket: str | None = None
    gcs_prefix: str = "fleetforge/"
    # Path to the service-account JSON key. REQUIRED for the GCS backend and never
    # defaulted to ADC: ADC on a GCE VM cannot sign a V4 URL (no private key) and would
    # silently use the project-wide compute default SA. Never committed — `secrets/` is
    # gitignored; see docs/runbooks/artifact-storage.md.
    gcs_credentials_file: str | None = None

    # How long a signed artifact URL lives. 30 min = 2x spec/prd.md's degraded-link
    # deploy budget (15 min), so a range-resumed download cannot outlive its own URL.
    # PROPOSED for spec/prd.md -> Requirements & targets.
    signed_url_ttl_s: int = 1800
    # `get()` loads the whole object into a 256 M container and spec/prd.md caps an
    # artifact at 1.9 MB; this is the verification path, not a streaming path.
    object_get_max_bytes: int = 8 * 1024 * 1024
    object_store_timeout_s: float = 30.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed on first use."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
