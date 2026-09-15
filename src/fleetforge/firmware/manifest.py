"""The on-disk agent bundle manifest, and the shape the API serves.

`agent/tools/make_manifest.py` writes one `manifest.json` per target inside the builder
container; this module is the *reader*. The two files are the contract between the
firmware pipeline and the API, so both name the same schema version and neither
recomputes what the other decided — in particular **no offset is ever derived here**.
An offset arrives from ESP-IDF's `flasher_args.json`, travels through the manifest, and
reaches `esptool-js` unmodified, because the bootloader lives at `0x1000` on ESP32 and
at `0x0` on the S3/C3/C6 and a wrong one flashes cleanly and never boots.

`chip_family` is the ESP Web Tools spelling (`ESP32`, `ESP32-C6`, …) so `R0-fe-3` hands
the structure to the browser flasher without a translation table of its own.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bumped only when the on-disk shape changes incompatibly. `make_manifest.py` writes it;
# `load_bundles` refuses anything else rather than guessing at an older layout.
MANIFEST_SCHEMA = 1

# `spec/device-protocol.md` → `up/announce` — identity. The layout every R0 board is
# flashed with, and the slot size it advertises so the server can perform `spec/flows.md`'s
# capability check. A bundle that disagrees with these is not servable: it would be
# flashed onto a board that then announces something the server does not support.
# Retyped from the spec on purpose — `agent/partitions.csv` is the other end of the same
# contract and `tests/test_agent_partitions.py` is what keeps the two equal.
EXPECTED_PARTITION_LAYOUT = "ab-4m-v1"
EXPECTED_OTA_SLOT_SIZE = 1966080

# Every layout this server understands, and the slot size a bundle claiming it MUST
# declare. One entry today: `ab-4m-v1` is frozen (DECISIONS.md 2026-09-09) and a new
# layout is a new id plus an entry here plus a `spec/device-protocol.md` change — never
# an edit to an existing row. The mapping is what keeps `partition_layout` and
# `ota_slot_size` from drifting apart: a bundle cannot claim `ab-4m-v1` with a 4 MB slot.
SUPPORTED_LAYOUTS: dict[str, int] = {EXPECTED_PARTITION_LAYOUT: EXPECTED_OTA_SLOT_SIZE}

# Logical part ids, and the only values `GET /v1/agent/{target}/{part}` will resolve.
# `bootloader` first, `app` last: the flasher writes them in ascending offset order.
PART_NAMES = ("bootloader", "partition-table", "ota-data", "app")

# Both `{target}` and `{part}` are path parameters. They are looked up in an in-memory
# index and never joined onto a path, so traversal is impossible by construction — this
# pattern exists so a hostile value cannot reach a log line, and so `..%2f` answers 404
# from the router rather than 422 from somewhere less predictable.
SAFE_SEGMENT = r"^[a-z0-9][a-z0-9-]{0,31}$"
SafeSegment = Annotated[str, Field(pattern=SAFE_SEGMENT)]

# S0-infra-3. Both are lowercase hex sha256, written by `make_manifest.py::build_identity`
# and never recomputed here — this module reads a manifest, it does not re-derive one.
# `Sha256Hex | None` everywhere: a bundle built before S0-infra-3 has neither field, and
# that makes it *old*, not invalid (see `BundleManifest` below).
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PartManifest(BaseModel):
    """One flashable file: where it goes, how big it is, what it hashes to."""

    model_config = ConfigDict(extra="ignore")

    name: SafeSegment
    # Always a bare filename. The loader re-checks this before resolving it.
    path: str = Field(min_length=1, max_length=64)
    offset: int = Field(ge=0)
    size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfigPartition(BaseModel):
    """Where per-board configuration is written at flash time (`ff_cfg`).

    Not a `PartManifest`: it has no file in the bundle. One build serves the whole fleet,
    and the 4 KB blob that makes a board *this* board — API URL, broker URI, link, a
    single-use enrollment token — is generated per device by the flasher and written to
    this offset. Carried in the manifest so `R0-fe-3` and the QEMU harness read `0x12000`
    from the built partition table rather than typing it.
    """

    model_config = ConfigDict(extra="ignore")

    # An ESP-IDF partition label: up to 16 characters, and underscores are legal (which
    # is why this is not a `SafeSegment` — `ff_cfg` would fail that pattern). It is never
    # joined onto a path; it exists so a mislabelled partition is visible.
    label: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,15}$")
    offset: int = Field(ge=0)
    size: int = Field(ge=1)


class BundleManifest(BaseModel):
    """`agent/dist/<target>/manifest.json`, as written by the builder."""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1] = Field(alias="schema")
    target: SafeSegment
    chip_family: str
    agent_version: str
    idf_version: str
    idf_image: str
    source_commit: str
    built_at: str
    partition_layout: str
    ota_slot_size: int = Field(ge=1)
    flash_size: str = ""
    # S0-infra-3, and optional for the same reason `config_partition` is: bundles built
    # before it exist. `config_sha256` is the hash of the bundle's `sdkconfig.resolved`;
    # `build_digest` covers the parts and the provenance fields together, so two builds of
    # one commit with different configuration are finally distinguishable. Malformed is
    # still refused — the pattern means a bundle either carries a real digest or none.
    config_sha256: Sha256Hex | None = None
    build_digest: Sha256Hex | None = None
    # Optional, and it has to stay optional: bundles built before R0-fw-1 are on disk and
    # in the registry, and refusing to load one would take the flasher offline for a field
    # that only the *new* flow needs.
    config_partition: ConfigPartition | None = None
    parts: list[PartManifest] = Field(min_length=1)


class AgentPartInfo(BaseModel):
    """A part as the dashboard sees it — the manifest's numbers, minus the filename.

    `path` is deliberately not exposed: the download route addresses a part by its
    logical `name`, so the API never hands a client a filename to send back.
    """

    name: str
    offset: int
    size: int
    sha256: str


class AgentBuildInfo(BaseModel):
    """One target's build, with everything `R0-fe-3` needs to flash a board."""

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
    # `None` for a bundle built before S0-infra-3. Served so the S0-fe-7 diagnostic bundle
    # can name the exact build a board was flashed from — the question that cost S0-fw-3
    # three sessions of correlating timestamps against `git log`.
    config_sha256: str | None = None
    build_digest: str | None = None
    # `None` for a bundle built before R0-fw-1; the flasher then has no offset to write a
    # config blob to and must say so rather than guess one.
    config_partition: ConfigPartition | None = None
    parts: list[AgentPartInfo]


class AgentManifest(BaseModel):
    """`GET /v1/agent/manifest` — every target this API can flash."""

    agent_version: str
    builds: list[AgentBuildInfo]
