# Fleetforge

Self-hosted OTA firmware management for embedded fleets — ESP32 first, architected to
grow to Raspberry Pi and eventually FPGAs.

**Safe remote firmware updates**, where "safe" means a bad build is caught before the
fleet, and any device that does get a bad update recovers itself.

> **Status:** R0 in progress. The registry schema and the Python project exist; the API,
> ingestor, agent and dashboard do not yet. See [`TODO.md`](TODO.md).

## Why

Makers and small teams who deploy connected devices they can't easily reach have no safe
way to update firmware remotely without handing their fleet to a vendor cloud. USB
flashing doesn't scale past the bench; a bad push with no recovery bricks devices.

## Shape

Two thin waists:

- **Device-facing** — an opaque, versioned artifact plus a four-verb contract
  (`stage → apply → confirm → rollback`). The server orchestrates; it never knows *how*,
  nor *when*. **The device owns the reboot** and owns its own rollback.
- **User-facing** — a headless, API-first core. Every UI is a client and none is
  privileged, including the built-in dashboard.

MQTT is the control plane; HTTPS carries artifact bytes.

## Where things live

| Path | Holds |
|---|---|
| [`TODO.md`](TODO.md) | **Live status — the only place task state lives** |
| [`src/fleetforge/`](src/fleetforge/) | The Python package — one image, two entrypoints (api, ingestor) |
| [`alembic/`](alembic/) | Migrations. `alembic/versions/` is a protected path |
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

## Development

```bash
just db-up          # dev Postgres on 127.0.0.1:5433 (5432 is taken on this host)
uv sync             # create .venv from uv.lock
just migrate        # alembic upgrade head
just test           # ruff + mypy + pytest against the real database
```

## Releases

**V1 (R0–R5)** — safe OTA on ~5 heterogeneous boards.
**V2 (R6–R10)** — source to artifact: VCS ingestion, server-side compile, simulation gate.
**V3** — robotic swarm: one ground vehicle as gateway plus many flying drones.
