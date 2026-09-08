"""Declarative base and lazily-constructed engine/session factory.

Importing this module must never open a connection or require `DATABASE_URL` to be
set: `alembic/env.py`, mypy and the unit tests all import the models, and only some
of them have a database. Hence `get_engine()` / `get_sessionmaker()` rather than
module-level singletons.
"""

from collections.abc import AsyncIterator
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


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, created on first use."""
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, created on first use."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session, rolling back on error. FastAPI dependency for R0-be-1."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
