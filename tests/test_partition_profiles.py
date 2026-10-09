"""`partition_profiles.py` against the migrated database. R3-be-2.

* The seed (migration 0007) IS `BUILTIN_LAYOUTS`, id for id, slot for slot, sha for sha.
  `tests/test_deploy_precheck.py::TestLayoutProfiles` pins `BUILTIN_LAYOUTS` to the spec, so
  the three cannot drift.
* Detection: an unknown table announced by a live, registered board becomes one pending
  `detected` row, idempotently, capped, and never for a board announcing a known id, never
  for an unregistered or decommissioned one, and never at the cost of the announce.

Every test uses the rolled-back `session` fixture: nothing here commits.
"""

import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import partition_profiles
from fleetforge.db.models import Device
from fleetforge.firmware.manifest import BUILTIN_LAYOUTS, UNKNOWN_PARTITION_LAYOUT
from fleetforge.partition_profiles import (
    DETECTED_PENDING_CAP,
    load_layout_catalog,
    note_detected,
)
from tests.conftest import capture_logs
from tests.test_ingestor import ANNOUNCE, DEVICE_ID, NOW, publish, reload, seed_device

# tests/fixtures/wrong-layout-partitions.csv: slots of 1835008, matches no builtin layout.
WRONG_SHA = "47db53920359cfb4581532a293d8e563401f3abe37f9b282cc713039ac937c4c"
AB_SHA = BUILTIN_LAYOUTS["ab-4m-v1"].partition_table_sha256
UNKNOWN_ANNOUNCE = {
    **ANNOUNCE,
    "partition_layout": UNKNOWN_PARTITION_LAYOUT,
    "ota_slot_size": 1835008,
    "flash_chip_size": 4194304,
    "partition_table_sha256": WRONG_SHA,
}


async def profiles(session: AsyncSession, sha: str = WRONG_SHA) -> list[tuple[object, ...]]:
    rows = await session.execute(
        text(
            "SELECT layout_id, origin, ota_slot_size, flash_chip_size, detected_device_id, "
            "adopted_at IS NOT NULL FROM partition_profiles WHERE partition_table_sha256 = :sha"
        ),
        {"sha": sha},
    )
    return [tuple(row) for row in rows]


async def clear_non_builtin(session: AsyncSession) -> None:
    """Start from the seed. Never touches the builtins: the gate depends on them."""
    await session.execute(text("DELETE FROM partition_profiles WHERE origin <> 'builtin'"))


async def unknown_board(session: AsyncSession, **overrides: object) -> Device:
    values: dict[str, object] = {
        "partition_layout": UNKNOWN_PARTITION_LAYOUT,
        "ota_slot_size": 1835008,
        "flash_chip_size": 4194304,
        "partition_table_sha256": WRONG_SHA,
    }
    values.update(overrides)
    return await seed_device(session, **values)


class TestSeed:
    async def test_the_table_is_the_builtin_layouts(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        catalog = await load_layout_catalog(session)
        assert dict(catalog.layouts) == BUILTIN_LAYOUTS
        assert list(catalog.layouts) == list(BUILTIN_LAYOUTS), "builtins keep their order"
        assert catalog.pending == frozenset()
        origins = await session.execute(
            text("SELECT DISTINCT origin, adopted_at IS NOT NULL FROM partition_profiles")
        )
        assert [tuple(row) for row in origins] == [("builtin", True)]

    async def test_the_catalog_reads_adopted_and_pending_rows(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        await session.execute(
            text(
                "INSERT INTO partition_profiles "
                "(partition_table_sha256, layout_id, origin, ota_slot_size, adopted_at) VALUES "
                "(:a, 'my-map', 'user', 1835008, now()), (:p, NULL, 'detected', NULL, NULL)"
            ),
            {"a": "a" * 64, "p": WRONG_SHA},
        )
        catalog = await load_layout_catalog(session)
        assert list(catalog.layouts) == [*BUILTIN_LAYOUTS, "my-map"]
        assert catalog.layouts["my-map"].ota_slot_size == 1835008
        assert catalog.by_fingerprint["a" * 64] == "my-map"
        assert catalog.pending == frozenset({WRONG_SHA})
        assert catalog.resolve(UNKNOWN_PARTITION_LAYOUT, "a" * 64) == "my-map"
        assert catalog.resolve(UNKNOWN_PARTITION_LAYOUT, WRONG_SHA) is None
        assert catalog.resolve("ab-4m-v1", "a" * 64) == "ab-4m-v1"


class TestNoteDetected:
    async def test_an_unknown_table_lands_pending_with_what_the_board_reported(
        self, session: AsyncSession
    ) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session)
        with capture_logs() as records:
            assert await note_detected(session, board) is True
        assert await profiles(session) == [(None, "detected", 1835008, 4194304, DEVICE_ID, False)]
        lines = [r.getMessage() for r in records if r.levelno == logging.INFO]
        assert any(
            "recorded as a detected profile, pending adoption" in line and WRONG_SHA in line
            for line in lines
        ), lines

    async def test_a_second_announce_is_still_one_row(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session)
        assert await note_detected(session, board) is True
        assert await note_detected(session, board) is False
        assert len(await profiles(session)) == 1

    async def test_a_builtin_id_with_another_table_is_a_mismatch_not_a_detection(
        self, session: AsyncSession
    ) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session, partition_layout="ab-4m-v1")
        with capture_logs() as records:
            assert await note_detected(session, board) is False
        assert await profiles(session) == []
        assert not [r for r in records if r.levelno >= logging.WARNING]

    async def test_no_fingerprint_is_no_row(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session, partition_table_sha256=None)
        assert await note_detected(session, board) is False
        pending = await session.scalar(
            text("SELECT count(*) FROM partition_profiles WHERE origin <> 'builtin'")
        )
        assert pending == 0

    async def test_a_known_fingerprint_is_no_new_row(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session, partition_table_sha256=AB_SHA)
        assert await note_detected(session, board) is False
        assert await profiles(session, AB_SHA) == [
            ("ab-4m-v1", "builtin", 1966080, None, None, True)
        ]

    async def test_an_id_less_board_with_a_new_table_is_detected(
        self, session: AsyncSession
    ) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session, partition_layout=None)
        assert await note_detected(session, board) is True
        assert len(await profiles(session)) == 1

    @pytest.mark.parametrize("slot", [0, -1, None])
    async def test_an_unusable_slot_is_stored_null(
        self, session: AsyncSession, slot: int | None
    ) -> None:
        await clear_non_builtin(session)
        board = await unknown_board(session, ota_slot_size=slot)
        assert await note_detected(session, board) is True
        assert await profiles(session) == [(None, "detected", None, 4194304, DEVICE_ID, False)]

    async def test_at_the_cap_nothing_is_recorded_and_it_says_so(
        self, session: AsyncSession
    ) -> None:
        await clear_non_builtin(session)
        await session.execute(
            text(
                "INSERT INTO partition_profiles (partition_table_sha256, origin) "
                "SELECT lpad(to_hex(n), 64, '0'), 'detected' FROM generate_series(1, :cap) AS n"
            ),
            {"cap": DETECTED_PENDING_CAP},
        )
        board = await unknown_board(session)
        with capture_logs() as records:
            assert await note_detected(session, board) is False
        assert await profiles(session) == []
        warned = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        assert any(
            f"{DETECTED_PENDING_CAP} detected profiles are already pending adoption" in line
            for line in warned
        ), warned


