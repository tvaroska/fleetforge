# Fleetforge — v1 Specification

*Centralized, self-hosted OTA management for embedded fleets. Starts with ESP32; architected to grow to Raspberry Pi and, eventually, FPGAs. Technical design in [DESIGN.md](./DESIGN.md).*

## Problem
Makers and small teams who deploy connected devices they can't easily reach (sensors, home/farm/product installs) have no safe, self-hosted way to update firmware remotely. USB flashing doesn't scale past the bench; a bad push with no recovery bricks devices and kills trust.

## v1 Scope — the one thing done well
**Safe remote firmware updates for a fleet of ESP32 devices, from a self-hosted server** — where "safe" means a bad build is caught *before* the fleet, and any device that does get a bad update recovers itself.

## Users
Primary: **solo makers / small teams** with a deployed ESP32 project (a handful to a few dozen boards). Values self-hosting (own your data) and a fast path from install to first update.

## Product surface
- **Server (self-hosted):** control plane + MQTT broker + web dashboard. Ships as one Docker Compose stack; opinionated defaults, TLS handled for you — onboarding friction is the make-or-break risk, so treat it as a feature.
- **Device side:** (a) a **prebuilt agent** to flash for instant wow, and (b) a **thin OTA library** (ESP-IDF/Arduino) to embed in custom firmware.
- **Dashboard:** fleet inventory + live health (online/offline, firmware version, last-seen, boot-success) + a few user-defined telemetry metrics + push/rollback controls.

## How it works — the deployment funnel
Every deployment passes gates; each catches what the previous can't (**defense in depth**):
1. **Simulate (pre-flight, advisory).** Boot the artifact in an emulator and run the user's self-test. Warns on failure; user may override. Catches boots-but-crashes bugs at **zero device cost**.
2. **Deliver.** Device pulls the update over MQTT/Wi-Fi (resumable), **verifies checksum + signature**, applies to a secondary A/B slot, reboots.
3. **Confirm (per-device).** New firmware must **reconnect to the broker** within a timeout (and pass an **optional custom self-test**) — else **auto-rollback** to last-known-good. Any custom self-test is reused as the sim gate in step 1: *write once, gate in sim, confirm on device.*

See [DESIGN.md](./DESIGN.md) for the platform-agnostic contract and simulation backend.

## Success metrics — track both
- **Delivery success** = healthy AND running the intended new version (rollback counts as a *miss*; drives build quality + simulation).
- **Fleet safety** = device ends healthy on *some* version (rollback counts as a *save*; drives robustness).

Two numbers so neither is gamed at the other's expense; the gap between them tells you *which stage* to fix.

## Explicitly out of scope (v1)
Develop/build/debug pillar · staged/canary rollouts *(top post-v1 lever for delivery success)* · Raspberry Pi & FPGA implementations · cloud SaaS · multi-tenant/orgs · rich observability & alerting.

## Roadmap (beyond v1)
- **V2 — VCS integration** (provider-agnostic push): CI posts artifacts with provenance (repo/commit/tag); per-group deploy policy; commit-level traceability & rollback. See [FLOWS.md](./FLOWS.md).
- **Then:** canary/staged rollout · Raspberry Pi adapter · SoftAP provisioning · per-device mTLS.
- **Interaction surfaces:** the core is **API-first/headless from v1** (dashboard = first client); priority integrations are **Home Assistant** (add-on + MQTT `update` entities) and **CLI**, later **Claude Code / MCP** (guarded writes). See [DESIGN.md](./DESIGN.md).

## Key risks
1. **Onboarding friction** (self-hosted broker + certs) — mitigate with one-command Compose + prebuilt agent.
2. **Bad pushes on flaky Wi-Fi** — mitigate with resumable download, integrity check, atomic A/B apply, mandatory rollback handshake.
3. **Wrong abstraction guess** for Pi/FPGA — mitigate by keeping the contract minimal and opaque (see DESIGN.md).
4. **Sim ≠ reality** — sim reduces logic/boot bugs but not hardware/RF/timing bugs; hence advisory-only, and canary is the planned next layer.
5. **Sim-engine lock-in / licensing** — mitigate by keeping sim pluggable behind the runner contract.
