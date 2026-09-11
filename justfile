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

# ── Capacity (R0-infra-4) ────────────────────────────────────────────────────
#
# Is this box out of headroom? Reads /proc and cgroup v2 directly — no docker
# stats, no credentials, no project venv needed, so the SAME script runs here
# and on prod over ssh. Stdlib only, and it must stay that way (prod has
# python3 3.11 and nothing else).
#
#     just capacity-check                        # one sample, this box
#     just capacity-check --watch 900 --interval 30
#     just capacity-check-prod                   # 15 min window on prod
#
# MEASURE IN THE PRODUCTION SHAPE (`just up-prod`). `just up` runs the Vite dev
# server in the frontend container and reads ~48 M against a 64 M limit; prod
# runs nginx and reads ~6 M.
capacity-check *args:
    uv run python scripts/capacity_snapshot.py {{args}}

# Same script, piped to prod — there is no checkout there.
capacity-check-prod *args:
    ssh prod 'python3 - --watch 900 --interval 30 {{args}}' < scripts/capacity_snapshot.py

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

# `agent/tools/` is linted too: those two scripts decide whether a firmware bundle is
# flashable, and they run inside the ESP-IDF container where nothing else checks them.
# They import stdlib only, on purpose — the IDF image has no uv and no project venv.
# `scripts/` is also linted: capacity_snapshot.py is piped to prod over ssh and must
# run with Python 3.11+ stdlib only, no project dependencies.
lint:
    uv run ruff check src/ tests/ alembic/ agent/tools/ scripts/
    uv run ruff format --check src/ tests/ alembic/ agent/tools/ scripts/

typecheck:
    uv run mypy src/ scripts/

# The T1 gate: lint + types + the full suite against a real Postgres.
test: lint typecheck
    uv run pytest tests/ -x -v

# The frontend's own type check + production build.
frontend-build:
    cd frontend && npm ci && npm run build

# The frontend's own test suite. SEPARATE from `frontend-build` because the vitest
# config lives in frontend/vitest.config.ts, which the frontend container never reads
# (see that file's header) — so `npm run build` cannot and must not run it.
frontend-test:
    cd frontend && npm ci && npm test

# ── Release: app images to Artifact Registry (R0-infra-5) ────────────────────
#
# TWO images, not three. `fleetforge` runs both the api and the ingestor — they
# are the same code with a different command, exactly as docker-compose.yml
# builds them from one `fleetforge:dev`. Splitting them would mean two builds of
# identical layers and two chances for the pair to drift.
#
# The firmware pipeline is a different thing entirely — see R0-infra-2.
#
#     just build                  # build, verify, push both images
#     just build-images           # build only, nothing leaves the machine
#
# `latest_tag` follows the estate convention (content/justfile): the newest git
# tag, or `latest` when there are none yet. Cut a tag before releasing if you
# want the deploy log to say something more useful than `latest`.

registry := env_var_or_default("REGISTRY", "us-central1-docker.pkg.dev/sites-470716/containers")
latest_tag := `git describe --tags --abbrev=0 2>/dev/null || echo "latest"`

# Build, verify and push both app images. Runs the T1 gate first — a broken
# build must not reach the registry, because prod pulls by tag.
#
# `_require-agent-dist` runs before anything is built: the app image bakes
# `agent/dist` in (Dockerfile -> `COPY agent/dist /app/agent`), so an empty
# `agent/dist` ships an api whose `/v1/agent/manifest` answers 503 — and the
# failure surfaces in production as a flasher with nothing to flash.
build: _require-agent-dist test frontend-build frontend-test _build-images _verify-images _push-images
    @echo ""
    @echo "✓ {{ registry }}/fleetforge:{{ latest_tag }}"
    @echo "✓ {{ registry }}/fleetforge-frontend:{{ latest_tag }}"

_build-images:
    @echo "Building images for tag: {{ latest_tag }}"
    DOCKER_BUILDKIT=1 docker build \
        --target=production \
        -t {{ registry }}/fleetforge:{{ latest_tag }} \
        -t {{ registry }}/fleetforge:latest \
        -f Dockerfile .
    DOCKER_BUILDKIT=1 docker build \
        --target=production \
        -t {{ registry }}/fleetforge-frontend:{{ latest_tag }} \
        -t {{ registry }}/fleetforge-frontend:latest \
        frontend

