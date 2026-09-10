"""Prebuilt agent firmware bundles — the seam between `agent/dist` and the flasher.

`R0-infra-2` created this package. `agent/Dockerfile` builds one bundle per chip target
(bootloader + partition table + otadata + app + the resolved sdkconfig + a manifest of
byte offsets and sha256s); `Dockerfile` bakes `agent/dist` into the app image at
`/app/agent`; this module reads it, and `api/routers/agent.py` serves it to the browser
flasher `R0-fe-3` ships.

**This seam is filesystem-only and deliberately has no local/GCS adapter pair**, unlike
`fleetforge.storage`. These bytes are *build outputs*, not user artifacts: they version
with the app image, they are the same for every tenant, and they are ~1.2 MB per target.
Routing them through `ObjectStore` would make the R0 flasher depend on a GCS credential
that cannot currently be minted at all (`docs/runbooks/artifact-storage.md` → BLOCKED:
`constraints/iam.disableServiceAccountKeyCreation`) — i.e. it would take a working
feature and make it unshippable. R1's *user* artifacts still go through `ObjectStore`.

**A bundle is verified before it is servable.** `load_bundles` re-hashes every part at
startup and drops, with a WARNING naming the target, any bundle whose bytes do not match
its manifest or whose `partition_layout`/`ota_slot_size` disagree with
`spec/device-protocol.md`. A silently corrupt bundle is a brick on someone's desk, and a
bundle with the wrong layout is a board that can never OTA — neither is worth serving to
keep an endpoint from answering 503.

Loaded **once per app** into `app.state.firmware_catalog` (`create_app()`), the same rule
as `token_cache`/`event_hub`: per app, never module-level, so tests get a fresh one. The
dev override bind-mounts `./agent/dist`, so a rebuilt bundle needs an api restart —
uvicorn `--reload` watches `src/` only (docs/runbooks/agent-build.md).
"""

from fleetforge.firmware.catalog import (
    AgentBundle,
    AgentBundleError,
    AgentPart,
    FirmwareCatalog,
    load_bundles,
)
from fleetforge.firmware.manifest import (
    EXPECTED_OTA_SLOT_SIZE,
    EXPECTED_PARTITION_LAYOUT,
    MANIFEST_SCHEMA,
    PART_NAMES,
    AgentBuildInfo,
    AgentManifest,
    AgentPartInfo,
    BundleManifest,
    ConfigPartition,
    PartManifest,
)

__all__ = [
    "EXPECTED_OTA_SLOT_SIZE",
    "EXPECTED_PARTITION_LAYOUT",
    "MANIFEST_SCHEMA",
    "PART_NAMES",
    "AgentBuildInfo",
    "AgentBundle",
    "AgentBundleError",
    "AgentManifest",
    "AgentPart",
    "AgentPartInfo",
    "BundleManifest",
    "ConfigPartition",
    "FirmwareCatalog",
    "PartManifest",
    "load_bundles",
]
