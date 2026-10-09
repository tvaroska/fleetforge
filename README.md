# Fleetforge

Self-hosted OTA firmware management for embedded fleets — ESP32 first, architected to
grow to Raspberry Pi and eventually FPGAs.

**Safe remote firmware updates**, where "safe" means the gate catches a bad build before the fleet. Any device that does get a bad update recovers itself.

> **Where this stands:** R0-R2b are released (v0.4.3 and earlier): enroll a board from the
> browser, deploy from the dashboard, device-side rollback of a build that never confirms,
> and the operator flows around them. R3 adds the thin OTA library, so a maker's own
> Arduino, PlatformIO or ESP-IDF firmware updates and rolls back the same way; it is proved
> in QEMU and the simulator, not on a hardware bench (DECISIONS 2026-10-08). What is open,
> next and blocked lives in [`TODO.md`](TODO.md) and nowhere else.

## Why

Makers and small teams who deploy connected devices they cannot easily reach have no safe
way to update firmware remotely without handing their fleet to a vendor cloud. USB
flashing does not scale past the bench. A bad push with no recovery bricks devices.

## Shape

Two thin waists:

- **Device-facing** — an opaque, versioned artifact plus a four-verb contract
  (`stage → apply → confirm → rollback`). The server orchestrates. It never knows *how*,
  nor *when*. **The device owns the reboot** and owns its own rollback.
- **User-facing** — a headless, API-first core. Every UI is a client and none has special privilege, including the built-in dashboard.

MQTT is the control plane. HTTPS carries artifact bytes.

## Where things live

| Path | Holds |
|---|---|
| [`TODO.md`](TODO.md) | **Live status — the only place task state lives** |
| [`src/fleetforge/`](src/fleetforge/) | The Python package — one image, two entrypoints (api, ingestor) |
| [`src/fleetforge/auth/`](src/fleetforge/auth/) | Credential primitives: argon2id hashing, opaque tokens, verification cache, login rate limiting. A protected path |
| [`alembic/`](alembic/) | Migrations. `alembic/versions/` is a protected path |
| [`Dockerfile`](Dockerfile) | One image, two commands — `api` and `ingestor` differ only in `command:` |
| [`docker-compose.yml`](docker-compose.yml) | The standalone stack: the dev loop *and* the V2 self-host artifact |
| [`frontend/`](frontend/) | Vite + React + TS dashboard. Its nginx serves the SPA and `/v1` on one origin |
| [`agent/components/fleetforge/`](agent/components/fleetforge/) | The OTA library: ESP-IDF component + Arduino library and their worked examples; its README is the quickstart |
| [`mosquitto/`](mosquitto/) | Broker config. Fleet authz is the two pattern rules in `acl`. Dynsec (authentication only) is provisioned by `bootstrap.sh` then `configure.sh`. A protected path |
| [`docs/runbooks/`](docs/runbooks/) | Operational procedures, starting with the dev stack |
| [`spec/prd.md`](spec/prd.md) | Requirements, targets, scope, risks |
| [`spec/device-protocol.md`](spec/device-protocol.md) | The wire contract — near-frozen |
| [`spec/flows.md`](spec/flows.md) | The two core user flows |
| [`design/architecture.md`](design/architecture.md) | Platform-agnostic contracts, adapters, flash-time immutables |
| [`design/production.md`](design/production.md) | Concrete stack and deployment topology |
| [`docs/roadmap.md`](docs/roadmap.md) | Release ladder index |
| [`docs/releases.md`](docs/releases.md) | What each release adds |
| [`docs/features/`](docs/features/) | Per-capability detail and task history |
| [`DECISIONS.md`](DECISIONS.md) | Append-only decision log |
| [`CRITICAL.md`](CRITICAL.md) | Protected paths — including the ones no OTA can fix |

## Put Fleetforge in your own firmware

The OTA library in [`agent/components/fleetforge/`](agent/components/fleetforge/README.md)
gives your own Arduino, PlatformIO or ESP-IDF firmware the same enroll, update and rollback
as the stock agent. Its README routes to a worked example per toolchain, and
`just lib-quickstart` plays the PlatformIO one in QEMU.

## Development

The primary loop is the whole stack — same file that self-hosting will ship
([runbook](docs/runbooks/dev-stack.md)):

```bash
cp -n .env.example .env
just up             # Traefik + Postgres + MinIO + Mosquitto + api + ingestor + frontend
                    # dashboard AND API on http://localhost:8080 (one origin, no CORS)
just up-prod        # the production-shaped stack — run before committing
just nuke           # stop and wipe every volume
```

Python alone, without the stack:

```bash
just db-up          # dev Postgres on 127.0.0.1:5433 (5432 is taken on this host)
uv sync             # create .venv from uv.lock
just migrate        # alembic upgrade head
just test           # ruff + mypy + pytest against the real database
```

## Releases

**V1 (R0–R6)** — safe OTA on ~5 heterogeneous boards.
**V2 (R7–R11)** — source to artifact: VCS ingestion, server-side compile, simulation gate.
**V3** — robotic swarm: one ground vehicle as gateway plus many flying drones.