# Prove each image is more than a successful `docker build` before pushing it.
#
# NOT `nginx -t` for the frontend: nginx resolves `proxy_pass http://api:8000`
# at CONFIG LOAD, so the syntax check fails with "host not found in upstream"
# on any machine without an api — including this one. That is real behaviour,
# not a test artefact (it is why the frontend image cannot run alone in prod),
# but it makes `nginx -t` useless as a standalone gate. Check the payload the
# builder stage was supposed to produce instead.
_verify-images:
    @echo ""
    @echo "Verifying images..."
    docker run --rm {{ registry }}/fleetforge:{{ latest_tag }} \
        python -c "from fleetforge.api.main import create_app; create_app(); print('  api image OK')"
    docker run --rm --entrypoint sh {{ registry }}/fleetforge-frontend:{{ latest_tag }} \
        -c 'test -s /usr/share/nginx/html/index.html && ls /usr/share/nginx/html/assets/*.js >/dev/null && echo "  frontend image OK"'

_push-images:
    @echo ""
    @echo "Pushing images..."
    docker push {{ registry }}/fleetforge:{{ latest_tag }}
    docker push {{ registry }}/fleetforge:latest
    docker push {{ registry }}/fleetforge-frontend:{{ latest_tag }}
    docker push {{ registry }}/fleetforge-frontend:latest

# ── Agent firmware: per-target bundles to Artifact Registry (R0-infra-2) ─────
#
# OFF-BOX ONLY. Never run any of this on `prod`: design/production.md → *Capacity*
# — the production VM cannot hold a ~9 GB ESP-IDF image, and building there would
# take the fleet's broker down with it. This is the FIRMWARE pipeline; `just build`
# above is the APP IMAGE pipeline (R0-infra-5). They ship different artifacts.
#
# DISK IS THE #1 FAILURE MODE on this box. `espressif/idf:v5.5.5` unpacks to ~8.9 GB
# and the pull needs headroom on top of that; a pull that runs out of space fails
# with "failed to register layer: no space left on device" after ten minutes.
# Reclaim with exactly these, in this order, and check before pulling:
#
#     docker builder prune -af      # build cache only
#     docker container prune -f     # stopped containers
#     docker image prune -f         # dangling only
#     df -h /                       # want >= 12 G before the first pull
#
# NEVER `docker image prune -a`, `docker system prune -a` or `docker volume prune`
# here: this box hosts other projects' images and 31 volumes, and deleting them is
# not this repo's call.
#
# THE IDF PIN IS A DIGEST and the tag beside it is documentation. Bumping it changes
# firmware behaviour on every board flashed afterwards, so it is a DECISIONS.md
# entry, not a version bump — docs/runbooks/agent-build.md.
#
#     just agent-build esp32        # bundle -> agent/dist/esp32/
#     just agent-build-all          # every target in `agent_targets`
#     just agent-verify esp32       # decode the built table, re-hash every part
#     just agent-push esp32         # same build, pushed as an OCI image

agent_targets := "esp32 esp32s3 esp32c3 esp32c6"
idf_image := "espressif/idf:v5.5.5@sha256:a9231d0697ab8f7517cc072e93b7c83e04907bfbfba80b6440d7dbbf90665cf2"

# sha256 of `qemu-system-xtensa` INSIDE the pinned image (esp_develop_9.2.2_20260417).
#
# The image pin above is not enough on its own: **Docker verifies a digest on `pull`,
# not on `run`**, so a locally damaged or replaced layer is used silently and the pin
# reads as a guarantee it does not give. The emulator is the one file in that image
# whose behaviour decides whether a boot means anything, so it is hashed before every
# run — see docs/runbooks/agent-qemu.md -> *What we know about the boot-loop panic*.
# Bump this only alongside `idf_image`, and record both in DECISIONS.md.
qemu_sha256 := "50813d894756c1448b8742e1d8db21e3cce0f4e85b56fe53e36760b941cb7f17"

