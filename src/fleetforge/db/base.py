"""Declarative base and lazily-constructed engine/session factory.

Importing this module must never open a connection or require `DATABASE_URL` to be
set: `alembic/env.py`, mypy and the unit tests all import the models, and only some
of them have a database. Hence `get_engine()` / `get_sessionmaker()` rather than
module-level singletons.

**`get_sessionmaker()` is the only door into the database from the API.** R0-db-1
also shipped a `get_session()` FastAPI dependency; R0-be-1 deleted it. It called the
`lru_cache`d `get_sessionmaker()` *directly*, so a
`app.dependency_overrides[get_sessionmaker]` did not affect it and a test would have
quietly talked to the developer's real dev database. Everything therefore depends on
`get_sessionmaker` and opens its own `async with sessionmaker() as session:` — which
is the one override point the test fixtures use.
"""

from functools import lru_cache

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from fleetforge.config import get_settings

# Every constraint gets a deterministic name, so Alembic autogenerate can compare
# the models against the database instead of proposing a drop/recreate churn. The
# acceptance criterion `alembic check` == "No new upgrade operations detected."
# depends on this.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every fleetforge ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def asyncpg_dsn(url: str) -> str:
    """Convert a SQLAlchemy async URL into one `asyncpg.connect()` accepts.

    `Settings.database_url` is `postgresql+asyncpg://…` because SQLAlchemy resolves
    its driver from the scheme; asyncpg's own connect refuses that scheme. The one
    caller that needs a raw connection is `api/eventstream.py`'s `LISTEN` connection
    — `LISTEN` only delivers to a backend that is between transactions, so it cannot
    share the pool (see `DECISIONS.md` 2026-09-08, R0-be-5). The conversion lives
    here rather than inlined at each call site: two spellings of the same URL is how
    a listener ends up pointed at a different database than the writer.
    """
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, created on first use."""
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, created on first use."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)
