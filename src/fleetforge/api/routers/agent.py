"""`GET /v1/agent/*` — the prebuilt agent images the browser flasher writes to a board.

`spec/flows.md` Flow 1 steps 3–4: the operator plugs a board in, the dashboard asks for a
chip target, and the browser flashes "the matching prebuilt agent". This router is what
`R0-fe-3`'s `esptool-js` page reads: one call for the manifest (offsets, sizes, sha256s,
chip family), then one call per part for the bytes.

**Authenticated, like every other dashboard endpoint.** The dashboard is the only client,
`R0-fe-1` already ships the login gate, and an unauthenticated public instance should not
serve multi-megabyte downloads to anyone who finds it. Nothing about the firmware is
secret — the reason is bandwidth and blast radius, not confidentiality.

**No key is ever built from a request.** Since S0-infra-6 the bytes come from the object
store, and a part's key is `blob_key(<the digest the published manifest carries>)` —
`blob_key` validates it, `{target}` and `{part}` only ever index the catalog. The
`SafeSegment` pattern on both parameters is the second layer: it keeps a hostile value out
of the log line and makes `..%2f…` a clean 404 instead of a 422 from deeper in the stack.

**The API reads and streams the bytes itself; it never redirects to a signed URL.** A
signed URL in the onboarding path would drag `signBlob` (a network call since S0-infra-5)
into the flasher and hand a browser a credential whose signature *is* its authority. The
admin cookie is the only credential on this path.

The three unhappy states are all 503 and all plain language: `detail` is lifted verbatim
into the flasher's banner by `api.ts::detailOf`, so it never carries an exception string,
a bucket, a key or a traceback.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from fleetforge.api.deps import AdminDep, ObjectStoreDep, bearer_scheme, cookie_scheme
from fleetforge.firmware import (
    AgentBuildInfo,
    AgentManifest,
    AgentPartInfo,
    AmbiguousBundleError,
    FirmwareCatalog,
)
from fleetforge.firmware.manifest import SafeSegment
from fleetforge.storage.blobs import digest_bytes
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStoreError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/agent",
    tags=["agent"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# `no-cache` means "store it, but revalidate every time" — NOT "do not cache".
#
# This URL is mutable: `/v1/agent/{target}/{part}` is the same path for every version of
# a target, and `just agent-publish` changes the bytes behind it. The previous value,
# `max-age=3600`, told the browser it could skip revalidation for an hour, so a publish
# mid-hour left a flasher holding one version's manifest and the previous version's
# parts. That is exactly what happened on 2026-09-23: a board was flashed with 0.2.0,
# esp32s3 0.3.2 was published minutes later, and the next flash aborted on a bootloader
# whose sha256 did not match the manifest. The image was not corrupt; it was the cached
# 0.2.0 bootloader. The flasher's integrity check caught it and wrote nothing.
#
# Revalidation is nearly free: `If-None-Match` is answered from the catalog with a 304
# before the object store is touched, so an unchanged part costs one conditional request
# and no blob read. `private` because the response is behind the admin credential and no
# shared cache may keep a copy.
CACHE_CONTROL = "private, no-cache"

# What an operator reads when the store is down. Named, retriable, and about the store —
# not about a bucket, a key or an exception class.
STORE_UNREACHABLE = (
    "the agent image store cannot be reached, so there is no firmware to offer. "
    "No board can be flashed until it is back."
)
NOTHING_PUBLISHED = (
    "no agent images have been published yet. Publish one with `just agent-publish <target>`."
)


async def firmware_catalog(request: Request, store: ObjectStoreDep) -> FirmwareCatalog:
    """The catalog, re-read from the store at most once per `AGENT_CATALOG_TTL_S`.

    Per app rather than module-level for the same reason as `token_cache` and
    `event_hub` (`api/deps.py`): tests get a fresh one, and `dependency_overrides`
    on this function is how they point it at a fixture. Reached through `Depends`
    below and never called directly — a direct call would bypass those overrides,
    which is a test that silently exercises the wrong catalog.

    An unreachable store is a **503 naming the store**, never an empty catalog: "nothing
    published" and "we cannot see what is published" are different answers and the
    operator acts differently on each.
    """
    cache = request.app.state.agent_catalog
    try:
        catalog: FirmwareCatalog = await cache.get(store)
    except ObjectStoreError as exc:
        # The exception names the bucket and the key; the log gets those, the body does not.
        logger.error("agent catalog unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=STORE_UNREACHABLE,
        ) from exc
    return catalog


CatalogDep = Annotated[FirmwareCatalog, Depends(firmware_catalog)]


def _require_catalog(catalog: FirmwareCatalog) -> FirmwareCatalog:
    """503 when the store is reachable but holds nothing servable.

    Distinct from the unreachable case above: here the answer is "publish something",
    and the message says so rather than sending an operator to look at the network.
    """
    if not catalog:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=NOTHING_PUBLISHED,
        )
    return catalog


@router.get("/manifest", response_model=AgentManifest, summary="Prebuilt agent images")
async def agent_manifest(admin: AdminDep, catalog: CatalogDep) -> AgentManifest:
    """Every flashable target, with the byte offsets the flasher writes them at.

    `agent_version` is hoisted to the top level from `bundles[0]` and is kept only for
    compatibility. It was true when one build produced every target; since S0-infra-6
    made publishing per-target it names whichever target happens to sort first, which is
    NOT necessarily the one being flashed. Read `builds[].agent_version` — the flasher
    does, matching on `chip_family`. The dashboard stops showing the hoisted value
    entirely once the targets disagree.
    """
    _require_catalog(catalog)
    return AgentManifest(
        agent_version=catalog.bundles[0].agent_version,
        builds=[
            AgentBuildInfo(
                target=bundle.target,
                chip_family=bundle.chip_family,
                agent_version=bundle.agent_version,
                idf_version=bundle.idf_version,
                idf_image=bundle.idf_image,
                source_commit=bundle.source_commit,
                built_at=bundle.built_at,
                partition_layout=bundle.partition_layout,
                ota_slot_size=bundle.ota_slot_size,
                flash_size=bundle.flash_size,
                config_sha256=bundle.config_sha256,
                build_digest=bundle.build_digest,
                config_partition=bundle.config_partition,
                parts=[
                    AgentPartInfo(
                        name=part.name,
                        offset=part.offset,
                        size=part.size,
                        sha256=part.sha256,
                    )
                    for part in bundle.parts
                ],
            )
            for bundle in catalog.bundles
        ],
    )


@router.get(
    "/{target}/{part}",
    response_class=Response,
    summary="One flashable part of one target's image",
)
async def agent_part(
    admin: AdminDep,
    catalog: CatalogDep,
    store: ObjectStoreDep,
    target: SafeSegment,
    part: SafeSegment,
    layout: SafeSegment | None = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """The raw bytes of one part, with the manifest's sha256 as a strong ETag.

    `?layout=` selects which partition_layout when more than one exists for a target. If
    omitted and exactly one layout exists, it resolves; if omitted and multiple exist, 409.

    404 for an unknown target, unknown layout and unknown part: the flasher only ever asks
    for what the manifest listed, so any is a client bug or a probe, and distinguishing
    them in the response body would tell a prober which targets exist.
    """
    _require_catalog(catalog)
    try:
        bundle = catalog.bundle(target, layout)
    except AmbiguousBundleError as exc:
        # 409, not 404: the target exists and every candidate is servable — the request is
        # under-specified. Behind the admin credential, so naming the layouts is not the
        # enumeration leak the 404-for-everything rule above guards against, and a caller
        # that cannot see them cannot fix the request.
        logger.info("agent image ambiguous: target=%s layouts=%s", target, exc.layouts)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    entry = bundle.part(part) if bundle is not None else None
    if entry is None:
        logger.info("agent image not found: target=%s part=%s layout=%s", target, part, layout)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    # Answered from the catalog, BEFORE the object store is touched: the ETag is the
    # part's sha256 out of the manifest, so a match means the caller already holds these
    # exact bytes and reading the blob to say so would be the whole cost of the request.
    # Weak comparison is not needed — these validators are only ever strong — but a `W/`
    # prefix is stripped so a proxy that weakened one does not force a pointless body.
    if if_none_match is not None:
        offered = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
        if entry.etag in offered or "*" in offered:
            # 304 must carry the same validators a 200 would, or the next revalidation
            # has nothing to send back.
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": entry.etag, "Cache-Control": CACHE_CONTROL},
            )

    try:
        data = await store.get(entry.blob_key)
    except ObjectNotFound as exc:
        # A publish-integrity bug, not a transport blip: the index promised a manifest
        # whose parts are not all there. ERROR, and the operator is told which part.
        logger.error(
            "agent part blob missing: target=%s part=%s key=%s", target, part, entry.blob_key
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"the agent image store is missing part '{part}' of {target}. "
                "Re-publish the bundle."
            ),
        ) from exc
    except ObjectStoreError as exc:
        logger.error("agent part unavailable: target=%s part=%s: %s", target, part, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=STORE_UNREACHABLE,
        ) from exc

    if digest_bytes(data) != entry.sha256:
        # Should be impossible under content addressing, which is exactly why it is worth
        # one hash of 1.2 MB: the failure that actually happens is a truncated read, and
        # the symptom without this check is a board that flashes cleanly and never boots.
        logger.error(
            "agent part bytes do not match the manifest: target=%s part=%s key=%s",
            target,
            part,
            entry.blob_key,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the agent image store served bytes that do not match the manifest",
        )

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "ETag": entry.etag,
            "Cache-Control": CACHE_CONTROL,
            "Content-Disposition": f'attachment; filename="{target}-{part}.bin"',
        },
    )