class TestTheIngestorDetects:
    """Through `handle_up_message`, the way a real announce arrives."""

    async def test_an_unknown_announce_records_a_detected_profile(
        self, session: AsyncSession
    ) -> None:
        await clear_non_builtin(session)
        await seed_device(session)
        event = await publish(session, "announce", UNKNOWN_ANNOUNCE)
        assert event is not None
        assert await profiles(session) == [(None, "detected", 1835008, 4194304, DEVICE_ID, False)]

    async def test_a_retained_replay_is_still_one_row(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        await seed_device(session)
        await publish(session, "announce", UNKNOWN_ANNOUNCE)
        await publish(session, "announce", UNKNOWN_ANNOUNCE, retained=True)
        await publish(session, "announce", UNKNOWN_ANNOUNCE)
        assert len(await profiles(session)) == 1

    async def test_an_unregistered_device_records_nothing(self, session: AsyncSession) -> None:
        """The enrollment boundary: a board that is not in the registry creates nothing."""
        await clear_non_builtin(session)
        assert await publish(session, "announce", UNKNOWN_ANNOUNCE) is None
        assert await profiles(session) == []

    async def test_a_decommissioned_device_records_nothing(self, session: AsyncSession) -> None:
        await clear_non_builtin(session)
        await seed_device(session, decommissioned_at=NOW)
        assert await publish(session, "announce", UNKNOWN_ANNOUNCE) is None
        assert await profiles(session) == []

    async def test_a_failed_detection_never_loses_the_announce(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(session: AsyncSession, device: Device) -> bool:
            raise SQLAlchemyError("simulated")

        monkeypatch.setattr(partition_profiles, "note_detected", broken)
        await seed_device(session)
        with capture_logs() as records:
            event = await publish(session, "announce", {**UNKNOWN_ANNOUNCE, "fw_version": "9.9.9"})
        assert event is not None and event.fw_version == "9.9.9"
        assert (await reload(session)).fw_version == "9.9.9"
        warned = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        assert any("detected-profile bookkeeping failed; announce kept" in w for w in warned)

    async def test_a_database_error_inside_detection_rolls_back_only_the_savepoint(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real failed statement aborts the transaction unless the SAVEPOINT is rolled back;
        the event is emitted (pg_notify) after it, so this fails if the announce is lost.
        It is the deploy-order window: a new ingestor on a schema without the table."""

        async def missing_table(session: AsyncSession, device: Device) -> bool:
            await session.execute(text("SELECT 1 FROM partition_profiles_not_migrated_yet"))
            return True

        monkeypatch.setattr(partition_profiles, "note_detected", missing_table)
        await seed_device(session)
        event = await publish(session, "announce", {**UNKNOWN_ANNOUNCE, "fw_version": "9.9.8"})
        assert event is not None
        assert (await reload(session)).fw_version == "9.9.8"
