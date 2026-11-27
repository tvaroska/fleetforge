"""The migration builds the expected schema, matches the models, and reverses."""

import asyncio
import contextlib
import os
import sys
import time

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from fleetforge.db.base import ALEMBIC_ADVISORY_LOCK_KEY
from tests.conftest import (
    REPO_ROOT,
    TEST_DB_NAME,
    alembic_config,
    database_url_for,
    drop_database,
    recreate_database,
    run_alembic,
    run_alembic_check,
    worker_db_name,
)

EXPECTED_TABLES = {
    "admin_tokens",
    "alembic_version",
    "artifact_deltas",
    "artifact_versions",
    "artifacts",
    "build_jobs",
    "builds",
    "deploy_events",
    "deploy_policies",
    "deploy_policy_runs",
    "device_commands",
    "device_faults",
    "device_groups",
    "device_logs",
    "device_metrics",
    "device_progress",
    "device_telemetry",
    "devices",
    "enrollment_tokens",
    "gateway_relays",
    "partition_profiles",
    "project_members",
    "project_rollouts",
    "project_secrets",
    "projects",
    "users",
}

MIGRATION_TEST_DB = worker_db_name("fleetforge_migration_test")
MIGRATION_RACE_TEST_DB = worker_db_name("fleetforge_migration_race_test")
MIGRATION_POPULATED_TEST_DB = worker_db_name("fleetforge_migration_populated_test")


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


async def test_migration_downgrades_cleanly(engine: AsyncEngine) -> None:
    """upgrade -> downgrade -> upgrade on a throwaway database.

    `alembic/versions/` is a CRITICAL.md path: a migration that cannot be reversed is
    a schema change with no way back. Runs against its own database so it cannot tear
    the schema out from under the rest of the suite.
    """
    del engine
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


_ADVISORY_LOCK_COUNT_SQL = text(
    "SELECT count(*) FROM pg_locks "
    "WHERE locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND objsubid = 1 "
    "AND ((classid::bigint << 32) | (objid::bigint & 4294967295)) = :key "
    "AND granted = :granted"
)


@pytest.mark.integration
async def test_concurrent_alembic_upgrades_serialize_with_advisory_lock(
    engine: AsyncEngine,
) -> None:
    """T-27: multi-replica `alembic upgrade head` serializes behind `pg_advisory_lock`."""
    del engine
    url = database_url_for(MIGRATION_RACE_TEST_DB)
    sub_env = {**os.environ, "DATABASE_URL": url}
    cmd = [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", "head"]
    expected_head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert expected_head is not None

    await recreate_database(MIGRATION_RACE_TEST_DB)
    try:
        # Hold the exact advisory lock on a separate connection and poll pg_locks until the
        # alembic subprocess is demonstrably queued (`granted = false`) before releasing.
        lock_engine = create_async_engine(url)
        try:
            async with lock_engine.connect() as lock_conn:
                await lock_conn.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": ALEMBIC_ADVISORY_LOCK_KEY},
                )
                await lock_conn.commit()
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(REPO_ROOT),
                    env=sub_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                unlocked = False
                try:
                    deadline = time.monotonic() + 20.0
                    queued = 0
                    while time.monotonic() < deadline:
                        if proc.returncode is not None:
                            out, err = await proc.communicate()
                            pytest.fail(
                                f"alembic exited early ({proc.returncode}) while advisory lock "
                                f"was held: stdout={out.decode()} stderr={err.decode()}"
                            )
                        queued = int(
                            await lock_conn.scalar(
                                _ADVISORY_LOCK_COUNT_SQL,
                                {"key": ALEMBIC_ADVISORY_LOCK_KEY, "granted": False},
                            )
                            or 0
                        )
                        if queued >= 1:
                            break
                        await asyncio.sleep(0.1)
                    assert queued >= 1, "alembic subprocess never queued on pg_advisory_lock"
                    assert await _tables_in(url) == set()

                    await lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": ALEMBIC_ADVISORY_LOCK_KEY},
                    )
                    await lock_conn.commit()
                    unlocked = True
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
                    assert proc.returncode == 0, (
                        f"stdout={stdout.decode()} stderr={stderr.decode()}"
                    )
                finally:
                    if not unlocked:
                        with contextlib.suppress(Exception):  # noqa: BLE001
                            await lock_conn.execute(
                                text("SELECT pg_advisory_unlock(:key)"),
                                {"key": ALEMBIC_ADVISORY_LOCK_KEY},
                            )
                            await lock_conn.commit()
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
        finally:
            await lock_engine.dispose()
        assert await _tables_in(url) == EXPECTED_TABLES

        # Race two independent OS processes simultaneously against an empty database.
        await recreate_database(MIGRATION_RACE_TEST_DB)
        proc_a = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REPO_ROOT),
            env=sub_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc_b = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REPO_ROOT),
            env=sub_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            (out_a, err_a), (out_b, err_b) = await asyncio.wait_for(
                asyncio.gather(proc_a.communicate(), proc_b.communicate()),
                timeout=90,
            )
        finally:
            for p in (proc_a, proc_b):
                if p.returncode is None:
                    p.kill()
                    await p.wait()
        assert proc_a.returncode == 0, f"proc_a failed: {out_a.decode()} {err_a.decode()}"
        assert proc_b.returncode == 0, f"proc_b failed: {out_b.decode()} {err_b.decode()}"
        assert await _tables_in(url) == EXPECTED_TABLES

        verify_engine = create_async_engine(url)
        try:
            async with verify_engine.connect() as conn:
                held = await conn.scalar(
                    _ADVISORY_LOCK_COUNT_SQL,
                    {"key": ALEMBIC_ADVISORY_LOCK_KEY, "granted": True},
                )
                assert held == 0
                version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert version == expected_head
        finally:
            await verify_engine.dispose()
    finally:
        await drop_database(MIGRATION_RACE_TEST_DB)


