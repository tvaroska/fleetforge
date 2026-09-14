# Artifacts — storage, identity, and where builds come from

**Area:** infrastructure · **Status:** design, partly implemented
**Scope:** every sequence of bytes the product ever writes to a device — agent bundles
today, user firmware from R1, compiled artifacts from R9, delta images in V3.

This document exists because fleetforge currently has **two** firmware distribution
paths with different rules, and because the number of distinct images the product must
hold is about to stop being small. It states one storage model for all of them, names
the three identifiers that are currently conflated into the word "version", and settles
the question of whether images are fixed artifacts or built on demand.

Related: [decisions/infrastructure-agent-bundles-are-artifacts.md](decisions/infrastructure-agent-bundles-are-artifacts.md)
(the decision to unify the two paths) · [architecture.md](architecture.md) (the
device-facing thin waist) · [../docs/features/build-pipeline.md](../docs/features/build-pipeline.md) (R9).

## Where we are

| | **Agent bundles** | **User artifacts** |
|---|---|---|
| Location | `agent/dist/<target>/`, `COPY`d into the app image at `/app/agent` | `ObjectStore` → GCS in prod, MinIO in dev |
| Identity | the directory name, which is the chip target | sha256 (stated in `objectstore.py::put`; unimplemented) |
| Versioned by | **the application image** | their own record |
| Verified | every part re-hashed at startup (`firmware/catalog.py`) | on upload |
| Served | `GET /v1/agent/{target}/{part}`, behind the admin credential | signed URL, short TTL |
| Status | shipping since R0 | R1 |

The split was a correct trade at R0-infra-2 and `firmware/__init__.py` records why: the
bundles are identical for every tenant, they version with the image, and routing them
through `ObjectStore` would have made the R0 flasher depend on a GCS credential that
could not be minted. A working flasher beat an elegant one.

What has changed is that both halves of that reasoning have expiry dates. Targets grow
monotonically; "versions with the image" turns into "cannot be rolled back without a
redeploy" the first time a bundle is bad — which is exactly the S0-fw-3 situation, where
getting agent `19b0a0b` back means rebuilding and redeploying the API.

## Three pressures

**1. One agent version at a time.** The deployed agent is a property of the server
deploy. There is no "flash the previous agent", and `S0-infra-2` — three stale bundles
shipped in v0.3.0 — is the same coupling presenting as staleness rather than as an
absent rollback.

**2. The combinatorics.** Four chip targets today, with ESP32-H2, Thread and a Pi
adapter named in the roadmap. More than one partition layout eventually — and note that
`firmware/catalog.py` keys bundles by directory name, i.e. by target *alone*, while
`partition_layout` is carried inside the manifest where nothing can select on it. Add
the agent versions worth keeping, then V2's repo × ref × target, then V3's delta images,
which are indexed by *pairs* of versions and therefore quadratic. Nothing in this
sequence is tractable if an image is "a directory someone named".

**3. Nothing identifies a build.** `manifest.json` carries `agent_version`,
`source_commit`, `idf_version` and a digest-pinned `idf_image` — good provenance, and
more than most projects have. It carries **no digest of the build configuration and no
digest of the build as a whole.** That is precisely the identifier the S0-fw-3
investigation needed and did not have: three sessions went by before anyone established
that every brownout on record came from a 160 MHz `-Og` build, and that was recovered by
correlating a timestamp against `git log` rather than read off the artifact.

## The model

> **A build is a cached pure function. An artifact is immutable and content-addressed.
> A manifest is a cheap view over artifacts.**

### Blobs

`fleetforge/blobs/sha256/<hex>` — write-once, never overwritten, never deleted while
referenced, served with `Cache-Control: immutable`. `objectstore.py::put` already
documents overwrite-is-fine *because* R1 content-addresses; this makes that concrete and
turns the overwrite into a no-op by construction.

Deduplication is not the headline benefit but it is a real one: a new agent version
usually changes `app.bin` only, and `bootloader`, `partition-table` and `ota-data` are
byte-identical across versions and across every device. Storing them per bundle is
storing the same four kilobytes a thousand times.

The prefix rule in `objectstore.py` is unaffected and non-negotiable: `gs://btvaroska`
is shared with `secrets/`, podcast audio and backups, fleetforge owns `fleetforge/`
only, and `resolve_key` rejects rather than normalises.

