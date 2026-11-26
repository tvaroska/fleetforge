"""Test harness: a real PostgreSQL database, migrated by Alembic.

Two deliberate choices:

* The schema is built by running the **migrations**, not `Base.metadata.create_all`.
  The migration is the artifact that runs in production; a test suite that bypasses
  it cannot catch a migration that does not match the models.
* Everything goes through asyncpg — no psycopg dependency just to issue
  `CREATE DATABASE`. An AUTOCOMMIT engine on the maintenance database does that job.

It also owns the app fixtures (`app_no_db`, `app_with_db`, `admin_app`) shared by
`test_api_health.py` and `test_api_auth.py`. Both configure the app with
**dependency overrides, never environment mutation**: `get_settings`, `get_engine`
and `get_sessionmaker` are all `lru_cache`d, so poking `os.environ` would either do
nothing or poison every later test in the session.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fleetforge.api.deps import COOKIE_NAME
from fleetforge.api.main import create_app
from fleetforge.auth.hashing import hash_secret
from fleetforge.broker.commands import encode_command
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_sessionmaker
from fleetforge.storage.objectstore import (
    ObjectNotFound,
    ObjectTooLarge,
    resolve_key,
    validate_prefix,
    validate_ttl,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_NAME = "fleetforge_test"
DEFAULT_DEV_URL = "postgresql+asyncpg://fleetforge:fleetforge@localhost:5433/fleetforge"

# The admin password the `admin_app` fixture is configured with. Hashed once at
# import: argon2 is ~40 ms a call and every auth test would otherwise pay it twice.
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_PASSWORD_HASH = hash_secret(TEST_PASSWORD)

# R1-be-3. The HMAC key `settings_for_tests` signs artifact download links with, and
# the origin it mints them for. Constants, not random values: a test that asserts on a
# URL needs the same secret the app used, and a fixed secret makes a failure diff
# readable. Nothing here is a credential — the app under test is the only verifier.
TEST_ARTIFACT_URL_SECRET = "test-artifact-url-secret"  # noqa: S105 - test fixture, not a credential
TEST_PUBLIC_BASE_URL = "https://downloads.test"


def database_url_for(db_name: str) -> str:
    """Return `TEST_DATABASE_URL` (e.g. shared btvaroska k3s CNPG) or optional Docker dev fallback with `db_name`."""
    base = os.environ.get("TEST_DATABASE_URL") or DEFAULT_DEV_URL
    head, _, _ = base.rpartition("/")
    return f"{head}/{db_name}"


def _is_ci_env() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false")


TERMINATE_SQL = text(
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = :db_name AND pid <> pg_backend_pid()"
)


async def drop_database(db_name: str, then_create: bool = False) -> None:
    """Drop `db_name` (optionally recreating it), evicting stragglers first.

    `DROP`/`CREATE DATABASE` cannot run inside a transaction and cannot take bind
    parameters, hence the AUTOCOMMIT engine and the quoted identifier. `db_name` is a
    module constant, never user input.
    """
    admin_engine = create_async_engine(
        database_url_for("postgres"), isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(TERMINATE_SQL, {"db_name": db_name})
            await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}"')
            if then_create:
                await conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_engine.dispose()


async def recreate_database(db_name: str) -> None:
    """Drop and recreate `db_name`."""
    await drop_database(db_name, then_create=True)


def alembic_config() -> Config:
    """Alembic config pinned to the repo root, wherever pytest was invoked from."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