@pytest.mark.integration
async def test_migration_upgrade_downgrade_with_populated_rows(engine: AsyncEngine) -> None:
    """T-30: staged `0010 -> 0017 -> head -> 0010 -> head` preserves populated rows and NOT NULL backfills."""
    del engine
    url = database_url_for(MIGRATION_POPULATED_TEST_DB)
    await recreate_database(MIGRATION_POPULATED_TEST_DB)
    sha_a = "a" * 64
    sha_b = "b" * 64
    sha_patch = "c" * 64
    try:
        await run_alembic(url, "upgrade", "0010")
        db_engine = create_async_engine(url)
        try:
            # Seed production-shaped rows prior to 0011..0017 NOT NULL backfills.
            async with db_engine.begin() as conn:
                project_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO projects (slug, name, description) "
                            "VALUES ('rover', 'Rover Fleet', 'Field rovers') RETURNING id"
                        )
                    )
                ).scalar_one()
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (username, email, password_hash) "
                            "VALUES ('operator1', 'op1@example.com', 'hash') RETURNING id"
                        )
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO project_members (project_id, user_id, role) "
                        "VALUES (:pid, :uid, 'operator')"
                    ),
                    {"pid": project_id, "uid": user_id},
                )
                group_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO device_groups (project_id, name) "
                            "VALUES (:pid, 'canary') RETURNING id"
                        ),
                        {"pid": project_id},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO devices (device_id, project_id, group_id, platform_type, "
                        "fw_version, link_type, power_class, partition_layout) "
                        "VALUES ('a4cf12b3de01', :pid, :gid, 'esp32s3', '1.0.0', 'wifi', "
                        "'always_on', 'ab-4m-v1')"
                    ),
                    {"pid": project_id, "gid": group_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO artifacts (sha256, size_bytes, kind, target, partition_layout) "
                        "VALUES (:sha_a, 65536, 'user_firmware', 'esp32s3', 'ab-4m-v1'), "
                        "(:sha_b, 65600, 'user_firmware', 'esp32s3', 'ab-4m-v1')"
                    ),
                    {"sha_a": sha_a, "sha_b": sha_b},
                )
                await conn.execute(
                    text(
                        "INSERT INTO artifact_versions (project_id, target, version, sha256) "
                        "VALUES (:pid, 'esp32s3', '1.0.0', :sha_a)"
                    ),
                    {"pid": project_id, "sha_a": sha_a},
                )
                await conn.execute(
                    text(
                        "INSERT INTO device_logs (device_id, logged_at, received_at, level, text) "
                        "VALUES ('a4cf12b3de01', now(), now(), 'I', 'booted')"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO device_commands (id, device_id, type) "
                        "VALUES ('cmd001', 'a4cf12b3de01', 'reboot')"
                    )
                )

            # Upgrade through 0011 (NOT NULL project/device defaults), 0013 (NOT NULL artifact_type),
            # 0014..0017 over populated rows, then populate 0011..0017 columns/tables before 0018..0021.
            await run_alembic(url, "upgrade", "0017")

            async with db_engine.begin() as conn:
                proj_defaults = (
                    await conn.execute(
                        text(
                            "SELECT track_mode, auto_deploy_target, secrets_rev "
                            "FROM projects WHERE id = :pid"
                        ),
                        {"pid": project_id},
                    )
                ).one()
                assert tuple(proj_defaults) == ("off", "off", 0)
                art_type = await conn.scalar(
                    text("SELECT artifact_type FROM artifacts WHERE sha256 = :sha_a"),
                    {"sha_a": sha_a},
                )
                assert art_type == "app"

                await conn.execute(
                    text(
                        "INSERT INTO project_secrets (project_id, key, ciphertext) "
                        "VALUES (:pid, 'WIFI_PASS', 'enc-blob')"
                    ),
                    {"pid": project_id},
                )
                await conn.execute(
                    text("UPDATE devices SET safe_mode = true WHERE device_id = 'a4cf12b3de01'")
                )
                await conn.execute(
                    text(
                        "UPDATE artifacts SET sim_status = 'passed', sim_detail = 'ok' "
                        "WHERE sha256 = :sha_a"
                    ),
                    {"sha_a": sha_a},
                )
                build_job_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO build_jobs (project_id, repo_url, git_ref, target, version, "
                            "idf_image, status) "
                            "VALUES (:pid, 'https://github.com/example/rover', 'main', 'esp32s3', "
                            "'1.1.0', 'espressif/idf:v5.5.5', 'queued') RETURNING id"
                        ),
                        {"pid": project_id},
                    )
                ).scalar_one()

            # Upgrade 0017 -> head over populated build_jobs and non-NULL artifacts.sim_status.
            await run_alembic(url, "upgrade", "head")

            async with db_engine.begin() as conn:
                dev_row = (
                    await conn.execute(
                        text(
                            "SELECT fw_version, safe_mode FROM devices "
                            "WHERE device_id = 'a4cf12b3de01'"
                        )
                    )
                ).one()
                assert tuple(dev_row) == ("1.0.0", True)

                await conn.execute(
                    text(
                        "UPDATE artifacts SET sim_runner = 'qemu', sim_lease_owner = 'sim-1' "
                        "WHERE sha256 = :sha_a"
                    ),
                    {"sha_a": sha_a},
                )
                await conn.execute(
                    text(
                        "UPDATE build_jobs SET lease_owner = 'builder-1', lease_expires_at = now() "
                        "WHERE id = :jid"
                    ),
                    {"jid": build_job_id},
                )
                policy_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO deploy_policies (project_id, name, match_mode, match_ref, "
                            "ring_group_ids) VALUES (:pid, 'canary-first', 'branch', 'main', "
                            "ARRAY[:gid]::uuid[]) RETURNING id"
                        ),
                        {"pid": project_id, "gid": group_id},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO deploy_policy_runs (policy_id, version) VALUES (:pol, '1.0.0')"
                    ),
                    {"pol": policy_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO project_rollouts (project_id, version, policy_id) "
                        "VALUES (:pid, '1.0.0', :pol)"
                    ),
                    {"pid": project_id, "pol": policy_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO gateway_relays (gateway_id, relay_base_url, cached_artifacts) "
                        "VALUES ('a4cf12b3de01', 'http://192.168.4.1:8080', ARRAY[:sha_a]::text[])"
                    ),
                    {"sha_a": sha_a},
                )
                await conn.execute(
                    text(
                        "INSERT INTO artifact_deltas (from_sha256, to_sha256, patch_sha256, "
                        "patch_size_bytes, target_size_bytes) "
                        "VALUES (:sha_a, :sha_b, :patch, 512, 65600)"
                    ),
                    {"sha_a": sha_a, "sha_b": sha_b, "patch": sha_patch},
                )

            # Downgrade all the way back to 0010 with every table populated, then verify all 9
            # pre-0011 seeded tables survived intact.
            await run_alembic(url, "downgrade", "0010")

            async with db_engine.connect() as conn:
                for table_name, expected_count in (
                    ("projects", 2),
                    ("users", 1),
                    ("project_members", 1),
                    ("device_groups", 1),
                    ("devices", 1),
                    ("artifacts", 2),
                    ("artifact_versions", 1),
                    ("device_logs", 1),
                    ("device_commands", 1),
                ):
                    count = await conn.scalar(text(f"SELECT count(*) FROM {table_name}"))  # noqa: S608
                    assert count == expected_count, (
                        f"{table_name}: expected {expected_count}, got {count}"
                    )

            await run_alembic(url, "upgrade", "head")
            await run_alembic_check(url)
            assert await _tables_in(url) == EXPECTED_TABLES
        finally:
            await db_engine.dispose()
    finally:
        await drop_database(MIGRATION_POPULATED_TEST_DB)


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


