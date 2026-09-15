"""Load, verify and index the agent bundles under `AGENT_IMAGES_DIR`.

See the package docstring (`firmware/__init__.py`) for why this seam is filesystem-only.
Everything here runs **once, at startup**: a scan of `<dir>/*/manifest.json`, a sha256 of
every part, and an index in memory. Nothing on the request path touches the filesystem
except `FileResponse` reading a path this module already resolved and confined.

The three rejections below are the whole point of the module, and each one is a board
that would otherwise be bricked or unflashable:

* bytes that do not match the manifest (a truncated `--output type=local`, an edited
  file, a half-copied bind mount);
* a `partition_layout` / `ota_slot_size` that disagrees with `spec/device-protocol.md`
  (the board announces a layout the server does not support, and `spec/flows.md`'s
  capability check silently passes);
* a `path` that is not a bare filename inside the bundle directory.

A bundle directory is `<target>` or `<target>.<layout>`. The suffix is how two
layouts for one target coexist on disk; `just agent-build` still writes the bare
`<target>` form and is unchanged. `.` separates because no chip target and no layout
id contains one (both are SAFE_SEGMENT: lowercase alnum and `-`), so the split is
unambiguous — `esp32-ab-4m-v1` would not be.

A rejected bundle is *dropped with a WARNING naming the target*, never repaired and never
raised: one bad target must not stop the other three from being flashable, and a stack
with no bundles at all is a legitimate configuration (`create_app()` warns, and
`GET /v1/agent/manifest` answers 503).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from fleetforge.firmware.manifest import (
    PART_NAMES,
    SAFE_SEGMENT,
    SUPPORTED_LAYOUTS,
    BundleManifest,
    ConfigPartition,
)

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"

# A manifest is a build output, not user input, but it arrives from the filesystem and
# `path` is the one field that becomes a path. Bare filenames only — no separators, no
# dot segments, nothing hidden.
SAFE_FILENAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_TARGET = re.compile(SAFE_SEGMENT)

# The manifest of a real bundle is ~1.5 KB; anything much larger is not one.
MAX_MANIFEST_BYTES = 64 * 1024


class AgentBundleError(ValueError):
    """A bundle directory is not a servable bundle.

    A `ValueError`, not a `RuntimeError`: it is never transient and retrying cannot help.
    `load_bundles` catches it, logs it and drops the target — it does not propagate,
    because one bad bundle must not stop the API from starting.
    """


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
    """One flashable file, resolved to an absolute path inside its bundle."""

    name: str
    path: Path
    offset: int
    size: int
    sha256: str

    @property
    def etag(self) -> str:
        """A strong ETag over the content hash the manifest already carries."""
        return f'"sha256-{self.sha256}"'


@dataclass(frozen=True, slots=True)
class AgentBundle:
    """One target's verified, flashable bundle."""

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
    # build time, where a mismatch is actionable. Doing it again per startup would re-read
    # `sdkconfig.resolved` on a path that exists to hash the parts.
    config_sha256: str | None
    build_digest: str | None
    # Absent in bundles built before R0-fw-1. Not verified against a file, because there
    # is none: the blob is written per board at flash time.
    config_partition: ConfigPartition | None
    parts: tuple[AgentPart, ...]

    @property
    def key(self) -> tuple[str, str]:
        """The catalog key: (target, partition_layout). S0-infra-6's store keys must use this shape."""
        return (self.target, self.partition_layout)

    def part(self, name: str) -> AgentPart | None:
        """The named part, or `None`. The lookup is by logical id, never by filename."""
        for part in self.parts:
            if part.name == name:
                return part
        return None


@dataclass(frozen=True, slots=True)
class FirmwareCatalog:
    """Every servable bundle, indexed by (target, partition_layout). Built once per app."""

    bundles: tuple[AgentBundle, ...]

    @classmethod
    def load(cls, directory: Path | str | None) -> FirmwareCatalog:
        """Scan `directory` for bundles. Never raises: an unreadable dir is an empty catalog."""
        if directory is None:
            return cls(bundles=())
        return cls(bundles=tuple(load_bundles(Path(directory))))

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


def _split_dir_name(name: str) -> tuple[str, str | None]:
    """Parse a bundle directory name into (target, layout_hint).

    Returns (target, None) for `esp32`, (target, layout) for `esp32.ab-8m-v1`.
    """
    target, sep, layout = name.partition(".")
    if not SAFE_TARGET.match(target):
        raise AgentBundleError(f"{name!r} is not a usable bundle directory name")
    if sep and not SAFE_TARGET.match(layout):
        raise AgentBundleError(f"{name!r} has an unusable layout suffix")
    return target, (layout or None)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(manifest_path: Path) -> BundleManifest:
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise AgentBundleError(f"{manifest_path} is implausibly large for a manifest")
    try:
        return BundleManifest.model_validate_json(manifest_path.read_bytes())
    except ValidationError as exc:
        raise AgentBundleError(
            f"{manifest_path} is not a valid manifest: {exc.error_count()} errors"
        ) from exc
    except (OSError, ValueError) as exc:
        raise AgentBundleError(f"{manifest_path} could not be read: {exc}") from exc


