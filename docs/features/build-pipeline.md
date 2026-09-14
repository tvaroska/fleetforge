# Build Pipeline — server-side compile

**Status:** Planned — **V2, not v1**
**Priority:** P1
**Target:** R9 (V2)
**Depends on:** Artifact API + provenance (vcs-integration.md) — R6
**Related:** [simulation.md](simulation.md) — R8 · [releases.md](../releases.md) → V2

## Overview

The server clones a repo at a ref and **builds the artifact itself**, so a user needs
neither a local toolchain nor a CI provider. Point Fleetforge at a repo, get a
deployable version.

Together with VCS ingestion (R6–R7) and the simulation gate (R8), this completes V2's
theme: **source → build → verify → deploy**, with nothing hand-carried.

## This reverses a v1 non-goal — deliberately

[spec/prd.md](../../spec/prd.md) v1 says *"the server never builds"* and puts the develop/build/debug pillar out
of scope. V2 takes the build half back. Two constraints keep that from corroding the
architecture:

1. **The builder is a producer, not a privileged path.** It emits an artifact into the
   same upload API that a GitHub Action or a human uses. Artifacts stay **opaque to the
   core** (design/architecture.md principle 2) — the core cannot tell a server-built blob from an
   uploaded one, and gains no ability to parse either.
2. **Debug stays out of scope.** Build only; no on-server debugging, no IDE.

## Security — this is arbitrary code execution

Building a repo means running that repo's build scripts on the server. On a
public-facing host this is the single most dangerous surface in the product, and it
must be treated as such **from the first commit**, not hardened later:

- Sandboxed, per-build ephemeral containers; no host mounts; no access to the control
  plane's network or database.
- Hard CPU / memory / disk / wall-clock caps.
- No ambient credentials in the build environment; private-repo tokens scoped to a
  single clone and never exposed to build scripts.
- Single-tenant v1 posture keeps the blast radius to your own code. **Multi-tenant
  hosting would make this a hostile-code problem** and needs a stronger boundary
  (microVM, not container).

## Reproducibility

- **Pinned ESP-IDF toolchain containers per target** (esp32 / S3 / C3 / C6), version
  pinned *per project* — an unpinned IDF makes builds irreproducible, which destroys
  the provenance guarantee R6 exists to provide.
- Record the toolchain version in the artifact's provenance alongside repo/commit/tag.
- Build cache keyed on
  `H(idf_image_digest, target, partition_layout, source_tree_digest, config_digest)` so
  re-deploys are instant. **Corrected 2026-09-14** from `(repo, ref, toolchain)`, which
  omits build configuration: two builds of the same ref with different `sdkconfig` or
  PlatformIO env produce different bytes and collide on that key. Not hypothetical — it
  is the shape of the S0-fw-3 confusion. Source *tree*, not commit, so a dirty tree or a
  config-only change cannot hit a stale entry. See `design/artifacts.md`.

## Toolchain support

Native **`idf.py` projects first** — it is the toolchain the agent and partition
tooling already assume. **PlatformIO second** (the recommended-but-optional multi-board
producer of design/architecture.md principle 2). Arduino CLI later, if asked for.

## Phase 1: R9 — Build from source

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R9-INFRA-1 | Pinned ESP-IDF builder images per target (esp32/S3/C3/C6) | P0 | 1.5d |
| R9-INFRA-2 | Sandboxed ephemeral build runner: no host mounts, resource + wall-clock caps | P0 | 2d |
| R9-BE-1 | Repo/ref registration + scoped clone credentials for private repos | P0 | 1.5d |
| R9-BE-2 | Build job queue + status state machine | P0 | 1.5d |
| R9-BE-3 | Emit artifact into the R6 upload API with full provenance incl. toolchain version | P0 | 1d |
| R9-BE-4 | Build cache: exact-key hit + warm toolchain tier (ccache / IDF build dir keyed on `(idf_image_digest, target)`) | P1 | 1d |
| R9-FE-1 | Repo config UI + build list | P0 | 1.5d |
| R9-FE-2 | Streamed build logs | P0 | 1d |
| R9-TEST-1 | E2E: register repo → build at a tag → artifact appears, deployable | P0 | 1d |
| R9-SEC-1 | Sandbox escape / resource-exhaustion test suite | P0 | 1d |

**Done when:** you point Fleetforge at a repo and a tag, and a deployable, fully
attributed artifact appears — with no toolchain on your machine.

## Post-V2

Build matrix (one repo → several targets in one job) · PR preview builds ·
Arduino CLI support · remote/scale-out builders.