### Metadata

Postgres holds what the bytes cannot: an `artifacts` row per digest (size, kind, target,
partition_layout, provenance) and a `builds` row mapping cache key → digest. "Do we
already have this?" becomes a lookup rather than a build.

### Manifests as views

A manifest is generated on read from digests, not stored as the unit of distribution.
This is what makes the combinatorics collapse — a manifest for a given
(target, layout, version) costs a query and some JSON rather than a directory of copied
bytes. `agent/dist/<target>/manifest.json` becomes a *build output* consumed at publish
time rather than the storage format.

The flasher already has the right shape for this. `flash.ts::buildFlashPlan` assembles a
list of `{label, address, data}` parts, one of which — `ff_cfg` — is **generated per
board** rather than fetched. An image is already a plan rather than a file; deltas,
alternate layouts and per-board config all fit that shape without a new concept.

### Per-device data stays out of artifact identity

`ff_cfg` as a separate part at a reserved offset is what lets N devices share one
artifact. It is the structural advantage over ESPHome, where configuration is compiled
in and a fleet of 377 means 377 compiles (see `products/docs/esphome-review.md`). Every
future feature gets checked against this: anything that pushes per-device data into the
image bytes re-creates their ten-hour fleet update, and should be routed through
`ff_cfg` or through runtime configuration instead.

## Versioning: three identifiers, one word

| Identifier | Answers | Today |
|---|---|---|
| `agent_version` (semver) | what we call it | `agent/version.txt` → `PROJECT_VER` ✅ |
| content digest | what it **is** | per-part sha256 ✅ · **no digest for the build as a whole** |
| provenance | what produced it | `source_commit` + `idf_version` + `idf_image` ✅ · **no config digest** |

Two gaps to close, both small:

**`config_sha256`.** The hash of `sdkconfig.resolved`, in the manifest and in the
`S0-fe-7` diagnostic bundle (which already ships the resolved config; hashing it is a
few lines). This is not speculative tidiness — "which build produced this brownout log?"
is the live question, and with a config digest on both sides it is a string comparison.

**The version-number collision.** `TODO.md` says S0-fw-3 "shipped in v0.3.3" while
`agent/version.txt` reads `0.2.0`: server release versions and agent versions, both
called "version", differing by more than a patch. The wire protocol gets this right —
`fw_version` and `agent_version` are separate fields in `up/announce` — and the prose
should follow it.

## Dynamic build: a cache miss, not a mode

The question that prompted this document was whether to move from fixed artifacts to
dynamic build with caching. The answer is that these are not alternatives. Prebuilt and
cached stays the steady state; a build is what happens when the cache misses. In
particular **onboarding must never depend on a build completing** — there is always a
pinned, known-good bundle set that needs no builder running, because the alternative is
an operator whose brand-new board waits on a compile.

Cache key:

```
H(idf_image_digest, target, partition_layout, source_tree_digest, config_digest)
```

`docs/features/build-pipeline.md` currently specifies `(repo, ref, toolchain)`. That key
is **wrong as written**: it omits build configuration, so two builds of the same ref with
different `sdkconfig` or PlatformIO env produce different bytes and collide. This is not
hypothetical — it is the shape of the S0-fw-3 confusion, one commit range and two
configurations. *Source tree*, not commit, for the same reason: a dirty tree or a
config-only change must not hit a stale entry.

Three tiers, cheapest first:

1. **Exact key hit** — no build; return the digest.
2. **Warm toolchain** — ccache plus an IDF build directory keyed on
   `(idf_image_digest, target)`. Order-of-magnitude faster than cold, and the tier that
   matters most in practice because most rebuilds change one component.
3. **Cold build** in the R9 sandbox.

Tier 1 is a Postgres lookup and belongs with the artifact work. Tiers 2 and 3 are the
R9 build runner and stay in V2; what changes in R9 is only the key.

## Sequencing

The R1 key scheme is not yet frozen — no `R1-` tasks exist — so the storage decisions
here are free now and expensive after the first object is written under a different
scheme. Filed in Sprint 0 as S0-infra-3 (build identity), S0-infra-4 (freeze the key
scheme), S0-infra-5 (a credential that is not a key file), S0-infra-6 (bundles served
from the store) and S0-infra-7 (catalog keyed by target *and* layout). The build engine
itself stays R9.
