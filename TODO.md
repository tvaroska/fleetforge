# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — a bad
build is caught before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-09-08
**Focus:** R0 (Enroll a board) active · pre-code, nothing implemented yet.

<!-- Counters: spec=1 infra=4 db=1 be=6 fe=3 sec=1 fw=1 test=2 -->

Live status lives ONLY here. States: `- [ ]` open · `- [x]` done · `- [!]`
attempted-but-failed. `spec/` and `design/` are status-free.

> Requirements: [spec/prd.md](spec/prd.md) · Wire contract: [spec/device-protocol.md](spec/device-protocol.md) ·
> Flows: [spec/flows.md](spec/flows.md) · Contracts & platform design: [design/architecture.md](design/architecture.md) ·
> Topology & stack: [design/production.md](design/production.md)
> Release ladder: [docs/roadmap.md](docs/roadmap.md) · [docs/releases.md](docs/releases.md) ·
> Completed work: [docs/features/](docs/features/) · Decisions: `DECISIONS.md`

> **Task IDs:** fleetforge is release-driven, so IDs are `R{N}-{category}-{number}`
> (e.g. `R0-be-1`). Sprint 0 uses `S0-{category}-{number}`.
> Categories: db, be, fe, test, qa, sec, infra, fw, spec, rel, perf.

**Deployment (v1):** single hosted instance at `bingo.tvaroska.sk` (domain reused from
the retired bingo app), single-tenant, **not a public product until V3**.
**Versions:** V1 = R0–R5 (safe OTA, ~5 boards) · V2 = R6–R10 (VCS + compile + simulation)
· V3 = robotic swarm (gateway + drones).

---

## Sprint 0: Critical Issues

Nothing here yet — the project is pre-R0. Bricking risks, broker auth and security
issues get filed here as they surface.

---

## R0: Enroll a board (UI + recognition + flash + connect)

**Goal:** *I can register a board and see it online.* No code-deploy yet.
**Risk retired:** onboarding · board recognition · device↔server connection.
**Done when:** plug in a board, flash & register it from the browser, watch it come
online — no toolchain, no CLI.

Decisions & guardrails: [docs/features/enrollment.md](docs/features/enrollment.md).
Targets these tasks must hit (intervals, timeouts, limits):
[spec/prd.md](spec/prd.md) → *Requirements & targets*.

Two non-obvious rules from [design/production.md](design/production.md): the **ingestor is
the only MQTT subscriber** (N API workers would otherwise ingest N times and split the SSE
audience), and **agent images are built off-box** (the ESP-IDF builder is 2–3 G against
5.5 G of free disk on `prod`).

### Protocol

- [x] **R0-spec-1**: Device protocol v1 — topic tree, QoS/retain, payload schemas, state machine (P0, 0.5d) ✅ 2026-09-08
      [spec/device-protocol.md](spec/device-protocol.md). Near-frozen: an R0 agent speaks
      this until someone physically retrieves the board.

### Infrastructure

