# Fleetforge for the swarm builder

**Date:** 2026-09-21
**Status:** product review and recommendation — not a spec, not a plan.
**Audience:** someone building mixed flying + ground drone swarms that have to
coordinate in the field.
**Companion:** [`docs/HOBBYIST.md`](HOBBYIST.md) — same reading of the product,
different persona, different top 3.

This is a reading of the current aim, documentation and feature set against one
question: *what would actually let a ground vehicle and a flock of drones take
new firmware into a field with no internet, apply it without falling out of the
sky, and still talk to each other while they work?* Requirements stay in
`spec/`; live tasks stay in `TODO.md`. Landing any of the recommendations below
is a `/new-feature` (or a release), not an edit of this file.

---

## 0. The boundary, said first

**Fleetforge manages firmware versions, not the swarm.** That sentence is in
[`spec/prd.md`](../spec/prd.md) → *Explicitly out of scope*, and it is the
load-bearing one for this persona.

It will not:

- assign tasks, hold formation, or run a mission
- carry MAVLink, ROS 2, DDS, Zenoh, or ESP-NOW
- let one drone publish to another on the Fleetforge broker
- push application config, waypoints, or “fly here”
- be a ground-control station

The pattern ACLs on the broker are why. MQTT username equals `device_id`, and
the only rules are:

```
pattern write  ff/v1/d/%u/up/#
pattern read   ff/v1/d/%u/dn/#
```

A drone can impersonate only itself. It cannot address a sibling. A ground
vehicle cannot command a child *through Fleetforge topics* except by becoming
the V3 gateway that *relays* those topics. There is no inter-device bus.

That is not a hole in the OTA product. It is the threat model that keeps a
captured drone from being a fleet-wide credential. Coordination belongs on a
radio and a protocol the swarm builder already has (MAVLink, ROS 2, a custom
mesh). Fleetforge’s job is to make firmware a solved *substrate* for that
stack: every node individually tracked, updates that wait for the ground, a
parent that keeps working when the internet does not.

The rest of this file is about doing *that* job well enough that the swarm
builder does not have to invent a second OTA.

---

## 1. Aim — what this actually is, for this persona

**One sentence:** a control plane that can follow a mixed air/ground flock into
a field with no uplink, push a new image without rebooting anyone who is flying
or rolling, and bring every node back to a known version when the image is bad.

The v1 problem statement in the PRD is written for makers and attic sensors.
The v1 *targets* are already a swarm sketch:

| Board | Character | What it exercises |
|---|---|---|
| Simple vehicle | safety-critical, in motion | must not reboot while moving |
| Simple drone | safety-critical, airborne | apply/confirm/rollback only on the ground |

`awaiting_safe_window` is unbounded by design — “a parked drone may wait days.”
The device owns the reboot; a rollback reboot is still a reboot. That state
machine is cheap to design in and expensive to retrofit, and it is the reason
this persona can use v1 at all.

The swarm itself is named as **V3** (roadmap, releases, `groups-deploy.md`):

> One ground vehicle acting as gateway, plus a large number of flying drones —
> a *mobile, in-fleet* node that is simultaneously a managed device and the
> parent of its drones.

Two properties shape it more than radio choice:

1. **Hierarchy** — the server tracks each drone individually but reaches it
   only via its parent. The core must never assume it holds an MQTT session
   per device. `parent_device_id` is already reserved in `up/announce`.
2. **Disconnected operation** — the vehicle will be out of internet range in
   the field. The gateway is an edge relay: local broker + artifact cache +
   upstream sync. Pull the image once on the uplink, serve it N times locally.

The topic namespace is deliberately **flat**. A drone behind a gateway still
owns `ff/v1/d/{drone_id}/…`. Nesting children under parents would break the
moment a drone is reassigned. The gateway republishes; the core stays unaware,
the same trick as the FPGA companion CPU.

That is a real swarm-OTA architecture, paid for in v1 as empty fields and
rules. Almost none of it is implemented. v1 capacity is **25 devices, 5
concurrent deploys, hosted on a public domain.** A field flock of 40 behind a
vehicle with no internet is a different product that happens to share a
protocol.

**Who it is for (stated):** the same solo maker as [`HOBBYIST.md`](HOBBYIST.md),
five heterogeneous boards, not a public product until V3.

**Who it is for (if V3 is serious):** an operator of a mixed air/ground flock
who will write their own flight and ground firmware, who needs OTA to be as
boring as fuel, and who will not accept a cloud round-trip as a condition of
taking off.

