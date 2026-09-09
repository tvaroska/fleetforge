# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — a bad
build is caught before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-09-08
**Focus:** R0 (Enroll a board) active · backend landed (schema, compose stack, admin auth,
enrollment tokens, ingest, `/v1/enroll`, SSE) · frontend, firmware and broker authz next.

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

- [x] **R0-infra-1**: Standalone Compose stack (P0, 2d) ✅ 2026-09-08
      Dev loop *and* the V2 self-host promise: Traefik + Postgres + MinIO + Mosquitto +
      api + ingestor + frontend. API and dashboard on **one origin** (no CORS).
      This is the dev environment by design, so it cannot rot.
      Done: seven services up from wiped volumes; migrations run from the api
      entrypoint (`RUN_MIGRATIONS=true`); `/v1/healthz` + `/v1/readyz` served through
      nginx with no CORS header, no host port and no Traefik router for the api; MQTT
      publish through Traefik's `mqtt` entrypoint reaching the ingestor's
      `ff/v1/d/+/up/#` subscription; broker persistence and ingestor reconnect verified
      across a broker restart; MinIO bucket idempotent; `just up-prod` (production
      shape, non-root image) reproduces all of it. Dev-only anonymous broker access is
      quarantined in `mosquitto/conf.d/10-dev-anonymous.conf` for `R0-sec-1` to delete.
      Note: the mosquitto CLI clients force TLS on port 8883, so use `just mqtt-pub` /
      `just mqtt-sub` (paho) against the dev broker.
      _(done 2026-09-08; reviewed; see docs/features/infrastructure.md)_

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

