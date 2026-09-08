# Infrastructure & Production Environment

## Database Schema Foundation (R0-db-1)

The first code committed to the repository established the database schema and project structure. This task bootstrapped the Python project with SQLAlchemy, Alembic migrations, and a comprehensive test harness that validates security-critical invariants.

### Schema design

Five tables form the core data model:

**device_groups** - Optional grouping for enrollment tokens and future bulk deployments. Groups are referenced by nullable foreign keys; an ungrouped device is represented by NULL rather than a magic "default" row.

**devices** - The fleet registry. Uses the device's eFuse MAC (lowercase hex, 12 characters) as the natural primary key because it serves double duty as the MQTT username that ACL patterns depend on. Each device declares its power class (always_on or sleepy), which determines how presence is derived. Sleepy devices must declare an expected wake interval; this is enforced by a table-level CHECK constraint because without it the presence formula becomes undefined. Devices soft-delete via `decommissioned_at` rather than hard deletion to preserve KPI history integrity.

**enrollment_tokens** - Single-use tokens that exchange for per-device MQTT credentials. The burn operation is implemented as a single conditional UPDATE with a WHERE clause that checks unused/unexpired/unrevoked state atomically. This prevents token reuse even under concurrent enrollment attempts. Tokens store only the argon2id hash of the secret; the plaintext is never persisted.

**admin_tokens** - Bearer tokens for dashboard and API access. Use the same argon2id storage and lookup strategy as enrollment tokens. The login cookie holds this token type directly rather than a separate session credential.

**deploy_events** - An append-only event log of every state transition observed on up/status, kept forever as the source of KPI metrics. Each row records the device, timestamp, state, and whether that state is terminal (confirmed/rolled_back/failed). The terminal flag is deliberately redundant with the state vocabulary to keep KPI queries indexed without coupling them to the protocol's state machine. A RESTRICT foreign key to devices prevents accidental destruction of history.

### Key architectural decisions

**No PostgreSQL ENUM types.** The device protocol is forward-compatible - servers must tolerate agents they cannot update. Using PG enums for link_type or deploy state would require a migration before storing a value a future agent invents, turning unknown-value ingests into silent fleet visibility outages. The sole exception is power_class, which gets a CHECK constraint because derived presence is only defined for the known vocabulary.

**Token format enables indexed lookup.** Admin and enrollment tokens follow the wire format `{prefix}_{uuid-hex}.{secret-b64url}` where the UUID is the table's primary key. This is necessary because argon2 hashes are salted and cannot be searched by value - looking up a token by scanning every row and verifying each hash would be O(n) argon2 calls per request. The UUID provides the indexed lookup; only the secret half is verified against the hash.

**Atomic single-use burn.** The enrollment token burn is a single UPDATE statement with a WHERE clause that simultaneously checks the token is unused, not revoked, and not expired, returning the row ID only if all conditions hold. Zero rows returned means already burned. Under PostgreSQL's default READ COMMITTED isolation, the loser of a race re-evaluates the predicate after the winner commits and correctly gets zero rows. This property is proven by a test that runs the burn from two independent connections concurrently.

**Presence is derived, not stored.** For always_on devices, presence comes from the retained up/presence topic. For sleepy devices, a device is considered online if server receipt time minus last_seen is less than 2.5 times the expected wake interval. The schema stores the ingredients (presence_reported, last_seen, expected_wake_interval_s) but no online column - the API derives the answer at read time.

**Deploy events survive device removal.** The PRD requires KPI history be kept forever while devices must remain removable from the dashboard. The schema reconciles this by soft-deleting devices (via decommissioned_at) and placing an ON DELETE RESTRICT foreign key from deploy_events to devices. Hard-deleting a device with history is impossible by construction rather than merely discouraged.

### Project conventions established

Since this was the first code in the repository, the choices made here set conventions that subsequent tasks inherit:

- Package manager: uv with src/fleetforge/ layout
- Python 3.12 with SQLAlchemy 2.0 async and asyncpg driver  
- Alembic migrations with numeric revision IDs (0001, 0002, ...)
- Lint/format/types: ruff (line-length 100) and mypy
- Tests: pytest + pytest-asyncio against a real Postgres database migrated by Alembic
- Docker Compose for local development with Postgres on port 5433 (5432 was already occupied on the dev host)
- Justfile for common tasks: db-up, migrate, lint, typecheck, test

### Files touched

Created the entire Python project structure from scratch including pyproject.toml, uv.lock, docker-compose.yml, justfile, Alembic configuration, source tree under src/fleetforge/, and comprehensive test suite in tests/. Modified README.md to remove "pre-code" status banner and document the new directory structure.

### Gotchas learned

