"""`POST /v1/artifact` — get a user's firmware into the store. R1-be-1.
`GET /v1/artifact` — list what can be deployed. R1-fe-1.

⚠️ **Both routes on this router are admin-only, and that is the whole reason this file
is separate from `routers/artifact_download.py`.** That module owns a router with the
*same* `/v1/artifact` prefix and no auth dependencies at all — public by design, because
there the signature over `(sha256, exp)` is the authorization. Adding a read route there
by accident would publish the artifact catalog to the internet, so every route this
router carries is covered by `tests/test_api_artifact_list.py`'s 401 assertions,
including one with an `exp`/`sig` query string that proves a download signature buys
nothing here.


The first writer of the `artifacts` table (S0-infra-4 landed it empty) and of the
`artifact_versions` labels over it. `firmware/publish.py` already writes *blobs* for
agent bundles; this endpoint is the user-facing half of the same storage model —
`spec/standards.md` → *One path, not two*: an agent bundle and a user upload are the
same kind of object stored the same way.

**The bytes are opaque.** The server stores, targets and tracks them; it never parses
them. There is no ELF check, no image-header validation and no "is this really an ESP32
app?" — `docs/features/ota-deploy.md` is explicit that users build with their own
toolchain, and a server that understood the format would be a server that has opinions
about which toolchains are allowed.

**Raw body, not `multipart/form-data`.** `python-multipart` is not a dependency and an
opaque blob does not need a form parser; the metadata is small enough to be query
parameters. R1-fe-1 posts the `File` object straight as the body.

**One size limit, not two.** `spec/prd.md` → *Capacity* says "Artifact size ≤ 1.9 MB"
and `spec/device-protocol.md` says `ota_slot_size` is 1966080 — those are one number
written twice (1966080 B = 1.875 MiB, which rounds to 1.9 MB), not two independent
caps. The authority is `SUPPORTED_LAYOUTS`, because it is the one tied to the partition
table a board actually carries; a second, slightly different constant would be a
rejection nobody could explain. Proposed as a spec clarification in
`spec/open-questions.md` rather than resolved by inventing a number here.

**Nothing reaches the store until it is known to be acceptable.** The limit is checked
against `Content-Length` before the body is read at all, and again while reading, so a
lying header cannot spend memory either. An upload that writes 3 MB and then apologises
has already paid for the object.

**Write order is blob, then rows** — the same crash posture as `publish.py`. A failure
between them leaves an unreferenced content-addressed blob, which is inert and
re-`put`-able; the reverse order would leave a row naming bytes that do not exist.
"""

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import text

