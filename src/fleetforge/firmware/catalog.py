"""The servable agent catalog, read from the object store at request time.

Until S0-infra-6 this module scanned `AGENT_IMAGES_DIR` once at startup and served parts
with `FileResponse`. Agent bundles are ordinary content-addressed artifacts now
(DECISIONS.md 2026-09-11 → *agent bundles are artifacts, not image contents*): the parts
and each bundle's `manifest.json` live at `blobs/sha256/<digest>`, and one small mutable
index object (`firmware/index.py`) says which of them are current. The local-directory
reader that used to live here moved to `firmware/bundledir.py`, where it now runs at
**publish** time.

Three properties this file exists to hold:

* **A manifest is never offered unless the parts behind it can be named.** A part's blob
  key is derived from the digest the manifest already carries, so listing a build is a
  promise the store can keep or a `ObjectNotFound` that says exactly which part is gone.
* **One bad entry does not take the others down.** A manifest blob that is missing,
  unparseable, or disagrees with `spec/device-protocol.md` is dropped with a WARNING
  naming the target — the same rule the filesystem loader followed.
* **A transport failure is never flattened into "empty".** `ObjectNotFound` on the index
  means *nothing published yet*; any other `ObjectStoreError` propagates, so the router
  can tell an operator the store is unreachable instead of "no agent images".

`CatalogCache` is the request-path front: one index read per `agent_catalog_ttl_s`,
one `asyncio.Lock` so a burst of four part downloads does not fire four reads, and
**no stale snapshot is ever served over a failing refresh** — a snapshot whose parts
cannot be fetched is precisely the "manifest we cannot honour" `spec/standards.md`
forbids.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from fleetforge.firmware.index import DEFAULT_INDEX_KEY, AgentIndex, AgentIndexEntry
from fleetforge.firmware.manifest import (
    PART_NAMES,
    SUPPORTED_LAYOUTS,
    BundleManifest,
    ConfigPartition,
)
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStore

logger = logging.getLogger(__name__)


class AmbiguousBundleError(LookupError):
    """More than one layout for a target, and the caller did not name one.

    A `LookupError`, not an `AgentBundleError`: nothing is wrong with any bundle. The
    request is under-specified, and the answer is to say so — `spec/standards.md`'s
    Unaided onboarding rule — rather than to serve whichever sorted first and flash a
    board with the wrong partition table.
    """

    def __init__(self, target: str, layouts: tuple[str, ...]) -> None:
        self.target = target
        self.layouts = layouts
        super().__init__(
            f"target {target!r} has bundles for layouts {', '.join(layouts)}; "
            "name one with ?layout="
        )


@dataclass(frozen=True, slots=True)
class AgentPart:
    """One flashable part, addressed by the digest its manifest carries."""

    name: str
    offset: int
    size: int
    sha256: str

    @property
    def blob_key(self) -> str:
        """Where the bytes live, store-relative. Never hand-built at a call site."""
        return blob_key(self.sha256)

    @property
    def etag(self) -> str:
        """A strong ETag over the content hash the manifest already carries."""
        return f'"sha256-{self.sha256}"'


@dataclass(frozen=True, slots=True)
class AgentBundle:
    """One target's published, flashable bundle."""

    target: str
    chip_family: str
    agent_version: str
    idf_version: str
    idf_image: str
    source_commit: str
    built_at: str
    partition_layout: str
    ota_slot_size: int
    flash_size: str
    # S0-infra-3 build identity; `None` in a bundle built before it. Carried, never
    # recomputed: `build_digest` is checked against its inputs by `just agent-verify`, at
    # build time, where a mismatch is actionable.
    config_sha256: str | None
    build_digest: str | None
    # Absent in bundles built before R0-fw-1. Not verified against a file, because there
    # is none: the blob is written per board at flash time.
    config_partition: ConfigPartition | None
    # S0-infra-6: the digest of the manifest bytes this bundle was published from. The
    # bundle's identity in the index, and what `agent-rollback` names.
    manifest_sha256: str
    parts: tuple[AgentPart, ...]

    @property
    def key(self) -> tuple[str, str]:
        """The catalog key: (target, partition_layout)."""
        return (self.target, self.partition_layout)

    def part(self, name: str) -> AgentPart | None:
        """The named part, or `None`. The lookup is by logical id, never by filename."""
        for part in self.parts:
            if part.name == name:
                return part
        return None


