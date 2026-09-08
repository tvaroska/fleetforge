# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-09-08 — Adopted gen-3 planning layout

- **Decision:** Migrated from `PLAN.md` + `docs/` to the gen-3 layout used by every
  other repo in the estate: `TODO.md` (live status only), `spec/` (the WHAT,
  status-free), `design/` (the HOW, status-free), `docs/` (planning/ops), this file,
  and `CRITICAL.md`.
- **Moves:** `docs/SPEC.md` → `spec/prd.md`; `docs/device-protocol.md` →
  `spec/device-protocol.md`; `docs/FLOWS.md` → `spec/flows.md`; `docs/DESIGN.md` →
  `design/architecture.md`; `docs/architecture.md` → `design/production.md`;
  `docs/RELEASES.md` → `docs/releases.md`; `PLAN.md` → `TODO.md` (task IDs lowercased,
  `R0-BE-1` → `R0-be-1`, tables → checkbox items).
- **Why:** fleetforge was the last repo on the old layout, and root `CLAUDE.md` still
  documented it. The restructure done the same day had already rebuilt gen-3's
  *distinctions* (requirements vs. design vs. tasks) under the old filenames, so the
  migration was mechanical.

---

## 2026-09-08 — Artifacts in GCS, MinIO for self-hosting

- **Decision:** Artifact bytes live in GCS (`gs://btvaroska/fleetforge/`) behind a narrow
  object-store adapter (`put` / `get` / `signed_url` / `delete`). MinIO is the
  self-hosted backend, S3-compatible, arriving with V2 turnkey self-hosting.
- **Why:** GCS signed URLs *are* the mechanism the `stage` command already specifies —
  short-lived, signature-as-authorization, range-capable, served without touching the
  API process. Artifact bytes also stay off the prod VM's 5.5 G of free disk and off its
  bandwidth.
- **Cost, accepted:** v1 has a cloud dependency for artifact storage. The adapter
  boundary is what keeps removing it a configuration change. Recorded honestly in
  `spec/prd.md` → *Security & data posture*.
- **Details:** [design/production.md](design/production.md) → *Artifact storage*.

---

## 2026-09-08 — Retire bingo; fleetforge takes `bingo.tvaroska.sk`

- **Decision:** The unfinished bingo app is retired from production and fleetforge reuses
  its domain. Not a public product until V3, so the domain is an operational detail.
- **Why:** frees 384 M of declared container limits on a box already swapping ~1 G, plus
  an existing Let's Encrypt route. Fleetforge needs ~512 M, so the net addition is ~128 M.
- **Not decided:** whether to delete the bingo repo or its Artifact Registry images.
  Retiring the deployment is not deleting the project. The bingo **database must be
  backed up before the role is dropped** — the one irreversible step (`R0-infra-0`).

---

## 2026-09-08 — The ingestor is the only MQTT subscriber

- **Decision:** A single-instance ingestor process is the sole MQTT subscriber; it writes
  to Postgres and `NOTIFY`s. API workers `LISTEN` and fan out over SSE. The API never
  subscribes.
- **Why:** N uvicorn workers each holding a subscription would ingest every message N
  times, and an SSE client on worker A would never see an event ingested by worker B.
  Both failures are silent until the worker count goes above one.
- **Why not Redis:** Postgres `LISTEN/NOTIFY` is sufficient at this scale and the
  database is already there. Mirrors the `content-api` / `content-worker` split already
  running on the same host.
- **Details:** [design/production.md](design/production.md) → *The single-subscriber rule*.

---

## 2026-09-08 — Requirements & targets written down (PROPOSED)

- **Decision:** `spec/prd.md` gained a *Requirements & targets* table — capacity, timing,
  retention, KPI thresholds — marked **PROPOSED** pending review. Downstream docs resolve
  against it instead of each deciding for themselves.
- **Why:** the doc set specified mechanisms with no numbers. `device-protocol.md` had
  "heartbeat default interval" as an open item — a spec decision leaking into a protocol
  doc. Metrics existed with no thresholds, so they could not fail.
- **Consequence:** exposed three missing tasks, now in R1 — a signed-URL + range download
  endpoint (Flow 2 promised resumable download with nothing to serve it), writing
  `deploy_events` from R1 (or R5 arrives with two KPIs and no history), and enforcing
  retention rather than only ingesting.

---

## 2026-09-08 — Enrolment over HTTPS, not MQTT; tokens are single-use

- **Decision:** A device exchanges its enrolment token at `POST /v1/enrol` over HTTPS for
  a per-device broker credential, then connects to the broker already credentialed. The
  token burns on use.
