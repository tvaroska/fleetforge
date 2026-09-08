# Fleetforge — Technical Design

*Companion to [SPEC.md](./SPEC.md). Covers how the system spans ESP32 → Pi → FPGA, and the simulation backend.*

## Multi-platform design — the thin waist
The server is **platform-blind**; every platform difference lives in a swappable adapter, so ESP32 → Pi → FPGA reuse one core.

**The contract (never changes per platform):**
- **Identity:** stable device ID + `platform_type` + capabilities.
- **Health:** heartbeat, firmware version, boot-success signal, telemetry channel.
- **Update transaction:** four verbs for everyone — `stage → apply → confirm → rollback`. The server orchestrates; it never knows *how*.
- **Artifact:** an **opaque, versioned blob** with a declared `type` — the server stores/targets/tracks but never parses it. That's what makes a `.bin`, an OS image, and a bitstream the same thing to the core. Optional **provenance** metadata (source repo, commit SHA, tag, build URL) rides alongside — set by VCS integration (V2), ignored by the core otherwise.

**Per-platform adapters** (device updater + server artifact handler):

| Platform | Artifact | Apply | Confirm | Rollback |
|---|---|---|---|---|
| ESP32 | `.bin` | write OTA1 partition | broker reconnect + self-test | switch to OTA0 |
| Raspberry Pi (later) | OS image / container | flash B slot / pull image | systemd/health OK | boot A slot |
| FPGA (later, via companion CPU) | bitstream | reload fabric / partial reconfig | fabric ID + self-test | reload prior bitstream |

FPGAs can't run an agent, so they're managed **through their companion CPU** (real FPGA products are SoCs anyway).

## Simulation backend
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
A second thin waist, mirroring the device side: the **core is headless** — a stable public **API + event stream (MQTT)**. Every UI is just a client; **none is privileged**, including the built-in dashboard (which is the *first* client, built against the same public API from R0).

- **Built-in web dashboard** — the default client.
- **Home Assistant** (priority post-v1): (a) ship Fleetforge as an **HA Add-on** (Docker/Supervisor); (b) publish **MQTT Discovery** configs so each managed board appears as an HA **`update` entity** (version, "available", Install → triggers a Fleetforge deploy). Reuses the broker already in place.
- **CLI** (priority post-v1): scriptable client for batch flash/deploy/CI — another API consumer.
- **Claude Code / MCP** (later): first-party MCP server (`list_devices`, `get_health`, `simulate`, `deploy`, `rollback`). **Safety posture = read-rich, guarded writes** — queries are free; deploy/rollback require sim-pass + explicit confirmation + scoped token (never a raw LLM push to real hardware).
- **Grafana/Prometheus** (optional): telemetry export for the observe pillar.

*Cost of this choice:* design the API deliberately from R0 rather than wiring a UI to internals — cheap insurance vs. the expensive retrofit later.

## Design principles
1. **Thin waist:** keep the server contract minimal and opaque — the fewer assumptions in the core, the cheaper a wrong guess about Pi/FPGA.
2. **Toolchain-agnostic artifacts:** whether PlatformIO, `idf.py`, Arduino CLI, or Vivado produced the blob must not matter. PlatformIO is a recommended-but-optional multi-board producer, never a core dependency.
3. **Write the self-test once:** it is the simulation gate, the on-device confirm, and the canary assertion.
4. **Defense in depth:** simulate (pre-flight) → canary (pre-fleet) → auto-rollback (per-device). Each layer catches what the previous can't.
5. **Two thin waists:** device-facing (opaque artifact contract) and user-facing (headless API-first core). Adapters/clients plug into each without touching the core.