@dataclass(frozen=True, slots=True)
class FirmwareCatalog:
    """Every servable bundle, indexed by (target, partition_layout)."""

    bundles: tuple[AgentBundle, ...]

    @property
    def keys(self) -> tuple[tuple[str, str], ...]:
        """The catalog keys, in order: (target, partition_layout) pairs."""
        return tuple(bundle.key for bundle in self.bundles)

    @property
    def targets(self) -> tuple[str, ...]:
        """Unique targets, order preserved. A target with two layouts is one target."""
        return tuple(dict.fromkeys(bundle.target for bundle in self.bundles))

    def layouts_for(self, target: str) -> tuple[str, ...]:
        """Every partition_layout registered for `target`, in catalog order."""
        return tuple(bundle.partition_layout for bundle in self.bundles if bundle.target == target)

    def bundle(self, target: str, layout: str | None = None) -> AgentBundle | None:
        """The bundle for (target, layout), or None.

        If `layout` is given, returns an exact match or None. If omitted, returns the sole
        bundle for `target` when exactly one exists, raises `AmbiguousBundleError` when more
        than one exists, and returns None when none exist.
        """
        candidates = [b for b in self.bundles if b.target == target]
        if layout is not None:
            # Exact match requested.
            for bundle in candidates:
                if bundle.partition_layout == layout:
                    return bundle
            return None
        # No layout specified: 0 → None, 1 → it, >1 → raise.
        if len(candidates) == 0:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # More than one layout for this target and the caller did not say which.
        layouts = tuple(b.partition_layout for b in candidates)
        raise AmbiguousBundleError(target, layouts)

    def __bool__(self) -> bool:
        return bool(self.bundles)


def parse_index(raw: bytes) -> AgentIndex:
    """Validate index bytes. Raises `ValueError` (a `ValidationError`) on anything else."""
    return AgentIndex.model_validate_json(raw)


def _bundle_from_manifest(entry: AgentIndexEntry, manifest: BundleManifest) -> AgentBundle:
    """Re-verify a published manifest against the index and the protocol, or raise.

    Checked **again** here even though `load_bundle_dir` checked it at publish: the store
    can be written to by something other than this CLI, and a bundle whose layout
    disagrees with `spec/device-protocol.md` flashes cleanly onto a board that can never
    OTA. Deliberate duplication — gotcha 8 of the S0-infra-6 plan.
    """
    if manifest.target != entry.target:
        raise ValueError(
            f"manifest says target {manifest.target!r} but the index filed it as {entry.target!r}"
        )
    if manifest.partition_layout != entry.partition_layout:
        raise ValueError(
            f"manifest says layout {manifest.partition_layout!r} but the index filed it as "
            f"{entry.partition_layout!r}"
        )
    expected_slot = SUPPORTED_LAYOUTS.get(manifest.partition_layout)
    if expected_slot is None:
        raise ValueError(
            f"partition_layout {manifest.partition_layout!r} is not one of "
            f"{list(SUPPORTED_LAYOUTS)} (spec/device-protocol.md)"
        )
    if manifest.ota_slot_size != expected_slot:
        raise ValueError(
            f"ota_slot_size {manifest.ota_slot_size} is not the {expected_slot} that layout "
            f"{manifest.partition_layout!r} declares"
        )

    names = [part.name for part in manifest.parts]
    missing = [name for name in PART_NAMES if name not in names]
    if missing:
        raise ValueError(f"manifest is missing part(s) {missing}")
    if len(set(names)) != len(names):
        raise ValueError("manifest names the same part twice")

    parts = tuple(
        sorted(
            (
                AgentPart(
                    name=part.name,
                    offset=part.offset,
                    size=part.size,
                    sha256=part.sha256,
                )
                for part in manifest.parts
            ),
            key=lambda part: part.offset,
        )
    )
    app = next(part for part in parts if part.name == "app")
    if app.size > manifest.ota_slot_size:
        raise ValueError(
            f"app is {app.size} bytes but an OTA slot is {manifest.ota_slot_size}; "
            "a board flashed with this image could never be updated"
        )

    return AgentBundle(
        target=manifest.target,
        chip_family=manifest.chip_family,
        agent_version=manifest.agent_version,
        idf_version=manifest.idf_version,
        idf_image=manifest.idf_image,
        source_commit=manifest.source_commit,
        built_at=manifest.built_at,
        partition_layout=manifest.partition_layout,
        ota_slot_size=manifest.ota_slot_size,
        flash_size=manifest.flash_size,
        config_sha256=manifest.config_sha256,
        build_digest=manifest.build_digest,
        config_partition=manifest.config_partition,
        manifest_sha256=entry.manifest_sha256,
        parts=parts,
    )


async def _load_entry(store: ObjectStore, entry: AgentIndexEntry) -> AgentBundle | None:
    """One index entry's bundle, or `None` with a WARNING naming the target.

    `ObjectStoreError` that is **not** `ObjectNotFound` propagates: a refused or
    unreachable store is a named fault, not three quietly missing targets.
    """
    try:
        raw = await store.get(blob_key(entry.manifest_sha256))
    except ObjectNotFound:
        logger.warning(
            "agent image for target %s (%s) dropped: its manifest blob %s is not in the "
            "store. Re-publish it with `just agent-publish %s`.",
            entry.target,
            entry.partition_layout,
            entry.manifest_sha256,
            entry.target,
        )
        return None
    try:
        manifest = BundleManifest.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning(
            "agent image for target %s dropped: its published manifest is not valid (%d errors)",
            entry.target,
            exc.error_count(),
        )
        return None
    try:
        return _bundle_from_manifest(entry, manifest)
    except ValueError as exc:
        logger.warning("agent image for target %s dropped: %s", entry.target, exc)
        return None


