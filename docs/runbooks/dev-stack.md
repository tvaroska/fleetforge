# Runbook — the standalone dev stack

The whole product on one machine: Traefik + Postgres + MinIO + Mosquitto + api +
ingestor + frontend. It is **both** the everyday dev loop and the V2 self-host
artifact (`design/production.md` → *Two deployment artifacts, deliberately*), which
is exactly why it is the dev environment: an artifact you use every day cannot rot.

```bash
cp -n .env.example .env    # first time only
just up                    # dev shape: hot reload + Vite dev server
just up-prod               # production shape: built images, nginx, no bind mounts
just down                  # stop
just nuke                  # stop AND wipe the database, broker sessions, bucket
just logs api              # follow one service
just stack-check           # parse both compose files
```

## Ports

All host ports are overridable in `.env`. 80/443 are deliberately **not** published
— this is a shared dev machine.

| Host port | Env var | Serves |
|---|---|---|
| 8080 | `FF_HTTP_PORT` | Traefik `web` → frontend → SPA **and** `/v1/*` (one origin) |
| 8081 | `FF_TRAEFIK_DASHBOARD_PORT` | Traefik dashboard / API (`--api.insecure`, dev only) |
| 8883 | `FF_MQTT_PORT` | Traefik `mqtt` TCP entrypoint → `mosquitto:1883` |
| 9000 | `FF_MINIO_PORT` | MinIO S3 API |
| 9001 | `FF_MINIO_CONSOLE_PORT` | MinIO console |
| 5433 | — | Postgres (5432 belongs to an unrelated `bridge-postgres` container) |

The API has **no host port and no Traefik router**. The only way to reach it is
`http://localhost:8080/v1/...`, through nginx in the frontend container. That is
what makes "no CORS" structural rather than configured.

The broker has **no host port either**: everything goes through Traefik's `mqtt`
entrypoint, so the dev path and the prod path are the same path.

## Publishing a test message

```bash
just mqtt-pub 'ff/v1/d/a4cf12b3de90/up/announce' '{"proto":1,"device_id":"a4cf12b3de90"}'
just mqtt-sub                      # defaults to ff/v1/d/+/up/#
docker compose logs -f ingestor    # the ingestor is the only subscriber
```

**Do not reach for `mosquitto_pub`/`mosquitto_sub` here.** The mosquitto CLI clients
switch to TLS whenever the port is 8883 and offer no flag to turn it off, so against
the plaintext dev broker they fail with `Error: Protocol error` /
`A TLS error occurred` — which looks exactly like a broken TCP router and is not.
The `just mqtt-*` recipes use paho (already installed as an `aiomqtt` dependency).
If you must use the mosquitto CLI, set `FF_MQTT_PORT` to something other than 8883
and recreate the stack.

## Failures you will actually hit

**1. Traefik returns 404 for the dashboard, but the container is up.**
Traefik silently skips containers that are not `healthy`. Check
`docker inspect fleetforge-frontend --format '{{json .State.Health}}'`. Two probes
have already been fixed this way: `node:22-slim` ships neither `wget` nor `curl`,
and nginx listens on IPv4 only, so a probe against `localhost` resolves to `::1`
and is refused. Use `127.0.0.1` in container healthchecks.

**2. Traefik returns 502 and the container is healthy.**
The `traefik.docker.network` label does not match the real compose-prefixed network
name. Confirm with `docker network ls` (expected: `fleetforge_frontend` for the
frontend, `fleetforge_backend` for mosquitto).

**3. The API cannot reach Postgres: "connection refused" on `localhost:5433`.**
Somebody added `env_file: .env` to a service. `.env` holds the **host** database
URL; containers get `postgres:5432` set explicitly in `docker-compose.yml`, and
pydantic-settings gives real environment variables precedence over `.env` values.
Remove the `env_file:`.

**4. "port is already allocated".**
5432 belongs to `bridge-postgres`. Override the `FF_*_PORT` values in `.env`;
Postgres itself stays on 5433 because alembic, pytest and `just db-up` all assume it.

**5. Mosquitto goes unhealthy right after R0-sec-1.**
The healthcheck runs `mosquitto_sub` anonymously. R0-sec-1 removes anonymous access
and must update that healthcheck to use the dynsec admin credential in the same
commit.

**6. The frontend container fails to start after a `package.json` change.**
The dev `node_modules` lives in the `ff_node_modules` named volume, seeded once from
the image. Re-seed it with `just rebuild frontend`, or `docker volume rm
fleetforge_ff_node_modules` after `just down`.

## Host workflow (no stack required)

`just db-up` starts Postgres alone, which is all `just migrate` and `just test`
need. Keep it that way: no test may shell out to `docker compose`.