# Build one target into agent/dist/<target>/ (bootloader, partition table, otadata,
# app, the resolved sdkconfig and manifest.json with byte offsets + sha256s).
#
# `--output type=local` rather than a bind-mounted `docker run`: BuildKit writes the
# result as the invoking user, and nothing root-owned lands in the repo.
agent-build target="esp32":
    @echo "Building agent firmware for {{ target }} (this takes a few minutes)…"
    mkdir -p agent/dist/{{ target }}
    DOCKER_BUILDKIT=1 docker build \
        --target export \
        --output type=local,dest=agent/dist/{{ target }} \
        --build-arg IDF_IMAGE={{ idf_image }} \
        --build-arg IDF_TARGET={{ target }} \
        --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) \
        agent
    @just agent-verify {{ target }}

agent-build-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for target in {{ agent_targets }}; do
        just agent-build "$target"
        # Each target leaves 150–300 MB of BuildKit cache behind; on this box that
        # is the difference between four targets and two.
        docker builder prune -f >/dev/null
    done

# Prove a bundle is what its manifest says: every part re-hashed, the partition
# table decoded from the BINARY (not read off the CSV), the bootloader posture
# greped out of the RESOLVED config. This is the T2 gate for a firmware build.
agent-verify target="esp32":
    python3 agent/tools/verify_bundle.py agent/dist/{{ target }}
    # The table the bootloader will actually read, decoded from the BINARY by IDF's
    # own tool — not read off partitions.csv, which is the input, not the artifact.
    docker run --rm -v "$PWD/agent/dist/{{ target }}:/d:ro" --entrypoint bash {{ idf_image }} -c \
        '. $IDF_PATH/export.sh >/dev/null 2>&1 && python $IDF_PATH/components/partition_table/gen_esp32part.py /d/partition-table.bin'
    @echo "BUNDLE OK: {{ target }}"

# The pushable artifact: a FROM-scratch OCI image whose entire payload is the
# bundle. Kilobytes in the registry, and a digest to pin in provenance.
agent-image target="esp32":
    DOCKER_BUILDKIT=1 docker build \
        --target export \
        -t {{ registry }}/fleetforge-agent-{{ target }}:{{ latest_tag }} \
        -t {{ registry }}/fleetforge-agent-{{ target }}:latest \
        --build-arg IDF_IMAGE={{ idf_image }} \
        --build-arg IDF_TARGET={{ target }} \
        --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) \
        agent

# NOT a compose service: never add fleetforge-agent-* to PULL_SERVICES or
# deploy.sh in `services` — `docker compose pull` fails as a unit
# (DECISIONS.md 2026-09-09, R0-infra-5).
agent-push target="esp32": (agent-image target)
    docker push {{ registry }}/fleetforge-agent-{{ target }}:{{ latest_tag }}
    docker push {{ registry }}/fleetforge-agent-{{ target }}:latest

# ─────────────────────────────────────────────────────────────────────────────
# Agent firmware in QEMU — a board with no board (R0-fw-1)
# ─────────────────────────────────────────────────────────────────────────────
#
#     just agent-qemu-smoke            # is the harness alive? no token, no stack, no board
#     just agent-cfg --api-base http://10.0.2.2:8080 --mqtt-uri mqtt://10.0.2.2:8883 \
#         --link ethernet --hb 10 --token "$FFE"
#     just agent-qemu esp32            # boot it; Ctrl-A x to quit
#     just agent-qemu esp32 --fresh    # rebuild the image = wipe NVS = forget the credential
#     just agent-qemu-clean            # delete .qemu/ — it holds a live credential
#
# `agent-qemu` works with or without a terminal. It used to pass `docker run -it`
# unconditionally, which meant it died with "cannot attach stdin to a TTY-enabled
# container" in every agent session, script and CI shell — i.e. it could not be run by
# the things that most need to run it, and they worked around it by hand-rolling an
# emulator invocation. S0-infra-1 is what that cost.
#
# Everything lands in `.qemu/` (0700, gitignored): the ff_cfg blob with a live
# single-use enrollment token, and a 4 MB flash image whose NVS holds the broker
# password the emulated board was issued. docs/runbooks/agent-qemu.md.