---

## 2. Documentation — the swarm is specified as a silhouette

### What is unusually good, for this persona

The architecture has already refused the mistakes that make swarm OTA a rewrite:

- **Device-owned apply** is in the v1 protocol, not a V3 patch. `apply: "auto"`
  vs `apply: "on_command"` is already on `dn/cmd`. Stage in the air, apply on
  the ground is a *payload flag*, not a new verb.
- **Presence is derived**, never a socket. Sleepy and parented nodes can go
  “offline” with no event. A drone behind a vehicle that drove into a valley
  is the same shape as the e-paper frame.
- **MQTT never carries payload.** A group deploy does not buffer 1.5 MB × N on
  the broker. HTTPS + signed URL + Range is the only way a 40-node update is
  tractable, and it is already the v1 data plane.
- **Flat namespace + reserved `parent_device_id`.** Hierarchy is an ACL grant
  and a relay, not a topic redesign.
- **Opaque artifacts.** The ground vehicle can be a Pi image and the drone an
  ESP32 `.bin` without the core knowing. That is the mixed-fleet contract.
- **CBOR called as a drop-in** for constrained V3 links. JSON in v1, same
  data model.
- **Link is IP-bearing, not Wi-Fi.** Thread joins free because it is
  IPv6-native. ESP-NOW / Wi-Fi AP on the vehicle keeps drones IP-bearing and
  needs no protocol bridge. Zigbee/BLE/LoRa *do* need a bridge — deferred as
  a second product, correctly.

[`design/architecture.md`](../design/architecture.md),
[`spec/device-protocol.md`](../spec/device-protocol.md) → *Hierarchy*, and
[`docs/features/groups-deploy.md`](features/groups-deploy.md) together are a
coherent V3 sketch. Flash-time immutables (A/B layout, rollback-enabled
bootloader) are already on the metal, so delta updates and R2 rollback do not
require a recall.

### What is weak or contradictory

**V2 vs V3.** [`spec/prd.md`](../spec/prd.md) has a section titled
“### V2 — robotic swarm” that describes the ground-vehicle gateway. The same
file’s roadmap, plus `roadmap.md`, `releases.md` and `groups-deploy.md`, put
that work in **V3**. V2 is source-to-artifact. A swarm builder reading the PRD
will plan against the wrong release.

**`groups-deploy.md` is a silhouette.** Phase 1 is a task table for tags and
bulk deploy. Phase 2 is four bullets (edge relay, disconnected op, hierarchy,
Wi-Fi AP / ESP-NOW). Phase 3 is three more bullets (airtime, delta,
safe-window at scale). There is no protocol for the relay, no store-and-forward
spec, no “what does the vehicle do with a `stage` when it has no uplink”, no
clock story (SNTP-before-TLS fails in a valley), no captured-gateway threat
model, no capacity numbers above 25. Compared to enrollment or OTA, this is
not a feature file. It is a placeholder with the right nouns.

**v1 topology fights the field.** The supported v1 deploy is a hosted public
domain. Devices need Let’s Encrypt and a public broker. The swarm’s defining
requirement is that the vehicle is *out of range*. Hosted-first makes R0–R6
demoable; it does not make V3 a configuration change. The gateway is a second
product that happens to speak the same four verbs — the PRD already says this
about non-IP radios, and it is also true of the IP gateway.

**Capacity is a v1 number pretending to be a product number.** 25 devices, 5
concurrent deploys, 1.9 MB images, 5-minute healthy-link deploy. A 40-drone
update of 1.5 MB on a vehicle AP is not 5 minutes and must not be 40
concurrent downloads. Airtime-aware scheduling is named, not specified. The
PRD table has no V3 column.

**Coordination is unnamed.** Out-of-scope is “application-level provisioning”
and “a data pipeline for telemetry.” A swarm builder will still ask: *where
do my drones talk?* The docs never say “use MAVLink / ROS 2 / your mesh; here
is how the gateway stays out of the way; here is a sibling broker you may
run.” Silence reads as either “we will do it later” or “you are on your own,”
and both are expensive.

**The clock in a valley.** `spec/device-protocol.md` requires SNTP before the
first TLS handshake. A vehicle that has been off for a week, boots under
canopy, and has no NTP source cannot enroll, cannot download, cannot even
connect. v1 can ignore this (public SNTP, always-on internet). V3 cannot.
Nothing in the swarm docs names a local time source, an HTTP-date fallback,
or a “trust the gateway’s clock” rule.

