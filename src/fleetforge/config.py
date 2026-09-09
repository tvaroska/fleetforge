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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed on first use."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
