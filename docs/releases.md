# Fleetforge — Release Plan

*Companion to [prd.md](../spec/prd.md) / [design/architecture.md](../design/architecture.md) / [flows.md](../spec/flows.md). Every release is a functioning app that does one more thing end-to-end. Ordered to retire the biggest risk (bricking) first. Each step is deliberately small.*

## V1 — safe OTA on a handful of boards (R0–R6)

*Target: ~5 heterogeneous boards, one per project (prd.md → Scope). No build pipeline, no simulation — a bad build is caught by per-device auto-rollback alone.*

### R0 — Enroll a board (UI + recognition + flash + connect)
The first functioning app: *I can register a board and see it online.* No code-deploy yet.
- **Add (internal milestones, buildable in order):**
  1. Minimal server: MQTT broker + device registry (persist id/platform/version/last-seen) + enrollment-token generation.
  2. Agent (connect-only, **no OTA yet**) shipping the **final flash-time-immutable layer**: A/B partition table, rollback-enabled bootloader, eFuse posture (see design/architecture.md → *Flash-time immutables*). Connects, announces identity and heartbeats per [device-protocol.md](../spec/device-protocol.md).
  3. Dashboard: "Enroll a board" page + a live list of registered devices.
  4. In-dashboard Web Serial flasher (`esptool-js`): port select → chip detect + board-confirm shortlist → flash prebuilt agent + baked config (broker/Wi-Fi/token).
  5. Auto-enroll: agent presents token on first connect → appears in the dashboard by itself.
- **You can now:** plug in a board, flash & register it from the browser, and watch it come online — no toolchain, no CLI.
- **Risk retired:** onboarding + board recognition + device↔server connection.
- **Architecture note:** build the dashboard against a **public API + event stream from R0** (API-first, headless core) so Home Assistant / CLI / MCP become cheap later clients. See design/architecture.md → *Frontend design*.

### R1 — Upload new code (OTA deploy)
- **Add:** artifact upload (`POST /artifact`); agent gains `esp_https_ota` + an "update" command; per-device Deploy button in the dashboard; version reported back after reboot.
- **You can now:** push new firmware to a registered board from the dashboard and watch its version change.
- **Risk retired:** the OTA transport works end-to-end.
- ⚠️ *Not yet safe* — a broken build stays broken until R2. Run a throwaway OTA+rollback spike **in parallel** to de-risk R2 early.

### R2 — Safe deploy: verify + auto-rollback ⭐
- **Add:** checksum verify before apply; A/B slot; **device-side** confirm timer → `esp_ota_mark_app_valid_cancel_rollback()` on success, else self-rollback. Dashboard shows `good` vs `rolled-back`.
- **Rollback authority is the device's, not the server's.** A device that cannot reach the broker is precisely the device that must roll back, and it will never receive a command telling it to. The server observes and reports; it never triggers a rollback. (It follows that the confirm timeout is armed on the device before the reboot, not measured server-side.)
- **You can now:** push a *deliberately broken* build → the board auto-recovers to the previous one.
- **Risk retired:** bricking — **the single most important milestone** (just later in this ordering).

### R3 — Thin OTA library + first CUJ
- **Add:** the four-verb contract as something a maker embeds in **their own** firmware — an ESP-IDF component extracted from the agent's `ff_*` modules, an Arduino library wrapping the same C, a worked example, and the project's first written CUJ ("I have a sketch and a DevKit on the desk").
- **Sequenced after R2 on purpose.** A library is a multiplier on however safe deploy currently is; handing out the contract before auto-rollback exists spreads the unsafe path into custom firmware on boards nobody can reach. See `design/decisions/ota-library-ships-after-safe-deploy.md`.
- **Opens with a spike, not with extraction.** Where a library user's config lives is undecided — the agent uses the `ff_cfg` flash partition, which an Arduino IDE build does not have. `spec/open-questions.md` carries the question; the spike answers it before the rest is estimated.
- **You can now:** OTA *your own* project — the plant waterer, the frame, the coop door — not the demo agent.
- **Risk retired:** the on-ramp. Until this exists, every release is a feature of firmware the user did not write.

### R4 — Health & telemetry view
- **Add:** agent reports boot-success, uptime, and user-defined metrics; dashboard shows live status + last-seen + metrics.
- **You can now:** watch fleet health live.

