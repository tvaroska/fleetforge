# VCS Integration (v2)

**Priority:** P2
**Target:** R7, R8, R11 (V2)
**Depends on:** v1 complete (R6)
**Flow:** [flows.md](../../spec/flows.md) → Flow 2 (V2)

## Overview

An **automated artifact producer** feeding the same deploy pipeline — CI builds a `.bin`
and **pushes** it to Fleetforge with provenance (repo + commit SHA + tag + build URL).
The capability-check / sim / pull / confirm / rollback pipeline (R1–R6) stays the same.

Payoff = **traceability**: every device's firmware links to a commit — "what is running
on device X?" and "roll back to tag v1.3" become first-class.

## Decisions (from flows.md)

- **Ingestion = push first**. CI POSTs the artifact (ship a GitHub Action, templates for
  GitLab/Gitea/Forgejo). Provider-agnostic, holds no repo secrets, air-gap-friendly.
  Pull adapters (Fleetforge watches releases) come later.
- **Provider scope = provider-agnostic API**. One generic upload endpoint + provenance
  schema.
- **Deploy policy = per group**. Dev fleet auto-deploys on tag. Prod fleet stays manual.
  - *Sequencing:* enable auto-deploy-per-group with confidence only once canary/staged
    rollout lands — auto-deploy is only as safe as its rollback.

## Phase 1: R7 — Artifact API + provenance

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R7-BE-1 | Upload API accepts `.bin` + provenance (repo/commit/tag/build URL) | P0 | 1.5d |
| R7-DB-1 | Provenance schema. Link version → commit | P0 | 0.5d |
| R7-FE-1 | Dashboard shows provenance per version | P1 | 1d |

## Phase 2: R8 — Push ingestion (GitHub Action + templates)

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R8-INFRA-1 | Reusable GitHub Action: build & push on tag | P0 | 2d |
| R8-INFRA-2 | GitLab / Gitea / Forgejo templates | P1 | 1d |

## Phase 3: R11 — Deploy policy

> Per-device granularity at R11. **Group** granularity depends on group deploy from
> [V3 — the swarm track](groups-deploy.md), which left v1.

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R11-BE-1 | Per-group deploy policy: manual versus auto-deploy-on-matching-tag | P0 | 1.5d |
| R11-FE-1 | Group policy config UI | P1 | 1d |

**Done when:** you can continuous-deploy a dev fleet while prod stays manual. **v2 complete.**

---

## As built (`CI-CD-1` / `CUJ-2`, 2026-10-21)

Pulled forward for the e-ink workflow (`CUJ-2`) across migration `0011_ci_cd_and_device_cfg.py`, backend routers (`artifacts.py`, `projects.py`, `devices.py`, `deploys.py`), CLI (`src/fleetforge/cli.py`), and the Builds/Fleet dashboard tabs:

- **Artifact git provenance (`artifact_versions`)**:
  - `POST /v1/artifact` accepts optional multipart fields `repo_url`, `branch`, `commit_sha` (`^[0-9a-fA-F]{7,40}$`, stored lowercase), `tag`, `build_url`, and `test_passed` (`bool`), persisted on `artifact_versions` and returned in `ArtifactUploadResponse` and `ArtifactVersionItem.provenance`.
- **Project CI/CD tracking (`projects`)**:
  - `projects` stores `repo_url`, `track_mode` (`off | branch | tag | commit`), `track_ref`, `auto_deploy_target` (`off | dev | all`), and `ci_webhook_url`.
  - When `POST /v1/artifact` creates a version whose `test_passed` is not `False` and whose provenance matches `matches_project_tracking(project, provenance)`, `deploy_version_to_project_devices()` automatically stages the build to live project boards (`is_dev = true` when `auto_deploy_target = 'dev'`, or all live boards in the project when `auto_deploy_target = 'all'`) and returns `auto_deployed_devices` in the upload response.
  - When `PATCH /v1/projects/{slug}` updates `track_mode` or `track_ref` (or when `POST /v1/projects/{slug}/deploy` is called explicitly), Fleetforge selects the newest matching `artifact_versions` row (`test_passed IS NOT FALSE`) and deploys it immediately; if no matching build exists yet and `ci_webhook_url` is configured, Fleetforge dispatches a best-effort JSON webhook (`{"event": "fleetforge.track_ref_changed", "project": slug, "repo_url": ..., "track_mode": ..., "track_ref": ...}`).
- **Dev-board bench controls (`devices` + `dn/cfg`)**:
  - `PATCH /v1/devices/{device_id}` updates `is_dev` and `stay_awake`. Changing `stay_awake` publishes retained `ff/v1/d/{device_id}/dn/cfg` (`{"stay_awake": bool, "secrets_rev": int}`, QoS 1) so a sleepy e-ink dev board stays awake on the bench during rapid CI iteration.
- **CLI & GitHub Actions template**:
  - `python -m fleetforge.cli push <bin> --target <chip> --version <ver> [--project <slug>] [--repo ...] [--branch ...] [--commit ...] [--tag ...] [--build-url ...] [--test-passed]` uploads a built binary with git provenance (auto-detecting `GITHUB_*` env vars when present).
  - `python -m fleetforge.cli track --project <slug> --mode <off|branch|tag|commit> [--ref <ref>] [--target <off|dev|all>]` updates project tracking from the terminal.
  - Ready-to-copy workflow templates:
    - GitHub Actions: `docs/examples/github-actions-fleetforge.yml`
    - Google Cloud Build (dockerless / Cloudtop friendly): `docs/examples/cloudbuild-fleetforge.yaml`
- **Dashboard (`ContinuousDeploymentCard` + `UploadedBuilds` + `FleetView`)**:
  - The `2. BUILDS` tab renders `ContinuousDeploymentCard` (`repo_url`, `track_mode`, `track_ref`, `auto_deploy_target`, `ci_webhook_url`, and `Deploy tracked ref now`) and provenance badges (`commit@branch`, `#tag`, `build` link, `CI PASS`/`CI FAIL`) in `UploadedBuilds`.
  - The `1. FLEET` tab exposes `DEV` (`is_dev`) and `AWAKE` (`stay_awake`) toggles on each device row.
