# Fleetforge — Technical Design

*Companion to [prd.md](../spec/prd.md). Covers how the system spans ESP32 → Pi → FPGA, and the simulation backend. The contract below is abstract; its concrete wire form is [device-protocol.md](../spec/device-protocol.md) and its concrete stack is [architecture.md](production.md).*

## Multi-platform design — the thin waist
The server is **platform-blind**; every platform difference lives in a swappable adapter, so ESP32 → Pi → FPGA reuse one core.

**The contract (never changes per platform):**
- **Identity:** stable device ID + `platform_type` + capabilities + **`link_type`** (wifi / ethernet / cellular; thread later) + **`power_class`** (always_on / sleepy) + optional **`parent_device_id`** + partition-layout version + `ota_slot_size`.
- **Health:** heartbeat, firmware version, boot-success signal, telemetry channel. **Presence is derived** — Last Will for always-on nodes, `last_seen` vs an expected interval otherwise — never a raw socket state.
- **Update transaction:** four verbs for everyone — `stage → apply → confirm → rollback`. The server orchestrates; it never knows *how* — **nor when**. `stage` delivers and verifies; the **device** decides when to `apply`, in a self-declared safe window, and may sit in `awaiting-safe-window` indefinitely. A drone must not reboot mid-flight, and a rollback reboot is still a reboot. The server observes and reports; it never forces a reboot.
- **Reachability:** a device is not necessarily connected to the server directly. `parent_device_id` allows a managed device to act as a gateway for its children (V2's ground vehicle and its drones), so the core must never assume it holds an MQTT session per device.
- **Artifact:** an **opaque, versioned blob** with a declared `type` — the server stores/targets/tracks but never parses it. That's what makes a `.bin`, an OS image, and a bitstream the same thing to the core. Optional **provenance** metadata (source repo, commit SHA, tag, build URL) rides alongside — set by VCS integration (V2), ignored by the core otherwise.

**Per-platform adapters** (device updater + server artifact handler):

| Platform | Artifact | Apply | Confirm | Rollback |
|---|---|---|---|---|
| ESP32 | `.bin` | write OTA1 partition | broker reconnect + self-test | switch to OTA0 |
| Raspberry Pi (later) | OS image / container | flash B slot / pull image | systemd/health OK | boot A slot |
| FPGA (later, via companion CPU) | bitstream | reload fabric / partial reconfig | fabric ID + self-test | reload prior bitstream |

FPGAs can't run an agent, so they're managed **through their companion CPU** (real FPGA products are SoCs anyway).

## Transport — a second adapter axis
Platform and *link* vary independently, so they are separate axes of the same waist. The core requires only **an IP-bearing link + TLS**: MQTT is the **control plane** (announce, heartbeat, the four verbs, status), HTTPS is the **data plane** (artifact bytes, range-resumable). MQTT never carries payload — no range requests, and the broker would buffer the image per subscriber on a group deploy. `stage` carries a **short-lived signed artifact URL** + checksum; both channels validate against the same public CA bundle.

Wi-Fi, Ethernet and cellular are therefore all covered with no core change; **Thread** joins them free when the swarm case arrives, because it is IPv6-native (see [roadmap.md](../docs/roadmap.md) → *The scale question*). **Non-IP radios (Zigbee, BLE, LoRa) need an on-site gateway** that terminates the internet link and proxies the four verbs onto the local radio — the same shape as the FPGA companion CPU, and deferred to V2+ for the same reason it works: the core stays unaware.

The agent keeps its network setup behind `esp_netif` rather than calling `esp_wifi` directly. That is the entire cost of the abstraction in v1.

## Flash-time immutables — decide these at R0 or recall the fleet
*The concrete map, the `ff_cfg` format and the rules for changing them: [partitions.md](partitions.md).*

An OTA image writes *into* an existing partition; it cannot rewrite the partition table, and updating the bootloader is the one operation with no rollback path. Three things are therefore fixed at first USB flash and unchangeable remotely — get them wrong and every deployed board needs physical retrieval, which is exactly the intervention this product exists to remove.

1. **Partition table.** Ship the full A/B layout — `nvs`, `otadata`, `phy_init`, `ota_0`, `ota_1` — from the very first flash at R0, even though nothing writes the second slot until R2. A `factory`-only board can never OTA its way to A/B. Budget **4 MB flash minimum** (~1.9 MB per slot; universal on ESP32/S3/C6 devkits). Watch 2 MB ESP32-H2 variants if the Thread path is ever taken: ~900 KB per slot likely will not hold Thread + mbedTLS.
2. **Bootloader.** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` is a *bootloader* build-time option, so R2's auto-rollback depends on a decision made at R0.
3. **eFuse — one-way burns.** v1 posture, chosen deliberately:
   - **Secure Boot v2: off.** Its key digest is burned to eFuse and needs a re-signed bootloader, so it can never be enabled on already-deployed devices. **R6 therefore ships app-level signature verification** — the agent verifies a signature over the artifact before apply, pure software, deliverable over OTA. Secure Boot v2 is post-v1 and explicitly *new-devices-only*. The two are not interchangeable and must not be conflated.
   - **Anti-rollback: off.** `CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK` burns a monotonic version counter that would directly block R2's rollback-to-previous.
   - **Flash encryption: off.** Accepted consequence: credentials in NVS are readable with physical access. The user-facing form of that promise is in [prd.md](../spec/prd.md) → *Security & data posture*.

**Capability reporting.** The announce payload carries `ota_slot_size` and a **partition-layout version**, without which the server cannot perform the capability check [flows.md](../spec/flows.md) promises ("reject on chip / partition-size mismatch"). The layout version also lets the server detect and quarantine any board flashed with a superseded layout.

## Simulation backend *(V2 — R9)*
Simulation is pluggable behind a `sim-runner` contract (`boot artifact + run self-test → pass/fail`), since no single engine spans the ladder.

- **Harness:** `pytest-embedded` — the abstraction across backends and real hardware. Its key power: run the *same* self-test on host, in sim, and on a real board — so one test is the sim gate, the on-device confirm, *and* the canary check.
- **v1 backend (ESP32):** **Espressif's QEMU fork** via `pytest-embedded-qemu`. First-party, fully self-hostable, mature, multi-DUT.
- **Growth backend:** **Renode** (MIT, self-host, CI-native, widest arch reach incl. Cortex-A/RISC-V/Xtensa) — spike ESP32 completeness before relying on it.
- **Per platform:** Pi = run the container/image or `qemu-system-aarch64`; FPGA = HDL sim (Verilator/cocotb).
- **Fidelity layer (canary):** real hardware via `pytest-embedded` serial, automatable at scale with **LAVA** (open, self-host).

**Rejected backends:**
- **Wokwi** — SaaS; on-prem only via custom deal (fails self-host).
- **Velxio** — AGPL + commercial license; OSS headless path is ESP32-only and behind license-gated QEMU binaries (Pi/STM32 emulation is a paid overlay).

## Frontend design — API-first, multiple surfaces
A second thin waist, mirroring the device side: the **core is headless** — a stable public **API + SSE event stream**. Every UI is just a client; **none is privileged**, including the built-in dashboard (which is the *first* client, built against the same public API from R0).

- **Built-in web dashboard** — the default client. It consumes the **HTTP event stream (SSE)** from the API, *not* MQTT directly: browser-side MQTT-over-WebSocket would mean broker credentials in the browser and a second, privileged channel that no other client uses — the same trap the auth design avoids. One public API, one event stream, for every client.
- **Home Assistant** (priority post-v1): (a) ship Fleetforge as an **HA Add-on** (Docker/Supervisor); (b) publish **MQTT Discovery** configs so each managed board appears as an HA **`update` entity** (version, "available", Install → triggers a Fleetforge deploy). Reuses the broker already in place.
- **CLI** (priority post-v1): scriptable client for batch flash/deploy/CI — another API consumer.
- **Claude Code / MCP** (later): first-party MCP server (`list_devices`, `get_health`, `simulate`, `deploy`, `rollback`). **Safety posture = read-rich, guarded writes** — queries are free; deploy/rollback require sim-pass + explicit confirmation + scoped token (never a raw LLM push to real hardware).
- **Grafana/Prometheus** (optional): telemetry export for the observe pillar.

*Cost of this choice:* design the API deliberately from R0 rather than wiring a UI to internals — cheap insurance vs. the expensive retrofit later.

## API surface & authentication

Three credentials, two zones, and nothing else. Devices never hold an admin credential.

| Zone | Endpoints | Credential |
|---|---|---|
| **Admin** | everything else | **opaque bearer token** |
| **Device** | `POST /v1/enroll` | single-use **enrollment token** in the body |
| **Device** | `GET /v1/artifact/…` | **short-lived signed URL** (signature *is* the authorization) |

### v1 admin auth — one credential type, two transports

Every admin request authenticates with **`Authorization: Bearer <token>`**. Tokens are
rows: `id, name, hash, subject, scopes, created, last_used, expires_at, revoked_at`.

The dashboard is not a special case. It POSTs the admin password to `/v1/auth/login` and
receives *the same kind of token*, returned in an **HttpOnly / Secure / SameSite=Strict**
cookie instead of the response body — so browser XSS cannot read it. The API accepts a
token from either the `Authorization` header or that cookie, through one verification
path. CLI, Home Assistant and MCP use the header form and are in no way second-class.

**Opaque random tokens, hashed at rest — not JWT.** JWT buys stateless verification we do
not need at one admin and a handful of clients, and costs instant revocation, which we do
need on a public-facing API. A DB lookup per request is free at this scale.

Serve the dashboard and the API from **one origin** behind the same Traefik host: no CORS
configuration, and SameSite=Strict is then sufficient against CSRF.

**Bootstrap:** admin password from the environment (`services/prod/.env`, per house
convention), argon2id at rest, and the login endpoint rate-limited — it is a password
endpoint on the public internet.

### The extension points — deliberately reserved, unused in v1

- **`scopes`** — always `admin` in v1. *Frontend design* above already promises MCP a *"read-rich,
  guarded writes … scoped token"*, so the column has to exist before that is buildable.
  Later: `read`, `deploy`, `enroll`.
- **`subject`** — always `"admin"` in v1. There is no users table and no rows to manage;
  when multi-tenant arrives, subjects gain an org and nothing else changes shape.
- **Versioned path** (`/v1/…`) so a breaking API revision can run alongside the old one,
  exactly as the device protocol does.

### Explicitly not in v1

OAuth/OIDC · multi-user · RBAC · JWT · refresh tokens · MFA. Each is additive on top of
the token table rather than a rewrite of it — which is the entire point of paying for the
table now.

## Artifact storage — one more adapter
Artifact *bytes* sit behind a narrow object-store adapter (`put` / `get` / `signed_url` / `delete`); artifact *metadata* lives in the database. **GCS in v1, MinIO for self-hosting** — S3-compatible, so a second implementation rather than a redesign. Signed URLs are generated by the adapter, which is what keeps the `stage` command's authorization model identical across backends. Concrete configuration in [architecture.md](production.md).

## Design principles
1. **Thin waist:** keep the server contract minimal and opaque — the fewer assumptions in the core, the cheaper a wrong guess about Pi/FPGA.
2. **Toolchain-agnostic artifacts:** whether PlatformIO, `idf.py`, Arduino CLI, or Vivado produced the blob must not matter. PlatformIO is a recommended-but-optional multi-board producer, never a core dependency. *This survives V2's server-side compiler: the builder is one more artifact **producer** feeding the same upload API, never a privileged path into the core.*
3. **Write the self-test once:** it is the simulation gate, the on-device confirm, and the canary assertion.
4. **Defense in depth:** simulate (pre-flight) → canary (pre-fleet) → auto-rollback (per-device). Each layer catches what the previous can't.
5. **The device owns the reboot.** The server may stage, request and observe; only the device knows whether it is airborne, in motion, or mid-task. Every reboot — apply *and* rollback — happens in a device-declared safe window.
6. **Two thin waists:** device-facing (opaque artifact contract) and user-facing (headless API-first core). Adapters/clients plug into each without touching the core.