from fleetforge.api.deps import (
    AdminDep,
    ObjectStoreDep,
    SessionMakerDep,
    bearer_scheme,
    cookie_scheme,
)
from fleetforge.api.schemas import ArtifactList, ArtifactSummary, ArtifactUploaded
from fleetforge.db.models import ArtifactKind
from fleetforge.firmware.manifest import EXPECTED_PARTITION_LAYOUT, SUPPORTED_LAYOUTS, SafeSegment
from fleetforge.storage.blobs import digest_bytes, put_blob
from fleetforge.storage.objectstore import ObjectStoreError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/artifact",
    tags=["artifacts"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# The label a human types. Bounded and **rejected, never normalised** (`identity.py`'s
# rule): it travels into a `dn/cmd` JSON payload (`spec/device-protocol.md`) and a
# dashboard list, so `1.5.0` and `1.5.0 ` must not become two spellings of one release.
# No semver requirement — the vocabulary is the user's; a date or a CI number is fine.
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")

# Read the body in chunks rather than one `await request.body()`, so the cap can be
# enforced as the bytes arrive instead of after they are all in memory.
CHUNK_SIZE = 64 * 1024

# Same cap and same reason as `routers/devices.py`/`routers/enrollment.py`: a guard
# against an unbounded response, not paging. `spec/prd.md` → *Retention* keeps 20
# versions per platform, so this is two orders of magnitude of headroom.
LIST_LIMIT = 500

STORE_UNAVAILABLE = "the artifact store cannot be reached; try again shortly"

# The label joined to the bytes it names. Two tables because labels are a many-to-one
# mutable pointer over immutable content (`db/models.py::ArtifactVersion`), so the
# `size_bytes`/`partition_layout`/`kind` an operator is choosing between live on the
# digest, not on the label.
_LIST_SQL = text(
    """
    SELECT v.target, v.version, v.sha256, v.created_at,
           a.size_bytes, a.partition_layout, a.kind
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     ORDER BY v.target, v.created_at DESC, v.version
     LIMIT :limit
    """
)

# Both success cases of a content-addressed store, kept apart because the caller cares:
# a new label is 201, re-uploading bytes already stored under this exact (target,
# version) is 200. Neither is an error — re-`put` of the same key is a no-op by
# construction (`storage/blobs.py`).
_ARTIFACT_UPSERT_SQL = text(
    """
    INSERT INTO artifacts (sha256, size_bytes, kind, target, partition_layout)
    VALUES (:sha256, :size_bytes, :kind, :target, :partition_layout)
    ON CONFLICT (sha256) DO NOTHING
    """
)

_LABEL_INSERT_SQL = text(
    """
    INSERT INTO artifact_versions (target, version, sha256)
    VALUES (:target, :version, :sha256)
    ON CONFLICT (target, version) DO NOTHING
    RETURNING sha256
    """
)

_LABEL_SELECT_SQL = text(
    "SELECT sha256 FROM artifact_versions WHERE target = :target AND version = :version"
)


def _slot_size(partition_layout: str) -> int:
    """The byte ceiling for this layout, or a 400 naming the layouts that exist.

    `SUPPORTED_LAYOUTS` is the same mapping `firmware/manifest.py` validates agent
    bundles against, so an upload and a bundle cannot disagree about how big an OTA
    slot is. A new layout is a new entry there plus a `spec/device-protocol.md` change
    — never a number typed in here.
    """
    try:
        return SUPPORTED_LAYOUTS[partition_layout]
    except KeyError:
        known = ", ".join(sorted(SUPPORTED_LAYOUTS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown partition_layout; this server understands: {known}",
        ) from None


def _declared_length(request: Request, limit: int) -> int:
    """`Content-Length`, checked against `limit` before a single byte is read.

    Required, not optional: this endpoint's whole promise is that an oversized upload
    is refused before it costs anything, and a client that cannot say how long its
    file is has opted out of that. Every browser `fetch` with a `File` body and every
    `curl --data-binary @file` sets it.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_411_LENGTH_REQUIRED,
            detail="Content-Length is required",
        )
    try:
        declared = int(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Content-Length is not a number"
        ) from None
    if declared > limit:
        raise _too_large(declared, limit)
    return declared


def _too_large(size: int, limit: int) -> HTTPException:
    """413, naming both numbers.

    The operator's next action depends on the gap — rebuild smaller, or pick a layout
    with a bigger slot — and neither is guessable from "too large".
    """
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=(
            f"artifact is {size} bytes; an OTA slot in this partition layout is {limit}. "
            "The image must fit the slot it will be written into."
        ),
    )


async def _read_capped(request: Request, limit: int) -> bytes:
    """The body, refusing at `limit`. The backstop against a lying `Content-Length`."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            # Stop reading: the answer cannot change and the rest is wasted transfer.
            raise _too_large(total, limit)
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "",
    response_model=ArtifactUploaded,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a firmware artifact",
)
async def upload_artifact(
    request: Request,
    response: Response,
    admin: AdminDep,
    store: ObjectStoreDep,
    sessionmaker: SessionMakerDep,
    target: Annotated[SafeSegment, Query(description="Chip target, e.g. esp32s3")],
    version: Annotated[str, Query(description="The version label, e.g. 1.5.0")],
    partition_layout: Annotated[
        SafeSegment, Query(description="Partition layout the image is built for")
    ] = EXPECTED_PARTITION_LAYOUT,
) -> ArtifactUploaded:
    """Store an opaque firmware blob and label it `(target, version)`.

    201 with a new label, 200 when the same bytes are re-uploaded under the same label,
    409 when the label already names *different* bytes — a version is a promise about
    which image it is, so silently re-pointing it would make every deploy record that
    mentions it ambiguous. Re-tagging the same bytes under a second label is fine and
    costs no storage: the blob is content-addressed, so both labels name one object.
    """
    if not VERSION_PATTERN.match(version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )

    limit = _slot_size(partition_layout)
    _declared_length(request, limit)
    data = await _read_capped(request, limit)

    if not data:
        # `artifacts.size_bytes > 0` is a CHECK, so the database would refuse this too —
        # but as a 500 with a constraint name in it. An empty upload is a mistake with an
        # obvious explanation, and it deserves one.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="artifact is empty; a deploy that ships nothing is never intended",
        )

    digest = digest_bytes(data)

    async with sessionmaker() as session:
        # Cheap pre-check so the common conflict costs no upload. It is not the
        # guarantee — the primary key below is — but a 409 discovered after storing
        # 1.9 MB is a 409 that wasted the operator's time.
        existing = await session.scalar(_LABEL_SELECT_SQL, {"target": target, "version": version})
        if existing is not None and existing != digest:
            raise _version_conflict(target, version, existing)

        try:
            await put_blob(store, data)
        except ObjectStoreError:
            # Never the bucket, the key or the exception text — `api.ts::detailOf` lifts
            # `detail` verbatim into the dashboard banner (the rule `routers/agent.py`
            # established for the flasher's three unhappy states).
            logger.exception("artifact upload failed: object store unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORE_UNAVAILABLE
            ) from None

        await session.execute(
            _ARTIFACT_UPSERT_SQL,
            {
                "sha256": digest,
                "size_bytes": len(data),
                "kind": ArtifactKind.USER_FIRMWARE.value,
                "target": target,
                "partition_layout": partition_layout,
            },
        )
        labelled = await session.scalar(
            _LABEL_INSERT_SQL, {"target": target, "version": version, "sha256": digest}
        )
        if labelled is None:
            # The label was taken between the pre-check and here, or by the pre-check's
            # own row. Re-read to tell an idempotent repeat from a genuine conflict.
            current = await session.scalar(
                _LABEL_SELECT_SQL, {"target": target, "version": version}
            )
            if current != digest:
                await session.rollback()
                raise _version_conflict(target, version, current)
        await session.commit()

    created = labelled is not None
    if not created:
        response.status_code = status.HTTP_200_OK

    logger.info(
        "artifact %s (%d bytes, %s/%s, layout %s) %s by %s",
        digest,
        len(data),
        target,
        version,
        partition_layout,
        "stored" if created else "re-uploaded unchanged",
        admin.token_id,
    )
    return ArtifactUploaded(
        sha256=digest,
        size_bytes=len(data),
        target=target,
        version=version,
        partition_layout=partition_layout,
        created=created,
    )