- [x] **R0-infra-0**: Retire bingo (P0, 0.5d) ✅ 2026-09-08
      `services` repo: drop `bingo` + `bingo-frontend` and the route, remove `bingo.env`
      and `BINGO_PASSWORD`, drop from the deploy registry in `scripts/deploy.sh`.
      **Back up the bingo database before dropping the role** — the one irreversible step.
      Frees 384 M of declared limits and `bingo.tvaroska.sk`.
      Done: both containers stopped and removed (prod 12 → 10 containers); `bingo_db`
      dumped to `gs://btvaroska/retired/bingo/` (verified `pg_restore -l`, 95 TOC
      entries) then `bingo_db` + `bingo_user` dropped; compose blocks, Traefik router,
      `prod/bingo.env`, postgres init lines, deploy/validate registry entries and the
      bingo smoke endpoint all removed; prod memory 1913 → 1742 MB used, swap 1038 →
      783 MB (baseline for R0-infra-4); `bingo.tvaroska.sk` now returns 404 from
      Traefik's default backend and is free for R0-infra-3. Follow-ups: boris dashboard
      tile edited locally but image not rebuilt (Phase 4 deferred); `BINGO_PASSWORD`
      left in `services/prod/.env` (dead credential, owner's call).
      _(done 2026-09-08; reviewed; see docs/features/* — record lives in
      [design/production.md](design/production.md) → *Retiring bingo* and
      `services/DECISIONS.md` 2026-09-08)_

- [ ] **R0-infra-1**: Standalone Compose stack (P0, 2d)
      Dev loop *and* the V2 self-host promise: Traefik + Postgres + MinIO + Mosquitto +
      api + ingestor + frontend. API and dashboard on **one origin** (no CORS).
      This is the dev environment by design, so it cannot rot.

- [ ] **R0-infra-2**: Agent firmware build pipeline (P0, 1d)
      Pinned ESP-IDF, per-target images, built **off-box** and pushed to Artifact
      Registry; binaries served by the API for the Web Serial flasher.

- [ ] **R0-infra-3**: Prod ingress — `services` repo (P0, 1d)
      Traefik `mqtt` entrypoint on 8883 + TCP router (``HostSNI(`bingo.tvaroska.sk`)`` →
      `mosquitto:1883`), GCP firewall rule, and the prod service fragment.
      First non-HTTP port in this stack; touches shared ingress.

- [ ] **R0-infra-4**: Capacity check on `prod` before E2E (P0, 0.5d)
      The box is already swapping ~1 G with 12 containers. Measure with fleetforge
      running; size the VM up rather than shaving container limits.

### Backend

- [ ] **R0-db-1**: Schema (P0, 0.5d)
      devices (+ `link_type`, `power_class`, `parent_device_id`, `partition_layout`,
      `ota_slot_size`), groups, enrolment tokens, admin tokens, and **`deploy_events`**
      (KPI-ready from R1 — R5 needs the history, not just the table).

- [ ] **R0-be-1**: API scaffold + admin auth (P0, 2d)
      Public, versioned (`/v1`). Opaque bearer tokens hashed at rest (argon2id);
      `/v1/auth/login` returns the same token in an HttpOnly/Secure/SameSite=Strict
      cookie. `scopes`/`subject` columns reserved, always `admin` in v1.

- [ ] **R0-be-2**: Enrolment tokens (P0, 0.5d)
      Short-lived (24 h), group-scoped, revocable, **single-use**.

- [ ] **R0-be-3**: Ingestor process (P0, 1.5d)
      Single instance, **sole MQTT subscriber**. announce / presence / heartbeat →
      derived presence (LWT for always_on, `last_seen` for sleepy) → Postgres + `NOTIFY`.

- [ ] **R0-be-4**: `POST /v1/enrol` over HTTPS, not MQTT (P0, 1.5d)
      Validate token → registry entry → provision broker credential (dynsec) → burn
      token. Keeps the broker from ever authenticating a client it has not heard of.

- [ ] **R0-be-5**: SSE event stream (P0, 0.5d)
      Fanned out from Postgres `LISTEN`, so it works with N API workers.

- [ ] **R0-be-6**: Object-store adapter (P0, 1d)
      `put` / `get` / `signed_url` / `delete`. **GCS** backend with a service-account key
      scoped to `gs://btvaroska/fleetforge/`; **MinIO** behind the same interface for the
      dev stack and V2 self-hosting.

### Security

- [ ] **R0-sec-1**: Mosquitto dynamic-security (P0, 1d)
      Per-device credentials + the two pattern ACLs
      (`pattern write ff/v1/d/%u/up/#`, `pattern read ff/v1/d/%u/dn/#`).

### Firmware

- [ ] **R0-fw-1**: ESP32 agent, connect-only (P0, 3d)
      Ships the **flash-time immutables**: A/B partition table, rollback-enabled
      bootloader, eFuse posture — none of which can be fixed by OTA later. Network via
      `esp_netif`; **SNTP before the first TLS handshake**; HTTPS enrol; announce /
      presence / heartbeat.

### Frontend

- [ ] **R0-fe-1**: "Enroll a board" page — generate token (P0, 1d)

- [ ] **R0-fe-2**: Live device list via SSE — online/offline, version, last-seen (P0, 1.5d)

- [ ] **R0-fe-3**: Web Serial flasher (P0, 2.5d)
      `esptool-js`: port select → chip detect → board-confirm shortlist → flash agent +
      baked config. Chromium-only; that limit is accepted in the spec.

### Test

- [ ] **R0-test-1**: Python device simulator (P0, 0.5d)
      Speaks the R0-spec-1 protocol. Unblocks backend and frontend without hardware;
      sleepy and slow-link modes. Build this early.

- [ ] **R0-test-2**: E2E on real hardware (P0, 1d)
      Flash → enrol → appears online in the dashboard.

---

**Total: ~24.5d** (20 tasks). Build order:
`infra-0 → spec-1 → db-1, infra-1 → be-1…be-6, sec-1` (with `test-1` early, so frontend
work needs no hardware) `→ infra-3 → fw-1, infra-2 → fe-1…fe-3 → infra-4 → test-2`.

**Parallel spike (de-risks R2):** throwaway OTA + auto-rollback spike on real flaky
Wi-Fi. Tracked in [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
