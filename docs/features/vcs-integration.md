# VCS Integration (v2)

**Status:** Planned
**Priority:** P2
**Target:** R8–R10 (v2)
**Depends on:** v1 complete (R7)
**Flow:** [FLOWS.md](../FLOWS.md) → Flow 2 (V2)

## Overview

An **automated artifact producer** feeding the same deploy pipeline — CI builds a `.bin`
and **pushes** it to Fleetforge with provenance (repo + commit SHA + tag + build URL).
The capability-check / sim / pull / confirm / rollback pipeline (R1–R7) is unchanged.

Payoff = **traceability**: every device's firmware links to a commit — "what's running
on device X?" and "roll back to tag v1.3" become first-class.

## Decisions (from FLOWS.md)

- **Ingestion = push first.** CI POSTs the artifact (ship a GitHub Action; templates for
  GitLab/Gitea/Forgejo). Provider-agnostic, holds no repo secrets, air-gap-friendly.
  Pull adapters (Fleetforge watches releases) come later.
- **Provider scope = provider-agnostic API.** One generic upload endpoint + provenance
  schema.
- **Deploy policy = per group.** Dev fleet auto-deploys on tag; prod fleet stays manual.
  - *Sequencing:* enable auto-deploy-per-group with confidence only once canary/staged
    rollout lands — auto-deploy is only as safe as its rollback.

## Phase 1: R8 — Artifact API + provenance

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R8-BE-1 | Upload API accepts `.bin` + provenance (repo/commit/tag/build URL) | P0 | 1.5d |
| R8-DB-1 | Provenance schema; link version → commit | P0 | 0.5d |
| R8-FE-1 | Dashboard shows provenance per version | P1 | 1d |

## Phase 2: R9 — GitHub Action (push ingestion)

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R9-INFRA-1 | Reusable GitHub Action: build & push on tag | P0 | 2d |
| R9-INFRA-2 | GitLab / Gitea / Forgejo templates | P1 | 1d |

## Phase 3: R10 — Per-group deploy policy (→ v2 complete)

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R10-BE-1 | Per-group deploy policy: manual vs auto-deploy-on-matching-tag | P0 | 1.5d |
| R10-FE-1 | Group policy config UI | P1 | 1d |

**Done when:** you can continuous-deploy a dev fleet while prod stays manual. **v2 complete.**
