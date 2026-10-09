"""The `partition_profiles` table: what flash maps this server supports. R3-be-2.

The owner of the table, for reads and writes (the way `deploys.py` owns `deploy_events`).
Rationale for the columns and CHECKs lives on `db/models.py::PartitionProfile`; the
decisions are DECISIONS.md → R3-be-2 (D1-D8).

**What the server supports at runtime is this table, not a code dict.** The builtin rows
are seeded by migration `0007` from the same literals as `firmware.manifest.BUILTIN_LAYOUTS`
(the firmware contract, test-pinned equal). An operator adds a `user` row by fingerprint,
or adopts a `detected` row by naming it. Readers take a `LayoutCatalog`, one SELECT of the
whole table, loaded per request by the caller: `deploy_precheck.py` stays pure because the
deploy router loads the catalog and passes it in.

**Resolution (D7).** A board's *effective* layout is the id it announces when that id is an
adopted profile; otherwise the adopted profile whose fingerprint it announces. That second
step is the only way a board announcing `unknown` (R3-fw-5: its firmware knows only the
builtin ids) can ever be deployed to. Neither: refused as `unsupported_layout`.

**Detection (D6)** is `note_detected`, called by the ingestor after an announce updated a
live, registered device: an unknown fingerprint becomes one pending `detected` row,
idempotent (`ON CONFLICT DO NOTHING`, safe under QoS-1 redelivery and the retained replay)
and bounded by `DETECTED_PENDING_CAP`. A board announcing a known id with a different table
is a `partition_table_mismatch`, never a detection.
"""

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import Device, PartitionProfile
from fleetforge.firmware.manifest import BUILTIN_LAYOUTS, LayoutProfile

logger = logging.getLogger(__name__)

# D8. The largest OTA slot an operator may enter or adopt: `frontend/nginx.conf` sets
# `client_max_body_size 4m` on `location = /v1/artifact`, and a larger slot would let the
# API accept an upload that nginx answers with its own HTML 413 first.
# `tests/test_frontend_nginx.py` keeps the two in step.
MAX_PROFILE_SLOT_SIZE = 4 * 1024 * 1024

# A bound on pending detected rows, so a misbehaving fleet cannot grow the table without
# limit. Adopting or deleting one frees a place.
DETECTED_PENDING_CAP = 32

# Same cap and reason as the routers' `LIST_LIMIT`: a guard, not paging.
DEVICE_IDS_LIMIT = 500

ORIGIN_BUILTIN = "builtin"
ORIGIN_USER = "user"
ORIGIN_DETECTED = "detected"


@dataclass(frozen=True, slots=True)
class LayoutCatalog:
    """The profiles a gate decision reads: adopted by id, adopted by fingerprint, pending.

    `layouts` is ordered builtins first (in `BUILTIN_LAYOUTS` order, so the builtin-only
    sentences read exactly as they did before R3-be-2), then the rest by name.
    """

    layouts: Mapping[str, LayoutProfile]  # adopted only, by layout_id
    by_fingerprint: Mapping[str, str]  # adopted sha -> layout_id
    pending: frozenset[str]  # fingerprints of pending detected rows

    def resolve(self, layout: str | None, fingerprint: str | None) -> str | None:
        """The board's effective adopted layout id (D7), or `None`.

        The announced id wins when it is adopted, even if the fingerprint names another
        profile: the gate then refuses the board as `partition_table_mismatch`.
        """
        if layout is not None and layout in self.layouts:
            return layout
        if fingerprint is not None:
            return self.by_fingerprint.get(fingerprint)
        return None


def _order_key(
    origin: str, layout_id: str | None, created_at: dt.datetime | None
) -> tuple[int, int, str, float]:
    """Builtins in `BUILTIN_LAYOUTS` order, then adopted by name, then pending by age."""
    builtin_order = list(BUILTIN_LAYOUTS)
    if origin == ORIGIN_BUILTIN and layout_id in builtin_order:
        return (0, builtin_order.index(layout_id), "", 0.0)
    if layout_id is not None:
        return (1, 0, layout_id, 0.0)
    return (2, 0, "", created_at.timestamp() if created_at is not None else 0.0)


def ordered(profiles: list[PartitionProfile]) -> list[PartitionProfile]:
    """The list order of `GET /v1/partition-profiles` and of the catalog."""
    return sorted(profiles, key=lambda p: _order_key(p.origin, p.layout_id, p.created_at))


