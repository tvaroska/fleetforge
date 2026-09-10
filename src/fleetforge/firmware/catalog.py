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
    EXPECTED_OTA_SLOT_SIZE,
    EXPECTED_PARTITION_LAYOUT,
    PART_NAMES,
    SAFE_SEGMENT,
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
    # Absent in bundles built before R0-fw-1. Not verified against a file, because there
    # is none: the blob is written per board at flash time.
    config_partition: ConfigPartition | None
    parts: tuple[AgentPart, ...]

    def part(self, name: str) -> AgentPart | None:
        """The named part, or `None`. The lookup is by logical id, never by filename."""
        for part in self.parts:
            if part.name == name:
                return part
        return None


@dataclass(frozen=True, slots=True)
class FirmwareCatalog:
    """Every servable bundle, indexed by target. Built once per app."""

    bundles: tuple[AgentBundle, ...]

    @classmethod
    def load(cls, directory: Path | str | None) -> FirmwareCatalog:
        """Scan `directory` for bundles. Never raises: an unreadable dir is an empty catalog."""
        if directory is None:
            return cls(bundles=())
        return cls(bundles=tuple(load_bundles(Path(directory))))

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(bundle.target for bundle in self.bundles)

    def bundle(self, target: str) -> AgentBundle | None:
        for bundle in self.bundles:
            if bundle.target == target:
                return bundle
        return None

    def __bool__(self) -> bool:
        return bool(self.bundles)


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
    target = bundle_dir.name
    if not SAFE_TARGET.match(target):
        raise AgentBundleError(f"{target!r} is not a usable target directory name")

    manifest = _read_manifest(bundle_dir / MANIFEST_FILENAME)
    if manifest.target != target:
        raise AgentBundleError(
            f"manifest says target {manifest.target!r} but it sits in {target!r}"
        )

    # The protocol contract. A bundle that disagrees would be flashed onto a board that
    # then announces a layout the server does not support — spec/device-protocol.md.
    if manifest.partition_layout != EXPECTED_PARTITION_LAYOUT:
        raise AgentBundleError(
            f"partition_layout {manifest.partition_layout!r} is not "
            f"{EXPECTED_PARTITION_LAYOUT!r} (spec/device-protocol.md)"
        )
    if manifest.ota_slot_size != EXPECTED_OTA_SLOT_SIZE:
        raise AgentBundleError(
            f"ota_slot_size {manifest.ota_slot_size} is not {EXPECTED_OTA_SLOT_SIZE} "
            "(spec/device-protocol.md)"
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
        config_partition=manifest.config_partition,
        parts=tuple(sorted(parts, key=lambda part: part.offset)),
    )


def load_bundles(directory: Path) -> list[AgentBundle]:
    """Every verified bundle under `directory`, sorted by target. Never raises.

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

    bundles: list[AgentBundle] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / MANIFEST_FILENAME).is_file():
            continue
        try:
            bundles.append(_load_bundle(child))
        except AgentBundleError as exc:
            logger.warning("agent image for target %s dropped: %s", child.name, exc)
        except OSError as exc:
            logger.warning("agent image for target %s is unreadable: %s", child.name, exc)

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
            ", ".join(f"{b.target}@{b.agent_version}" for b in bundles),
        )
    return bundles
