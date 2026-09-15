"""Publish a local agent bundle into the object store, and roll one back.

The write half of S0-infra-6. `firmware/bundledir.py` verifies a bundle directory,
this module uploads it, and `firmware/catalog.py` reads it back. Three object writes
per part-set:

1. every part's bytes at `blobs/sha256/<the part's own digest>` (`put_blob`, immutable);
2. the bundle's `manifest.json` **verbatim** at `blobs/sha256/<the file's digest>` — the
   bytes as built, never a re-serialised model, or the digest recorded in the index is
   the digest of a file nobody can reproduce;
3. the index (`firmware/index.py`) at `Settings.agent_index_key`, through plain
   `store.put(..., cache_control="no-store")` and **never** `put_blob`: it is the one
   mutable object in the scheme.

Deliberately **database-free**: the `artifacts`/`builds` tables stay empty and unread
until R1/R9. Publishing an agent bundle needs no Postgres, and the store stays the single
source of truth for what is flashable.

Nothing here deploys anything. The API notices a new bundle within
`AGENT_CATALOG_TTL_S`, with no restart, no image rebuild and no container replaced —
that separation is the whole point of the feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from fleetforge.firmware.bundledir import AgentBundleError, LocalBundle
from fleetforge.firmware.index import (
    DEFAULT_INDEX_KEY,
    AgentIndex,
    AgentIndexEntry,
    utc_now,
)
from fleetforge.firmware.manifest import BundleManifest
from fleetforge.storage.blobs import blob_key, digest_bytes, put_blob
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStore

logger = logging.getLogger(__name__)

INDEX_CONTENT_TYPE = "application/json"
# The index is a pointer, not content. Everything it points at is immutable; it is not.
INDEX_CACHE_CONTROL = "no-store"


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What one `publish_bundle` call wrote."""

    target: str
    partition_layout: str
    agent_version: str
    manifest_sha256: str
    part_keys: tuple[str, ...]
    index: AgentIndex


async def read_index(store: ObjectStore, index_key: str = DEFAULT_INDEX_KEY) -> AgentIndex:
    """The current index, or an empty one when nothing has been published yet.

    Only `ObjectNotFound` becomes "empty". Every other `ObjectStoreError` propagates:
    overwriting an index we merely failed to *read* would delete every other target.
    """
    try:
        raw = await store.get(index_key)
    except ObjectNotFound:
        return AgentIndex()
    try:
        return AgentIndex.model_validate_json(raw)
    except ValidationError as exc:
        raise AgentBundleError(
            f"the index at {index_key} is not a valid agent index ({exc.error_count()} errors); "
            "refusing to overwrite it blind — inspect it before re-publishing"
        ) from exc


async def write_index(
    store: ObjectStore, index: AgentIndex, index_key: str = DEFAULT_INDEX_KEY
) -> None:
    """Write the index. Plain `put`, `no-store` — never `put_blob`."""
    payload = index.model_dump_json(by_alias=True, indent=2).encode()
    await store.put(
        index_key,
        payload,
        content_type=INDEX_CONTENT_TYPE,
        cache_control=INDEX_CACHE_CONTROL,
    )


async def publish_bundle(
    store: ObjectStore,
    bundle: LocalBundle,
    *,
    index_key: str = DEFAULT_INDEX_KEY,
) -> PublishResult:
    """Upload every part and the manifest, then point the index at it.

    Order matters: **blobs first, index last.** A crash between the two leaves unreferenced
    blobs (harmless, and the next publish re-uses them by digest); the reverse order would
    leave an index promising bytes that are not there.
    """
    part_keys: list[str] = []
    for part in bundle.parts:
        data = part.path.read_bytes()
        # `expected_digest` is exactly this check: `put_blob` refuses to store bytes under
        # a key that does not describe them, so a race with a rebuild cannot slip through.
        key = await put_blob(store, data, expected_digest=part.sha256)
        part_keys.append(key)

    manifest_sha256 = digest_bytes(bundle.manifest_bytes)
    await put_blob(
        store,
        bundle.manifest_bytes,
        content_type="application/json",
        expected_digest=manifest_sha256,
    )

    index = await read_index(store, index_key)
    entry = AgentIndexEntry(
        target=bundle.target,
        partition_layout=bundle.partition_layout,
        agent_version=bundle.agent_version,
        manifest_sha256=manifest_sha256,
        published_at=utc_now(),
    )
    updated = index.upsert(entry)
    await write_index(store, updated, index_key)

    logger.info(
        "published %s/%s@%s as %s (%d parts)",
        bundle.target,
        bundle.partition_layout,
        bundle.agent_version,
        manifest_sha256,
        len(part_keys),
    )
    return PublishResult(
        target=bundle.target,
        partition_layout=bundle.partition_layout,
        agent_version=bundle.agent_version,
        manifest_sha256=manifest_sha256,
        part_keys=tuple(part_keys),
        index=updated,
    )


async def rollback(
    store: ObjectStore,
    target: str,
    manifest_sha256: str,
    *,
    layout: str | None = None,
    index_key: str = DEFAULT_INDEX_KEY,
) -> AgentIndex:
    """Make a previously published bundle current again. One object write, no rebuild.

    The digest must already appear in that entry's `superseded`: pointing the index at an
    arbitrary digest would let it name bytes nobody ever verified. The manifest blob must
    also still be fetchable, parse, and agree with the target/layout being rolled back —
    an index entry is a promise, and this is where it is checked.
    """
    index = await read_index(store, index_key)
    entry = index.entry_for(target, layout)
    if entry is None:
        raise AgentBundleError(
            f"nothing is published for target {target!r}"
            + (f" layout {layout!r}" if layout else "")
        )
    if manifest_sha256 == entry.manifest_sha256:
        raise AgentBundleError(
            f"{manifest_sha256} is already current for {target}/{entry.partition_layout}"
        )
    known = {item.manifest_sha256 for item in entry.superseded}
    if manifest_sha256 not in known:
        raise AgentBundleError(
            f"{manifest_sha256} is not a previously published bundle for "
            f"{target}/{entry.partition_layout}; `list` shows what can be rolled back to"
        )

    try:
        raw = await store.get(blob_key(manifest_sha256))
    except ObjectNotFound as exc:
        raise AgentBundleError(
            f"the manifest blob {manifest_sha256} is no longer in the store; that bundle "
            "cannot be made current again"
        ) from exc
    try:
        manifest = BundleManifest.model_validate_json(raw)
    except ValidationError as exc:
        raise AgentBundleError(
            f"the manifest blob {manifest_sha256} is not a valid manifest "
            f"({exc.error_count()} errors)"
        ) from exc
    if manifest.target != target or manifest.partition_layout != entry.partition_layout:
        raise AgentBundleError(
            f"the manifest blob {manifest_sha256} is {manifest.target}/"
            f"{manifest.partition_layout}, not {target}/{entry.partition_layout}"
        )

    replacement = AgentIndexEntry(
        target=entry.target,
        partition_layout=entry.partition_layout,
        agent_version=manifest.agent_version,
        manifest_sha256=manifest_sha256,
        published_at=utc_now(),
        # Empty on purpose: `upsert` pushes the displaced (current) entry onto the history
        # and carries the rest of it over, dropping the digest being restored.
        superseded=[],
    )
    updated = index.upsert(replacement)
    await write_index(store, updated, index_key)
    logger.info(
        "rolled %s/%s back to %s (%s)",
        entry.target,
        entry.partition_layout,
        manifest_sha256,
        manifest.agent_version,
    )
    return updated
