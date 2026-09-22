# Artifacts — storage, identity, and where builds come from

**Area:** infrastructure · **Status:** design, partly implemented ·
**the key scheme below is FROZEN** (S0-infra-4 — `src/fleetforge/storage/blobs.py`)
**Scope:** every sequence of bytes the product ever writes to a device — agent bundles
today, user firmware from R1, compiled artifacts from R10, delta images in V3.

This document exists because fleetforge currently has **two** firmware distribution
paths with different rules, and because the number of distinct images the product must
hold is about to stop being small. It states one storage model for all of them, names
the three identifiers that are currently conflated into the word "version", and settles
the question of whether images are fixed artifacts or built on demand.

Related: [decisions/infrastructure-agent-bundles-are-artifacts.md](decisions/infrastructure-agent-bundles-are-artifacts.md)
(the decision to unify the two paths) · [architecture.md](architecture.md) (the
device-facing thin waist) · [../docs/features/build-pipeline.md](../docs/features/build-pipeline.md) (R10).

## Where we are

| | **Agent bundles** | **User artifacts** |
|---|---|---|
| Location | `ObjectStore` → GCS in prod, MinIO in dev (S0-infra-6) | `ObjectStore` → GCS in prod, MinIO in dev |
| Identity | sha256 per part, `blobs/sha256/<digest>` (`storage/blobs.py`) | sha256 (stated in `objectstore.py::put`; unimplemented) |
| Versioned by | **the index object**, re-pointed by `just agent-publish` | their own record |
| Verified | at publish (`firmware/bundledir.py`) and again on read (`firmware/catalog.py`) | on upload |
| Served | `GET /v1/agent/{target}/{part}`, streamed behind the admin credential | signed URL, short TTL |
| Status | shipping since R0, in the store since S0-infra-6 | R1 |

The one key in the whole scheme that is **not** content-addressed is
`agent/index.json` — a small mutable JSON naming the current manifest digest per
`(target, partition_layout)`, plus a capped history of superseded digests. It exists
because `ObjectStore` has four verbs and `list` is not one of them (listing is a
per-backend paging contract, and the catalog would then be defined by whatever bytes
happen to be in a prefix). A pointer object makes "what is current" one read, makes a
publish atomic at the pointer, and makes rollback an index write rather than a rebuild.
It is written with `Cache-Control: no-store`; every blob it points at is immutable.

**Until S0-infra-6** the left-hand column read `agent/dist/<target>/`, `COPY`d into the
app image at `/app/agent`, verified once at startup and versioned by the application
image. That split was a correct trade at R0-infra-2 and `firmware/__init__.py` recorded
why: the bundles are identical for every tenant, they version with the image, and
routing them through `ObjectStore` would have made the R0 flasher depend on a GCS
credential that could not be minted. A working flasher beat an elegant one.

Both halves of that reasoning then expired — S0-infra-5 produced a keyless credential,
and the coupling started costing releases: targets grow monotonically, and "versions
with the image" turns into "cannot be rolled back without a redeploy" the first time a
bundle is bad — exactly the S0-fw-3 situation, where getting agent `19b0a0b` back used
to mean rebuilding and redeploying the API. It is now `just agent-rollback esp32 <digest>`
and one index write.

## Three pressures

**1. One agent version at a time.** The deployed agent is a property of the server
deploy. There is no "flash the previous agent", and `S0-infra-2` — three stale bundles
shipped in v0.3.0 — is the same coupling presenting as staleness rather than as an
absent rollback.

**2. The combinatorics.** Four chip targets today, with ESP32-H2, Thread and a Pi
adapter named in the roadmap. More than one partition layout eventually — `firmware/catalog.py`
is now keyed on `(target, partition_layout)` since S0-infra-7, so two layouts for one
chip coexist. Add the agent versions worth keeping, then V2's repo × ref × target, then
V3's delta images, which are indexed by *pairs* of versions and therefore quadratic.
Nothing in this sequence is tractable if an image is "a directory someone named".

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
referenced. Implemented and **frozen** in `src/fleetforge/storage/blobs.py`;
`objectstore.py::put` documents overwrite-is-fine *because* artifacts are
content-addressed, and that module is what makes the sentence true rather than a promise.

**The key handed to `ObjectStore` is store-relative.** `fleetforge/` above is the
*store's* prefix, joined on by `resolve_key`, not part of the key:

| | `Settings` | prefix | key handed to the store | resulting object |
|---|---|---|---|---|
| prod (GCS) | `gcs_prefix` | `fleetforge/` | `blobs/sha256/<hex>` | `gs://btvaroska/fleetforge/blobs/sha256/<hex>` |
| dev (MinIO) | `s3_prefix` | `""` (dedicated bucket) | `blobs/sha256/<hex>` | `fleetforge/blobs/sha256/<hex>` (bucket `fleetforge`) |