async def load_catalog(
    store: ObjectStore, *, index_key: str = DEFAULT_INDEX_KEY
) -> FirmwareCatalog:
    """Read the index and every manifest it points at. Never caches; see `CatalogCache`.

    An absent index is an **empty catalog**, not an error: it is the honest shape of a
    stack where nothing has been published yet. A malformed one is a WARNING and an empty
    catalog, because the API must still start and still answer honestly. Anything the
    transport does wrong is an `ObjectStoreError` and **propagates**.
    """
    try:
        raw = await store.get(index_key)
    except ObjectNotFound:
        logger.warning(
            "no agent index at %s: nothing has been published, so /v1/agent/manifest will "
            "answer 503 and the flasher has nothing to offer. Publish one with "
            "`just agent-publish esp32` (docs/runbooks/artifact-storage.md).",
            index_key,
        )
        return FirmwareCatalog(bundles=())

    try:
        index = parse_index(raw)
    except ValidationError as exc:
        logger.warning(
            "the agent index at %s is not a valid index (%d errors): serving nothing rather "
            "than guessing at it. Re-publish with `just agent-publish-all`.",
            index_key,
            exc.error_count(),
        )
        return FirmwareCatalog(bundles=())

    # First-wins on a duplicate key, mirroring the old `load_bundles`. A hand-edited index
    # is the only way to get one.
    entries: list[AgentIndexEntry] = []
    seen: set[tuple[str, str]] = set()
    for entry in index.bundles:
        if entry.key in seen:
            logger.warning(
                "agent index entry %s dropped: %s is already provided by an earlier entry",
                entry.manifest_sha256,
                entry.key,
            )
            continue
        seen.add(entry.key)
        entries.append(entry)

    loaded = await asyncio.gather(*(_load_entry(store, entry) for entry in entries))
    bundles = sorted((b for b in loaded if b is not None), key=lambda b: b.key)

    if not bundles:
        logger.warning(
            "the agent index at %s names no usable bundle: /v1/agent/manifest will answer "
            "503 and the flasher has nothing to offer.",
            index_key,
        )
    else:
        logger.info(
            "loaded %d agent image(s) from the store: %s",
            len(bundles),
            ", ".join(f"{b.target}/{b.partition_layout}@{b.agent_version}" for b in bundles),
        )
    return FirmwareCatalog(bundles=tuple(bundles))


class CatalogCache:
    """A `FirmwareCatalog` refreshed from the store at most once per `ttl_s`.

    Constructed in `create_app()` and therefore **I/O-free to build**: a container must
    start when the bucket is down (`storage/factory.py::object_store_configured`).

    `ttl_s=0` means read the index on every request — which is what `AGENT_CATALOG_TTL_S=0`
    buys an operator mid-incident.

    Failures are **not cached and the previous snapshot is not served over them**. The
    cached index would promise parts the store cannot deliver, and offering a manifest we
    cannot honour is the one thing `spec/standards.md` rules out.
    """

    def __init__(self, ttl_s: float = 60.0, *, index_key: str = DEFAULT_INDEX_KEY) -> None:
        self._ttl_s = max(ttl_s, 0.0)
        self._index_key = index_key
        self._lock = asyncio.Lock()
        self._catalog: FirmwareCatalog | None = None
        self._loaded_at = 0.0

    def _fresh(self) -> FirmwareCatalog | None:
        """The snapshot if it is still inside the TTL, else `None`."""
        catalog = self._catalog
        if catalog is None:
            return None
        if (time.monotonic() - self._loaded_at) >= self._ttl_s:
            return None
        return catalog

    async def get(self, store: ObjectStore) -> FirmwareCatalog:
        """The catalog, refreshed if the TTL has passed. Propagates `ObjectStoreError`."""
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock:
            # Double-checked: a burst of four part downloads must cost one index read.
            cached = self._fresh()
            if cached is not None:
                return cached
            # Dropped BEFORE the read, not after: if `load_catalog` raises, there must be
            # no snapshot left to serve. A stale catalog promises parts the store may no
            # longer hold, which is the one thing this endpoint must never do.
            self._catalog = None
            self._loaded_at = 0.0
            catalog = await load_catalog(store, index_key=self._index_key)
            self._catalog = catalog
            self._loaded_at = time.monotonic()
            return catalog

    def clear(self) -> None:
        """Forget the snapshot. For tests and for a future `POST /v1/agent/refresh`."""
        self._catalog = None
        self._loaded_at = 0.0