@router.get("", response_model=ArtifactList, summary="Deployable firmware artifacts")
async def list_artifacts(
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ArtifactList:
    """Every labelled artifact, grouped by target and newest first within a target.

    **No `?target=` filter in R1.** The dashboard picks a board's chip out of this list
    client-side, and at `spec/prd.md` → *Capacity* — 20 versions per platform — a query
    parameter would be a response shape fixed before anything needs it.

    `kind` is reported so a future screen can tell a user upload from an agent bundle.
    In practice R1 only ever sees `user_firmware`: `artifact_versions` has no writer but
    the upload endpoint above, and an agent bundle is labelled by `agent/index.json`
    rather than by a row here. That is worth knowing rather than worth filtering on.
    """
    async with sessionmaker() as session:
        rows = (await session.execute(_LIST_SQL, {"limit": LIST_LIMIT})).all()

    return ArtifactList(
        artifacts=[
            ArtifactSummary(
                target=row.target,
                version=row.version,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                partition_layout=row.partition_layout,
                kind=row.kind,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


def _version_conflict(target: str, version: str, existing: str | None) -> HTTPException:
    """409 — the label is taken by other bytes, and it keeps pointing at them."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"{target} version {version} already names artifact {existing}. "
            "A version label is not re-pointed; upload the new image under a new version."
        ),
    )