### R5 — Custom self-test confirm
- **Add:** optional self-test entrypoint run at confirm time; deploy rolls back if it fails.
- **You can now:** catch "boots but app logic broken," not just boot-loops.
- *Lands before the simulation gate that reuses it — so the self-test is written once, proven on-device, then adopted by sim in V2 with no code change.*

### R6 — Signed OTA + resumable hardening → **v1 complete**
- **Add:** **app-level firmware signing** (agent verifies a signature over the artifact before apply — *not* Secure Boot v2, which needs eFuse burns at flash time and is new-devices-only; see design/architecture.md), resumable range downloads, retry/backoff, both KPIs surfaced.
- **You can now:** run it in earnest — production-grade safety + security.

> **v1's defense in depth is one layer deep.** Simulate (V2) → canary (later) → auto-rollback (R2): only the last exists in v1. That is a deliberate bet that per-device auto-rollback is sufficient at 5 boards, where a bad build costs one board a reboot rather than a fleet.

---

## V2 — source to artifact: build & pre-flight verification (R7–R11)

*Theme: stop hand-carrying `.bin` files. Connect a repo, and let the server ingest, build, and verify before anything reaches a board.*

### R7 — Artifact API + provenance
- **Add:** upload API accepting `.bin` + provenance (repo/commit/tag/build URL); dashboard shows provenance.
- **You can now:** upload versions from any script, with commit-level traceability.

### R8 — Push ingestion (GitHub Action + templates)
- **Add:** reusable GitHub Action + GitLab/Gitea templates that build & push on tag.
- **You can now:** `git tag` → a deployable, commit-linked version appears automatically.

### R9 — Advisory simulation gate
- **Add:** `pytest-embedded` + Espressif QEMU boots the artifact and runs the **R5 self-test** *before* deploy; warn + override in the UI.
- **You can now:** catch bad builds before any device is touched.
- *Moved out of v1: at 5 boards, auto-rollback already makes a bad build cheap. Sim earns its keep once builds arrive automatically from CI (R8) and nobody is watching each one.*

### R10 — Build from source (server-side compile)
- **Add:** the server clones a repo at a ref and builds the artifact itself — pinned ESP-IDF toolchain containers per target (esp32/S3/C3/C6), queued builds, streamed build logs, build cache.
- **You can now:** point Fleetforge at a repo and get a deployable artifact — no local toolchain, no CI required.
- ⚠️ **This reverses a v1 non-goal** ("the server never builds"). Two things follow: artifacts stay *opaque to the core* — the builder is a producer feeding the same upload API, not a special path — and **building a repo is arbitrary code execution on the server**, so the build step must be sandboxed and resource-capped from its first commit.
- **Toolchain:** native `idf.py` projects first; PlatformIO second. Pin the IDF version per project or builds stop being reproducible.

### R11 — Deploy policy → **v2 complete**
- **Add:** per-target policy — manual (appears as deployable, human clicks) vs auto-deploy on a matching tag.
- **You can now:** continuously deploy your dev board while everything else stays manual.
- *Per-device granularity here; **group** granularity needs group deploy from V3.*
- ⚠️ *Sequencing:* auto-deploy is only as safe as its rollback. Per-device auto-rollback (R2) makes it survivable; canary makes it comfortable.

---

## V3 — robotic swarm: one ground vehicle (gateway) + many flying drones

*The first genuinely homogeneous fleet, and the first hierarchy: a gateway acting as edge relay for many drones, groups & bulk deploy, airtime-aware scheduling, delta updates. Ordering and tasks in [features/groups-deploy.md](features/groups-deploy.md).*

## Beyond
See [roadmap.md](roadmap.md) → *Beyond V3*.

## Notes
- **v1 is R0–R6.** V2 (build pipeline) and V3 (swarm) are independent of each other; V2's R11 group-granularity policy is the one dependency running from V2 into V3.
- **R2 is the whole gamble** in this ordering (verify + auto-rollback). De-risk it with a throwaway spike *during R0–R1* — prove auto-rollback saves a bad build on real flaky Wi-Fi before you rely on it.
- **R0 ships R2's prerequisites.** The A/B partition table and rollback-enabled bootloader cannot be added by OTA later, so they are flashed at R0 even though nothing uses them until R2.
- Each release is shippable and demoable on its own; nothing here requires a later step to be useful.
