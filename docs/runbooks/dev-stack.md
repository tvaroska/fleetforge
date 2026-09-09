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

## Broker authentication (R0-sec-1)

**`allow_anonymous false`. Every client authenticates, including yours.** Two
mechanisms, both consulted by the broker, and **allow wins**:

| Mechanism | File | Decides |
|---|---|---|
| dynamic-security plugin | `/mosquitto/data/dynamic-security.json` (data volume) | *who exists* and what their password is — written by `POST /v1/enroll` |
| `acl_file` | `mosquitto/acl` | *what anyone may do* — the two `%u` pattern rules |

The ACL rules are **not** in a dynsec role: Mosquitto 2.0's plugin has no `%u`
substitution (DECISIONS.md 2026-09-08). The `device` role exists and is deliberately
empty — `createClient` requires a role name and nothing more.

Three credentials exist before any board enrolls, all created by
`mosquitto/bootstrap.sh` in the `mosquitto-init` one-shot:

| Username (`.env`) | Role | May |
|---|---|---|
| `MQTT_DYNSEC_USERNAME` (`ff-admin`) | dynsec `admin` | create clients, read `$SYS` — the API and the healthcheck |
| `MQTT_INGESTOR_USERNAME` (`ff-ingestor`) | `ingestor` | subscribe/receive `ff/v1/d/+/up/#`, nothing else |
| a device id | `device` (empty) | write `ff/v1/d/<its own id>/up/#`, read `…/dn/#` |

Prove the whole matrix against the running broker at any time:

```bash
just broker-check          # provisions two ffff… throwaways, prints SELFTEST OK
```

It is deliberately not part of `just test` (that must not need a broker).

## Publishing a test message

```bash
# As the dynsec admin (the default): fine for reading, useless for writing up/*.
just mqtt-sub                      # defaults to ff/v1/d/+/up/#

# As a board — username = device_id, password = the `mqtt_password` that
# POST /v1/enroll returned for it. That value exists NOWHERE else: not in
# Postgres, not in a log. Lose it and the board re-enrolls.
just mqtt-pub 'ff/v1/d/a4cf12b3de90/up/announce' '{"proto":1}' 0 a4cf12b3de90 "$PW_A"
just mqtt-pub 'ff/v1/d/a4cf12b3de90/up/presence' '{"online":true}' 1 a4cf12b3de90 "$PW_A"
docker compose logs -f ingestor    # the ingestor is the only subscriber
```

**The admin credential cannot publish a device's `up/*`.** The `%u` patterns bind to
the *username*, and `ff-admin` is not a device id — so `just mqtt-pub` without the
last two arguments connects fine and is then **silently dropped**. See failure 5.

**Do not reach for `mosquitto_pub`/`mosquitto_sub` here.** The mosquitto CLI clients
switch to TLS whenever the port is 8883 and offer no flag to turn it off, so against
the plaintext dev broker they fail with `Error: Protocol error` /
`A TLS error occurred` — which looks exactly like a broken TCP router and is not.
The `just mqtt-*` recipes use paho (already installed as an `aiomqtt` dependency).
Inside the broker container the port is 1883, so the CLI works there — which is the
one place to get a **reason code**:

```bash
docker compose exec -T mosquitto sh -c "mosquitto_pub -V 5 -h 127.0.0.1 \
  -u a4cf12b3de90 -P '$PW_A' -q 1 -d -t ff/v1/d/b0b0b0b0b0b0/up/hb -m x | grep PUBACK"
#  => RC:135  Not authorized     (RC:0 or RC:16 = allowed)
```

## Watching a device come online

The ingestor only ever **updates** device rows — it never creates one, because the
only way into the registry is a burned enrollment token (`POST /v1/enroll`, R0-be-4).
**Since R0-sec-1 that is also the only way to get a broker credential**, so seeding a
row with `INSERT INTO devices` no longer buys you anything: the fake board has no
password and its publishes are dropped. Enroll for real (`$TOKEN` is the admin
session from the next block):

```bash
BASE=http://localhost:${FF_HTTP_PORT:-8080}
DEV=aabbccddeeff
psql() { docker compose exec -T postgres psql -U fleetforge -d fleetforge -Aqt "$@"; }

ET=$(curl -sS -X POST "$BASE/v1/enrollment-tokens" -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{}' | jq -r .token)
PW=$(curl -sS -X POST "$BASE/v1/enroll" -H 'content-type: application/json' \
     -d "{\"token\":\"$ET\",\"device_id\":\"$DEV\",\"platform_type\":\"esp32c6\",
          \"link_type\":\"wifi\",\"power_class\":\"always_on\",\"proto\":1}" \
     | jq -r .mqtt_password)      # the only copy — keep it in the shell, not in a file

just mqtt-pub "ff/v1/d/$DEV/up/announce" '{"proto":1,"platform_type":"esp32c6","fw_version":"1.4.2","link_type":"wifi","power_class":"always_on"}' 0 "$DEV" "$PW"
psql -c "SELECT fw_version, last_seen, presence_reported FROM devices WHERE device_id='$DEV';"
#  => 1.4.2 | a timestamp | (empty — announce says nothing about presence)

just mqtt-pub "ff/v1/d/$DEV/up/presence" '{"online":true}' 1 "$DEV" "$PW"  # retained
psql -c "SELECT presence_reported FROM devices WHERE device_id='$DEV';"     # => t
```