- **Why:** the original flow had the device present its token *to the broker*, which
  would force Mosquitto to authenticate clients it has never heard of against a
  group-scoped token — a custom auth plugin bridging broker to control plane. Instead the
  broker only ever sees fully-credentialed clients and its authz collapses to two pattern
  ACLs. The agent already needs an HTTPS client for artifact download, so this is free.
- **Single-use:** a group-scoped token surviving in flash would let anyone with physical
  access to one board enrol arbitrary devices, and on a public-facing broker there is no
  LAN perimeter to hide behind.
- **Details:** [spec/device-protocol.md](spec/device-protocol.md).

---

## 2026-09-08 — The device owns the reboot, and the rollback

- **Decision:** Two authority rules, both device-side. The device decides *when* to apply
  and may sit in `awaiting_safe_window` indefinitely; and the confirm timer is armed on
  the device before the reboot, so rollback is the device's decision, never a server
  command.
- **Why:** a drone rebooting mid-flight falls out of the sky, and a board that cannot
  reach the broker is exactly the board that must roll back — it will never receive a
  command telling it to. The server observes and records; it never forces a reboot.
- **Details:** `design/architecture.md` principle 5; `spec/prd.md` → *Scope*.

---

## 2026-09-08 — MQTT is the control plane only

- **Decision:** MQTT carries identity, presence, commands, status and telemetry. **HTTPS
  carries artifact bytes.** MQTT never carries payload.
- **Why:** MQTT has no range requests, so any drop restarts the whole transfer, and the
  broker would buffer the image per subscriber on a group deploy. The `stage` command
  carries a short-lived signed artifact URL instead.
- **Supersedes:** the initial spec's implication that MQTT was the delivery transport.

---

## 2026-09-08 — Flash-time immutables frozen at R0

- **Decision:** The full A/B partition table (`nvs`/`otadata`/`phy_init`/`ota_0`/`ota_1`,
  4 M flash minimum), `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, and the eFuse posture
  (Secure Boot v2 **off**, anti-rollback **off**, flash encryption **off**) ship from the
  very first flash at R0 — even though nothing writes the second slot until R2.
- **Why:** an OTA image writes *into* a partition; it cannot rewrite the partition table,
  and the bootloader is the one update with no rollback path. Wrong at R0 means
  physically retrieving every deployed board — the exact intervention this product exists
  to remove.
- **Consequence:** R5 ships **app-level signature verification**, not Secure Boot v2.
  Secure Boot v2 burns a key digest to eFuse and needs a re-signed bootloader, so it can
  never be enabled on an already-deployed board — it is post-v1 and new-devices-only.
  The two are not interchangeable.
- **Details:** [design/architecture.md](design/architecture.md) → *Flash-time immutables*.

---

## 2026-09-08 — Version structure: V1 safe OTA, V2 build, V3 swarm

- **Decision:** V1 = R0–R5, ~5 heterogeneous boards, safe OTA. V2 = R6–R10, VCS +
  server-side compile + simulation. V3 = robotic swarm (gateway + drones).
- **Moved out of v1:** groups & bulk deploy (five different builds have nothing to
  bulk-deploy) → V3; the advisory simulation gate → V2/R8.
- **Rejected as v1 scope:** a 100+ node swarm. Aspirational, not v1. What v1 *does* pay
  for is schema and shape only — `link_type` / `power_class` / `parent_device_id`,
  derived presence, device-owned reboots — never speculative machinery.
- **Rule learned:** take what is a schema or config decision; defer what is machinery.

---

## 2026-09-08 — v1 is a hosted instance; IP-bearing links only

- **Decision:** v1 ships as a single hosted, single-tenant instance on a public domain
  with Let's Encrypt TLS. Turnkey self-hosting is V2. The device contract requires an
  IP-bearing link and TLS, nothing more — never "Wi-Fi".
- **Why hosted:** onboarding friction is the make-or-break risk, and the hard part of
  self-hosting is TLS without public DNS (a local CA the browser *and* the device trust).
  Deferred, not solved — it returns in full at V2.
- **Consequence:** the broker and artifact endpoint are on the public internet from R0,
  so per-device broker credentials, topic ACLs and single-use enrolment tokens are R0
  requirements, not post-v1 hardening. There is no LAN perimeter to fall back on.
- **Gateway-mediated non-IP radios** (Zigbee/BLE/LoRa) cannot reach a hosted server at
  all and need an on-site gateway — a second product, deferred to V2+.