**Alembic env.py must not import application settings.** Importing app config in env.py forces every unrelated setting to validate before `alembic upgrade head` can run. The migration environment reads DATABASE_URL directly from the environment and escapes % characters before passing to configparser.

**Server defaults and autogenerate.** Enabling compare_server_default in Alembic produces permanent false diffs on columns with now() or gen_random_uuid() defaults. The comparison was left disabled.

**Test database over asyncpg, not psycopg.** The test harness creates and drops the test database using an AUTOCOMMIT engine over asyncpg rather than adding a synchronous psycopg dependency just for setup.

**Acceptance criteria include KPI queries.** The task was verified by proving that both KPI definitions from the PRD (delivery success rate and fleet safety rate) are answerable from the schema on day one, even though the dashboards won't exist until R5.

## Bingo Retirement (R0-infra-0)

To prepare the production environment for fleetforge deployment, the unfinished bingo application was fully retired from production on 2026-09-08. This freed critical resources on a memory-constrained host and made the `bingo.tvaroska.sk` domain available for fleetforge.

### What was removed

- **Containers**: `bingo` (backend) and `bingo-frontend` services stopped and removed from prod docker-compose
- **Database**: `bingo_db` database and `bingo_user` role dropped after verified backup to `gs://btvaroska/retired/bingo/`
- **Memory**: Freed 384 MB of declared container limits (host was swapping ~1 GB before retirement)
- **Domain**: `bingo.tvaroska.sk` Traefik route removed, domain ready for fleetforge use
- **Deployment integration**: Removed from deploy scripts, smoke tests, validation scripts, and disaster recovery runbooks

### What was kept

The bingo git repository and its Artifact Registry images (`us-central1-docker.pkg.dev/sites-470716/containers/bingo*`) were intentionally preserved as historical artifacts. Only the production deployment was retired.

### Key gotchas learned

1. **Container removal order matters**: Removing services from docker-compose.yml does not stop running containers. They must be explicitly stopped before syncing new config, otherwise `docker compose stop <service>` can no longer address them by name.

2. **Smoke test configuration**: Deploy script smoke tests must be updated in the same commit that removes services, otherwise the deploy fails its health checks and auto-rolls back.

3. **Database backup before drop**: The database drop is irreversible. A dedicated, verified single-database dump was taken and shipped to GCS before any destructive operations. The nightly cluster backup exists but requires full-cluster restore.

4. **Tracked secrets in git**: Discovered that `prod/bingo.env` was tracked in git despite being listed in `.gitignore` (gitignore does not apply to already-tracked files). The bingo credentials became dead the moment the role was dropped, so removal was safe. Similar issues with other env files were filed as separate security tasks.

5. **Init script inertness**: Postgres init scripts only run on fresh volumes. Removing database creation from init scripts does not drop existing databases - that requires explicit `DROP` commands. Conversely, leaving a `CREATE USER` line with an undefined variable creates a user with an empty password on disaster recovery.

### Files touched

Primary changes in `services/` repository:
- `prod/docker-compose.yml`: Removed bingo service definitions and Traefik labels
- `scripts/deploy.sh`: Removed bingo from service filter, smoke tests, and usage text
- `scripts/validate-config.sh`: Removed bingo environment validation
- `prod/postgres/01-init.sh`: Removed bingo database/user creation
- `prod/.env`: Removed `BINGO_PASSWORD` variable
- `prod/bingo.env`: Removed from git tracking
- `docs/runbooks/disaster-recovery.md`: Removed all bingo references

Secondary changes in `products/` root:
- `justfile`: Removed bingo.env from backup/restore loops
- `boris/main.py`: Removed bingo service health tile from dashboard
- `.claude/skills/release/SKILL.md`: Removed bingo app configuration block

### Production verification

Post-retirement checks confirmed:
- Container count reduced from 12 to 10
- Memory usage reduced, swap pressure decreased
- Traefik route returns 404 for bingo.tvaroska.sk
- Database and role fully removed, other databases unaffected
- Backup verified restorable via `pg_restore -l`
- Full deploy pipeline green with bingo removed
- Other services (boris, content, download) unaffected

The Let's Encrypt certificate for `bingo.tvaroska.sk` was intentionally kept in Traefik's `acme.json` to avoid a fresh ACME challenge when fleetforge reuses the domain.

## Standalone Compose Stack (R0-infra-1)

Turned the single-service `docker-compose.yml` seed from R0-db-1 into the complete
seven-service stack: Traefik, Postgres, MinIO (+ a one-shot `minio-init`), Mosquitto,
api, ingestor and frontend. The stack is simultaneously the everyday dev loop and the
V2 self-hosting artifact the PRD promises; making it the dev environment is the
anti-rot mechanism named in `design/production.md`.

### The two properties the rest of R0 is built on