Watch the same thing the dashboard sees — the SSE stream (R0-be-5). Log in first;
`curl` will not send the `Secure` session cookie over plain http, so take the token
out of the `Set-Cookie` and use the bearer transport (same credential, R0-be-1):

```bash
BASE=http://localhost:${FF_HTTP_PORT:-8080}
TOKEN=$(curl -sSi -X POST "$BASE/v1/auth/login" -H 'Content-Type: application/json' \
        -d '{"password":"fleetforge-dev-only"}' \
        | grep -i '^set-cookie:' | sed -E 's/.*ff_session=([^;]+).*/\1/')

# terminal 1 — `-N` is essential: curl buffers otherwise and you will blame nginx
curl -N -H "Authorization: Bearer $TOKEN" "$BASE/v1/events"
#   : connected
#   retry: 2000
# terminal 2
just mqtt-pub "ff/v1/d/$DEV/up/hb" '{"fw_version":"1.4.2"}' 0 "$DEV" "$PW"
# terminal 1, within ~2 s:
#   data: {"v":1,"type":"device.heartbeat","device_id":"aabbccddeeff","at":"…","online":true,"fw_version":"1.4.2"}
# and a `: keepalive` comment every 15 s while nothing happens.
```

The event is only a hint — the fleet view is `curl -sS -H "Authorization: Bearer
$TOKEN" "$BASE/v1/devices"`, which recomputes presence on read (a sleepy board goes
offline with no event at all).

**Is it the API or the database?** Bisect with psql, which prints notifications when
the command it is running returns — so give it a sleep to publish into:

```bash
docker compose exec -T postgres psql -U fleetforge -d fleetforge \
  -c "LISTEN ff_events" -c "SELECT pg_sleep(20)"
#   Asynchronous notification "ff_events" with payload
#   {"v":1,"type":"device.heartbeat",…}
```

Two results that look like bugs and are not: publishing for a device that is not in
`devices` logs *"ignoring message from unregistered or decommissioned device"* and
creates nothing (that is the enrollment boundary), and a `docker compose restart
ingestor` replays every **retained** `announce`/`presence` without moving `last_seen`
— a replay is not evidence that a board is alive.

The dev container has no `--reload`: after editing `src/`, run
`docker compose restart ingestor`.

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

**5. Something MQTT does not work, and nothing says why.** Three distinct symptoms:

*"Connection Refused: not authorized" / `[code:135]` at connect* — authentication.
No credential, a wrong password, or a username the broker does not know. The broker
log says `client … disconnected, not authorised`. For a board this usually means it
enrolled during the `NullProvisioner` era (see below), or somebody wiped the
`fleetforge_mosquitto` volume without re-enrolling.

*A publish that "succeeds" and never arrives* — authorization. **In MQTT 3.1.1 there
is no reason code**: paho reports success and the broker drops the message. Neither
does the broker log say so at the default log level — `Denied PUBLISH` is
`MOSQ_LOG_DEBUG` in 2.0.22 and `mosquitto.conf` enables only
error/warning/notice/information (turning on `log_type debug` logs every topic; not
worth it). Get the truth with `mosquitto_pub -V 5 -d` from **inside** the container
(recipe above): `RC:135` is the denial. The usual cause is publishing as the dynsec
admin instead of as the device, or a topic under another device's id.

*`Error saving Dynamic security plugin config` / `not writable`* — ownership.
`dynamic-security.json` must be `mosquitto:mosquitto` `0600`; root-owned, the plugin
applies enrollments **in memory only** and loses every device credential on the next
restart while the API reports success. `mosquitto/bootstrap.sh` fixes it; verify with
`docker compose exec -T mosquitto ls -l /mosquitto/data/dynamic-security.json` and
`docker compose logs mosquitto | grep -ci "not writable"` (must be `0`).

Two lines in the broker log are **expected and harmless**: `chown:
/mosquitto/config/acl: Read-only file system` from the entrypoint (it is a `:ro` bind
mount from the repo, on purpose) and the matching "world readable / owner is not
mosquitto" warnings about that file.

**5b. A device enrolled before R0-sec-1 cannot connect, and cannot be repaired.**
Those enrollments ran through `NullProvisioner`: the broker never stored a
credential, and the password only ever existed in the enrollment response, so the
server cannot re-provision one the board would know. Find them with `SELECT device_id
FROM devices WHERE broker_provisioned_at IS NULL` — the only fix is re-enrollment
with a fresh `ffe_` token.