def _catalog(rows: list[tuple[str, str | None, int | None]]) -> LayoutCatalog:
    """(sha, layout_id, slot) rows, already ordered → a catalog."""
    layouts: dict[str, LayoutProfile] = {}
    by_fingerprint: dict[str, str] = {}
    pending: set[str] = set()
    for sha, layout_id, slot in rows:
        if layout_id is None:
            pending.add(sha)
            continue
        # `adopted_has_slot` guarantees the slot for an adopted row.
        layouts[layout_id] = LayoutProfile(slot if slot is not None else 0, sha)
        by_fingerprint[sha] = layout_id
    return LayoutCatalog(layouts=layouts, by_fingerprint=by_fingerprint, pending=frozenset(pending))


def builtin_catalog() -> LayoutCatalog:
    """The catalog a freshly migrated database holds: the builtins, nothing pending.

    For tests and offline use. Production reads the table (`load_layout_catalog`).
    """
    layouts = dict(BUILTIN_LAYOUTS)
    return LayoutCatalog(
        layouts=layouts,
        by_fingerprint={
            profile.partition_table_sha256: layout_id
            for layout_id, profile in layouts.items()
            if profile.partition_table_sha256 is not None
        },
        pending=frozenset(),
    )


async def load_layout_catalog(session: AsyncSession) -> LayoutCatalog:
    """One SELECT of every profile. Not `load_catalog`: that is the agent catalog."""
    profiles = list((await session.scalars(select(PartitionProfile))).all())
    return _catalog(
        [(p.partition_table_sha256, p.layout_id, p.ota_slot_size) for p in ordered(profiles)]
    )


# --- detection (the ingestor) -----------------------------------------------------------

# One statement, idempotent: a fingerprint that is already builtin, user, adopted or
# pending conflicts on the PK and inserts nothing. Not for a board announcing an id that is
# already a profile (that board is a `partition_table_mismatch`, not a new map). A NULL
# `:layout` never equals anything, so an id-less board with a fingerprint is detected.
# Casts, because the select list gives PostgreSQL no column type to infer a parameter from.
_DETECT_SQL = text(
    """
    INSERT INTO partition_profiles
           (partition_table_sha256, origin, ota_slot_size, flash_chip_size, detected_device_id)
    SELECT CAST(:sha AS TEXT), 'detected', CAST(:slot AS BIGINT), CAST(:flash AS BIGINT),
           CAST(:device_id AS TEXT)
     WHERE NOT EXISTS (SELECT 1 FROM partition_profiles WHERE layout_id = CAST(:layout AS TEXT))
       AND (SELECT count(*) FROM partition_profiles WHERE adopted_at IS NULL) < :cap
    ON CONFLICT (partition_table_sha256) DO NOTHING
    RETURNING partition_table_sha256
    """
)

# Why nothing was inserted: known already (the common case), named already, or the cap.
_DETECT_MISS_SQL = text(
    """
    SELECT EXISTS (SELECT 1 FROM partition_profiles
                    WHERE partition_table_sha256 = CAST(:sha AS TEXT)) AS known,
           EXISTS (SELECT 1 FROM partition_profiles
                    WHERE layout_id = CAST(:layout AS TEXT)) AS named,
           (SELECT count(*) FROM partition_profiles WHERE adopted_at IS NULL) AS pending
    """
)


async def note_detected(session: AsyncSession, device: Device) -> bool:
    """Record the board's partition table as a pending `detected` profile if it is new.

    `device` is the row the announce just updated (normalised values), so it is a live,
    registered board: never call this for an unregistered or decommissioned one. Returns
    whether a row was inserted. Does not commit. The ingestor runs it in a SAVEPOINT, so a
    failure here never costs the announce.
    """
    sha = device.partition_table_sha256
    if sha is None:
        return False
    slot = device.ota_slot_size
    # The announce has no lower bound on `ota_slot_size`; `slot_positive` would raise.
    slot_value = slot if isinstance(slot, int) and not isinstance(slot, bool) and slot > 0 else None
    params = {
        "sha": sha,
        "slot": slot_value,
        "flash": device.flash_chip_size,
        "device_id": device.device_id,
        "layout": device.partition_layout,
        "cap": DETECTED_PENDING_CAP,
    }
    inserted = await session.scalar(_DETECT_SQL, params)
    if inserted is not None:
        logger.info(
            "device %s announced partition layout %s with an unknown partition table "
            "fingerprint %s; recorded as a detected profile, pending adoption",
            device.device_id,
            device.partition_layout,
            sha,
        )
        return True
    miss = (await session.execute(_DETECT_MISS_SQL, {"sha": sha, "layout": params["layout"]})).one()
    if not miss.known and not miss.named:
        logger.warning(
            "device %s announced an unknown partition table fingerprint %s; not recorded: "
            "%d detected profiles are already pending adoption (cap %d)",
            device.device_id,
            sha,
            miss.pending,
            DETECTED_PENDING_CAP,
        )
    return False


