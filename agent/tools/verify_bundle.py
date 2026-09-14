"""Check that a built agent bundle is what its manifest says it is.

The host-side half of `just agent-verify <target>`: re-hashes every part, re-checks
every size, recomputes the S0-infra-3 build identity (`config_sha256`, `build_digest`)
rather than trusting it, and greps the **resolved** sdkconfig for the posture. The other
half — decoding `partition-table.bin` with ESP-IDF's own `gen_esp32part.py` — needs the
builder image and lives in the justfile recipe.

Why re-verify something this script's sibling just wrote: `agent/dist/` is a working
directory on a developer's box, and its contents get flashed onto boards. A truncated
copy, a half-finished `--output type=local`, or an edited byte is otherwise invisible
until the board does not boot. The API's loader (`src/fleetforge/firmware/`) applies the
same rule at startup for the same reason.

Standard library only, so it also runs inside the builder image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# Its sibling in this directory, and the writer of the digest this script re-checks.
# `sys.path[0]` is `agent/tools/` whether the script is run from the repo root on the host
# or from `/project/tools` inside the builder, and both files are stdlib-only. Importing
# rather than reimplementing is the point: a second copy of the canonical serialisation
# would eventually disagree with the first, and the disagreement would look like a
# corrupt bundle.
from make_manifest import BuildError, build_identity

# design/architecture.md → Flash-time immutables, item 2. A BOOTLOADER option: absent
# here means R2 auto-rollback is impossible on every board flashed with this bundle.
REQUIRED_RESOLVED = [
    "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
    # R0-fw-1: both outbound channels are TLS in production and both validate against
    # the compiled-in Mozilla bundle. Without it the agent links but every handshake
    # fails inside mbedTLS, which is the least readable place for it to happen.
    "CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y",
    # R0-fw-1: without it mbedTLS ignores notBefore/notAfter, so an expired certificate
    # validates and the protocol's SNTP-before-TLS rule enforces nothing. IDF's default
    # is off, which is why this is checked in the BUILT config and not just the defaults.
    "CONFIG_MBEDTLS_HAVE_TIME_DATE=y",
]

# Item 3 — one-way eFuse burns, all off in v1. Only `=y` is a failure.
#
# EXACT names, never a prefix: a resolved sdkconfig is full of SoC *capability* symbols
# that are always `=y` on capable silicon — `CONFIG_SECURE_BOOT_V1_SUPPORTED=y` says the
# chip could do secure boot, not that this build burns anything. A prefix match here
# fails every esp32 build and teaches whoever hits it to delete the check.
FORBIDDEN_RESOLVED_OPTIONS = [
    "CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK",
    "CONFIG_APP_ANTI_ROLLBACK",
    "CONFIG_SECURE_BOOT",
    "CONFIG_SECURE_BOOT_V1_ENABLED",
    "CONFIG_SECURE_BOOT_V2_ENABLED",
    "CONFIG_SECURE_FLASH_ENC_ENABLED",
    "CONFIG_FLASH_ENCRYPTION_ENABLED",
]
FORBIDDEN_RESOLVED = re.compile(
    r"^(" + "|".join(FORBIDDEN_RESOLVED_OPTIONS) + r")=y$", re.MULTILINE
)


class BundleError(RuntimeError):
    """The bundle does not match its manifest, or carries the wrong posture."""


def verify(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BundleError(f"no manifest in {bundle_dir} — run: just agent-build <target>")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())

    for part in manifest["parts"]:
        # `path` is always a bare filename by construction (make_manifest.py); refuse
        # anything else rather than following it.
        if "/" in part["path"] or part["path"].startswith("."):
            raise BundleError(f"{part['name']}: manifest path {part['path']!r} is not a filename")
        blob = (bundle_dir / part["path"]).read_bytes()
        if len(blob) != part["size"]:
            raise BundleError(
                f"{part['name']}: {len(blob)} bytes on disk, manifest says {part['size']}"
            )
        digest = hashlib.sha256(blob).hexdigest()
        if digest != part["sha256"]:
            raise BundleError(f"{part['name']}: sha256 mismatch (disk {digest[:12]}…)")
        print(f"  0x{part['offset']:06x}  {part['path']:<22} {part['size']:>8} B  ok")

    app = next(part for part in manifest["parts"] if part["name"] == "app")
    if app["size"] > manifest["ota_slot_size"]:
        raise BundleError(
            f"app.bin is {app['size']} B but an OTA slot is {manifest['ota_slot_size']} B; "
            "this image could never be updated over the air"
        )

    # Where the flasher writes per-board config. Absent means a board flashed from this
    # bundle can never be told which server it belongs to.
    config = manifest.get("config_partition")
    if not isinstance(config, dict) or "offset" not in config:
        raise BundleError(
            "the manifest has no config_partition; a board flashed with this bundle "
            "would have nowhere to put its ff_cfg blob"
        )
    print(
        f"  0x{int(config['offset']):06x}  {str(config['label']):<22} "
        f"{int(config['size']):>8} B  config partition (written per board)"
    )

    # Build identity (S0-infra-3), recomputed rather than read. Both are optional: a
    # bundle built before this landed is old, not broken, and refusing it here would stop
    # `just agent-verify` on artefacts that are still perfectly flashable.
    resolved_bytes = (bundle_dir / "sdkconfig.resolved").read_bytes()
    claimed_config = manifest.get("config_sha256")
    if claimed_config is None:
        print("  build identity: absent (bundle predates S0-infra-3)")
    else:
        actual_config = hashlib.sha256(resolved_bytes).hexdigest()
        if actual_config != claimed_config:
            raise BundleError(
                f"config_sha256 mismatch: sdkconfig.resolved hashes to {actual_config[:12]}…, "
                f"the manifest claims {str(claimed_config)[:12]}…"
            )
        # Recomputed from the manifest's own fields with the writer's function, so the
        # digest cannot drift from what it claims to cover.
        actual_digest = build_identity(manifest)
        if actual_digest != manifest.get("build_digest"):
            raise BundleError(
                f"build_digest mismatch: recomputed {actual_digest[:12]}…, "
                f"the manifest claims {str(manifest.get('build_digest'))[:12]}…"
            )
        print(f"  config_sha256 {actual_config}")
        print(f"  build_digest  {actual_digest}  (recomputed, matches)")

    resolved = resolved_bytes.decode()
    for option in REQUIRED_RESOLVED:
        if option not in resolved.splitlines():
            raise BundleError(f"the BUILT config does not carry {option}")
    burned = FORBIDDEN_RESOLVED.findall(resolved)
    if burned:
        raise BundleError(f"the BUILT config enables an irreversible eFuse option: {burned}")

    print(
        f"  {manifest['target']} / {manifest['chip_family']}"
        f" · agent {manifest['agent_version']} · idf {manifest['idf_version']}"
        f" · {manifest['partition_layout']} · slot {manifest['ota_slot_size']} B"
        f" · rollback enabled, no eFuse burns"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        verify(args.bundle_dir)
    except (BundleError, BuildError, KeyError, OSError, ValueError) as exc:
        print(f"verify_bundle: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
