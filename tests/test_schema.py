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
    "artifact_versions",
    "artifacts",
    "builds",
    "deploy_events",
    "device_groups",
    "device_progress",
    "devices",
    "enrollment_tokens",
    "partition_profiles",
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


async def test_artifacts_has_a_nullable_boolean_library_marker_verdict(
    session: AsyncSession,
) -> None:
    """R3-be-1, migration 0008: NULL = never scanned (every older row), never warns."""
    row = (
        await session.execute(
            text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'artifacts' "
                "AND column_name = 'has_lib_marker'"
            )
        )
    ).one()
    assert tuple(row) == ("boolean", "YES", None)


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


# ---------------------------------------------------------------------------
# partition_profiles (R3-be-2): the adoption state machine lives in CHECKs, so no writer
# can leave a named-but-pending row, a pending builtin, or a deployable row with no slot.
# ---------------------------------------------------------------------------

_PROFILE_INSERT = text(
    "INSERT INTO partition_profiles "
    "(partition_table_sha256, layout_id, origin, ota_slot_size, adopted_at) "
    "VALUES (:sha, :layout_id, :origin, :slot, "
    "CASE WHEN :adopted THEN now() ELSE NULL END)"
)


async def _profile(
    session: AsyncSession,
    *,
    sha: str = "e" * 64,
    layout_id: str | None = "my-map",
    origin: str = "user",
    slot: int | None = 1966080,
    adopted: bool = True,
) -> None:
    await session.execute(
        _PROFILE_INSERT,
        {"sha": sha, "layout_id": layout_id, "origin": origin, "slot": slot, "adopted": adopted},
    )


async def test_the_builtins_are_seeded(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT layout_id, origin, ota_slot_size, partition_table_sha256, "
            "adopted_at IS NOT NULL FROM partition_profiles WHERE origin = 'builtin' "
            "ORDER BY layout_id"
        )
    )
    assert [tuple(row) for row in rows] == [
        (
            "ab-4m-arduino-v1",
            "builtin",
            1966080,
            "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4",
            True,
        ),
        (
            "ab-4m-v1",
            "builtin",
            1966080,
            "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed",
            True,
        ),
    ]


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"sha": "E" * 64}, "ck_partition_profiles_sha256_format"),
        ({"sha": "e" * 63}, "ck_partition_profiles_sha256_format"),
        ({"origin": "detected", "adopted": False}, "ck_partition_profiles_adopted_is_named"),
        ({"adopted": False}, "ck_partition_profiles_adopted_is_named"),
        ({"layout_id": None}, "ck_partition_profiles_adopted_is_named"),
        (
            {"origin": "builtin", "layout_id": None, "adopted": False},
            "ck_partition_profiles_only_detected_pending",
        ),
        (
            {"origin": "user", "layout_id": None, "adopted": False},
            "ck_partition_profiles_only_detected_pending",
        ),
        ({"layout_id": "unknown"}, "ck_partition_profiles_layout_id_format"),
        ({"layout_id": "my/map"}, "ck_partition_profiles_layout_id_format"),
        ({"layout_id": "My-Map"}, "ck_partition_profiles_layout_id_format"),
        ({"layout_id": "-map"}, "ck_partition_profiles_layout_id_format"),
        ({"layout_id": "m" * 33}, "ck_partition_profiles_layout_id_format"),
        ({"slot": 0}, "ck_partition_profiles_slot_positive"),
        ({"slot": None}, "ck_partition_profiles_adopted_has_slot"),
        ({"origin": "vendor"}, "ck_partition_profiles_origin"),
    ],
)
async def test_partition_profiles_refuses_an_impossible_row(
    session: AsyncSession, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        await _profile(session, **overrides)  # type: ignore[arg-type]


async def test_a_layout_id_names_one_profile(session: AsyncSession) -> None:
    await _profile(session)
    with pytest.raises(IntegrityError, match="uq_partition_profiles_layout_id"):
        await _profile(session, sha="f" * 64)


async def test_many_pending_detected_profiles_coexist(session: AsyncSession) -> None:
    """NULL is never equal to NULL, so UNIQUE(layout_id) allows any number of pending rows."""
    for digit in "abc":
        await _profile(
            session, sha=digit * 64, layout_id=None, origin="detected", slot=None, adopted=False
        )
    await _profile(session, sha="d" * 64, layout_id="adopted-map", origin="detected")
    pending = await session.scalar(
        text("SELECT count(*) FROM partition_profiles WHERE adopted_at IS NULL")
    )
    assert pending == 3
