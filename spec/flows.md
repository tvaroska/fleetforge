# Fleetforge — Core User Flows

*Companion to [prd.md](prd.md), [design/architecture.md](../design/architecture.md) and [device-protocol.md](device-protocol.md). The two tasks that define the product from the user's chair.*

## Flow 1 — Enroll a board into the registry

```
1. Dashboard → "Enroll a board" → generate scoped, revocable ENROLLMENT TOKEN
2. Plug board into USB → click "Detect" (Web Serial; 1 click to grant port)
3. esptool-js auto-detects: chip family + revision + flash size + PSRAM + MAC
   → confirm board from a narrowed shortlist (always incl. "enter manually")
4. Dashboard flashes the matching prebuilt agent + baked config:
   broker URL + Wi-Fi creds + enrollment token          [USB config flash]
5. Board boots → POSTs /v1/enroll over HTTPS: token + identity
   (device_id from eFuse MAC, platform_type, capabilities, link/power
    class, partition layout, slot size, fw version)
6. Token valid → AUTO-ENROLL: registry entry created, per-device broker
   credential issued, TOKEN BURNED (single-use)
   → agent stores credential in NVS and connects to the broker
7. Dashboard: user names it, assigns a group/tag
→ Managed, online device
```

**Decisions:**
- **Detection/flashing = in-dashboard Web Serial** (`esptool-js`, the ESP Web Tools stack). Plug in → one click to grant the port → chip auto-detected → flash. Zero install.
  - *Caveats:* Chrome/Edge only; needs `localhost`/HTTPS — **satisfied in v1 by the
    hosted public domain** (Let's Encrypt), and by `localhost` for the dev loop; one mandatory click (browsers forbid silent port enumeration); no batch enrollment in v1 — **CLI flasher for batch/CI is post-v1.**
- **ID granularity = chip-level auto + confirm board.** esptool reliably identifies the *silicon*; the exact dev board is a **heuristic shortlist** (chip + flash + PSRAM matched to a board DB) the user confirms — with "enter manually" always available. The running agent self-reports authoritative `platform_type` + capabilities anyway (step 5), so detection only needs to pick the right binary + seed identity.
- **device_id = eFuse MAC** (stable, factory-unique) — satisfies the identity contract.
- **Provisioning = USB config flash (v1).** Creds baked in at flash time. *SoftAP captive portal is post-v1* — until then, a Wi-Fi change means re-flash (accepted trade-off for a lean v1).
- **Trust = auto-enroll via a single-use token, exchanged over HTTPS** — not over MQTT, so the broker never has to authenticate a client it has never heard of. Zero-friction and batch-friendly. Rationale, threat model and the exact exchange: [device-protocol.md](device-protocol.md) → *Enrolment happens over HTTPS*. Token lifetime and posture: [prd.md](prd.md) → *Security & data posture*.

## Flow 2 — Put code onto a registered board

```
1. Upload artifact (.bin) → declare version + platform_type
2. Server capability-checks: reject on chip / partition-size mismatch
3. SIMULATE (advisory, V2): boot artifact + run self-test → pass / warn+override
4. Select target: this device, or a group/tag
5. Deploy → device pulls (resumable) → verify checksum+sig →
   write OTA1 slot → reboot
6. CONFIRM (timeout): new fw reconnects to broker [+ optional self-test]
   → mark good, else AUTO-ROLLBACK to OTA0
7. Dashboard: per-device progress + final delivery-success / fleet-safety state
```

**Decisions:**
- **Artifact source = user's own toolchain (v1).** They build the `.bin` (idf.py / PlatformIO / Arduino); the server never builds in v1. *V2 adds a server-side compiler as one more producer — see [build-pipeline.md](../docs/features/build-pipeline.md).* Artifacts are opaque + versioned.
- **Self-test = default + optional custom.** Default self-test is **"boots and reconnects to the broker"** (catches boot-loops, zero user effort). Users may add a **custom self-test baked into firmware** — one entrypoint the agent calls after boot and the simulator calls as its gate: *write once, run in sim and on device, no drift.*
- **Targeting = device or group/tag** (v1). Staged/canary rollout is post-v1.

## Flow 2 (V2) — Deploy from version control
VCS integration and the server-side compiler are **automated artifact producers** feeding the same pipeline; steps 2–7 above are unchanged.

```
1. git tag / release in the user's repo
2. CI builds the .bin and PUSHes it to Fleetforge's upload API,
   with PROVENANCE: repo + commit SHA + tag + build URL     [push model]
   (or Fleetforge builds it itself from the repo ref — R9)
3. Fleetforge registers it as a deployable version
4. Per-group DEPLOY POLICY decides:
     • manual   → appears as deployable, human clicks deploy
     • auto     → deploys to that group on a matching tag
   → then the normal capability-check / sim / pull / confirm / rollback runs
```

**Decisions:**
- **Ingestion = push first.** User's CI POSTs the artifact (ship a ready **GitHub Action** + templates for GitLab/Gitea). Provider-agnostic, holds no repo secrets, air-gap-friendly. *Pull adapters (Fleetforge watches releases) come later.*
- **Provider scope = provider-agnostic API.** One generic upload endpoint + provenance schema; GitHub Action provided, but self-hosted GitLab/Gitea/Forgejo work with a few lines. Matches the self-host ethos.
- **Deploy policy = configurable per group.** Dev fleet can auto-deploy on tag; prod fleet stays manual.
  - *Sequencing:* auto-deploy is only as safe as its rollback. Per-device auto-rollback makes it *survivable* in early V2; **enable auto-deploy-per-group with confidence only once canary/staged rollout lands.**
- **Payoff = traceability.** Every device's firmware links to a commit: "what's running on device X?" and "roll back to tag v1.3" become first-class.

## Where the pieces line up
- The **self-test** appears in Flow 2 step 3 (sim gate) and step 6 (device confirm) — the same code, two enforcement points.
- **Identity** announced in Flow 1 step 5 is the `platform_type` + capabilities the server matches against in Flow 2 step 2.