def _resolve_part_path(bundle_dir: Path, filename: str) -> Path:
    """Resolve `filename` inside `bundle_dir`, or refuse.

    Belt and braces, the same shape as `storage/objectstore.py::resolve_key`: the pattern
    already forbids a separator, and the `is_relative_to` check is what still holds if a
    future caller reaches this function with something the pattern never saw (a symlink
    planted in the bundle directory, for instance).
    """
    if not SAFE_FILENAME.match(filename):
        raise AgentBundleError(f"manifest path {filename!r} is not a bare filename")
    resolved = (bundle_dir / filename).resolve()
    if not resolved.is_relative_to(bundle_dir.resolve()):
        raise AgentBundleError(f"manifest path {filename!r} escapes {bundle_dir}")
    if not resolved.is_file():
        raise AgentBundleError(f"{resolved} named by the manifest does not exist")
    return resolved


def _load_bundle(bundle_dir: Path) -> AgentBundle:
    """Read, verify and return the bundle in `bundle_dir`. Raises `AgentBundleError`."""
    target, layout_hint = _split_dir_name(bundle_dir.name)

    manifest = _read_manifest(bundle_dir / MANIFEST_FILENAME)
    if manifest.target != target:
        raise AgentBundleError(
            f"manifest says target {manifest.target!r} but it sits in {bundle_dir.name!r}"
        )

    # The protocol contract. A bundle that disagrees would be flashed onto a board that
    # then announces a layout the server does not support — spec/device-protocol.md.
    known_layouts = list(SUPPORTED_LAYOUTS.keys())
    expected_slot = SUPPORTED_LAYOUTS.get(manifest.partition_layout)
    if expected_slot is None:
        raise AgentBundleError(
            f"partition_layout {manifest.partition_layout!r} is not one of {known_layouts} "
            "(spec/device-protocol.md)"
        )
    if manifest.ota_slot_size != expected_slot:
        raise AgentBundleError(
            f"ota_slot_size {manifest.ota_slot_size} is not the {expected_slot} that layout "
            f"{manifest.partition_layout!r} declares"
        )

    if layout_hint is not None and manifest.partition_layout != layout_hint:
        raise AgentBundleError(
            f"manifest says layout {manifest.partition_layout!r} but it sits in {bundle_dir.name!r}"
        )

    names = [part.name for part in manifest.parts]
    missing = [name for name in PART_NAMES if name not in names]
    if missing:
        raise AgentBundleError(f"manifest is missing part(s) {missing}")
    if len(set(names)) != len(names):
        raise AgentBundleError("manifest names the same part twice")

    parts: list[AgentPart] = []
    for entry in manifest.parts:
        path = _resolve_part_path(bundle_dir, entry.path)
        actual_size = path.stat().st_size
        if actual_size != entry.size:
            raise AgentBundleError(
                f"{entry.name}: {actual_size} bytes on disk, manifest says {entry.size}"
            )
        actual_sha = _sha256_of(path)
        if actual_sha != entry.sha256:
            raise AgentBundleError(f"{entry.name}: sha256 does not match the manifest")
        parts.append(
            AgentPart(
                name=entry.name,
                path=path,
                offset=entry.offset,
                size=entry.size,
                sha256=entry.sha256,
            )
        )

    app = next(part for part in parts if part.name == "app")
    if app.size > manifest.ota_slot_size:
        raise AgentBundleError(
            f"app is {app.size} bytes but an OTA slot is {manifest.ota_slot_size}; "
            "a board flashed with this image could never be updated"
        )

    # Loaded, not dropped: the bundle is flashable and nothing on the flash path needs
    # this. What is lost is the ability to answer "which build is on that board?" from a
    # diagnostic bundle, which is worth one line in the log and not an outage.
    if manifest.config_sha256 is None:
        logger.warning(
            "agent image for target %s carries no build identity (config_sha256): it "
            "predates S0-infra-3, so a diagnostic bundle from a board flashed with it "
            "cannot name the build configuration. Rebuild with `just agent-build %s`.",
            target,
            target,
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
        parts=tuple(sorted(parts, key=lambda part: part.offset)),
    )


def load_bundles(directory: Path) -> list[AgentBundle]:
    """Every verified bundle under `directory`, sorted by (target, partition_layout). Never raises.

    A missing directory, an empty one, and one holding nothing but junk all yield `[]` —
    that is the shape of a stack built without `agent/dist`, and `create_app()` logs one
    WARNING about it rather than refusing to start.
    """
    if not directory.is_dir():
        logger.warning(
            "agent images directory %s does not exist: the flasher (R0-fe-3) has nothing "
            "to offer and /v1/agent/manifest will answer 503. Build one with "
            "`just agent-build esp32` (docs/runbooks/agent-build.md).",
            directory,
        )
        return []

    loaded: list[AgentBundle] = []
    seen_keys: dict[tuple[str, str], str] = {}
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / MANIFEST_FILENAME).is_file():
            continue
        try:
            bundle = _load_bundle(child)
            # Drop duplicate keys (first-wins).
            if bundle.key in seen_keys:
                logger.warning(
                    "agent image dir %r dropped: %s is already provided by %r",
                    child.name,
                    bundle.key,
                    seen_keys[bundle.key],
                )
                continue
            seen_keys[bundle.key] = child.name
            loaded.append(bundle)
        except AgentBundleError as exc:
            logger.warning("agent image for target %s dropped: %s", child.name, exc)
        except OSError as exc:
            logger.warning("agent image for target %s is unreadable: %s", child.name, exc)

    # Sort by (target, partition_layout).
    bundles = sorted(loaded, key=lambda b: b.key)

    if not bundles:
        logger.warning(
            "no usable agent images in %s: the flasher has nothing to offer and "
            "/v1/agent/manifest will answer 503.",
            directory,
        )
    else:
        logger.info(
            "loaded %d agent image(s): %s",
            len(bundles),
            ", ".join(f"{b.target}/{b.partition_layout}@{b.agent_version}" for b in bundles),
        )
    return bundles