async def test_device_logs_refuses_a_negative_gap(session: AsyncSession) -> None:
    """R4-be-1: `dropped` counts lost lines; a negative count is a writer bug, not data."""
    await session.execute(
        text(
            "INSERT INTO devices (device_id, platform_type, link_type, power_class) "
            "VALUES ('a4cf12b3de9f', 'esp32', 'wifi', 'always_on')"
        )
    )
    with pytest.raises(IntegrityError, match="ck_device_logs_dropped_non_negative"):
        await session.execute(
            text(
                "INSERT INTO device_logs (device_id, logged_at, received_at, level, dropped) "
                "VALUES ('a4cf12b3de9f', now(), now(), 'I', -1)"
            )
        )


async def test_the_device_health_columns_are_nullable_with_no_default(
    session: AsyncSession,
) -> None:
    """R4-be-1, migration 0009: NULL = not reported, and adding them rewrote no row."""
    rows = (
        await session.execute(
            text(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'devices' AND column_name IN "
                "('last_reset_reason', 'health_at', 'uptime_s', 'rssi', 'free_heap', "
                "'min_free_heap', 'boot_ok')"
            )
        )
    ).all()
    assert len(rows) == 7
    assert {(row[1], row[2]) for row in rows} == {("YES", None)}


async def test_device_commands_publish_failed_defaults_false_and_answers_are_nullable(
    session: AsyncSession,
) -> None:
    """R5-be-2, migration 0010: a sent verb has no answer yet, and may never get one."""
    await session.execute(
        text(
            "INSERT INTO devices (device_id, platform_type, link_type, power_class) "
            "VALUES ('a4cf12b3de9e', 'esp32', 'wifi', 'always_on')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO device_commands (id, device_id, type) "
            "VALUES ('c0ffee', 'a4cf12b3de9e', 'reboot')"
        )
    )
    row = (
        await session.execute(
            text(
                "SELECT publish_failed, sent_at IS NOT NULL, cmd_id, sent_by, answered_at, "
                "result, answer_cmd_id, answer_detail FROM device_commands WHERE id = 'c0ffee'"
            )
        )
    ).one()
    assert tuple(row) == (False, True, None, None, None, None, None, None)
    # Operational data about a board, never KPI history: it goes with the device.
    await session.execute(text("DELETE FROM devices WHERE device_id = 'a4cf12b3de9e'"))
    remaining = await session.scalar(text("SELECT count(*) FROM device_commands"))
    assert remaining == 0


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


