"""Read and verify a *local* agent bundle directory — the publish-time gate.

This module was the startup-time catalog until S0-infra-6 made agent bundles ordinary
content-addressed artifacts in `ObjectStore` (DECISIONS.md 2026-09-11 → *agent bundles
are artifacts, not image contents*). The verification did not go away with the
filesystem: it **moved forward**, from "the API starts" to "the bytes are published",
which is where a rejection is actionable. Only the publisher
(`firmware/publish.py`, `python -m fleetforge.firmware publish`) imports this.

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

Unlike the old `load_bundles`, `load_bundle_dir` **raises**: a publisher that silently
skipped a corrupt bundle would report success and leave the flasher serving the previous
build. Dropping-with-a-warning is still the right answer on the *read* side, and it
lives in `firmware/catalog.py` where one bad entry must not take the other three down.
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
    """A bundle directory is not a publishable bundle.

    A `ValueError`, not a `RuntimeError`: it is never transient and retrying cannot help.
    `firmware/__main__.py` turns it into a one-line `FAILED:` and a non-zero exit, which
    is what "corruption is refused with the target named" looks like from a terminal.
    """


@dataclass(frozen=True, slots=True)
class LocalPart:
    """One flashable file on disk, resolved to an absolute path inside its bundle."""

    name: str
    path: Path
    offset: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LocalBundle:
    """One target's verified bundle, as it sits in `agent/dist/<target>`."""

    target: str
    partition_layout: str
    agent_version: str
    directory: Path
    # The manifest **exactly as it is on disk**. The publisher uploads these bytes
    # verbatim and records their sha256 in the index: re-serialising the validated model
    # would give a digest that is not the digest of any file anyone can reproduce, and
    # `build_digest` would stop being checkable against the artifact.
    manifest_bytes: bytes
    manifest: BundleManifest
    parts: tuple[LocalPart, ...]

    @property
    def key(self) -> tuple[str, str]:
        """The catalog key: (target, partition_layout) — the same shape `AgentBundle` uses."""
        return (self.target, self.partition_layout)


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


def _read_manifest(manifest_path: Path) -> tuple[bytes, BundleManifest]:
    """The manifest's raw bytes and the validated model. Both, because the bytes ship."""
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise AgentBundleError(f"{manifest_path} is implausibly large for a manifest")
    try:
        raw = manifest_path.read_bytes()
        return raw, BundleManifest.model_validate_json(raw)
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


def load_bundle_dir(bundle_dir: Path | str) -> LocalBundle:
    """Read, verify and return the bundle in `bundle_dir`. Raises `AgentBundleError`.

    Every part is re-hashed against the manifest, the layout is checked against
    `spec/device-protocol.md`, and the app is checked to fit an OTA slot. Nothing here
    touches the network: this is the gate a publish runs *before* a byte is uploaded.
    """
    bundle_dir = Path(bundle_dir)
    if not (bundle_dir / MANIFEST_FILENAME).is_file():
        raise AgentBundleError(f"{bundle_dir} holds no {MANIFEST_FILENAME}")
    target, layout_hint = _split_dir_name(bundle_dir.name)

    manifest_bytes, manifest = _read_manifest(bundle_dir / MANIFEST_FILENAME)
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

    parts: list[LocalPart] = []
    for entry in manifest.parts:
        path = _resolve_part_path(bundle_dir, entry.path)
        actual_size = path.stat().st_size
        # The target is named in every part failure: `just agent-publish-all` verifies
        # several bundles in one run, and "app: 993697 bytes on disk" alone does not say
        # which board would have been bricked.
        if actual_size != entry.size:
            raise AgentBundleError(
                f"{target}: {entry.name} is {actual_size} bytes on disk, manifest says {entry.size}"
            )
        actual_sha = _sha256_of(path)
        if actual_sha != entry.sha256:
            raise AgentBundleError(f"{target}: {entry.name} sha256 does not match the manifest")
        parts.append(
            LocalPart(
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

    # Published, not refused: the bundle is flashable and nothing on the flash path needs
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

    return LocalBundle(
        target=manifest.target,
        partition_layout=manifest.partition_layout,
        agent_version=manifest.agent_version,
        directory=bundle_dir,
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        parts=tuple(sorted(parts, key=lambda part: part.offset)),
    )
