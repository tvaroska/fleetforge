"""`/v1/partition-profiles` — the flash maps this server supports. R3-be-2. Admin-only.

`GET ""` lists every profile; `POST ""` adds an operator (`user`) profile by fingerprint,
named and deployable at once; `PATCH "/{sha}"` adopts a pending `detected` profile by naming
it; `DELETE "/{sha}"` removes a profile nothing depends on. The table and its rules belong
to `fleetforge/partition_profiles.py`; the decisions are DECISIONS.md → R3-be-2.

**Builtins never change** (D4): a PATCH or DELETE on one is a 409. They are the firmware
contract (every agent and library build knows their ids and fingerprints), and a new one is
a migration. **A name never changes once given**, for a user or an adopted profile alike:
artifacts carry it as text, so renaming would orphan every image uploaded for it. Such a
profile may be deleted only while no artifact names it. A pending detected profile may be
deleted at any time; a board still announcing that map records it again at its next
announce, which makes DELETE the way to clear one left by a re-flashed board.

Every `detail` is plain text lifted verbatim into the dashboard banner (`api.ts::detailOf`):
no backticks, no SQL, no constraint names. A path fingerprint that is not 64 lowercase hex
is a 404 and never reaches SQL. No event type of its own: profiles change on operator
action (this router) or on an announce, which already emits `device.announce`.
"""

import logging
import re
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import AdminDep, SessionMakerDep, bearer_scheme, cookie_scheme
from fleetforge.api.schemas import (
    PartitionProfileAdopt,
    PartitionProfileCreate,
    PartitionProfileList,
    PartitionProfileSummary,
    ProfileOrigin,
)
from fleetforge.db.models import PartitionProfile
from fleetforge.partition_profiles import (
    MAX_PROFILE_SLOT_SIZE,
    ORIGIN_BUILTIN,
    adopt_profile,
    artifacts_labelled,
    delete_profile,
    device_ids_by_fingerprint,
    get_profile,
    insert_user_profile,
    list_profiles,
    owner_of_layout_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/partition-profiles",
    tags=["partition-profiles"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

BUILTIN_IMMUTABLE = (
    "builtin profiles are part of the firmware contract and never change: every agent and "
    "library build knows their names and fingerprints"
)
NAME_IS_FINAL = "a profile's name never changes once given: artifacts are labelled with it"


def _no_such_profile(sha: str) -> HTTPException:
    # Only a well-formed fingerprint is echoed; anything else is not ours to repeat.
    named = sha if _SHA256.match(sha) else "that fingerprint"
    return HTTPException(status.HTTP_404_NOT_FOUND, f"no partition profile for {named}")


def _summary(profile: PartitionProfile, device_ids: list[str]) -> PartitionProfileSummary:
    return PartitionProfileSummary(
        partition_table_sha256=profile.partition_table_sha256,
        layout_id=profile.layout_id,
        # A DB CHECK holds it to the three; pydantic re-checks the literal.
        origin=cast(ProfileOrigin, profile.origin),
        deployable=profile.adopted_at is not None,
        ota_slot_size=profile.ota_slot_size,
        flash_chip_size=profile.flash_chip_size,
        detected_device_id=profile.detected_device_id,
        device_ids=device_ids,
        created_at=profile.created_at,
        adopted_at=profile.adopted_at,
    )


async def _summary_of(session: AsyncSession, profile: PartitionProfile) -> PartitionProfileSummary:
    devices = await device_ids_by_fingerprint(session)
    return _summary(profile, devices.get(profile.partition_table_sha256, []))


async def _taken(session: AsyncSession, layout_id: str) -> HTTPException | None:
    """409 when `layout_id` already names a profile."""
    if await owner_of_layout_id(session, layout_id) is None:
        return None
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"the name {layout_id} is already taken by another partition profile; pick another",
    )


def _fingerprint_known(profile: PartitionProfile) -> HTTPException:
    """409 for a fingerprint that is already a profile, pointing to the right route."""
    if profile.adopted_at is None:
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "this fingerprint is already a detected profile; adopt it by naming it",
        )
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"this fingerprint is already the partition profile {profile.layout_id}",
    )


@router.get("", response_model=PartitionProfileList, summary="Partition profiles")
async def list_partition_profiles(
    admin: AdminDep, sessionmaker: SessionMakerDep
) -> PartitionProfileList:
    """Builtins first, then adopted profiles by name, then pending ones oldest first."""
    async with sessionmaker() as session:
        profiles = await list_profiles(session)
        devices = await device_ids_by_fingerprint(session)
    return PartitionProfileList(
        profiles=[_summary(p, devices.get(p.partition_table_sha256, [])) for p in profiles]
    )