async def test_default_project_is_seeded(session: AsyncSession) -> None:
    """P1-db-1: the `'default'` project is seeded with `DEFAULT_PROJECT_ID`."""
    row = (
        await session.execute(
            text("SELECT id::text, slug, name FROM projects WHERE slug = 'default'")
        )
    ).one()
    assert tuple(row) == ("00000000-0000-4000-8000-000000000001", "default", "Default")


async def test_projects_refuses_an_invalid_slug(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_projects_slug_format"):
        await session.execute(text("INSERT INTO projects (slug, name) VALUES ('Bad_Slug', 'Bad')"))


async def test_users_refuses_an_invalid_username(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_users_username_format"):
        await session.execute(
            text("INSERT INTO users (username, password_hash) VALUES ('-bad', 'hash')")
        )


async def test_project_members_refuses_an_invalid_role(session: AsyncSession) -> None:
    uid = (
        await session.execute(
            text("INSERT INTO users (username, password_hash) VALUES ('alice', 'h') RETURNING id")
        )
    ).scalar_one()
    with pytest.raises(IntegrityError, match="ck_project_members_role"):
        await session.execute(
            text(
                "INSERT INTO project_members (project_id, user_id, role) "
                "VALUES ('00000000-0000-4000-8000-000000000001'::uuid, :uid, 'guest')"
            ),
            {"uid": uid},
        )


async def test_device_groups_name_is_unique_per_project_not_globally(
    session: AsyncSession,
) -> None:
    p2 = (
        await session.execute(
            text("INSERT INTO projects (slug, name) VALUES ('caelus', 'Caelus') RETURNING id")
        )
    ).scalar_one()
    await session.execute(text("INSERT INTO device_groups (name) VALUES ('canary')"))
    await session.execute(
        text("INSERT INTO device_groups (project_id, name) VALUES (:p2, 'canary')"),
        {"p2": p2},
    )
    with pytest.raises(IntegrityError, match="uq_device_groups_project_id_name"):
        await session.execute(
            text("INSERT INTO device_groups (project_id, name) VALUES (:p2, 'canary')"),
            {"p2": p2},
        )