async def run_alembic(db_url: str, action: str = "upgrade", revision: str = "head") -> None:
    """Run an Alembic command against `db_url` in a worker thread.

    `alembic/env.py` calls `asyncio.run()`, which cannot be nested inside the running
    test loop — hence `to_thread`. `env.py` reads DATABASE_URL from the environment,
    so that is how the target database is selected.
    """
    runner = {"upgrade": command.upgrade, "downgrade": command.downgrade}[action]
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        await asyncio.to_thread(runner, alembic_config(), revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def run_alembic_check(db_url: str) -> None:
    """Run `alembic check` against `db_url`; raises AutogenerateDiffsDetected on drift."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        await asyncio.to_thread(command.check, alembic_config())
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped engine bound to a freshly migrated `fleetforge_test`."""
    test_url = database_url_for(TEST_DB_NAME)
    try:
        await recreate_database(TEST_DB_NAME)
    except (
        SQLAlchemyError,
        OSError,
    ) as exc:  # pragma: no cover - environment problem, not a test failure
        redacted_url = make_url(database_url_for("postgres")).render_as_string(hide_password=True)
        msg = (
            f"Cannot reach PostgreSQL at {redacted_url}: {exc} "
            "(set TEST_DATABASE_URL to the shared btvaroska k3s CloudNativePG test environment; "
            "on a Docker-equipped dev box you may alternatively run `just db-up`, "
            "or pass FF_ALLOW_DB_SKIP=1 locally to skip DB tests)"
        )
        if os.environ.get("FF_ALLOW_DB_SKIP") == "1":
            if _is_ci_env():
                pytest.exit(
                    f"FF_ALLOW_DB_SKIP=1 is forbidden when CI is set; {msg}",
                    returncode=1,
                )
            pytest.skip(msg)
        pytest.exit(msg, returncode=1)

    await run_alembic(test_url)

    test_engine = create_async_engine(test_url, pool_pre_ping=True)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()
        await drop_database(TEST_DB_NAME)


_CLEANUP_BLOCK_SQL = """
DO $$
BEGIN
    DELETE FROM partition_profiles WHERE origin <> 'builtin';
    DELETE FROM project_rollouts;
    DELETE FROM deploy_policy_runs;
    DELETE FROM deploy_policies;
    DELETE FROM deploy_events;
    DELETE FROM enrollment_tokens;
    DELETE FROM gateway_relays;
    DELETE FROM devices;
    DELETE FROM device_groups;
    DELETE FROM build_jobs;
    DELETE FROM builds;
    DELETE FROM artifact_deltas;
    DELETE FROM artifact_versions;
    DELETE FROM artifacts;
    DELETE FROM project_members;
    DELETE FROM admin_tokens;
    DELETE FROM users;
    DELETE FROM projects WHERE slug <> 'default';
END $$;
"""


@pytest.fixture
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Clean up rows committed via the app's own sessionmaker before and after a test."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CLEANUP_BLOCK_SQL)
    yield
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CLEANUP_BLOCK_SQL)


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    Tests never see each other's rows, and nothing needs cleaning up.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async_session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield async_session
        finally:
            await async_session.close()
            if transaction.is_active:
                await transaction.rollback()


