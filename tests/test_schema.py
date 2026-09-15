"""The migration builds the expected schema, matches the models, and reverses."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
    "artifacts",
    "builds",
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


# ---------------------------------------------------------------------------
# The CHECKs that survive autogenerate (S0-infra-4). Cheap, and they are the only
# thing standing between a bad digest and a key that names no object.
# ---------------------------------------------------------------------------


async def test_artifacts_refuses_a_digest_that_is_not_a_digest(session: AsyncSession) -> None:
    """`artifacts.sha256` IS the object key — `storage.blobs.blob_key` would refuse it."""
    with pytest.raises(IntegrityError, match="ck_artifacts_sha256_format"):
        await session.execute(
            text(
                "INSERT INTO artifacts (sha256, size_bytes, kind) "
                "VALUES ('not-a-digest', 1, 'user_firmware')"
            )
        )


async def test_artifacts_refuses_an_uppercase_digest(session: AsyncSession) -> None:
    """The DB agrees with `blobs.SHA256_HEX`: one spelling, lowercase, never repaired."""
    with pytest.raises(IntegrityError, match="ck_artifacts_sha256_format"):
        await session.execute(
            text(
                "INSERT INTO artifacts (sha256, size_bytes, kind) VALUES (:d, 1, 'user_firmware')"
            ),
            {"d": "A" * 64},
        )


async def test_artifacts_refuses_a_zero_byte_artifact(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_artifacts_size_positive"):
        await session.execute(
            text(
                "INSERT INTO artifacts (sha256, size_bytes, kind) VALUES (:d, 0, 'user_firmware')"
            ),
            {"d": "a" * 64},
        )


async def test_builds_refuses_outputs_that_are_not_an_object(session: AsyncSession) -> None:
    """JSONB would happily store `[]`; a reader doing `outputs['app']` on it gets a 500."""
    with pytest.raises(IntegrityError, match="ck_builds_outputs_object"):
        await session.execute(
            text(
                "INSERT INTO builds (cache_key, key_inputs, outputs) "
                "VALUES (repeat('a', 64), '{}'::jsonb, '[]'::jsonb)"
            )
        )


async def test_builds_accepts_a_well_formed_row(session: AsyncSession) -> None:
    """A vacuous pass on the four tests above would be worse than no test at all."""
    await session.execute(
        text(
            "INSERT INTO builds (cache_key, key_inputs, outputs, target) "
            "VALUES (repeat('b', 64), '{\"target\": \"esp32c6\"}'::jsonb, "
            '\'{"app": {"sha256": "' + "c" * 64 + "\", \"offset\": 65536}}'::jsonb, 'esp32c6')"
        )
    )
    row = await session.execute(text("SELECT outputs -> 'app' ->> 'offset' FROM builds"))
    assert row.scalar_one() == "65536"