**Captured node vs captured parent.** v1 security is “a stolen credential is
worth one device.” A gateway with extra ACL entries for every child is worth
the flock. Physical access to a recovered drone exposes NVS (flash encryption
off). A lost drone is expected in this persona. A captured ground vehicle is
the control plane. Neither threat is written down.

**No CUJ for “the flock is in a field.”** Unaided onboarding is a desk CUJ.
There is no “stage 40, 3 still airborne, 1 canary on the ground, vehicle has
had no uplink for six hours.” That is the journey this persona will judge
the product by.

---

## 3. Feature set — as built, as planned, as missing

### What is actually there (and what it is worth in a flock)

| Capability | State | Swarm value |
|---|---|---|
| Four-verb transaction, `awaiting_safe_window`, `apply: auto \| on_command` | Spec + R1 orchestration | The apply primitive. Not productized as a fleet action. |
| Per-device deploy, signed URL, Range | R1, QEMU-proven | One drone. Not N, not airtime-aware. |
| `parent_device_id` on announce | Reserved, unused | Hierarchy is a column, not a behaviour. |
| Flat topic namespace, per-device ACLs | Live | Correct; blocks inter-drone traffic on this broker. |
| A/B partition + rollback-enabled bootloader | Flashed at R0 | Prerequisite for R2 and for deltas. Unused until R2. |
| Enrollment, Web Serial, one board at a time | Live | Unusable for 40 airframes. |
| Hosted broker + GCS artifacts | Live | Unreachable in the field. |
| Device simulator | Live | Can fake N devices on a desk; no radio, no parent, no airtime. |

R0 is not closed on metal. R2 (auto-rollback) is not built. **Do not put this
on an airframe that cannot be walked to.** The hobbyist warning applies
harder: a bricked drone is a crash, not a trip to the attic.

### V3 as written (`groups-deploy.md`)

| Phase | Contents | Gap |
|---|---|---|
| 1. Groups & bulk deploy | Tags, fan-out, per-device progress UI | Necessary, not sufficient. Fan-out of the v1 transaction with concurrency 5 will saturate a vehicle AP. |
| 2. Gateway & hierarchy | Local broker, artifact cache, upstream sync, `parent_device_id` | The actual product for this persona. Unspecified. |
| 3. Scale hardening | Airtime limits, deltas 5–50 KB, safe-window at scale | Named because 1.5 MB × N on 802.15.4 is 10–30 min radio-on *per device*. |
| Post | Canary / staged rollout | Unlocks *safe* auto-deploy. At flock size this is not post-anything; it is how you do not lose the flock. |

### Explicitly out of scope — and this is where the swarm *lives*

From the PRD, the architecture, and the protocol:

- Mission coordination, formation, tasking
- Inter-device messages on the Fleetforge broker
- Application config through `dn/cfg`
- Raspberry Pi adapter (the likely ground vehicle)
- Self-hosting / TLS without public DNS (the likely field topology)
- CLI / batch enroll (the likely factory path)
- Canary (deferred past v1)
- Telemetry as a data plane (30 days of “a few user-defined metrics”)
- Thread, delta, airtime (Beyond / V3 phase 3)
- Gateway-mediated non-IP radios (second product)

Some of those refusals must stand (mission control would eat the product).
Some are sequencing that this persona cannot wait for V3 to finish before
using. The Pi adapter and the field gateway *are* the swarm; treating them
as later platforms treats the most important node as optional.

### Competitive position

There is no good “safe OTA for a mixed ESP32/Pi flock you take off-grid.”

| Stack | What it is | Why it is not this |
|---|---|---|
| MAVLink + QGC | Vehicle command and telemetry | Not firmware. Companion-computer updates are ad hoc. |
| ROS 2 / DDS / Zenoh | Onboard and inter-robot bus | Not an OTA. |
| PX4 / ArduPilot bootloader | Vehicle-side firmware install | One vehicle, USB or a vendor path, not a flock, not mixed platforms. |
| Balena / Mender | Linux OTA, often cloud | Strong on Pi, empty on ESP32, weak on “no internet, device owns reboot.” |
| Particle / Arduino Cloud | Vendor device cloud | Opposite of operator-owned; no hierarchy. |
| ESPHome | YAML build + HA | Per-device compile, no flock, no airframe. |
| Custom `esp_https_ota` | What everyone writes first | No inventory, no deferral, no parent, no rollback you can prove. |

