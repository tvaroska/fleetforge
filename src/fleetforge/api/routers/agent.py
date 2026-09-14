"""`GET /v1/agent/*` — the prebuilt agent images the browser flasher writes to a board.

`spec/flows.md` Flow 1 steps 3–4: the operator plugs a board in, the dashboard asks for a
chip target, and the browser flashes "the matching prebuilt agent". This router is what
`R0-fe-3`'s `esptool-js` page reads: one call for the manifest (offsets, sizes, sha256s,
chip family), then one call per part for the bytes.

**Authenticated, like every other dashboard endpoint.** The dashboard is the only client,
`R0-fe-1` already ships the login gate, and an unauthenticated public instance should not
serve multi-megabyte downloads to anyone who finds it. Nothing about the firmware is
secret — the reason is bandwidth and blast radius, not confidentiality.

**No path is ever built from a request.** `{target}` and `{part}` are looked up in the
in-memory catalog (`firmware/catalog.py`) and the filename comes from the manifest the
loader already resolved and confined, so traversal is impossible by construction. The
`SafeSegment` pattern on both parameters is the second layer: it keeps a hostile value out
of the log line and makes `..%2f…` a clean 404 instead of a 422 from deeper in the stack.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from fleetforge.api.deps import AdminDep, bearer_scheme, cookie_scheme
from fleetforge.firmware import (
    AgentBuildInfo,
    AgentManifest,
    AgentPartInfo,
    FirmwareCatalog,
)
from fleetforge.firmware.manifest import SafeSegment

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/agent",
    tags=["agent"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# Downloaded once per flash and immutable for the life of an app image, but private:
# it is behind the admin credential, so no shared cache may keep a copy.
CACHE_CONTROL = "private, max-age=3600"


def firmware_catalog(request: Request) -> FirmwareCatalog:
    """The per-app catalog, loaded once in `create_app()`.

    Per app rather than module-level for the same reason as `token_cache` and
    `event_hub` (`api/deps.py`): tests get a fresh one, and `dependency_overrides`
    on this function is how they point it at a fixture. Reached through `Depends`
    below and never called directly — a direct call would bypass those overrides,
    which is a test that silently exercises the wrong catalog.
    """
    catalog: FirmwareCatalog = request.app.state.firmware_catalog
    return catalog


CatalogDep = Annotated[FirmwareCatalog, Depends(firmware_catalog)]


def _require_catalog(catalog: FirmwareCatalog) -> FirmwareCatalog:
    """503 when nothing is servable — the same shape as `deps.get_object_store`.

    The honest answer on a stack built with an empty `agent/dist`. `just build` refuses
    to produce such an app image (`_require-agent-dist`), so in production this means the
    bind mount or the `COPY` is wrong, and the startup WARNING says which.
    """
    if not catalog:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no agent images available",
        )
    return catalog


@router.get("/manifest", response_model=AgentManifest, summary="Prebuilt agent images")
async def agent_manifest(admin: AdminDep, catalog: CatalogDep) -> AgentManifest:
    """Every flashable target, with the byte offsets the flasher writes them at.

    `agent_version` is hoisted to the top level because one build of the agent produces
    every target: a response whose targets disagreed about it would mean a partial
    `just agent-build-all`, and the per-build copy is still there to show which.
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
    response_class=FileResponse,
    summary="One flashable part of one target's image",
)
async def agent_part(
    admin: AdminDep,
    catalog: CatalogDep,
    target: SafeSegment,
    part: SafeSegment,
) -> FileResponse:
    """The raw bytes of one part, with the manifest's sha256 as a strong ETag.

    404 for an unknown target and for an unknown part: the flasher only ever asks for
    what the manifest listed, so either is a client bug or a probe, and distinguishing
    them in the response body would tell a prober which targets exist.
    """
    _require_catalog(catalog)
    bundle = catalog.bundle(target)
    entry = bundle.part(part) if bundle is not None else None
    if entry is None:
        logger.info("agent image not found: target=%s part=%s", target, part)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    return FileResponse(
        entry.path,
        media_type="application/octet-stream",
        filename=f"{target}-{part}.bin",
        headers={"ETag": entry.etag, "Cache-Control": CACHE_CONTROL},
    )