class _RecordCollector(logging.Handler):
    """Keeps every record it is handed. Levels are decided by the logger, not here."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def capture_logs(name: str = "fleetforge") -> Iterator[list[logging.LogRecord]]:
    """Collect records emitted under `name`. **Use this instead of `caplog`.**

    Two things in this suite silently break `caplog`, and both of them turn an
    assertion like "the broker password is never logged" into a **vacuous pass**, which
    is worse than having no test:

    * `api/main.py::configure_logging` calls `logging.basicConfig(force=True)`, which
      removes every handler on the **root** logger — `caplog`'s included.
    * `alembic/env.py` calls `fileConfig(...)`, whose default is
      `disable_existing_loggers=True`. Running the migrations (the `engine` fixture
      does, once per session) therefore sets `disabled = True` on every `fleetforge.*`
      logger that has been imported by then, and they emit nothing for the rest of the
      run. Production is unaffected: the container migrates in a **separate**
      `alembic upgrade` process (`docker/entrypoint.sh`), not in-process.

    So this attaches to the package logger, lowers its level, and un-disables the
    subtree for the duration — restoring all three afterwards. Every assertion using it
    should also assert that *something* was captured.
    """
    logger = logging.getLogger(name)
    handler = _RecordCollector()
    previous_level = logger.level

    prefix = f"{name}."
    silenced = [
        existing
        for logger_name, existing in logging.Logger.manager.loggerDict.items()
        if isinstance(existing, logging.Logger)
        and (logger_name == name or logger_name.startswith(prefix))
        and existing.disabled
    ]

    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    for existing in silenced:
        existing.disabled = False
    try:
        yield handler.records
    finally:
        for existing in silenced:
            existing.disabled = True
        logger.setLevel(previous_level)
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# App fixtures. In-process against the ASGI app — no uvicorn, no network.
# ---------------------------------------------------------------------------


def client_for(app: FastAPI, base_url: str = "http://testserver") -> httpx.AsyncClient:
    """An httpx client bound straight to the ASGI app.

    Pass `base_url="https://testserver"` for anything that must keep a cookie:
    `http.cookiejar` (which httpx uses) drops a `Secure` cookie on a plain-http
    origin. It does not model `localhost` as a secure context the way browsers do,
    so the fix is the test's URL scheme — never a weaker cookie.
    """
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


class _IsolatedTestSettings(Settings):
    """`Settings` that reads only explicit kwargs — never `os.environ` or `.env`."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def settings_for_tests(**overrides: object) -> Settings:
    """A `Settings` built explicitly, never reading `os.environ` or `.env`.

    The database URL is deliberately unusable: every app fixture overrides
    `get_sessionmaker`, so anything that reaches for this URL is a bug worth a loud
    connection error rather than a silent write to the developer's dev database.
    """
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        "admin_password_hash": TEST_PASSWORD_HASH,
        "object_store_backend": None,
        "s3_endpoint_url": None,
        "s3_bucket": None,
        "s3_access_key": None,
        "s3_secret_key": None,
        "s3_public_endpoint_url": None,
        "gcs_bucket": None,
        "gcs_credentials_file": None,
        "gcs_impersonate_service_account": None,
        "artifact_url_secret": TEST_ARTIFACT_URL_SECRET,
        "public_base_url": TEST_PUBLIC_BASE_URL,
    }
    values.update(overrides)
    return _IsolatedTestSettings(**values)  # type: ignore[arg-type]


@pytest.fixture
def app_no_db() -> FastAPI:
    """The app with nothing overridden — usable without a database."""
    return create_app()


@pytest.fixture
def app_with_db(engine: AsyncEngine) -> FastAPI:
    """The app bound to the migrated test database.

    A dependency override, not an environment mutation: `get_settings` and
    `get_engine` are both `lru_cache`d, so poking `DATABASE_URL` into `os.environ`
    would either do nothing or poison every later test in the session.
    """
    app = create_app()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    return app


@pytest.fixture
def admin_app(app_with_db: FastAPI) -> FastAPI:
    """`app_with_db` plus a known admin password (`TEST_PASSWORD`)."""
    settings = settings_for_tests()
    app_with_db.dependency_overrides[get_settings] = lambda: settings
    return app_with_db