# --- the operator API (api/routers/partition_profiles.py) -------------------------------


async def list_profiles(session: AsyncSession) -> list[PartitionProfile]:
    """Every profile, in list order."""
    return ordered(list((await session.scalars(select(PartitionProfile))).all()))


async def device_ids_by_fingerprint(session: AsyncSession) -> dict[str, list[str]]:
    """Live boards per announced fingerprint, sorted, at most `DEVICE_IDS_LIMIT` each."""
    rows = (
        await session.execute(
            select(Device.partition_table_sha256, Device.device_id).where(
                Device.decommissioned_at.is_(None), Device.partition_table_sha256.is_not(None)
            )
        )
    ).all()
    grouped: dict[str, list[str]] = {}
    for sha, device_id in rows:
        grouped.setdefault(sha, []).append(device_id)
    return {sha: sorted(ids)[:DEVICE_IDS_LIMIT] for sha, ids in grouped.items()}


async def get_profile(
    session: AsyncSession, sha: str, *, for_update: bool = False
) -> PartitionProfile | None:
    """One profile by fingerprint, optionally locked for the rest of the transaction."""
    stmt = select(PartitionProfile).where(PartitionProfile.partition_table_sha256 == sha)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.scalars(stmt.execution_options(populate_existing=True))).one_or_none()


async def owner_of_layout_id(session: AsyncSession, layout_id: str) -> str | None:
    """The fingerprint of the profile already named `layout_id`, if any."""
    owner: str | None = await session.scalar(
        select(PartitionProfile.partition_table_sha256).where(
            PartitionProfile.layout_id == layout_id
        )
    )
    return owner


async def insert_user_profile(
    session: AsyncSession, *, sha: str, layout_id: str, ota_slot_size: int
) -> PartitionProfile:
    """INSERT an operator profile, adopted at insert. Raises `IntegrityError` on a clash."""
    stmt = (
        insert(PartitionProfile)
        .values(
            partition_table_sha256=sha,
            layout_id=layout_id,
            origin=ORIGIN_USER,
            ota_slot_size=ota_slot_size,
            adopted_at=func.now(),
        )
        .returning(PartitionProfile)
    )
    return (await session.scalars(stmt)).one()


async def adopt_profile(
    session: AsyncSession, *, sha: str, layout_id: str, ota_slot_size: int | None
) -> PartitionProfile | None:
    """Name a pending profile, one-way. `None` when it is not (or no longer) pending.

    The measured slot wins: `ota_slot_size` only fills a slot the board did not report.
    Raises `IntegrityError` when `layout_id` is taken.
    """
    stmt = (
        update(PartitionProfile)
        .where(
            PartitionProfile.partition_table_sha256 == sha,
            PartitionProfile.adopted_at.is_(None),
        )
        .values(
            layout_id=layout_id,
            adopted_at=func.now(),
            ota_slot_size=func.coalesce(PartitionProfile.ota_slot_size, ota_slot_size),
        )
        .returning(PartitionProfile)
        .execution_options(populate_existing=True, synchronize_session=False)
    )
    return (await session.scalars(stmt)).one_or_none()


async def artifacts_labelled(session: AsyncSession, layout_id: str) -> int:
    """How many stored artifacts carry `layout_id` as their partition layout."""
    count = await session.scalar(
        text("SELECT count(*) FROM artifacts WHERE partition_layout = :layout"),
        {"layout": layout_id},
    )
    return int(count or 0)


async def delete_profile(session: AsyncSession, sha: str) -> None:
    """DELETE one non-builtin profile. The caller has checked what may be deleted."""
    await session.execute(
        delete(PartitionProfile)
        .where(
            PartitionProfile.partition_table_sha256 == sha,
            PartitionProfile.origin != ORIGIN_BUILTIN,
        )
        .execution_options(synchronize_session=False)
    )