Fleetforge’s wedge for this persona is **one contract across the Pi gateway
and the ESP32 airframes, with apply deferred to a safe window, with the
parent as a cache rather than a protocol translator.** Nobody else is selling
that. Nobody else has to: most swarm builders roll it themselves, badly,
after the first mid-air brick.

---

## 4. Top 3 features for the swarm builder

Ranked by *unlock*, not by roadmap order. “Unlock” = a mixed air/ground flock
can take a new image into a field with no uplink, apply it without losing
vehicles, and keep using *their* coordination stack while they do.

The embeddable OTA library from [`HOBBYIST.md`](HOBBYIST.md) is a
**prerequisite, not a top-3 item here.** You do not fly the stock agent. The
four verbs have to live inside the flight controller and the vehicle
computer. Without that extraction, everything below is a feature of a
firmware that cannot leave the ground. Treat R3 (library + CUJ) as a gate
on this persona the same way R2 is a gate on any unreachable board.

### 1. The field gateway — disconnected hierarchy, plus a sibling bus

**This is the swarm. V3 phase 2, specified as four bullets.**

One ground vehicle, in the flock, out of internet range:

- **Local broker** that drones already speak (the v1 protocol, unchanged).
- **Artifact cache** — pull once on the uplink (or from USB at camp), serve
  N times on the local AP. Signed URLs have to be re-minted against the
  vehicle’s origin; a GCS URL from the hosted server is unreachable in a
  valley. This is the same `S3_PUBLIC_ENDPOINT_URL` split that already
  exists in dev, aimed at a moving host.
- **Upstream sync** when the link returns: announce, status, deploy_events.
  The core already must not assume a live MQTT session per device; this is
  that rule doing its job.
- **Store-and-forward `stage`.** An operator at camp says “this version, this
  group.” The vehicle carries the bytes and the command. Drones that are
  airborne sit in `awaiting_safe_window`. Drones that are down apply. The
  server finds out later.

Two things the current sketch does not say, and this persona will hit on day
one:

**Clock.** SNTP-before-TLS cannot mean “public NTP.” The vehicle is the time
source. Drones trust the parent’s clock, or the gateway terminates TLS and
the last mile is a local CA, or HTTP-date on the cached artifact endpoint
is allowed to step a board that woke at epoch zero. Pick one in spec before
any field agent is flashed; this is a flash-time-immutable *behaviour* even
if it is not an eFuse.

**A sibling application bus, not a Fleetforge topic.** Do not open the
`ff/v1/d/%u/…` ACLs. Do not carry waypoints on `dn/cfg`. Do put, on the same
vehicle image, a **second listener** (or a second vhost on the same
Mosquitto) that the swarm builder owns: their topics, their ACLs, their
MAVLink-over-MQTT or ROS 2 bridge if they want one. Document it as *the*
coordination surface. Fleetforge stays out of the payload. The builder does
not run two computers in the rack because the OTA broker refused to share a
kernel with MAVLink.

A captured vehicle is then worth the flock on *both* planes. That is honest,
and it is the reason the vehicle is a first-class managed node with its own
rollback, not a piece of scenery. Per-child ACL grants for the *relay* stay
narrow; the app bus is the builder’s threat model.

Without this feature, V3 is “bulk deploy from a hosted dashboard,” which is
v1 with tags. With it, the product matches the sentence already in the PRD.

### 2. Coordinated apply — stage the flock, apply on the ground, never all at once

**R2 is necessary and not sufficient. A flock that auto-rollbacks one-by-one
can still all reboot together.**

The protocol already has the primitive: `apply: "on_command"` stages and
waits; `apply: "auto"` lets the device pick the window. What this persona
needs is that primitive as a *fleet action*, with policy:

1. **Canary first.** One airframe, on the ground, at the vehicle. Self-test
   (R5) plus “it armed, it hovered, it landed” as the custom confirm — that
   last part is the builder’s firmware, not Fleetforge. The rest of the
   group does not leave `staged` until the canary is `confirmed`.
   `groups-deploy.md` parks canary in “Post.” At flock size it is the
   difference between losing one drone and losing the sortie.