**6. An SSE stream connects and stays empty** — `: connected` and keepalives arrive,
device events never do. The `LISTEN` connection is down (the fan-out itself is
in-process and cannot half-work). Check that the API has one:
`docker compose exec -T postgres psql -U fleetforge -d fleetforge -c "SELECT
application_name, state FROM pg_stat_activity WHERE
application_name='fleetforge-events';"` — expect one row **per api worker**, `idle`.
Then `docker compose logs api | grep ff_events`: `listening on ff_events` at startup,
and a reconnect warning for every blip since. If psql *does* see the notification
(bisect above) and the stream does not, it is the API; if neither does, it is the
ingestor or the broker. Note that a reconnect deliberately **ends every open stream** —
the client is expected to reconnect and re-read `GET /v1/devices`.

**7. The frontend container fails to start after a `package.json` change.**
The dev `node_modules` lives in the `ff_node_modules` named volume, seeded once from
the image. Re-seed it with `just rebuild frontend`, or `docker volume rm
fleetforge_ff_node_modules` after `just down`.

## Host workflow (no stack required)

`just db-up` starts Postgres alone, which is all `just migrate` and `just test`
need. Keep it that way: no test may shell out to `docker compose`.

### Setting the admin password

`.env.example` ships a working dev value: the argon2id hash of the obviously-fake
password **`fleetforge-dev-only`**. `cp -n .env.example .env` and login works out of
the box. To use your own:

```bash
just admin-password        # prompts twice, echoes nothing, prints the PHC string
```

Paste the printed line into `.env` **exactly as printed — single-quoted**:

```
ADMIN_PASSWORD_HASH='$argon2id$v=19$m=19456,t=2,p=1$...'
```

Then `just up` and verify the container actually received it:

```bash
docker compose config | grep -i ADMIN_PASSWORD_HASH
#  correct:  ADMIN_PASSWORD_HASH: $$argon2id$$v=19$$m=19456,t=2,p=1$$...
#  broken:   ADMIN_PASSWORD_HASH: =19=19456...
```

**Why the quotes matter.** A PHC string is full of `$`, and docker compose
interpolates `$argon2id` / `$v` / `$m` as (empty) variables. Unquoted, the container
receives a truncated string, `argon2` rejects it as an invalid hash, and **every
login returns 401 with nothing in the logs to explain it**. `python-dotenv` strips
the surrounding single quotes, so the one quoted line serves both the host process
and compose interpolation.

The value is a *hash*, never a password, and the API cannot recover the plaintext
from it — losing the password means minting a new hash. Production sets its own in
`services/prod/.env` (ask before changing that file). Without the variable, the
`api` service refuses to start (`${ADMIN_PASSWORD_HASH:?…}`) — deliberately, since a
default admin credential is worse than a stack that will not boot.

**Logging in:**

```bash
curl -i -c /tmp/ff.jar -X POST http://localhost:8080/v1/auth/login \
  -H 'content-type: application/json' -d '{"password":"fleetforge-dev-only"}'
curl -b /tmp/ff.jar http://localhost:8080/v1/auth/me
```

The cookie value *is* an ordinary admin token: the same string works as
`Authorization: Bearer …`. Revoke a session with `POST /v1/auth/logout`, or directly:
`UPDATE admin_tokens SET revoked_at = now() WHERE id = '<uuid>'` — the next request
is a 401, with no restart and no waiting.

Five failed logins in 60 s from one client IP (or 30 across all of them) return 429
until the window passes; the counter is per API process, so `docker compose restart
api` clears it.

## Object storage

The dev stack runs MinIO with a `fleetforge` bucket created at startup by the
`minio-init` one-shot. Round-trip it without booting the whole stack:

```bash
just minio-up && just storage-check     # put → get → signed URL → delete, SELFTEST OK
```

Full details, the GCS side and the provisioning recipe:
[artifact-storage.md](artifact-storage.md).

**The signed URL's host is `localhost:9000`, never `minio:9000`.** A presigned S3 URL
signs the `Host` header, so the URL cannot be rewritten after signing — which is why the
`api` service sets *two* endpoints: `S3_ENDPOINT_URL=http://minio:9000` for its own I/O
and `S3_PUBLIC_ENDPOINT_URL=http://localhost:${FF_MINIO_PORT}` for the URLs it hands out.
Run the selftest inside the container and it says it could not fetch the URL it printed:

```bash
docker compose exec -T api python -m fleetforge.storage selftest
```

That is correct — `localhost:9000` inside the api container is that container's own
loopback. Copy the printed URL and `curl` it from the host; you get 200. A **403 or 404**
would be a real failure (bad signature, expired, or no permission); a **connection
refused from inside the container** is the design working.

Two more things worth knowing before you lose an hour:

* **`.env` holds the HOST values** (`S3_ENDPOINT_URL=http://localhost:9000`), used by
  `just storage-check` and the object-store tests. The containers get their own values
  from `docker-compose.yml`. Same trap as `DATABASE_URL`: never add `env_file: .env` to
  the `api` service.
* **`tests/test_object_store_minio.py` skips itself** when nothing answers on
  `localhost:9000`. A green `just test` therefore does *not* mean the real backend was
  exercised — run `just minio-up` first, and check the run does not say `3 skipped`.
