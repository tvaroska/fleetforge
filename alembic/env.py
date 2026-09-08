import asyncio
import os
import sys
from logging.config import fileConfig

sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "src"))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from fleetforge.db import models  # noqa: F401  # imported for its side effect: registers the tables
from fleetforge.db.base import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# DATABASE_URL is read straight from the environment, NOT via fleetforge.config:
# importing application settings would make every unrelated setting validate before
# `alembic upgrade head` could run. (`content` carries the scar; do not "tidy" this.)
database_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://fleetforge:fleetforge@localhost:5433/fleetforge",
)
# Escape % characters for configparser interpolation.
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without a DBAPI connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # No `compare_server_default=True`: it produces permanent false diffs on now() /
    # gen_random_uuid() and would break the `alembic check` acceptance criterion.
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and associate a connection with the context."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