- [x] **R0-db-1**: Schema (P0, 0.5d) ✅ 2026-09-08
      devices (+ `link_type`, `power_class`, `parent_device_id`, `partition_layout`,
      `ota_slot_size`), groups, enrollment tokens, admin tokens, and **`deploy_events`**
      (KPI-ready from R1 — R5 needs the history, not just the table).
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-1**: API scaffold + admin auth (P0, 2d)
      Public, versioned (`/v1`). Opaque bearer tokens hashed at rest (argon2id);
      `/v1/auth/login` returns the same token in an HttpOnly/Secure/SameSite=Strict
      cookie. `scopes`/`subject` columns reserved, always `admin` in v1.
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-2**: Enrolment tokens (P0, 0.5d)
      Short-lived (24 h), group-scoped, revocable, **single-use**.
      `POST/GET /v1/enrollment-tokens` + `POST …/{id}/revoke`; the burn ships as the
      single importable `auth.enrollment.BURN_SQL` that R0-be-4 will call.
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-3**: Ingestor process (P0, 1.5d)
      Single instance, **sole MQTT subscriber**. announce / presence / heartbeat →
      derived presence (LWT for always_on, `last_seen` for sleepy) → Postgres + `NOTIFY`.
      _(done 2026-09-08; see docs/features/*)_

- [x] **R0-be-4**: `POST /v1/enroll` over HTTPS, not MQTT (P0, 1.5d)
      Validate token → registry entry → provision broker credential (dynsec) → burn
      token. Keeps the broker from ever authenticating a client it has not heard of.
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-5**: SSE event stream (P0, 0.5d)
      Fanned out from Postgres `LISTEN`, so it works with N API workers.
      `GET /v1/events` (SSE) + `GET /v1/devices` (the read model the stream tells every
      client to re-read; presence derived on read, never stored). One dedicated asyncpg
      `LISTEN` connection per API process (`application_name='fleetforge-events'`) into a
      per-app `EventHub`; a slow client and a listener reconnect both **end the stream**
      rather than degrade it, and the client resyncs. Payload validated then forwarded
      verbatim, `\r`/`\n` refused — a board's `fw_version` must not be able to forge a
      frame. Auth once at connect, so the stream is capped at 15 min
      (`sse_max_stream_s`); no token in the query string.
      _(done 2026-09-08; reviewed; verified through nginx on `just up-prod`: event on the
      stream ~0.2 s after publish, keepalives every 15 s, fan-out to two clients, both
      401 paths, `docker compose restart postgres` → every stream ended, listener
      reconnected and events flowed again with the api never restarting and `/v1/healthz`
      200 throughout, retained replay still did not move `last_seen`; 209 tests green.
      Gotcha carried forward: httpx's `ASGITransport` buffers, so SSE tests drive the ASGI
      app directly. See docs/features/*)_

- [x] **R0-be-6**: Object-store adapter (P0, 1d)
      `put` / `get` / `signed_url` / `delete`. **GCS** backend with a service-account key
      scoped to `gs://btvaroska/fleetforge/`; **MinIO** behind the same interface for the
      dev stack and V2 self-hosting. One `ObjectStore` Protocol, two adapters
      (`storage/s3.py`, `storage/gcs.py`), selection in `storage/factory.py`,
      `ObjectStoreDep` in `api/deps.py` (no R0 route uses it — R1 does), and
      `just storage-check` (`python -m fleetforge.storage selftest`) as both the T2
      harness and the ops answer to "can this container reach the artifact store?".
      Nothing in `spec/` touched; two proposals (signed-URL TTL 30 min, `get()` cap
      8 MiB) recorded in `DECISIONS.md` instead.
      _(done 2026-09-08; reviewed; verified: 289 tests green with
      `tests/test_object_store_minio.py` **running**, not skipped, plus ruff/format/mypy
      and `just stack-check` clean; `just storage-check` on the host → put / get
      (sha256 match) / signed URL fetched over HTTP / delete / `ObjectNotFound` /
      idempotent second delete, `SELFTEST OK`; `--ttl 5 --keep` URL → 200 then 403
      `Request has expired` after 7 s; `../escape.bin`, `/abs.bin`, `a/../../b.bin`,
      `a//b.bin` all rejected before any backend call and `ls -R /data` inside MinIO
      showed nothing outside the bucket prefix; inside the api container the selftest
      prints a URL signed against `localhost:9000` (not `minio:9000`) that returns 200 /
      4096 bytes when curled from the host. `fleetforge:dev` 320 MB after the two SDKs.
      Gotchas carried forward: (1) a presigned URL signs the Host header, hence the
      `S3_ENDPOINT_URL` / `S3_PUBLIC_ENDPOINT_URL` split — the container selftest cannot
      fetch the URL it prints and says so; (2) `objectAdmin` under a prefix IAM condition
      cannot `storage.objects.list` (list is bucket-level), so a 403 on
      `gcloud storage ls` is correct; (3) no ADC fallback — GCS needs a key file or it
      fails at construction. **GCS was never round-tripped**: `btvaroska` inherits
      `constraints/iam.disableServiceAccountKeyCreation`, so the key cannot be minted —
      the SA and its conditional binding exist, the credential does not, T2 AC5/AC6 are
      unexecuted, and closing this is a prerequisite for R1
      (`docs/runbooks/artifact-storage.md` → BLOCKED). See docs/features/*)_

### Security

- [x] **R0-sec-1**: Mosquitto dynamic-security (P0, 1d)
      Per-device credentials + the two pattern ACLs
      (`pattern write ff/v1/d/%u/up/#`, `pattern read ff/v1/d/%u/dn/#`).
      _(done 2026-09-09; reviewed; see docs/features/* — CRITICAL, so T1 and T2 were
      re-run independently by the reviewer before the commit. `allow_anonymous false`;
      `mosquitto/conf.d/10-dev-anonymous.conf` deleted. **The plugin has no `%u`
      substitution** (verified on 2.0.22), so the design split in two: dynsec =
      authentication (`dynamic-security.json`, written by `/v1/enroll`), `acl_file` =
      the two pattern rules in the new `mosquitto/acl`; both are consulted and allow
      wins, and the dynsec `device` role is deliberately EMPTY. New `mosquitto-init`
      one-shot runs `mosquitto/bootstrap.sh` (idempotent; throwaway broker on 1884
      because `dynsec init` is the only file-mode subcommand) creating the empty
      `device` role, a read-only `ingestor` role + client, deny-by-default, and
      `chmod 0600` + `chown` on the store; the broker `depends_on` it with
      `service_completed_successfully` and its healthcheck now authenticates as the
      dynsec admin. Ingestor got its own credential; `just mqtt-pub`/`mqtt-sub` take
      user/password. New `just broker-check`
      (`python -m fleetforge.broker selftest`) is the permanent live harness, plus
      `tests/test_broker_config.py` (23 file-level tripwires, no broker).
      Verified: 312 tests + ruff + mypy + `just stack-check` green; wiped-state
      bring-up in **both** the dev and the production shape; two real boards enrolled
      through `POST /v1/enroll`, both `broker_provisioned_at` set and present in
      `dynamic-security.json`; own `up/` PUBACK RC:0, another device's `up/` RC:135,
      own `dn/` RC:135, `$CONTROL` RC:135, wrong password and anonymous refused; a
      device subscribed to `#` received nothing of another board's; a forged LWT
      produced no ingestor event; credential survived `docker compose restart
      mosquitto`; bootstrap re-run tolerated "already exists"; no password in any log.
      Vacuity-checked by breaking `%u`→`+` in `mosquitto/acl` and watching the
      selftest fail. Gotchas carried forward: (1) an allowed publish reads RC:0, not
      RC:16, whenever the ingestor is subscribed; (2) `Denied PUBLISH` is
      `MOSQ_LOG_DEBUG` in 2.0.22 and is therefore **absent** from the log — the
      `-V 5` PUBACK is the only evidence; (3) the `:ro` acl bind mount always logs
      `chown: … Read-only file system` + permission warnings, expected; (4) devices
      enrolled during the `NullProvisioner` era cannot be reconciled and must
      re-enroll. Nothing in `spec/` touched; three proposals in the hand-off.
      Reviewer note for `R0-infra-3`: the `acl_file` patterns apply to **every**
      authenticated username, so a `MQTT_DYNSEC_USERNAME`/`MQTT_INGESTOR_USERNAME`
      that happened to be 12 lowercase hex digits could be re-keyed by enrolling
      that device_id (`ensure_client` does create → already-exists →
      `setClientPassword`). Safe today — `ff-admin`/`ff-ingestor` are not valid
      device ids and `POST /v1/enroll` 422s them — but prod credentials must keep
      a non-hex username.
      See docs/features/infrastructure.md, DECISIONS.md 2026-09-08.)_

### Firmware

- [ ] **R0-fw-1**: ESP32 agent, connect-only (P0, 3d)
      Ships the **flash-time immutables**: A/B partition table, rollback-enabled
      bootloader, eFuse posture — none of which can be fixed by OTA later. Network via
      `esp_netif`; **SNTP before the first TLS handshake**; HTTPS enroll; announce /
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
      Flash → enroll → appears online in the dashboard.

---

**Total: ~24.5d** (20 tasks). Build order:
`infra-0 → spec-1 → db-1, infra-1 → be-1…be-6, sec-1` (with `test-1` early, so frontend
work needs no hardware) `→ infra-3 → fw-1, infra-2 → fe-1…fe-3 → infra-4 → test-2`.

**Parallel spike (de-risks R2):** throwaway OTA + auto-rollback spike on real flaky
Wi-Fi. Tracked in [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
