# Fleetforge

Self-hosted OTA firmware management for embedded fleets — ESP32 first, architected to
grow to Raspberry Pi and eventually FPGAs.

**Safe remote firmware updates**, where "safe" means a bad build is caught before the
fleet, and any device that does get a bad update recovers itself.

> **Where this stands:** enrolment and OTA both work on real hardware, against the hosted
> instance. A board is flashed and enrolled from the browser — no toolchain, no CLI — and
> shows up live in the dashboard — fleet list, live event stream, a diagnosing serial
> console — (R0, closed 2026-09-22, including an unaided run by someone who had never
> seen the code). The dashboard's per-device Deploy pushes a new agent build to that
> board and watches its version change (R1, 2026-09-23), and a board that boots a bad
> image but never confirms it rolls itself back unattended (proven on an ESP32-S3 the same
> day). **Next is R2, safe deploy:** confirm/rollback reporting and a checksum gate, so the
> remaining gamble — an image that boots, confirms, and is broken anyway — is narrowed.
> Until then, deploy to one board at a time. What is open, next and blocked lives in
> [`TODO.md`](TODO.md) and nowhere else.

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
| [`src/fleetforge/auth/`](src/fleetforge/auth/) | Credential primitives: argon2id hashing, opaque tokens, verification cache, login rate limiting. A protected path |
| [`alembic/`](alembic/) | Migrations. `alembic/versions/` is a protected path |
| [`Dockerfile`](Dockerfile) | One image, two commands — `api` and `ingestor` differ only in `command:` |
| [`docker-compose.yml`](docker-compose.yml) | The standalone stack: the dev loop *and* the V2 self-host artifact |
| [`frontend/`](frontend/) | Vite + React + TS dashboard; its nginx serves the SPA and `/v1` on one origin |
| [`mosquitto/`](mosquitto/) | Broker config. Fleet authz is the two pattern rules in `acl`; dynsec (authentication only) is provisioned by `bootstrap.sh` then `configure.sh`. A protected path |
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