# Write .qemu/ff_cfg.bin — the 4 KB blob the flasher would write per board.
# Runs in the IDF image so it needs no host Python: the tool is stdlib-only and
# `.qemu` is bind-mounted, so the file lands here owned by the invoking user.
#
# `@` on the docker line is NOT cosmetic: `{{ args }}` carries `--token ffe_…`, and
# an echoed recipe line puts a live single-use token in the terminal scrollback (and
# in whatever CI captured it). The tool itself never prints the token back.
agent-cfg *args:
    @mkdir -p .qemu && chmod 700 .qemu
    @docker run --rm -u $(id -u):$(id -g) \
        -v "$PWD/.qemu:/q" -v "$PWD/agent/tools:/t:ro" \
        --entrypoint python3 {{ idf_image }} /t/ff_cfg.py --out /q/ff_cfg.bin {{ args }}

# The in-container program BOTH QEMU recipes run, defined exactly once.
#
# One definition is the point: if `agent-qemu-smoke` booted a machine assembled
# differently from `agent-qemu`, a green smoke run would prove nothing about the recipe
# it exists to guard. Everything that varies arrives as an environment variable
# ($TARGET, $FLASH, $QEMU_SHA256, $IDF_IMAGE_REF), so the argv itself cannot drift.
#
# It hashes the emulator before using it — see `qemu_sha256` above for why a digest pin
# is not sufficient — and prints the QEMU version, so every transcript carries the
# provenance of the thing that produced it.
#
# No single quotes anywhere below: this whole string is spliced into a `bash -c '…'`.
qemu_program := '''
    set -e
    . $IDF_PATH/export.sh >/dev/null 2>&1
    bin=$(command -v qemu-system-xtensa)
    got=$(sha256sum "$bin" | cut -d" " -f1)
    if [ "$got" != "$QEMU_SHA256" ]; then
        echo "the emulator is not the one this repo pins." >&2
        echo "  expected  $QEMU_SHA256" >&2
        echo "  got       $got" >&2
        echo "  binary    $bin" >&2
        echo "Docker verifies an image digest on PULL, not on RUN, so a damaged or" >&2
        echo "replaced local layer is used in silence. Re-pull the pinned image:" >&2
        echo "  docker rmi $IDF_IMAGE_REF && docker pull $IDF_IMAGE_REF" >&2
        exit 3
    fi
    qemu-system-xtensa --version | head -1
    test -f /q/efuse.bin || python3 /t/qemu_image.py efuse --target "$TARGET" --out /q/efuse.bin
    test -f "$FLASH" || python3 /t/qemu_image.py flash \
        --bundle /d --config "$FFCFG" --out "$FLASH"
    exec qemu-system-xtensa -M esp32 -m 4M \
        -drive file=$FLASH,if=mtd,format=raw \
        -drive file=/q/efuse.bin,if=none,format=raw,id=efuse \
        -global driver=nvram.esp32.efuse,property=drive,value=efuse \
        -global driver=timer.esp32.timg,property=wdt_disable,value=true \
        -nic user,model=open_eth -nographic -serial mon:stdio
'''

