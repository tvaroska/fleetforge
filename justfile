default:
    @just --list

# ── The dev loop ─────────────────────────────────────────────────────────────

# Full stack with hot reload (docker-compose.override.yml is applied automatically).
up:
    docker compose up -d --build
    @echo "dashboard+api: http://localhost:${FF_HTTP_PORT:-8080}  ·  traefik: http://localhost:${FF_TRAEFIK_DASHBOARD_PORT:-8081}  ·  mqtt: localhost:${FF_MQTT_PORT:-8883}  ·  minio: http://localhost:${FF_MINIO_PORT:-9000}"

# The production-shaped stack: built images, nginx, no bind mounts, no --reload.
# Run this before committing — it is what keeps the V2 self-host artifact honest.
up-prod:
    docker compose -f docker-compose.yml up -d --build

down:
    docker compose down

# Also wipes the database, the broker's persisted sessions and the MinIO bucket.
nuke:
    docker compose down -v

logs service="":
    docker compose logs -f --tail=100 {{service}}

rebuild service="":
    docker compose build --no-cache {{service}}
    docker compose up -d {{service}}

# ── Talking to the broker ────────────────────────────────────────────────────
#
# READ THIS BEFORE REACHING FOR mosquitto_pub: the mosquitto CLI clients force
# TLS whenever the port is 8883, with no flag to turn it off. The dev broker is
# plaintext behind Traefik's `mqtt` entrypoint (8883 is TLS in production only),
# so `mosquitto_pub -p 8883` fails with "A TLS error occurred" and looks like a
# broken router. These two recipes use paho — already installed, via aiomqtt.

# EVERY BROKER CLIENT NOW AUTHENTICATES (R0-sec-1) — `allow_anonymous false`.
# `user`/`password` default to $MQTT_DYNSEC_USERNAME/$MQTT_DYNSEC_PASSWORD from the
# environment (`.env`), which is the dynsec ADMIN.
#
# THE ADMIN CANNOT PUBLISH A DEVICE'S up/* — the admin role carries no publish
# grant and the `%u` patterns in mosquitto/acl bind to the *username*, which is the
# device_id. To publish as a board, pass its credential: the username is the
# device_id and the password is the `mqtt_password` that `POST /v1/enroll` returned
# for it (it exists nowhere else). A denied publish is INVISIBLE in MQTT 3.1.1 —
# paho reports success and the broker drops it silently.
#
# The payload travels in the environment, not in the python source: a JSON body is
# full of double quotes, and inside a double-quoted shell word the shell eats them —
# the broker then receives `{proto:1,…}` and the ingestor logs a JSONDecodeError that
# looks like an ingest bug. `retain=1` publishes retained state (announce/presence).
#     just mqtt-pub 'ff/v1/d/a4cf12b3de90/up/hb' '{}' 0 a4cf12b3de90 "$PW_A"
mqtt-pub topic message='{}' retain='0' user='' password='':
    @FF_PUB_TOPIC='{{topic}}' FF_PUB_MESSAGE='{{message}}' FF_PUB_RETAIN='{{retain}}' FF_PUB_USER='{{user}}' FF_PUB_PASSWORD='{{password}}' uv run python -c 'import os, dotenv, paho.mqtt.publish as publish; dotenv.load_dotenv(); t = os.environ["FF_PUB_TOPIC"]; u = os.environ["FF_PUB_USER"] or os.environ["MQTT_DYNSEC_USERNAME"]; p = os.environ["FF_PUB_PASSWORD"] or os.environ["MQTT_DYNSEC_PASSWORD"]; publish.single(t, os.environ["FF_PUB_MESSAGE"], qos=1, retain=os.environ["FF_PUB_RETAIN"] == "1", hostname="127.0.0.1", port=int(os.environ.get("FF_MQTT_PORT", "8883")), auth={"username": u, "password": p}); print("published to " + t + " as " + u)'

# Read authorisation is enforced on DELIVERY, not on SUBSCRIBE: subscribing as a
# device to a filter it may not read still SUBACKs, and simply never delivers.
mqtt-sub topic='ff/v1/d/+/up/#' user='' password='':
    @FF_SUB_TOPIC='{{topic}}' FF_SUB_USER='{{user}}' FF_SUB_PASSWORD='{{password}}' uv run python -c 'import os, dotenv, paho.mqtt.subscribe as subscribe; dotenv.load_dotenv(); u = os.environ["FF_SUB_USER"] or os.environ["MQTT_DYNSEC_USERNAME"]; p = os.environ["FF_SUB_PASSWORD"] or os.environ["MQTT_DYNSEC_PASSWORD"]; subscribe.callback(lambda c, u_, m: print(m.topic, m.payload.decode("utf-8", "replace")), os.environ["FF_SUB_TOPIC"], qos=1, hostname="127.0.0.1", port=int(os.environ.get("FF_MQTT_PORT", "8883")), auth={"username": u, "password": p})'