@router.post(
    "",
    response_model=PartitionProfileSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Add an operator partition profile by fingerprint",
)
async def create_partition_profile(
    body: PartitionProfileCreate, admin: AdminDep, sessionmaker: SessionMakerDep
) -> PartitionProfileSummary:
    """201 with the new profile, deployable at once. 409 when the fingerprint or the name
    is already a profile (a pending detected fingerprint is adopted with PATCH instead)."""
    sha = body.partition_table_sha256
    async with sessionmaker() as session:
        existing = await get_profile(session, sha)
        if existing is not None:
            raise _fingerprint_known(existing)
        taken = await _taken(session, body.layout_id)
        if taken is not None:
            raise taken
        try:
            profile = await insert_user_profile(
                session, sha=sha, layout_id=body.layout_id, ota_slot_size=body.ota_slot_size
            )
            await session.commit()
        except IntegrityError:
            # Created at the same moment by someone else (or the ingestor): answer as if
            # the checks above had seen it.
            await session.rollback()
            existing = await get_profile(session, sha)
            if existing is not None:
                raise _fingerprint_known(existing) from None
            taken = await _taken(session, body.layout_id)
            raise taken or HTTPException(
                status.HTTP_409_CONFLICT, "this profile clashes with another; reload and retry"
            ) from None
        summary = await _summary_of(session, profile)

    logger.info(
        "partition profile %s (%s, slot %d) created by %s",
        body.layout_id,
        sha,
        body.ota_slot_size,
        admin.token_id,
    )
    return summary


@router.patch(
    "/{sha}",
    response_model=PartitionProfileSummary,
    summary="Adopt a detected partition profile by naming it",
)
async def adopt_partition_profile(
    sha: str, body: PartitionProfileAdopt, admin: AdminDep, sessionmaker: SessionMakerDep
) -> PartitionProfileSummary:
    """Name a pending detected profile, which makes it deployable. One-way.

    The slot the detecting board reported is a measurement and wins: a different
    `ota_slot_size` is a 409, a missing one when the board reported none is a 422.
    """
    if not _SHA256.match(sha):
        raise _no_such_profile(sha)
    async with sessionmaker() as session:
        profile = await get_profile(session, sha, for_update=True)
        if profile is None:
            raise _no_such_profile(sha)
        if profile.origin == ORIGIN_BUILTIN:
            raise HTTPException(status.HTTP_409_CONFLICT, BUILTIN_IMMUTABLE)
        if profile.adopted_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, NAME_IS_FINAL)
        measured = profile.ota_slot_size
        if measured is None and body.ota_slot_size is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "this board did not report its OTA slot size; give ota_slot_size",
            )
        if measured is not None and body.ota_slot_size not in (None, measured):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"the board measured an OTA slot of {measured} bytes, not "
                f"{body.ota_slot_size}; the measurement is kept, so leave ota_slot_size out",
            )
        if measured is not None and measured > MAX_PROFILE_SLOT_SIZE:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"the board measured an OTA slot of {measured} bytes, larger than the "
                f"{MAX_PROFILE_SLOT_SIZE} bytes an upload may be; this map cannot be adopted",
            )
        taken = await _taken(session, body.layout_id)
        if taken is not None:
            raise taken
        try:
            adopted = await adopt_profile(
                session, sha=sha, layout_id=body.layout_id, ota_slot_size=body.ota_slot_size
            )
            if adopted is None:  # unreachable under the row lock above
                raise HTTPException(status.HTTP_409_CONFLICT, NAME_IS_FINAL)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise (await _taken(session, body.layout_id)) or HTTPException(
                status.HTTP_409_CONFLICT, "this profile clashes with another; reload and retry"
            ) from None
        summary = await _summary_of(session, adopted)

    logger.info(
        "partition profile %s adopted as %s (slot %s) by %s",
        sha,
        body.layout_id,
        summary.ota_slot_size,
        admin.token_id,
    )
    return summary


@router.delete(
    "/{sha}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a partition profile nothing depends on",
)
async def delete_partition_profile(
    sha: str, admin: AdminDep, sessionmaker: SessionMakerDep
) -> Response:
    """204. Builtins are 409; a named profile with artifacts labelled for it is 409.

    A pending detected profile is always deletable. A board still announcing that map
    records it again at its next announce.
    """
    if not _SHA256.match(sha):
        raise _no_such_profile(sha)
    async with sessionmaker() as session:
        profile = await get_profile(session, sha, for_update=True)
        if profile is None:
            raise _no_such_profile(sha)
        if profile.origin == ORIGIN_BUILTIN:
            raise HTTPException(status.HTTP_409_CONFLICT, BUILTIN_IMMUTABLE)
        layout_id = profile.layout_id
        if layout_id is not None:
            labelled = await artifacts_labelled(session, layout_id)
            if labelled:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{labelled} artifact{'s are' if labelled != 1 else ' is'} labelled for "
                    f"{layout_id}; a profile with images cannot be removed",
                )
        await delete_profile(session, sha)
        await session.commit()

    logger.info(
        "partition profile %s (%s, %s) deleted by %s",
        sha,
        layout_id or "pending",
        profile.origin,
        admin.token_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