So `blob_key()` returns `blobs/sha256/<hex>` and never contains `fleetforge/`;
a `fleetforge/`-prefixed key is *refused* by `parse_blob_key`. Hardcoding the prefix
would write `fleetforge/fleetforge/blobs/…` in production and put dev and prod on two
different layouts.

Three more rules, all encoded:

* **Lowercase hex only, rejected and never repaired.** `AB…` and `ab…` would be two
  objects holding one artifact — the "two spellings of one object" failure
  `objectstore.py`'s reject-never-normalise rule exists to stop.
* **`Cache-Control: public, max-age=31536000, immutable`**, exactly, written as object
  metadata by `put_blob` — not a note in this document. The key *is* the bytes, so the
  object can never change; `ObjectStore.put` takes `cache_control` and both adapters
  send it only when asked, so an ordinary `put` is unchanged.
* **No existence pre-check before a put.** It is a round trip and a race, and it buys
  nothing: the same key always carries the same bytes, so a retried upload is a no-op.

Deduplication is not the headline benefit but it is a real one: a new agent version
usually changes `app.bin` only, and `bootloader`, `partition-table` and `ota-data` are
byte-identical across versions and across every device. Storing them per bundle is
storing the same four kilobytes a thousand times.

The prefix rule in `objectstore.py` is unaffected and non-negotiable: `gs://btvaroska`
is shared with `secrets/`, podcast audio and backups, fleetforge owns `fleetforge/`
only, and `resolve_key` rejects rather than normalises.

### Metadata

Postgres holds what the bytes cannot. Both tables exist as of S0-infra-4 (migration
`0003`) and both land **empty with no readers** — freezing them is free before the first
object is written and a data migration afterwards. Column rationale lives in the
`Artifact` / `Build` docstrings in `db/models.py`.

`artifacts`, one row per content digest:

| column | type | notes |
|---|---|---|
| `sha256` | TEXT **PK** | `CHECK ~ '^[0-9a-f]{64}$'` — the digest *is* the object key |
| `size_bytes` | BIGINT NOT NULL | `CHECK > 0`; a zero-byte artifact ships nothing |
| `kind` | TEXT NOT NULL | vocabulary in `ArtifactKind`, advisory, no CHECK |
| `target` | TEXT NULL | `esp32c6`; NULL where the bytes are target-independent |
| `partition_layout` | TEXT NULL | mirrors `devices.partition_layout` |
| `provenance` | JSONB NULL | the S0-infra-3 manifest identity, copied |
| `created_at` | TIMESTAMPTZ NOT NULL | server receipt time |

`builds`, cache key → the artifacts it produced:

| column | type | notes |
|---|---|---|
| `cache_key` | TEXT **PK** | `CHECK ~ '^[0-9a-f]{64}$'`; the key below under *Dynamic build* |
| `key_inputs` | JSONB NOT NULL | what was hashed; `CHECK jsonb_typeof = 'object'` |
| `outputs` | JSONB NOT NULL | `{"<part>": {"sha256", "offset"?}}`; same CHECK |
| `target`, `partition_layout` | TEXT NULL | denormalised for lookup; the key is authority |
| `created_at` | TIMESTAMPTZ NOT NULL | |

"Do we already have this?" is then a lookup rather than a build. Two absences are
deliberate and must stay:

* **No `artifacts.storage_key` column.** The key is `blob_key(sha256)`, a pure function
  of the primary key; storing it makes a second spelling that can disagree with the
  first. (Nor a `deleted_at`/refcount: nothing deletes blobs before R2, and a refcount
  with no decrementer is a lie.)
* **No FK from `builds.outputs`** — PostgreSQL cannot FK into JSONB. `outputs` is JSONB
  rather than a `build_outputs` join table because an agent bundle build produces four
  parts (one `artifact_sha256` column cannot hold them, and a per-part row would collide
  on the cache-key PK) and the set is consumed as a unit. The price is that **a future
  pruner must treat `builds.outputs` as a GC root**; a join table is the additive
  migration the day part-wise queries appear.

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
3. **Cold build** in the R10 sandbox.

Tier 1 is a Postgres lookup and belongs with the artifact work. Tiers 2 and 3 are the
R10 build runner and stay in V2; what changes in R10 is only the key.

## Sequencing

The storage decisions here were free while no object had been written under a different
scheme and expensive afterwards, which is why the key scheme and the two tables were
taken first, empty and unread. Filed in Sprint 0 as S0-infra-3 (build identity),
S0-infra-4 (the frozen key scheme — `storage/blobs.py`, migration `0003`), S0-infra-5 (a
credential that is not a key file), S0-infra-6 (bundles served from the store) and
S0-infra-7 (catalog keyed by target *and* layout). The build engine itself stays R10.

`just storage-check --blob` proves the frozen scheme against whichever backend is
configured: it writes at `blobs/sha256/<digest of the payload>` and reads the
`Cache-Control` back off a signed-URL GET.