# The live broker ACL matrix: per-device credentials + the two pattern rules.
# Both the T2 harness for R0-sec-1 and the ops answer to "is broker authz still
# what we think it is?". Needs the stack up (`just up`); prints no password.
# PYTHONPATH=src for the same reason as `admin-password`.
broker-check *args:
    PYTHONPATH=src uv run python -m fleetforge.broker selftest {{args}}

# Cheap syntax gate for both compose files (dev shape and production shape).
stack-check:
    docker compose config -q
    docker compose -f docker-compose.yml config -q

# ── Host workflow (works without the full stack) ─────────────────────────────

# Start the dev Postgres (host port 5433) and wait for it to accept connections.
db-up:
    docker compose up -d postgres
    @until docker compose exec -T postgres pg_isready -U fleetforge >/dev/null 2>&1; do sleep 1; done

db-down:
    docker compose down

migrate:
    uv run alembic upgrade head

# Hash an admin password for ADMIN_PASSWORD_HASH (prompts twice, never echoes).
# Paste the printed SINGLE-QUOTED line into .env: compose eats the `$` segments
# of an unquoted argon2 PHC string, and the login then can never succeed.
# PYTHONPATH=src because the project is deliberately not installed as a package
# (see the Dockerfile header) — same reason pytest sets `pythonpath`.
admin-password:
    PYTHONPATH=src uv run python -m fleetforge.auth hash-password

# MinIO on its own. `just up` starts it too; this is the short path for the
# object-store tests and `just storage-check`, which need nothing else running.
minio-up:
    docker compose up -d minio minio-init
    @until curl -fsS http://localhost:${FF_MINIO_PORT:-9000}/minio/health/live >/dev/null 2>&1; do sleep 1; done

# Round-trip the configured object store: put / get / signed_url / delete.
# PYTHONPATH=src for the same reason as `admin-password` — the project is
# deliberately not installed as a package. Prints the bucket, never a credential.
#     just storage-check                       # the backend the environment selects
#     just storage-check --backend gcs         # force one
#     just storage-check --ttl 5 --keep        # a short-lived URL, object left behind
storage-check *args:
    PYTHONPATH=src uv run python -m fleetforge.storage selftest {{args}}

# ── Simulated boards (R0-test-1) ─────────────────────────────────────────────
#
# A fake ESP32 that enrolls, connects, announces, holds presence and heartbeats,
# so R0-be-2/3/4/5 and the dashboard can be exercised before R0-fw-1 exists.
# Needs the stack up (`just up`). PYTHONPATH=src for the same reason as
# `admin-password` — the project is deliberately not installed as a package.
#
# THE FIRST RUN NEEDS AN ENROLLMENT TOKEN and burns it; every later run reuses
# the credential in `.sim/` (gitignored, 0600) and enrolls nothing. That file is
# the ONLY copy of the board's `mqtt_password`: `POST /v1/enroll` returns it once
# and it exists nowhere else — not in Postgres, not in a log, not in dynsec.
# Delete one and that board needs a fresh token (`--forget` does it deliberately).
#
#     TOKEN=$(curl -sS -X POST localhost:8080/v1/enrollment-tokens \
#             -H "Authorization: Bearer $FFA" -H 'content-type: application/json' \
#             -d '{}' | jq -r .token)
#     just sim --token "$TOKEN" --name blinker --heartbeat-interval 5
#     just sim --name blinker --duration 30          # reuses .sim/, no token needed
#     just sim --name blinker --crash-after 15       # ungraceful: the broker's LWT fires
#     just sim --token "$TOKEN" --name sleeper --power-class sleepy --wake-interval 20
sim *args:
    PYTHONPATH=src uv run python -m fleetforge.simulator run {{args}}

# N boards at once, issuing their own single-use tokens (so needs the admin
# PASSWORD — --admin-password or $FF_ADMIN_PASSWORD — never the hash). Prints
# token ids, never token plaintexts. Names are sim-01, sim-02, …
#     just sim-fleet 5 --heartbeat-interval 5 --duration 120
sim-fleet count='3' *args='':
    PYTHONPATH=src uv run python -m fleetforge.simulator fleet --count {{count}} {{args}}

lint:
    uv run ruff check src/ tests/ alembic/
    uv run ruff format --check src/ tests/ alembic/

typecheck:
    uv run mypy src/

# The T1 gate: lint + types + the full suite against a real Postgres.
test: lint typecheck
    uv run pytest tests/ -x -v

# The frontend's own type check + production build.
frontend-build:
    cd frontend && npm ci && npm run build
