# Agent bundles are artifacts, not image contents

**Date:** 2026-09-11 · **Area:** infrastructure · **Status:** Accepted, not yet implemented
**Supersedes:** the distribution half of R0-infra-2's `COPY agent/dist /app/agent` — not
its build half, which is unchanged.

## Context

`Dockerfile:68` bakes `agent/dist` into the application image; `src/fleetforge/firmware/`
reads it from `AGENT_IMAGES_DIR` and serves it to the browser flasher. R1's user
artifacts go through `fleetforge.storage` / `ObjectStore`. The product therefore has two
firmware distribution paths, and onboarding depends on the one that is not the general
mechanism.

R0-infra-2 chose this deliberately and the module docstring records the reasoning: the
bundles version with the image, they are identical for every tenant, and `ObjectStore`
would have made the flasher depend on a GCS credential blocked by
`constraints/iam.disableServiceAccountKeyCreation`. A working flasher beat an elegant
one, and that trade was correct at the time.

Two facts have since changed it:

1. **Target count grows monotonically.** Four chips at ~1.2 MB each today; ESP32-H2, a
   Thread path and a Raspberry Pi adapter are already named in the roadmap. Every future
   chip taxes every application image, forever.
2. **The coupling has already produced the defect it implies.** `S0-infra-2`: three of
   four bundles missed the S0-fw-1 stage reporter and shipped stale in v0.3.0. Baking
   makes firmware currency a property of human memory — whether someone ran
   `just agent-build-all` before `just build`.

## Decision

Agent bundles move behind `ObjectStore`, distributed as versioned artifacts through the
same path as user artifacts. **The application image ships zero agent bundles.**
Publishing a bundle must not require rebuilding or redeploying the application.

Verification moves with the bundles rather than being dropped: per-part sha256,
`partition_layout` / `ota_slot_size` agreement with `spec/device-protocol.md`, a failing
bundle dropped with its target named. Provenance stays in the manifest.

### Rejected: a baked fallback tier

The considered alternative was store-first with a known-good baked set as fallback,
which would have kept onboarding working through a store outage. Rejected because it
preserves the defect rather than mitigating it: the image still grows with target count,
the bundles still have to be rebuilt at image build time to be worth falling back to,
and "which tier answered" becomes a new thing to diagnose during onboarding — the one
flow the product has just spent four tasks making self-explanatory. A fallback that is
allowed to be stale is the v0.3.0 bug with an extra branch.

## Consequences

**Accepted, with the cost stated.** Onboarding — the core flow — comes to depend on the
object store being reachable and credentialled. Today it is neither. This makes the GCS
blocker in `docs/runbooks/artifact-storage.md` a hard prerequisite for this work rather
than a caveat on it; that blocker already gates R1, so it is not new debt, but this
feature cannot be started before it closes.

An unreachable store must therefore present as a named fault in the flasher, held to the
*Unaided onboarding* standard: a board that cannot be flashed says why, in plain
language. It must not offer a manifest it cannot honour.

Self-hosting is unaffected — dev and self-host stacks serve through the same interface
against MinIO, with no GCS dependency. That the interface already has two adapters is
what makes this move small.

`S0-infra-2`'s staleness guard must be re-pointed from the image build to the publish
step. The two pieces of work must not contradict each other; whoever lands `S0-infra-2`
first should site the guard so re-pointing is a move rather than a rewrite.