# Boot agent/dist/<target> in QEMU with that config.
#
# `--network host` is load-bearing: it is what makes slirp's 10.0.2.2 this dev box,
# so `--api-base http://10.0.2.2:8080` reaches `just up`. The flash image is created
# once and then WRITTEN BACK by QEMU — NVS, and therefore the enrolled credential,
# lives inside it between runs. Pass `--fresh` to rebuild it, which is the same thing
# as handing the board an eraser.
#
# `-it` is passed ONLY when stdin is a terminal. Unconditional `-it` is what made this
# recipe unrunnable from every non-interactive shell (see the section header).
#
# `--name ff-qemu-<target>` is not cosmetic, and neither is the refusal below it. Every
# emulated board has the same all-zero eFuse MAC, so a second one is not a second device
# — it is the SAME device_id enrolling, heartbeating and reporting stages over the top of
# the first (see the runbook's *Every emulated board is `000000000000`*). Two of them make
# every observation on the server ambiguous, and the fleet cannot tell you it is happening.
#
# The obvious cleanup — `docker ps --filter ancestor=espressif/idf:v5.5.5` — does NOT
# match these containers, because `idf_image` is digest-pinned and the ancestor filter
# compares the reference you typed. It exits 0 having killed nothing, so a stray board
# survives every attempt to stop it. A name is the thing that can actually be found.
# S0-fw-1 lost a set of results to exactly this.
agent-qemu target="esp32" fresh="":
    #!/usr/bin/env bash
    set -euo pipefail
    test -f "agent/dist/{{ target }}/manifest.json" || {
        echo "no bundle for {{ target }} — run: just agent-build {{ target }}"; exit 1; }
    test -f .qemu/ff_cfg.bin || {
        echo "no .qemu/ff_cfg.bin — run: just agent-cfg --api-base … --mqtt-uri … --token …"
        exit 1; }
    if [ -n "$(docker ps -q --filter name='^ff-qemu-{{ target }}$')" ]; then
        echo "a {{ target }} board is ALREADY RUNNING as container ff-qemu-{{ target }}."
        echo "Every emulated board claims device_id 000000000000, so a second one would"
        echo "report stages and heartbeats as the same device and make both runs unreadable."
        echo "Stop it first:  just agent-qemu-stop {{ target }}"
        exit 1
    fi
    case "{{ fresh }}" in
        "") ;;
        --fresh) rm -f ".qemu/flash-{{ target }}.bin" ;;
        *) echo "unknown argument '{{ fresh }}' (the only one is --fresh)"; exit 2 ;;
    esac
    tty_flag=()
    if [ -t 0 ]; then
        tty_flag=(-it)
        echo "QEMU: Ctrl-A x quits. NVS persists in .qemu/flash-{{ target }}.bin (--fresh wipes it)."
    else
        echo "QEMU: no terminal, so no Ctrl-A x — stop it with 'just agent-qemu-stop {{ target }}'."
    fi
    docker run --rm "${tty_flag[@]}" --name "ff-qemu-{{ target }}" \
        --network host -u $(id -u):$(id -g) \
        -e TARGET={{ target }} -e FLASH=/q/flash-{{ target }}.bin -e FFCFG=/q/ff_cfg.bin \
        -e QEMU_SHA256={{ qemu_sha256 }} -e IDF_IMAGE_REF={{ idf_image }} \
        -v "$PWD/.qemu:/q" -v "$PWD/agent/dist/{{ target }}:/d:ro" -v "$PWD/agent/tools:/t:ro" \
        --entrypoint bash {{ idf_image }} -c '{{ qemu_program }}'

# Unplug the emulated board. Killing the `just` process does NOT stop it — `docker run`
# without `-it` leaves the container running with the emulator inside, which is the other
# half of how S0-fw-1 ended up with six boards all claiming 000000000000.
#
# It is an ungraceful stop on purpose: that is what makes the broker publish the board's
# LWT, so `/v1/devices` flips `online: false` the way pulling power would.
agent-qemu-stop target="esp32":
    @docker kill "ff-qemu-{{ target }}" >/dev/null 2>&1 && echo "stopped ff-qemu-{{ target }}" \
        || echo "no ff-qemu-{{ target }} running"

