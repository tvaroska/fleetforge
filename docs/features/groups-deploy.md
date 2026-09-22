# Groups, Bulk Deploy & the Swarm Gateway

**Status:** Planned — **V3, not v1**
**Priority:** P1
**Target:** V3 — robotic swarm (was R4)
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md) — R2
**Scope:** [prd.md](../../spec/prd.md) → *Scope* · [releases.md](../releases.md) → *V3 — robotic swarm*

## Why this left v1

v1 targets **~5 heterogeneous boards** — an e-paper frame, a vehicle, a drone, a morse
blinker: five projects, five different builds. There is nothing to bulk-deploy, and
per-device capability checking matters far more than group targeting.

**Groups do not disappear from v1** — enrollment tokens are group-scoped (R0-BE-2) and
the dashboard organises by tag. It is the bulk-deploy *release* that moves here, to the
first genuinely homogeneous fleet: many identical drones.

## Phase 1: Groups & bulk deploy

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| SW-DB-1 | Tags/groups schema + device↔group membership | P0 | 0.5d |
| SW-BE-1 | Assign devices to groups/tags (API) | P0 | 1d |
| SW-BE-2 | Deploy to a group (fan out the per-device transaction) | P0 | 1.5d |
| SW-FE-1 | Group management UI + group deploy | P0 | 1d |
| SW-FE-2 | Per-device progress view for a bulk deploy | P0 | 1.5d |
| SW-TEST-1 | E2E: deploy to a group → all members update, progress visible | P0 | 1d |

**Done when:** you can update many boards at once and watch per-device progress.

## Phase 2: Gateway & hierarchy — ground vehicle + drones

One ground vehicle acts as **gateway** for a large number of flying drones. It is a
*mobile, in-fleet* node, not static on-site infrastructure: simultaneously a managed
device in the registry and the parent of its drones.

- **Gateway = edge relay**, not a radio protocol translator: local broker + artifact
  cache + upstream sync. Pull an artifact once over the uplink, serve it N times
  locally — which also collapses the airtime cost of a swarm update.
- **Disconnected operation** is the defining requirement: the vehicle will be out of
  internet range in the field.
- **Hierarchy:** `parent_device_id` (reserved in the R0 schema). The server tracks each
  drone individually but reaches it via its parent, and never assumes it holds an MQTT
  session per device.
- **Link:** if the vehicle runs a Wi-Fi AP (or ESP-NOW), the drones stay IP-bearing and
  no protocol bridging is needed at all. Zigbee/BLE/LoRa bridging is a separate,
  later question.

## Phase 3: Scale hardening

- **Airtime-aware scheduling** — concurrency limits so a fleet deploy does not saturate
  the local link.
- **Delta updates** — 5–50 KB diffs against ~1.5 MB images, applied read-active-slot →
  write-inactive-slot, so the A/B layout frozen at R0 is a prerequisite.
- **Safe-window deferral at scale** — drones must not apply mid-flight. The
  device-owned-reboot rule (design/architecture.md principle 5) is load-bearing here.

## Post-v1

Staged / canary rollout — deploy to a canary subset, gate the rest on canary health.
Unlocks *safe* auto-deploy (see vcs-integration.md, R11).
