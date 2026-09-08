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

mqtt-pub topic message='{}':
    uv run python -c "import os, paho.mqtt.publish as publish; publish.single('{{topic}}', '''{{message}}''', qos=1, hostname='127.0.0.1', port=int(os.environ.get('FF_MQTT_PORT', '8883'))); print('published to {{topic}}')"

mqtt-sub topic='ff/v1/d/+/up/#':
    uv run python -c "import os, paho.mqtt.subscribe as subscribe; subscribe.callback(lambda c, u, m: print(m.topic, m.payload.decode('utf-8', 'replace')), '{{topic}}', qos=1, hostname='127.0.0.1', port=int(os.environ.get('FF_MQTT_PORT', '8883')))"

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
