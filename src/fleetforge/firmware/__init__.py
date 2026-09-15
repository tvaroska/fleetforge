"""Prebuilt agent firmware bundles — the seam between `agent/dist` and the flasher.

`R0-infra-2` created this package. `agent/Dockerfile` builds one bundle per chip target
(bootloader + partition table + otadata + app + the resolved sdkconfig + a manifest of
byte offsets and sha256s); `just agent-publish` uploads one into the object store; the
API reads it back per request and `api/routers/agent.py` serves it to the browser flasher
`R0-fe-3` ships.

**Agent bundles are artifacts, not image contents** (DECISIONS.md 2026-09-11, implemented
in S0-infra-6). They used to be baked into the app image by `COPY agent/dist /app/agent`
and read once at startup, which meant a firmware fix needed an image build and a deploy,
and the image carried ~4.5 MB of firmware that had nothing to do with the code in it.
They now go through the same `ObjectStore` seam R1's user artifacts use — the prerequisite
for that was never an org-policy exemption but a credential that is not a key file, and
S0-infra-5 landed it. **No signed URL enters the onboarding path**: the API reads the
bytes under the admin credential and streams them, so nothing device-shaped reaches a
browser.

Four modules, in the order bytes flow through them:

* `manifest.py` — the on-disk manifest and the shape the API serves. Unchanged by the move.
* `bundledir.py` — reads and verifies a *local* bundle directory. Publish-time only.
* `index.py` — the one mutable object naming which published bundles are current.
* `catalog.py` / `publish.py` — the store-backed read and write halves.

**A bundle is verified before it is servable, twice.** `load_bundle_dir` re-hashes every
part at publish and refuses the bundle, naming the target; `load_catalog` re-checks the
published manifest against `spec/device-protocol.md` and drops a bad entry with a WARNING.
The duplication is deliberate: the store can be written to by something other than the
CLI. A silently corrupt bundle is a brick on someone's desk, and a bundle with the wrong
layout is a board that can never OTA.

**The catalog is keyed by `(target, partition_layout)`** (S0-infra-7), and so is the
index. A bundle directory is `<target>` or `<target>.<layout>`; `just agent-build` still
writes the bare `<target>` name and is unchanged.

Held in `app.state.agent_catalog` as a `CatalogCache` (`create_app()`), the same per-app
rule as `token_cache`/`event_hub`. Construction does **no I/O** — a container must start
when the bucket is down — and the index is re-read at most once per `AGENT_CATALOG_TTL_S`,
so a newly published bundle appears without a restart.
"""

from fleetforge.firmware.bundledir import (
    AgentBundleError,
    LocalBundle,
    LocalPart,
    load_bundle_dir,
)
from fleetforge.firmware.catalog import (
    AgentBundle,
    AgentPart,
    AmbiguousBundleError,
    CatalogCache,
    FirmwareCatalog,
    load_catalog,
)
from fleetforge.firmware.index import (
    DEFAULT_INDEX_KEY,
    INDEX_SCHEMA,
    MAX_SUPERSEDED,
    AgentIndex,
    AgentIndexEntry,
    SupersededEntry,
)
from fleetforge.firmware.manifest import (
    EXPECTED_OTA_SLOT_SIZE,
    EXPECTED_PARTITION_LAYOUT,
    MANIFEST_SCHEMA,
    PART_NAMES,
    SUPPORTED_LAYOUTS,
    AgentBuildInfo,
    AgentManifest,
    AgentPartInfo,
    BundleManifest,
    ConfigPartition,
    PartManifest,
)

__all__ = [
    "AgentBuildInfo",
    "AgentBundle",
    "AgentBundleError",
    "AgentIndex",
    "AgentIndexEntry",
    "AgentManifest",
    "AgentPart",
    "AgentPartInfo",
    "AmbiguousBundleError",
    "BundleManifest",
    "CatalogCache",
    "ConfigPartition",
    "DEFAULT_INDEX_KEY",
    "EXPECTED_OTA_SLOT_SIZE",
    "EXPECTED_PARTITION_LAYOUT",
    "FirmwareCatalog",
    "INDEX_SCHEMA",
    "LocalBundle",
    "LocalPart",
    "MANIFEST_SCHEMA",
    "MAX_SUPERSEDED",
    "PART_NAMES",
    "PartManifest",
    "SUPPORTED_LAYOUTS",
    "SupersededEntry",
    "load_bundle_dir",
    "load_catalog",
]