async def login_admin(app: FastAPI, password: str = TEST_PASSWORD) -> str:
    """Log in against `app` and return the raw `ffa_` token from the `Set-Cookie`.

    Shared by every HTTP suite that needs a credential, so there is one login helper
    rather than one per test module. The client uses `https://testserver` because the
    session cookie is `Secure` — see `client_for`.
    """
    async with client_for(app, base_url="https://testserver") as client:
        response = await client.post("/v1/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    token = response.cookies[COOKIE_NAME]
    assert isinstance(token, str)
    return token


class MemoryObjectStore:
    """An in-memory `ObjectStore`: a dict, plus the contract's error behaviour.

    Shared by `test_blob_keys.py` (which only reads `puts`), `test_agent_publish.py` and
    `test_agent_catalog_store.py`, so there is one fake rather than three that drift. It
    implements the Protocol structurally — no base class, the same way the real adapters
    do — and its `get` raises `ObjectNotFound`, never returns `None`, because a fake that
    is more forgiving than the contract hides exactly the bug the contract exists to stop.

    `fail_with` makes the store refuse every verb: the named-fault path
    (`ObjectStoreError` → 503) has to be reachable without unplugging MinIO.
    """

    def __init__(
        self,
        *,
        prefix: str = "",
        default_ttl_s: int = 1800,
        max_get_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._prefix = validate_prefix(prefix)
        self._default_ttl_s = default_ttl_s
        self._max_get_bytes = max_get_bytes
        self.objects: dict[str, bytes] = {}
        # (key, data, content_type, cache_control) per put, in order.
        self.puts: list[tuple[str, bytes, str, str | None]] = []
        self.gets: list[str] = []
        # (key, ttl_s) per signed_url, in order. R1-be-3 asserts on the *count*: a
        # tampered link must cost zero signing calls, and ten range requests for one
        # artifact must cost one — on GCS every call here is a signBlob round trip.
        self.signed_urls: list[tuple[str, int | None]] = []
        self.fail_with: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        resolved = resolve_key(self._prefix, key)
        self._maybe_fail()
        self.puts.append((key, data, content_type, cache_control))
        self.objects[resolved] = data

    async def get(self, key: str) -> bytes:
        resolved = resolve_key(self._prefix, key)
        self._maybe_fail()
        self.gets.append(key)
        try:
            data = self.objects[resolved]
        except KeyError as exc:
            raise ObjectNotFound(f"no object at {resolved}") from exc
        if len(data) > self._max_get_bytes:
            raise ObjectTooLarge(
                f"object {resolved} is {len(data)} bytes, over the {self._max_get_bytes} cap"
            )
        return data

    async def signed_url(self, key: str, *, ttl_s: int | None = None) -> str:
        resolved = resolve_key(self._prefix, key)
        effective_ttl = validate_ttl(self._default_ttl_s if ttl_s is None else ttl_s)
        self._maybe_fail()
        self.signed_urls.append((key, ttl_s))
        return f"https://memory.invalid/{resolved}?ttl={effective_ttl}"

    async def delete(self, key: str) -> None:
        resolved = resolve_key(self._prefix, key)
        self._maybe_fail()
        self.objects.pop(resolved, None)


class FakeCommandPublisher:
    """An in-memory `CommandPublisher`: records what was published, or refuses. R1-be-2.

    Structural, like `MemoryObjectStore` — no base class, because the real adapter has
    none either. It records the **whole** payload so a test can assert the exact wire
    shape against `spec/device-protocol.md`, and `fail_with` makes the
    `CommandPublishError` → `failed` row → 503 path reachable with no broker running.
    """

    def __init__(self) -> None:
        # (device_id, payload) per publish, in order.
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.published_cfg: list[tuple[str, dict[str, Any]]] = []
        self.cleared_cfg: list[str] = []
        self.fail_with: Exception | None = None
        self.fail_publish_many_with: Exception | None = None
        self.fail_after_publishes: int | None = None
        self._publish_calls = 0

    async def publish(self, device_id: str, payload: dict[str, Any]) -> None:
        self._publish_calls += 1
        encode_command(payload)
        if self.fail_with is not None and (
            self.fail_after_publishes is None or self._publish_calls > self.fail_after_publishes
        ):
            raise self.fail_with
        self.published.append((device_id, payload))

    async def publish_cfg(self, device_id: str, payload: dict[str, Any]) -> None:
        encode_command(payload)
        if self.fail_with is not None:
            raise self.fail_with
        self.published_cfg.append((device_id, payload))

    async def clear_cfg(self, device_id: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.cleared_cfg.append(device_id)

    async def publish_many(self, items: Sequence[tuple[str, dict[str, Any]]]) -> None:
        if not isinstance(items, Sequence):
            raise TypeError("publish_many requires a Sequence, not a one-shot iterator")
        if not items:
            return
        for _, payload in items:
            encode_command(payload)
        if self.fail_publish_many_with is not None:
            raise self.fail_publish_many_with
        if self.fail_with is not None and self.fail_after_publishes is None:
            raise self.fail_with
        for device_id, payload in items:
            self.published.append((device_id, payload))

    async def publish_cfg_many(self, items: Sequence[tuple[str, dict[str, Any]]]) -> None:
        if not isinstance(items, Sequence):
            raise TypeError("publish_cfg_many requires a Sequence, not a one-shot iterator")
        if not items:
            return
        for _, payload in items:
            encode_command(payload)
        if self.fail_with is not None:
            raise self.fail_with
        for device_id, payload in items:
            self.published_cfg.append((device_id, payload))

    @property
    def last(self) -> dict[str, Any]:
        """The most recent payload; fails the test loudly if nothing was published."""
        assert self.published, "nothing was published"  # noqa: S101 - a test helper
        return self.published[-1][1]
