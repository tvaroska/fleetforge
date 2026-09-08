# Fleetforge — Release Plan

*Companion to [SPEC.md](./SPEC.md) / [DESIGN.md](./DESIGN.md) / [FLOWS.md](./FLOWS.md). Every release is a functioning app that does one more thing end-to-end. Ordered to retire the biggest risk (bricking) first. Each step is deliberately small.*

## Path to v1

### R0 — Enroll a board (UI + recognition + flash + connect)
The first functioning app: *I can register a board and see it online.* No code-deploy yet.
- **Add (internal milestones, buildable in order):**
  1. Minimal server: MQTT broker + device registry (persist id/platform/version/last-seen) + enrollment-token generation.
  2. Agent (connect-only, **no OTA yet**): Wi-Fi + MQTT connect, announce identity (eFuse-MAC `device_id` + platform + capabilities) + heartbeat.
  3. Dashboard: "Enroll a board" page + a live list of registered devices.
  4. In-dashboard Web Serial flasher (`esptool-js`): port select → chip detect + board-confirm shortlist → flash prebuilt agent + baked config (broker/Wi-Fi/token).
  5. Auto-enroll: agent presents token on first connect → appears in the dashboard by itself.
- **You can now:** plug in a board, flash & register it from the browser, and watch it come online — no toolchain, no CLI.
- **Risk retired:** onboarding + board recognition + device↔server connection.
- **Architecture note:** build the dashboard against a **public API + event stream from R0** (API-first, headless core) so Home Assistant / CLI / MCP become cheap later clients. See DESIGN.md → *Frontend design*.

### R1 — Upload new code (OTA deploy)
- **Add:** artifact upload (`POST /artifact`); agent gains `esp_https_ota` + an "update" command; per-device Deploy button in the dashboard; version reported back after reboot.
- **You can now:** push new firmware to a registered board from the dashboard and watch its version change.
- **Risk retired:** the OTA transport works end-to-end.
- ⚠️ *Not yet safe* — a broken build stays broken until R2. Run a throwaway OTA+rollback spike **in parallel** to de-risk R2 early.

### R2 — Safe deploy: verify + auto-rollback ⭐
- **Add:** checksum verify before apply; A/B slot; confirm-on-reconnect within timeout → mark valid, else `esp_ota` rollback. Dashboard shows `good` vs `rolled-back`.
- **You can now:** push a *deliberately broken* build → the board auto-recovers to the previous one.
- **Risk retired:** bricking — **the single most important milestone** (just later in this ordering).

### R3 — Groups & bulk deploy
- **Add:** tags/groups; deploy to a group; per-device progress view.
- **You can now:** update many boards at once.

### R4 — Health & telemetry view
- **Add:** agent reports boot-success, uptime, and user-defined metrics; dashboard shows live status + last-seen + metrics.
- **You can now:** watch fleet health live.

### R5 — Custom self-test confirm
- **Add:** optional self-test entrypoint run at confirm time; deploy rolls back if it fails.
- **You can now:** catch "boots but app logic broken," not just boot-loops.

### R6 — Advisory simulation gate
- **Add:** `pytest-embedded` + Espressif QEMU boots the artifact + runs the self-test *before* deploy; warn + override in UI.
- **You can now:** catch bad builds before any device is touched.

### R7 — Signed OTA + resumable hardening → **v1 complete**
- **Add:** firmware signing (secure boot / signature verify), resumable downloads, retry/backoff, both KPIs surfaced.
- **You can now:** run it in earnest — production-grade safety + security.

## Path to v2 (VCS integration)

### R8 — Artifact API + provenance
- **Add:** upload API accepting `.bin` + provenance (repo/commit/tag/build URL); dashboard shows provenance.
- **You can now:** upload versions from any script, with commit-level traceability.

### R9 — GitHub Action (push ingestion)
- **Add:** reusable GitHub Action + GitLab/Gitea templates that build & push on tag.
- **You can now:** `git tag` → a deployable, commit-linked version appears automatically.

### R10 — Per-group deploy policy → **v2 complete**
- **Add:** per-group policy (manual vs auto-deploy-on-matching-tag).
- **You can now:** continuous-deploy a dev fleet while prod stays manual.

## Beyond
Canary/staged rollout (unlocks *safe* auto-deploy) · Raspberry Pi adapter · SoftAP provisioning · per-device mTLS.

**Interaction surfaces** (enabled by the API-first core): **CLI** (batch flash/deploy/CI) · **Home Assistant** (add-on + MQTT Discovery `update` entities) · **Claude Code / MCP** (read-rich, guarded writes) · Grafana/Prometheus export.

## Notes
- **R2 is the whole gamble** in this ordering (verify + auto-rollback). De-risk it with a throwaway spike *during R0–R1* — prove auto-rollback saves a bad build on real flaky Wi-Fi before you rely on it.
- Each release is shippable and demoable on its own; nothing here requires a later step to be useful.