# Is the harness alive? One command, no enrollment token, no running stack, no board.
#
# This exists because S0-infra-1 — "the emulator boot-loops" — cost a decoded backtrace
# and a blocked firmware task to investigate, and the answer was reachable in a minute.
# It boots the same machine `agent-qemu` does (see `qemu_program`) and asserts four
# things about the FIRST seconds, which is all a harness check can honestly claim.
# They are checked most-specific first, so the failure you read is a diagnosis:
#
#   1. it did not panic              — no `Guru Meditation` / `LoadProhibited` / `abort()`
#   2. it did not boot-loop          — exactly one ROM `rst:0x` banner
#   3. the app runs at all           — the `ff-agent` banner appears
#   4. it reads its config partition — `ff_cfg v1 loaded`
#
# It does NOT prove enrolment or MQTT: those need a token and `just up`, and belong to
# `agent-qemu` and the runbook transcript. A throwaway config pointing at a dead port
# and its own `flash-<target>-smoke.bin` keep it away from your real board state — it
# spends no token and never touches the NVS `agent-qemu` is accumulating.
agent-qemu-smoke target="esp32" deadline="120":
    #!/usr/bin/env bash
    set -euo pipefail
    test -f "agent/dist/{{ target }}/manifest.json" || {
        echo "no bundle for {{ target }} — run: just agent-build {{ target }}"; exit 1; }
    mkdir -p .qemu && chmod 700 .qemu
    # Tokenless and pointed at a closed port on purpose: the board must get as far as
    # reading its config, and no further for this check to mean what it says.
    test -f .qemu/ff_cfg-smoke.bin || docker run --rm -u $(id -u):$(id -g) \
        -v "$PWD/.qemu:/q" -v "$PWD/agent/tools:/t:ro" \
        --entrypoint python3 {{ idf_image }} /t/ff_cfg.py --out /q/ff_cfg-smoke.bin \
        --api-base http://127.0.0.1:1 --mqtt-uri mqtt://127.0.0.1:1 --link ethernet
    rm -f ".qemu/flash-{{ target }}-smoke.bin"
    log=$(mktemp)
    name="ff-qemu-smoke-$$"
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -f "$log"' EXIT
    echo "smoke: booting {{ target }} (up to {{ deadline }}s)…"
    docker run --rm --name "$name" --network host -u $(id -u):$(id -g) \
        -e TARGET={{ target }} -e FLASH=/q/flash-{{ target }}-smoke.bin -e FFCFG=/q/ff_cfg-smoke.bin \
        -e QEMU_SHA256={{ qemu_sha256 }} -e IDF_IMAGE_REF={{ idf_image }} \
        -v "$PWD/.qemu:/q" -v "$PWD/agent/dist/{{ target }}:/d:ro" -v "$PWD/agent/tools:/t:ro" \
        --entrypoint bash {{ idf_image }} -c '{{ qemu_program }}' > "$log" 2>&1 &
    runner=$!
    # Stop as soon as the answer is knowable rather than burning the whole deadline:
    # wait for the config line, then hold LOOP_WINDOW seconds — long enough for a
    # panicking board to come round again and print a second ROM banner.
    LOOP_WINDOW=10
    for _ in $(seq 1 {{ deadline }}); do
        grep -q "ff-cfg: ff_cfg v1 loaded" "$log" 2>/dev/null && { sleep "$LOOP_WINDOW"; break; }
        kill -0 "$runner" 2>/dev/null || break   # the emulator died on its own
        sleep 1
    done
    docker rm -f "$name" >/dev/null 2>&1 || true
    wait "$runner" 2>/dev/null || true
    fail() { echo; echo "SMOKE FAILED: $1"; echo "--- last 40 lines ---"; tail -40 "$log"; exit 1; }
    # Ordered most-specific first: "it reset 6 times" is a diagnosis, "no banner" is
    # only a symptom, and a looping board satisfies both.
    grep -Eqi "Guru Meditation|LoadProhibited|StoreProhibited|abort\(\) was called" "$log" \
        && fail "the app panicked during boot" || true
    boots=$(grep -c "rst:0x" "$log" || true)
    [ "$boots" -le 1 ] \
        || fail "the board reset $boots times — this is the boot loop S0-infra-1 described"
    grep -q "ff-agent: fleetforge agent" "$log" \
        || fail "the app never reached app_main — no ff-agent banner in {{ deadline }}s"
    grep -q "ff-cfg: ff_cfg v1 loaded" "$log" \
        || fail "the app booted but never read its ff_cfg partition"
    grep -m1 "QEMU emulator version" "$log"
    grep -m1 "ff-agent: fleetforge agent" "$log"
    grep -m1 "ff-cfg: ff_cfg v1 loaded" "$log"
    echo "HARNESS OK: {{ target }} boots, reads its config, and does not loop."

# Delete the QEMU working directory. It holds a LIVE enrollment token (ff_cfg.bin)
# and, inside the flash image's NVS, the broker password that board was issued —
# so this is a credential deletion, not a cache clean.
agent-qemu-clean:
    rm -rf .qemu

# Bundles only. The ESP-IDF image is deliberately kept — re-pulling is 2.4 GB.
agent-clean:
    find agent/dist -mindepth 1 -not -name .gitkeep -delete
    docker builder prune -f

# `just build` bakes agent/dist into the app image; an empty one ships a flasher
# with nothing to flash. Fail here, loudly, rather than in production.
_require-agent-dist:
    @ls agent/dist/*/manifest.json >/dev/null 2>&1 || { \
        echo "agent/dist holds no bundle — the app image would ship an empty flasher."; \
        echo "Run: just agent-build esp32   (see docs/runbooks/agent-build.md)"; \
        exit 1; }