2. **Stage in the air, apply on the ground.** Upload and verify while the
   radio is idle-ish and the vehicle still has the cache hot. Do not apply
   until `power_class` / a builder-supplied “safe” bit says on_ground /
   parked. The server already must not time out `awaiting_safe_window`.
   The dashboard must not look like a hang when twenty nodes sit there
   (R1-fe-1 already learned this for one device).
3. **Never all at once.** Airtime-aware concurrency is V3 phase 3 as
   “don’t saturate the AP.” For this persona it is also **don’t take the
   whole flock through reboot together.** Even on the ground, stagger apply
   so a bad image plus a rollback bug loses a canary-sized slice, not N.
   Mixed versions during the roll are the builder’s compatibility problem;
   Fleetforge’s job is to *show the skew* (count per `fw_version` in the
   group) so they can refuse to arm a mixed flock if they want.
4. **Rollback is still device-owned, still on the ground.** A drone that
   applied, took off, and then fails confirm must not reboot in the air.
   The sleepy-device confirm bug in [`HOBBYIST.md`](HOBBYIST.md) has an
   airborne twin: confirm-timeout vs “I am flying.” The rule has to key
   off the same safe-window bit as apply. Cheap in spec now; a recall
   after R2.

R2 (checksum, A/B, device-armed confirm) is the floor. Canary + staggered
`on_command` + skew visibility is the flock-shaped product. Bulk-deploy
fan-out without those three is how you brick a swarm in one click.

### 3. Mixed platforms and cheap bytes — Pi vehicle, ESP32 airframes, deltas

**The most important node in the swarm is probably not an ESP32.**

The ground vehicle is a gateway, a broker, an artifact cache, a time source,
and usually a companion computer running Linux (Pi, Jetson). The airframes
are ESP32 (or similar) on a constrained radio. The thin waist already says
this is one product: opaque artifact, four verbs, `platform_type`. The
adapters do not exist.

Ship, in this order:

1. **Raspberry Pi adapter.** OS image or container as the artifact, A/B or
   an equivalent confirm, health = systemd/app, rollback = previous slot.
   Until this exists, the parent is an unmanaged box that happens to
   forward MQTT — the worst node to leave unmanaged, because it is the
   flock’s uplink, cache, clock and (if you took recommendation 1)
   coordination bus.
2. **Delta updates, indexed by version *pairs*.** [`design/artifacts.md`](../design/artifacts.md)
   already flags that this is quadratic and that the A/B layout frozen at
   R0 is the prerequisite (read active, write inactive). 5–50 KB instead of
   1.5 MB is not a convenience on a vehicle AP, and it is the difference
   between possible and not on Thread / 802.15.4. Airtime spent on OTA is
   airtime stolen from telemetry and command — this is a coordination
   feature pretending to be a compression feature.
3. **Airtime budget as a first-class deploy constraint.** Concurrent
   downloads, per-link, advertised by the gateway. v1’s “5 concurrent, 5
   minutes, healthy Wi-Fi” is a desk number. The vehicle should refuse to
   start N downloads that would knock MAVLink off the AP.

Thread stays a later radio. The IP-bearing contract plus `esp_netif` is the
right payment; do not pull 802.15.4 forward until deltas exist, or the first
Thread deploy is a 30-minute radio-on per airframe.

Heterogeneous artifacts also force the capability check to be real:
`partition_layout` + `ota_slot_size` + `platform_type` already ride on
`up/announce`. A Pi image must not be `stage`d at an ESP32. That check is
promised in `flows.md` and is the thing that makes mixed fleets survivable.

---

## Honourable mentions (not top 3, not ignore)

| Feature | Why it is not #1–#3 | When it starts to matter |
|---|---|---|
| **OTA library (IDF + Arduino + a C API a Pi agent can speak)** | Prerequisite, see above. Hobbyist #1. | Before any of this flies. |
| **CLI / batch enroll** | Desk problem, not a field problem. | Factory: 40 airframes, not 40 Web Serial clicks. Chromium-only is a non-starter. |
| **Self-host Compose on the vehicle** | Subsumed by the gateway image. TLS-without-DNS is the unsolved piece; a local CA the drones and the operator laptop both trust. | The moment the hosted domain is unreachable, which is takeoff. |
| **Custom self-test (R5)** | The confirm hook canary needs. | “Boots and reconnects” is not “safe to arm.” |
| **Simulation (R9) + hardware canary (LAVA)** | Earns its keep when CI pushes and the flock is too big to watch. | After coordinated apply is boring. |
| **Signing (R6)** | A field cache serving unsigned bytes is a supply-chain attack on the flock. | Before the vehicle is allowed to stage without an uplink to the mothership. |
| **`up/telemetry` as a data plane** | Refused, correctly, as a product-shape. | Use the sibling bus / MAVLink. Do not grow a second GCS inside Fleetforge. |
| **Groups UI** | Tags already exist on tokens. Checkboxes-to-deploy is phase 1. | After canary policy, or groups become “brick these.” |

