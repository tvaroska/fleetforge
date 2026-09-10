"""The migration builds the expected schema, matches the models, and reverses."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from tests.conftest import (
    TEST_DB_NAME,
    database_url_for,
    drop_database,
    recreate_database,
    run_alembic,
    run_alembic_check,
)

EXPECTED_TABLES = {
    "admin_tokens",
    "alembic_version",
    "deploy_events",
    "device_groups",
    "device_progress",
    "devices",
    "enrollment_tokens",
}

MIGRATION_TEST_DB = "fleetforge_migration_test"


async def _tables_in(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def test_expected_tables_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    assert {row[0] for row in rows} == EXPECTED_TABLES


async def test_models_match_migration(engine: AsyncEngine) -> None:
    """`alembic check`: db/models.py and migration 0001 cannot drift apart."""
    await run_alembic_check(database_url_for(TEST_DB_NAME))


async def test_migration_downgrades_cleanly() -> None:
    """upgrade -> downgrade -> upgrade on a throwaway database.

    `alembic/versions/` is a CRITICAL.md path: a migration that cannot be reversed is
    a schema change with no way back. Runs against its own database so it cannot tear
    the schema out from under the rest of the suite.
    """
    url = database_url_for(MIGRATION_TEST_DB)
    await recreate_database(MIGRATION_TEST_DB)
    try:
        await run_alembic(url, "upgrade", "head")
        assert await _tables_in(url) == EXPECTED_TABLES

        await run_alembic(url, "downgrade", "base")
        assert await _tables_in(url) == {"alembic_version"}

        await run_alembic(url, "upgrade", "head")
        assert await _tables_in(url) == EXPECTED_TABLES
    finally:
        await drop_database(MIGRATION_TEST_DB)