**One origin, no CORS.** Traefik routes only the frontend. nginx inside the frontend
container serves the SPA and proxies `/v1/*` to `api:8000`; the api publishes no host
port, joins no frontend network and carries no Traefik labels. "No CORS" is therefore
a topology property, not a configuration setting, and a unit test asserts that no
`access-control-allow-*` header can appear.

**MQTT only through Traefik.** Mosquitto publishes no host port. A TCP router on a
dedicated `mqtt` entrypoint (`:8883`) with ``HostSNI(`*`)`` is the only way in, which
is the same shape production will use with `HostSNI(bingo.tvaroska.sk)` plus a cert
resolver. A publish to `ff/v1/d/{id}/up/announce` on host port 8883 arrives at the
ingestor's `ff/v1/d/+/up/#` subscription, exercising entrypoint, router, broker and
subscription in one command.

### The skeletons this task had to ship

The build order puts infra-1 before the API, ingestor and dashboard exist, and a
compose file referencing three services that cannot start is unverifiable. So the
task also shipped the thinnest runnable version of each, with the seams documented in
code:

- **api** — `create_app()`, `GET /v1/healthz` (liveness, no I/O) and `GET /v1/readyz`
  (`SELECT 1`, 503 with a reason rather than a bare 500). Everything under `/v1`,
  including the OpenAPI schema. R0-be-1 adds routers and auth.
- **ingestor** — connect, subscribe to `ff/v1/d/+/up/#` at QoS 1 with a stable client
  id and `clean_session=False`, log topic and payload length (never the body),
  reconnect with capped exponential backoff, clean SIGTERM shutdown, and a heartbeat
  file that serves as the container healthcheck for a process with no HTTP server.
  R0-be-3 replaces the handler.
- **frontend** — Vite + React + TS, one page that fetches `/v1/healthz` on its own
  origin and reports whether `navigator.serial` exists (the R0-fe-3 precondition).

### Infrastructure decisions

**One image, two commands.** A single root Dockerfile with `base` → `development` →
`production` stages. `api` and `ingestor` are the same image with different
`command:` values. The production stage runs as a non-root user that does *not* own
`/app`, so the running process cannot rewrite its own source.

**Migrations run from the api entrypoint** behind `RUN_MIGRATIONS=true`, bingo's
pattern, so the production fragment reuses the identical image and switch. The
ingestor never migrates.

**Broker authz is a directory listing.** `mosquitto.conf` contains no ACL of any
kind; the only line granting unauthenticated access lives alone in
`conf.d/10-dev-anonymous.conf` under a delete-me banner. R0-sec-1 deletes one file
rather than auditing a config, and must update the broker healthcheck in the same
commit.

**Object storage is pre-wired.** The `fleetforge` bucket is created idempotently, and
`S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY` and `S3_SECRET_KEY` are already in the
api's environment, so R0-be-6 is pure code.

### Gotchas learned

**The mosquitto CLI forces TLS on port 8883.** `mosquitto_pub -p 8883` never sends a
plaintext byte — it attempts a TLS handshake, and the broker logs "disconnected due
to protocol error" while the client reports `Error: Protocol error`. Against a
plaintext dev broker on the prod-parity port this looks exactly like a broken TCP
router. Verified by capturing the wire bytes: port 8884 sends a normal MQTT CONNECT,
port 8883 sends nothing. `just mqtt-pub` / `just mqtt-sub` use paho instead.

**Traefik silently skips containers that are not healthy.** A failing healthcheck
presents as a 404 from the entrypoint, with no router in `/api/http/routers` and no
error in Traefik's log. Two probes hit this: `node:22-slim` ships neither `wget` nor
`curl`, and nginx listens on IPv4 only so a probe against `localhost` resolves to
`::1` and is refused. Container healthchecks use `127.0.0.1`.

**`env_file: .env` would break every container.** `.env` holds the host database URL
(`localhost:5433`) that alembic, pytest and `just` need; pydantic-settings gives real
environment variables precedence over `.env` values, so injecting it would point the
API at its own network namespace. Containers get `postgres:5432` explicitly, and the
rule is written into both `.env.example` and the compose header.

### Verification

Clean bring-up from wiped volumes; migrations observed running inside the api
container with all six tables present; `/v1/healthz` and `/v1/readyz` served through
nginx with no CORS header and no Traefik router for the api; the SPA rendering
"Fleetforge — API: ok" in Chrome with zero console errors, `isSecureContext` true and
`navigator.serial` defined; an MQTT publish through Traefik landing in the ingestor's
log; `mosquitto.db` present and the ingestor reconnecting by itself after
`docker compose restart mosquitto` without exiting; the MinIO bucket idempotent; and
the production-shaped stack (`just up-prod`, no override file) reproducing all of it.