---

## What not to pull forward for this persona

- **Mission control, formation, task allocation, a GCS.** That is a different
  product. The sibling bus exists so those products can sit next to
  Fleetforge without sharing a schema.
- **Opening `ff/v1` ACLs so drones can talk.** Destroys the captured-node
  story and turns the control plane into an ad-hoc mesh. If the builder
  wants MQTT-as-mesh, that is the sibling listener.
- **Application config on `dn/cfg`.** Same refusal as the hobbyist file.
  Waypoints are not heartbeat intervals.
- **Hosted-multitenant swarm-as-a-service.** V3 as “public product” and V3
  as “vehicle in a valley” do not want the same deployment. The field
  gateway is single-operator infrastructure.
- **Per-device compile (R10 aimed at N airframes).** The ESPHome failure
  mode. One artifact per *role* (vehicle image, drone image), N devices.
  Content-addressed cache keyed on the build, not on the node.
- **Hobbyist #3 (Improv) as a swarm P0.** Useful at the bench. In the field
  the vehicle *is* the AP; drones join it at boot. Re-provisioning 40
  airframes over BLE is not the path.

---

## Suggested sequencing

The stack still has to close R0 and R1 on metal, and R2 still has to exist
before anything that flies is OTA’d. For this persona, V3 is not “after V2
source-to-artifact.” The PRD even says V2 and V3 are independent except for
group-granularity policy. Use that:

```
R0 / R1 close on metal     ← do not skip
R3    OTA library          ← you do not fly the stock agent (renumbered 2026-09-22)
R2    auto-rollback
      + airborne confirm rule (safe-window owns rollback too)
      + safe_mode           ← a double-fault in the air is a crash
R2.5  Coordinated apply    ← canary, stage-in-air, stagger, skew view
      (needs groups fan-out, so a thin slice of V3 phase 1)
R4    health / version-skew dashboard that a GCS operator can read
V3a   Field gateway        ← local broker, cache, time source, sibling bus
      + Pi adapter for the vehicle
V3b   Deltas + airtime budget
V2    source-to-artifact   ← whenever; not on the critical path to a sortie
then  Thread, non-IP radios, Secure Boot on new airframes only
```

Until the library exists, the flock’s firmware is not in the product. Until
R2 exists, OTA is a crash factory. Until coordinated apply exists, bulk
deploy is how you lose N at once. Until the gateway exists, the product is
a desk dashboard for a hosted broker, and the vehicle is a hope.

The architecture can carry this — flat namespace, parent field, device-owned
reboot, opaque artifacts, IP-bearing link, MQTT/HTTPS split. The gap is not
the waist. The gap is that V3 is a silhouette, v1 is a public website, and
coordination is being silently confused with OTA. Keep them adjacent. Do
not merge them. Make the adjacent part a real, specified, field-ready
gateway instead of a bullet list.

---

## Related

- [`docs/personas/PERSONAS.md`](personas/PERSONAS.md) — canonical persona descriptions and priority matrix
- [`docs/HOBBYIST.md`](HOBBYIST.md) — maker persona; library, R2+safe-mode, Improv
- [`docs/PLATFORMS.md`](PLATFORMS.md) — STM32 agent vs FPGA/PX4 cargo; depin identity before the second vendor
- [`spec/prd.md`](../spec/prd.md) — v1 boards (vehicle, drone), V3 swarm paragraph
- [`spec/device-protocol.md`](../spec/device-protocol.md) — hierarchy, `apply`, ACLs
- [`design/architecture.md`](../design/architecture.md) — parent, adapters, transport
- [`design/artifacts.md`](../design/artifacts.md) — deltas as version pairs
- [`docs/features/groups-deploy.md`](features/groups-deploy.md) — V3 as written
- [`docs/roadmap.md`](roadmap.md) — V3 and the Thread scale question
- [`docs/releases.md`](releases.md) — V2 ⊥ V3 except R11 group policy
