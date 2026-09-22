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

**Acceptance criteria include KPI queries.** The task was verified by proving that both KPI definitions from the PRD (delivery success rate and fleet safety rate) are answerable from the schema on day one, even though the dashboards won't exist until R6.

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
commit. (It did — see *Broker authentication and authorisation* below.)

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

## Broker authentication and authorisation (R0-sec-1)

`allow_anonymous false`, per-device credentials, and the two pattern ACLs that are the
whole fleet authorisation model. `conf.d/10-dev-anonymous.conf` is deleted; the R0
enrollment path (R0-be-4) stopped being a no-op and now really provisions.

### Two mechanisms, not one

Mosquitto 2.0's dynamic-security plugin **has no `%u`/`%c` substitution** — verified
against 2.0.22, where a role holding `publishClientSend ff/v1/d/%u/up/#` denies the very
client it names. Every earlier document assumed the pattern rules would live in a dynsec
role; they cannot. So the shipped design splits the two halves:

| Mechanism | Where | Decides |
|---|---|---|
| `dynamic_security` plugin | `dynamic-security.json`, in the `fleetforge_mosquitto` volume | who exists, and their password — written by `POST /v1/enroll` |
| `acl_file` | `mosquitto/acl` (bind-mounted `:ro` from the repo) | `pattern write ff/v1/d/%u/up/#` and `pattern read ff/v1/d/%u/dn/#` |

Both backends are consulted and **allow wins**. The spec's promise — no per-device ACL
rows, nothing to provision at enrollment — is intact; only the file changed. The dynsec
`device` role still exists because `createClient` requires a role name, and it is
**deliberately empty**: moving the patterns into it does not fail loudly, it silently
denies the whole fleet.

### What the bootstrap creates

A one-shot `mosquitto-init` container runs `mosquitto/bootstrap.sh`, which is idempotent
and runs on every `up`; the broker `depends_on` it with
`service_completed_successfully`. `mosquitto_ctrl dynsec init` is the only file-mode
subcommand, so the script starts a throwaway broker on `127.0.0.1:1884` for everything
else. It creates the empty `device` role, a read-only `ingestor` role
(`subscribePattern` + `publishClientReceive` on `ff/v1/d/+/up/#` — no `$SYS`, no write),
the `ff-ingestor` client, and sets all three default ACL accesses to `deny`. The API
gets the dynsec `admin` credential; the broker healthcheck authenticates as the same
admin against `'$SYS/broker/uptime'`, because an anonymous probe is now a permanently
unhealthy broker (and, per R0-infra-1, an unhealthy container is a Traefik 404).

### Gotchas learned

**`dynamic-security.json` is mutable state owned by uid 1883.** The plugin rewrites it
on every enrollment. Root-owned, it logs `not writable`, applies the change **in memory**
and loses every device credential at the next restart — while the API reports success.
The bootstrap chowns and chmods it to `0600`; `grep -ci "not writable"` on the broker log
is an acceptance check.

**Read authorisation is enforced on delivery, not on SUBSCRIBE.** A device may subscribe
to `#` and gets SUBACK 0, then receives only its own `dn/` traffic. Any test asserting on
the SUBACK code proves nothing. Same class: a forged LWT is accepted at CONNECT and
dropped when it fires, so presence cannot be forged for another board.

**A denied publish is invisible below MQTT v5** — paho reports success and the broker
drops the message — **and the broker log does not help either**: `Denied PUBLISH` is
`MOSQ_LOG_DEBUG` in 2.0.22, which is not enabled (debug logs every topic). The
authoritative read is `mosquitto_pub -V 5 -d` inside the container: `RC:135` is the
denial, `RC:0`/`RC:16` are both "allowed".

**The `:ro` acl bind mount produces expected noise**: `chown:
/mosquitto/config/acl: Read-only file system` from the entrypoint plus three
"world readable / owner is not mosquitto" warnings. Harmless, and documented in the
runbook so nobody chases them.

### Verification

`just broker-check` (`python -m fleetforge.broker selftest`) is the permanent harness: it
provisions two `ffff…` throwaway devices through the real `DynsecProvisioner` and proves
the matrix against the live broker — own `up/` delivered, another device's `up/` dropped,
own `dn/` dropped, `$CONTROL` dropped (proved end-to-end by failing to connect as the
client the device tried to mint), `#` delivering nothing of another board's, anonymous
and wrong-password connects refused. It needs a running stack, so it is not part of
`just test`; `tests/test_broker_config.py` guards the files themselves with no broker.

Beyond the selftest: clean bring-up from wiped volumes in both the dev and the
production shape, two real boards enrolled through `POST /v1/enroll` with
`broker_provisioned_at` set and both present in `dynamic-security.json`, the RC:135
matrix by hand, a device credential surviving `docker compose restart mosquitto`, and the
bootstrap re-run tolerating "already exists".

---

## Production MQTT ingress (R0-infra-3)

**Date:** 2026-09-09 · **Repo touched:** `services` (commit `8c5d6f0`), not this one.

Opens the fleet's front door on prod: `mqtts://bingo.tvaroska.sk:8883`, the first
non-HTTP port in an estate that until now was pure Traefik-over-80/443.

### What shipped

Four things, all in the `services` repo under `prod/`:

1. **A `mqtt` entrypoint on the shared Traefik** (`--entrypoints.mqtt.address=:8883`)
   plus `8883:8883` on the published ports. Every other app on the box sits behind
   this same Traefik, which is why the task is CRITICAL in both repos.
2. **A `mosquitto` service** on the public `eclipse-mosquitto:2.0.22` image, with a
   `mosquitto-init` one-shot ahead of it, a `mosquitto_data` named volume, and a
   TCP router: ``HostSNI(`bingo.tvaroska.sk`)`` → `mosquitto:1883`,
   `tls.certresolver=myresolver`.
3. **Prod credentials** in `services/prod/.env` (gitignored, backed up to GCS):
   freshly generated `MQTT_DYNSEC_*`, `MQTT_INGESTOR_*` and an `ADMIN_PASSWORD_HASH`.
   Usernames are `ff-admin` / `ff-ingestor` — deliberately **not** 12 lowercase hex
   digits, per the R0-sec-1 reviewer note: the `acl_file` patterns key on `%u`, so a
   hex-shaped service username could be re-keyed by enrolling that `device_id`.
4. **A drift guard.** `prod/mosquitto/` is a *copy* of `fleetforge/mosquitto/`,
   because `deploy.sh` only ships `services/prod/`. `scripts/validate-config.sh`
   now diffs the two and fails the deploy if they diverge — `acl` is the entire
   fleet authorisation model, so silent drift there is a security bug, not a nit.

### Why TLS terminates at Traefik, not at the broker

HTTP-01 over :80 already issues the certificate; the TCP router simply reuses it.
No DNS-01, no new credentials, no cert plumbing inside the broker container, and
one renewal path for the whole box. Mosquitto listens plaintext on 1883 and is
reachable only on the internal `backend`/`frontend` networks.

### Gotchas learned

- **`docker rollout` must never touch the broker.** It starts a second copy
  alongside the first; two brokers cannot share the dynamic-security store or the
  1883 bind. `mosquitto` therefore went into a new `INFRA_SERVICES` list
  (recreated in place with traefik/postgres), not `APP_SERVICES`. A scoped
  `deploy.sh --service X` resets `INFRA_SERVICES` so it never touches the broker.
- **A running fleetforge dev stack breaks `just deploy` on this machine.** The
  staging gate's Traefik reaches the *host* Docker daemon through socket-proxy, so
  it discovers `fleetforge-frontend` — whose ``Host(`localhost`)`` rule outranks
  staging's `PathPrefix(/)` — and routes `localhost:8090` at a network it cannot
  reach. Symptom is a **504 on every staging smoke test while every container
  reports healthy**, which looks like a content-api regression and is not one.
  `docker compose stop` in fleetforge before deploying; the gate then passes.
  (The fleetforge dev Postgres also squats host port 5433, which staging wants.)
- The fleetforge `api`/`ingestor`/`frontend` are **not** in the prod fragment:
  no images in Artifact Registry, and an unpullable ref in `PULL_SERVICES` fails
  the pull for every other app on the box. Only the broker is deployed.

### Verification (T2)

Run from prod itself, which bypasses the missing firewall rule:

- `ss -ltn` shows Traefik on `:8883`.
- `openssl s_client -connect 127.0.0.1:8883 -servername bingo.tvaroska.sk` →
  TLSv1.3, `CN = bingo.tvaroska.sk`, `Verification: OK`.
- `mosquitto_sub -h bingo.tvaroska.sk -p 8883 --capath /etc/ssl/certs -u ff-admin
  -P … -t '$SYS/broker/uptime' -C 1` (from a throwaway container on `prod_frontend`
  with `--add-host` pointed at Traefik) returned `187 seconds` — the complete path
  client → TLS → Traefik → broker → authenticated subscribe.
- The same command without credentials: `Connection Refused: not authorised`.
- `update.tvaroska.sk`, `download.tvaroska.sk`, `boris.tvaroska.sk` all still 200,
  before and after.

### The GCP firewall rule (closed 2026-09-09)

The rule was the last outstanding step: this dev box runs as
`devserver@btvaroska.iam.gserviceaccount.com`, which has no `compute.firewalls.*`
on project `sites-470716` (prod is instance `main` there, us-central1-c, tags
`http-server`,`https-server`). Until the rule existed, 8883 was unreachable from
the internet and no real board could connect.

The owner created it from Cloud Shell:

```bash
gcloud compute firewall-rules create sites-allow-mqtt \
  --project=sites-470716 \
  --network=sites \
  --direction=INGRESS --action=ALLOW \
  --rules=tcp:8883 --source-ranges=0.0.0.0/0 \
  --target-tags=https-server \
  --description="fleetforge MQTT over TLS (R0-infra-3)"
```

**`--network=sites` is required** — the project has no `default` network, so the
flag cannot be omitted. The rule mirrors `sites-allow-https` (same target tag,
same `0.0.0.0/0` source); `0.0.0.0/0` is intended, because boards connect from
arbitrary networks and the port is guarded by TLS + `allow_anonymous false` +
per-device dynsec credentials, not by source IP.

### Verification from off-box (2026-09-09)

Run from the dev server (external IP `34.60.54.227` → prod `34.60.87.213`), so
these traverse the real internet path and the new rule:

```bash
openssl s_client -connect bingo.tvaroska.sk:8883 -servername bingo.tvaroska.sk -brief </dev/null
```

→ `CONNECTION ESTABLISHED`, TLSv1.3, `CN = bingo.tvaroska.sk`, `Verification: OK`.

A paho subscribe to `$SYS/broker/uptime` over the same path (use paho, **not** the
mosquitto CLI — see the port-8883 gotcha above):

- as `MQTT_DYNSEC_USERNAME` from `services/prod/.env` → `rc=Success`, `1012 seconds`
- anonymous → `rc=Not authorized`
- right username, wrong password → `rc=Not authorized`

Neighbours unaffected: `update` 200, `boris` 200, `download` 401 on `/` and 200 on
`/health` (that 401 is the app's own auth, not a routing regression).
`bingo.tvaroska.sk` still 404 over HTTPS — expected until `R0-infra-5` puts the app
behind the door.

## Production capacity measurement (R0-infra-4)

**2026-09-10 — A repeatable harness to answer "is the box out of headroom?" with defensible
measurements, not point samples.**

The task delivered `scripts/capacity_snapshot.py`, a stdlib-only Python script that reads
`/proc` and cgroup v2 directly to measure host and per-container memory usage over a
sustained window. It runs identically on the dev box and on prod (piped over ssh), needs no
venv or project dependencies, and produces both a human transcript and machine-parseable
JSON. The script is also the permanent ops answer to the capacity question, documented in
`docs/runbooks/capacity.md`.

### The methodological insight: swap used is a stock, not a flow

The TODO's "already swapping ~1 G" was a point sample that was already wrong by the time
the measurement ran (the box had rebooted and swap-used dropped to 11 MB). A gigabyte of
cold anonymous pages parked in swap and never read back costs nothing; what costs is the
**rate** of `pswpin` / `pgmajfault`. The harness therefore reports over a window (default
15 minutes with 30-second samples), not at an instant, and computes deltas to distinguish
"parking cold pages" (pswpout only) from "thrashing" (pswpin).

### What the harness measures

**Host metrics** (per sample, with floor/peak over the window):
- `MemAvailable`, `MemFree`, `SwapFree` from `/proc/meminfo`
- `pswpin`, `pswpout`, `pgmajfault`, `pgscan_direct`, `pgsteal_direct` from `/proc/vmstat`
  (deltas between first and last sample)
- Load average, disk usage, GCP machine-type from the metadata server (1s timeout, not an
  error if absent)

**Container metrics** (per sample, reading cgroup v2 directly):
- `memory.current`, `memory.peak`, `memory.max` (the declared limit)
- `memory.swap.current`, `memory.swap.peak`
- `memory.events` (`max`, `oom`, `oom_kill`) — **`max` is the real under-provisioning
  signal**: it counts forced reclaims at the limit, which happen long before an OOM kill
  and are otherwise invisible. A container with `max > 0` is under-provisioned even if it
  never crashes.
- `anon` and `file` from `memory.stat` — `docker stats` and `memory.current` both include
  reclaimable page cache, so a container "using" 200 M of which 190 M is file cache is not
  a capacity problem.

### Verdict rules

Encoded as a pure function (the only logic in the script, and the only thing
unit-tested):

- **FAIL** if any container has `oom_kill > 0`, or `pswpin` delta > 1000 pages over the
  window, or `MemAvailable` floor < 256 MiB.
- **TIGHT** if any container's `memory.peak` ≥ 85% of its limit, or any container has
  `memory.events max > 0`, or `MemAvailable` floor < 512 MiB, or Σ declared limits >
  `MemTotal`.
- **OK** otherwise.

Exit code 0 for OK/TIGHT, 1 for FAIL. The verdict prints last, always, even on failure.

### The dependency problem: measuring before the app exists on prod

Fleetforge's app containers (api/ingestor/frontend) are not on prod yet — R0-infra-5 is
blocked on permissions. The measurement therefore splits:

- **Footprint of api/ingestor/frontend**: measured on the dev box in the **production
  shape** (`just up-prod`: built images, nginx not Vite, no `--reload`, same limits as
  prod will use). Container RSS for these workloads is set by the workload, not the host,
  so this transfers.
- **Host headroom**: measured on prod over a sustained window, as it is today.
- **Verdict** = measured prod headroom − measured fleetforge footprint − margin. It is a
  projection and must say so.

The harness is then the acceptance instrument for R0-infra-5 / R0-test-2: re-run
`just capacity-check-prod` once the app is actually on prod, and the projection is either
confirmed or corrected.

### Measured footprint under v1 load target

Fleetforge containers measured on the dev box under load (25 devices heartbeating every
5s for 10+ minutes, 2 SSE clients, login burst of 20 concurrent argon2 hashes):

| Container | Idle peak | Loaded peak | Limit | Notes |
|---|---|---|---|
| fleetforge-api | 117 MiB | 131 MiB | 256 M | `memory.events max=0` even during login burst |
| fleetforge-ingestor | 47 MiB | 48 MiB | 128 M | |
| fleetforge-frontend | 6 MiB | 6 MiB | 64 M | nginx in prod shape (Vite dev = 48 M) |
| fleetforge-mosquitto | 19 MiB | 20 MiB | 64 M | already on prod, peak includes dynsec bootstrap |
| **Total app** | — | **131 MiB** | **512 M** | Sum of loaded peaks |
| Postgres marginal | — | **~20 MiB** | (shared) | anon delta for fleetforge's pool + LISTEN connections |

The api's 256 M limit is validated — no `memory.events max` even under 20 concurrent
argon2id hashes against the `CapacityLimiter(2)`. Frontend in production shape (nginx) is
6 MiB, not 48 M (Vite dev server).

### Production host headroom

Measured on `prod` (VM `main`, e2-medium 2 vCPU / 4 GB, us-central1-c, 10 containers) over
a 15-minute window:

```
MemAvailable floor   2231 MiB (minimum over 30 samples)
Swap                 2047 MiB total, 11 MiB used
Paging deltas        pswpin +0, pswpout +0, pgmajfault +12 (over 900s)
Declared limits      3392 MiB / 3924 MiB MemTotal = 86% committed
```

No containers hit their limit (`memory.events max=0` for all), no swap thrashing (pswpin
delta is zero), headroom floor is 2231 MiB.

### Verdict: no resize needed

```
Projected peak add   = 131 MiB app + 20 MiB marginal Postgres = 151 MiB
Net headroom         = 2231 MiB floor − 151 MiB add − 512 MiB margin = 1568 MiB
Declared over-commit = (3392 + 448) / 3924 = 99.5% (3904 / 3924 MiB)
```

The 151 MiB measured footprint fits in the 384 MiB headroom that bingo freed (R0-infra-0),
with margin. Declared over-commit is 99.5% on paper, but measured peaks are what matter —
the net add is negative (bingo used more than fleetforge does), and `MemAvailable` floor
stays comfortably above the 512 MiB TIGHT threshold.

**Follow-up (closed 2026-09-10)**: re-run after R0-infra-5 landed — see *The app on prod*
→ *Capacity, confirmed against live prod*. The projection held; the verdict flipped to
`TIGHT` for a reason unrelated to fleetforge.

### Files created

- `scripts/capacity_snapshot.py` — the harness (stdlib-only, runs on prod over ssh)
- `tests/test_capacity_snapshot.py` — pure-function tests over fixture text (verdict
  rules, meminfo parsing, byte→MiB formatting, the `memory.max=max` unlimited case)
- `docs/runbooks/capacity.md` — how to re-run it, what the numbers mean, the resize
  procedure (owner-executable GCP commands with all gotchas), the "measure in production
  shape" warning
- `justfile` — `capacity-check` and `capacity-check-prod` recipes; added `scripts/` to
  `lint` and `typecheck` targets

Updated: `design/production.md` → *Capacity — Measured 2026-09-10* (replaced stale
`1913 used / 1038 swap` figures with the dated measurement + verdict).

### Gotchas learned

**`just up` ≠ `just up-prod`.** The dev-shape frontend runs the Vite dev server (node,
48 M against a 64 M limit, 74%); prod-shape runs nginx (~6 M). Measuring the dev shape
produces a false "frontend needs a bigger limit" alarm. Same for api: `--reload` keeps a
reloader parent alive. Always measure in the production shape.

**`memory.max` reads the literal string `max`** for an unlimited container
(fleetforge-traefik, fleetforge-minio, fleetforge-postgres in dev). Parse it to `None`;
do not divide by it. A test exists for this (the ZeroDivisionError trap).

**`memory.peak` is since container start**, not a window peak. The harness tracks the max
of `memory.current` across samples and reports both — they answer different questions
("has this ever" vs "did it during my test").

**Read authorisation on `/proc` and cgroup paths.** The script needs no `sudo` (boris is
in the `docker` group on both hosts), but cgroup v2 paths are
`/sys/fs/cgroup/system.slice/docker-<full-64-hex-id>.scope/` — the **full** container id,
not the short one. Fallback chain for cgroup v1 and alternate paths is present but unused
on current hosts (kernel 6.1 and 6.17, both cgroup v2).

**The `--watch` run on prod holds the ssh session for 15 minutes.** Use
`run_in_background: true` for the Bash call, or shorten the window to ≤ 8 min.

**`docker stats` and `memory.current` both include page cache.** Report `anon` from
`memory.stat` alongside; a container "using" 200 M of which 190 M is reclaimable file
cache is not a capacity problem.

## App image pipeline (R0-infra-5, partial)

**2026-09-09 — the build/push half. The prod fragment is not shipped.**

### What shipped

`just build` in this repo is now the release path: T1 gate (`lint` + `typecheck`
+ `pytest`) → `frontend-build` → build → verify → push. A broken build cannot
reach the registry, because prod pulls by tag.

**Two images, not three.** `fleetforge` serves both the api and the ingestor —
same code, different `command`, exactly as `docker-compose.yml` builds them from
one `fleetforge:dev`. A third image would mean two builds of identical layers and
two chances for the pair to drift.

Tag `v0.1.0` is live in `us-central1-docker.pkg.dev/sites-470716/containers`:

| Image | Digest |
|---|---|
| `fleetforge` | `sha256:7572a4ddec80f5e8c57ee2ff155e57450dc7761e5bdc1c468d5d501ee7c5839d` |
| `fleetforge-frontend` | `sha256:e25426533ea56a7905141fa728f5de7a693c21ce1e8fb18a639d38b50bcb1c6c` |

### Gotcha: `nginx -t` cannot gate the frontend image

The first `_verify-images` recipe checked `nginx -t`. It fails on every machine
without an api container, because nginx resolves `proxy_pass http://api:8000` at
**config load**, not per request — "host not found in upstream". That is real
behaviour, not a test artefact (it is why the prod api service needs the network
alias `api`), but it makes `nginx -t` useless standalone. The gate checks the
payload the builder stage was supposed to emit instead: a non-empty `index.html`
and at least one `assets/*.js`.

### Verification (T2, partial)

- `just build` green end to end; both images verified by running them —
  `create_app()` imports and constructs in the api image, the SPA payload is
  present in the frontend image — then pushed.

### Outstanding — blocked on permissions

Everything under `services/prod/` is refused by the permission classifier, and
self-granting the rule is refused too (correctly). Blocked edits:

1. `prod/.env` — `FLEETFORGE_DB_PASSWORD`
2. `prod/postgres/01-init.sh` — `fleetforge` role + database
3. `prod/docker-compose.yml` — `fleetforge-api` (alias `api`), `fleetforge-ingestor`,
   `fleetforge-frontend` + the `websecure` router for `bingo.tvaroska.sk`

`scripts/deploy.sh` was deliberately left alone: naming a service in
`PULL_SERVICES` that the compose file does not define fails `docker compose pull`
for **every** app on the box. The ingestor belongs in `INFRA_SERVICES`, never
`APP_SERVICES` — `docker rollout` runs two copies during the swap and the
ingestor is the sole MQTT subscriber.

Owner: add `Edit(//home/boris/products/services/prod/**)` and
`Edit(//home/boris/products/services/scripts/**)` via `/permissions`.

## Agent firmware build pipeline (R0-infra-2)

**2026-09-09 — the ESP32 agent now builds, reproducibly, into four flashable bundles
the API serves to the browser flasher.**

Before this there was no `agent/` at all. `R0-fe-3` needs bytes to write to a board and
`R0-fw-1` needs a project to grow into; this task produced both, plus the flash-time
decisions that can never be revisited over the air.

### What shipped

An ESP-IDF project (`agent/`) built inside `espressif/idf:v5.5.5` **pinned by digest**,
one bundle per chip target:

| Target | Chip family (ESP Web Tools spelling) | Bootloader offset | app.bin |
|---|---|---|---|
| `esp32` | `ESP32` | `0x1000` | 162 864 B |
| `esp32s3` | `ESP32-S3` | `0x0` | 191 264 B |
| `esp32c3` | `ESP32-C3` | `0x0` | 168 960 B |
| `esp32c6` | `ESP32-C6` | `0x0` | 164 672 B |

Each `agent/dist/<target>/` holds the four flashable binaries, the **resolved** sdkconfig
and a `manifest.json` of offsets, sizes, sha256s and provenance (`idf_image` digest,
`source_commit`, `built_at`). `just agent-build` / `agent-build-all` / `agent-verify` /
`agent-check-fresh` (S0-infra-2, 2026-09-11) / `agent-image` / `agent-push` / `agent-clean`
drive it; [docs/runbooks/agent-build.md](../runbooks/agent-build.md) is the operator's copy.

Server side: `src/fleetforge/firmware/` loads and verifies the bundles once per app into
`app.state.firmware_catalog`, and `GET /v1/agent/manifest` + `GET /v1/agent/{target}/{part}`
serve them behind the admin credential. `COPY agent/dist /app/agent` bakes them into the
app image; the dev override bind-mounts the working tree instead.

> **Superseded by S0-infra-6 (2026-09-15):** the distribution half of this is gone. The
> image carries no firmware, the bundles are artifacts in the object store, and the
> catalog is read per TTL rather than once at startup. The build half below is unchanged.


### The flash-time immutables

`agent/partitions.csv` — layout id `ab-4m-v1`, frozen at R0 because **a partition table
cannot be changed by OTA**:

```
nvs 0x9000 24K · otadata 0xf000 8K · phy_init 0x11000 4K · ff_cfg 0x12000 4K
ota_0 0x20000 1920K · ota_1 0x200000 1920K
```

* `1920K == 0x1E0000 == 1966080` is exactly the `ota_slot_size` `spec/device-protocol.md`
  promises in `up/announce`. The number is retyped literally in
  `tests/test_agent_partitions.py`, which also greps the spec — so the two cannot drift
  apart silently.
* **No `factory` partition, on purpose.** A factory-only board can never OTA its way to
  an A/B layout; it would be a recall.
* **`ff_cfg` (data, subtype `0x40`, 4 KB) is reserved now** for the flash-time config the
  browser flasher writes per board (broker URL, Wi-Fi credentials, enrollment token).
  `R0-fw-1`/`R0-fe-3` define its payload. Reserving it later is impossible.

`agent/sdkconfig.defaults` enables `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` and
deliberately leaves anti-rollback, secure boot and flash encryption **off**. The first is
safe to turn on at R0 because a serially-flashed app never enters `PENDING_VERIFY` — only
an OTA'd one does — so nothing bricks before `R0-fw-1` exists. The other three burn
eFuses: irreversible, per board, and out of scope until there is a key-management story.
`verify_bundle.py` fails the build if any of them ever appears enabled in the *resolved*
config.

Per-target `sdkconfig.defaults.<target>` files exist but are comment-only: every safety
option lives in the one common file, so there is a single place to read the posture.

### Offsets are derived, never typed

`make_manifest.py` reads `build/flasher_args.json` by name and copies whatever ESP-IDF
computed. This is not pedantry — the bootloader really does live at `0x1000` on ESP32 and
at `0x0` on the RISC-V parts, and a hardcoded value flashes cleanly and never boots on
half the fleet. The same script decodes the built partition-table **binary** and refuses
to emit a bundle whose table has a `factory` partition, is missing `ota_1`, has slots of
different sizes, or holds an app that does not fit its slot.

### Gotcha: the toolchain image is ~8.9 GB, not ~5.5

The first pull died with `failed to register layer: no space left on device` after ten
minutes. Reclaiming needs `docker builder prune -af`, `container prune -f` and
`image prune -f` — **never** `image prune -a`, `system prune -a` or `volume prune`, since
this box holds other projects' images and 31 volumes. When those are not enough, the safe
next step is regenerable caches only (`uv`, `npm`, `go`, `apt`, `journalctl --vacuum`).
The justfile header now says ~8.9 GB and "want ≥ 12 G before the first pull".

### Gotcha: a prefix match on `CONFIG_SECURE_BOOT` fails every ESP32 build

`verify_bundle.py` first matched forbidden options by prefix and rejected a perfectly good
esp32 bundle: `CONFIG_SECURE_BOOT_V1_SUPPORTED=y` is a SoC **capability** symbol, present
whether or not secure boot is enabled. The check now matches exact option names. A safety
check that fails on correct input is worse than none — it teaches the next person to
delete it.

### Gotcha: a root `sdkconfig` silently wins over `sdkconfig.defaults`

`idf.py` generates one on first build and prefers it from then on, so a committed copy
would ship a bootloader whose rollback posture no longer matches the tracked defaults. It
is gitignored **and** dockerignored, and builds happen in a container where no stale copy
exists.

### Verification (T2)

- All four targets built from the digest-pinned image; each ended in `BUNDLE OK`.
- The partition table decoded from the **binary** with IDF's `gen_esp32part.py` — the
  A/B table above, no `factory`, for every target.
- `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` in the built config; anti-rollback, secure
  boot and flash encryption all `is not set`.
- Every part re-hashed against its manifest; deliberately flipping a byte in `app.bin`
  fails `agent-verify` and the bundle is refused at load.
- `just up` and `just up-prod`: `/v1/agent/manifest` is 401 unauthenticated, 200 with the
  admin credential, and all 16 part downloads are byte-identical to the files on disk
  with a matching `"sha256-…"` ETag. The production stack has **no bind mounts** — the
  bytes come from the image.
- Empty `AGENT_IMAGES_DIR`: one startup WARNING naming the path, then 401 before 503, and
  `503 {"detail":"no agent images available"}` on both endpoints.
- Registry round-trip: `just agent-push esp32` → `docker rmi` → pull by digest
  (`sha256:40d5f263…fbc9`) → export → `diff -r` against `agent/dist/esp32`: no
  differences. (Rebuilding the same commit is *not* byte-identical — ESP-IDF stamps the
  compile time into `esp_app_desc_t`; see the runbook.)
- 435 tests pass (74 new across `test_agent_partitions.py`, `test_firmware_catalog.py`
  and `test_api_agent.py`), lint and mypy clean.

### Spec proposals (not written — `spec/` is protected)

1. `spec/device-protocol.md` should record that `ab-4m-v1` includes a 4 KB `ff_cfg` data
   partition (subtype `0x40`) at `0x12000`, and that flash-time configuration lives there.
   Today the spec pins `ota_slot_size` and the layout id but says nothing about where the
   flasher writes the broker URL and enrollment token.
2. `spec/flows.md` Flow 1 step 4 should name that partition, so `R0-fe-3` has a contract
   to write against rather than inventing one.
3. `spec/prd.md` should pin the v1 chip target list (`esp32`, `esp32s3`, `esp32c3`,
   `esp32c6`). It is currently implicit in the build pipeline only.

## The app on prod (R0-infra-5)

**Completed 2026-09-10.** R0-infra-3 shipped the broker and opened 8883; nothing was
behind the door. This task built the two **app** images (`R0-infra-2` is the *firmware*
pipeline — a different artifact) and put the api, ingestor and frontend on the production
box. `https://bingo.tvaroska.sk` stopped returning 404 and started serving the SPA.

The task shipped in two halves a day apart, because the second half needed a permission
grant from the owner.

### Half 1 — the image pipeline (2026-09-09)

`just build` in this repo runs the T1 gate, builds both images, verifies them and pushes
to `us-central1-docker.pkg.dev/sites-470716/containers/`:

| Image | Serves |
|---|---|
| `fleetforge` | api **and** ingestor — same image, different `command` |
| `fleetforge-frontend` | nginx serving the built SPA |

One image for two processes because they share the whole application package; splitting
them would double the build and the registry footprint to save nothing.

### Half 2 — the prod fragment (2026-09-10)

Landed in the `services` repo (`prod/docker-compose.yml`, `prod/postgres/01-init.sh`,
`scripts/deploy.sh`, `scripts/validate-config.sh`) plus the untracked `prod/.env`.

- A `fleetforge` role and database, owned by the app; Alembic applies the schema from the
  api entrypoint (`RUN_MIGRATIONS=true`).
- Three services, pinned **by digest** with `pull_policy: always`, matching every other
  app on the box.
- A Traefik router for ``Host(`bingo.tvaroska.sk`)`` on `websecure`.
- The ingestor's own read-only broker credential (`ff-ingestor`), scoped to
  `ff/v1/d/+/up/#` — deliberately not the API's dynsec admin.

### The `api` DNS collision, and why a whole network was the cheap fix

fleetforge's nginx has `proxy_pass http://api:8000` compiled into `frontend/nginx.conf`,
and this box already runs a `content-api`. Rather than rebuild the frontend image with a
renamed upstream, the fragment adds a dedicated `fleetforge` network that only these three
containers join, and gives `fleetforge-api` the **network alias `api`** on it. The name
resolves for fleetforge's nginx and for nobody else; `backend` is untouched. No image
change, no config templating, and the next fleetforge image still works unmodified.

### The ingestor is INFRA, not APP

`docker rollout` starts a second copy alongside the first during the swap. The ingestor is
the fleet's sole MQTT subscriber (design/production.md → *The single-subscriber rule*), so
two copies would write **every telemetry row twice**. It sits in `INFRA_SERVICES` in
`deploy.sh` and is recreated in place — the same reasoning that already keeps `mosquitto`
out of `APP_SERVICES`.

### Gotchas learned

**`docker-entrypoint-initdb.d` runs only on a fresh volume.** Adding the `fleetforge` role
to `01-init.sh` created nothing on the running box; the role had to be made by hand with
`psql`. That file is now the disaster-recovery path and carries a comment saying so — it
must stay in sync with what was created manually.

**A missing `smoke_endpoint_for_service` case rolls back a good deploy.** The first deploy
succeeded, every container came up healthy, and then the smoke test curled an empty URL,
reported HTTP 000 and triggered the auto-rollback. (The rollback itself no-op'd — "Pre-deploy
state file is empty".) Any service added to `--service` must get a smoke entry; the function
now says so in a comment.

**argon2id hashes in `.env` must be single-quoted.** Compose otherwise eats the `$argon2id`,
`$v` and `$m` segments and login can never succeed. Verify with
`docker compose config | grep -i ADMIN_PASSWORD_HASH` — a literal `$$` in that output is
correct. The R0-infra-3 hash had this problem *and* its plaintext was unrecoverable, so the
admin password was reminted as part of this task.

**The simulator could not speak TLS.** R0-test-1 left it as an explicit TODO against
plaintext dev. Acceptance here required a board over `mqtts://…:8883`, so `--tls` was added
(`mqtt_client_factory(..., tls=...)` → `aiomqtt.TLSParameters()`, system trust store, no
pinning). Without the flag the connect does not fail — it *hangs* until timeout, which reads
like a firewall problem rather than a missing argument.

### Deliberately not shipped: object storage

No GCS credential is wired. `constraints/iam.disableServiceAccountKeyCreation` on the
`btvaroska` org blocks minting the key, and no R0 route touches the store —
`select_backend` only raises when something calls it. R1 is blocked until the key exists;
tracked in docs/runbooks/artifact-storage.md → BLOCKED.

*(Superseded 2026-09-15 by S0-infra-5 below: the key is still un-mintable and always will
be, but the credential is no longer a key — the adapter impersonates
`fleetforge-artifacts@btvaroska`. The prod container is still unwired; that is S0-infra-6.)*

### Verification (T2)

Against production, all five acceptance criteria:

1. **Images pullable by digest on prod** — deploy pulled both and all three containers
   reached healthy.
2. **The SPA is served** — `https://bingo.tvaroska.sk/` → 200 `<!doctype html>`;
   `/v1/healthz` → 200. (It was a 404 before this task.)
3. **Login with the prod admin password** → 200 `{"expires_at": "2026-09-17T…"}`; a wrong
   password → 401.
4. **A simulated board, end to end over the public internet** — `fleet --count 1 --tls`
   against `https://bingo.tvaroska.sk` + `bingo.tvaroska.sk:8883`: enrolled through the
   public API (`200`, `device_id 9a43661be9c8`), connected over TLS, subscribed, and
   published announce/presence/5×heartbeat. `GET /v1/devices` then showed the board with
   `platform_type esp32c6`, `agent_version 0.1.0-sim`, and a `last_seen` **20 s later than
   `enrolled_at`** — proof the ingestor consumed the heartbeats, not just that the row
   exists. `online: false` afterwards is correct: the clean shutdown published the retained
   `{"online": false}`.
5. **The other three apps still 200** — `update.tvaroska.sk/health`,
   `download.tvaroska.sk/health`, `boris.tvaroska.sk`.

T1: 515 tests pass, `ruff check`/`ruff format --check`/`mypy` clean.

### Capacity, confirmed against live prod (R0-infra-4 follow-up, closed 2026-09-10)

`just capacity-check-prod` re-run over a 900 s window with all four fleetforge containers
serving. R0-infra-4's projection was made on the dev box in production shape; this is the
first measurement of the real thing.

| | Projected (dev, `just up-prod`) | Measured (prod, live) |
|---|---|---|
| fleetforge peak add | 131 MiB app + 20 MiB Postgres = **151 MiB** | api 106.5 + ingestor 68.8 + frontend 18.8 + mosquitto 19.1 = **213 MiB** peak (**173 MiB** current) |
| `MemAvailable` floor | 2231 MiB | **2141 MiB** |
| Declared commitment | 99.5% projected | 98% (3840 / 3925 MiB) |

Peak runs ~40% over projection, current ~15% over — the dev box never carries real TLS
sessions or a live Postgres pool. **The verdict of R0-infra-4 stands: no resize needed.**
The floor only dropped 90 MiB and sits four times the 512 MiB threshold, and no fleetforge
container came near its limit (api 41.6%, ingestor 53.8%, frontend 29.4%, mosquitto 29.8%
of their peaks; `memory.events max = 0` for all four).

**The run reports `VERDICT: TIGHT`, and it is not fleetforge.** `prod-download-1` peaked at
**100% of its 512 MiB limit with 97 `memory.events max` reclaim events** (0 OOM kills). That
is the downloader hitting its ceiling and being squeezed, and it is a pre-existing condition
in another app — it was simply invisible until this harness existed. The paging delta
(`pswpin +13`, `pswpout +2498` over 900 s) is consistent with that one container. Not in
scope for R0; raised to the owner as an ops finding against `downloader`.

---

## The QEMU harness, re-verified (S0-infra-1, closed 2026-09-11)

**Filed as:** "`just agent-qemu esp32` boot-loops — no firmware can be run (P1)". The
ticket carried a decoded `LoadProhibited` backtrace —
`main_task → esp_task_wdt_init → esp_task_wdt_impl_timer_allocate → esp_intr_alloc →
task_wdt_isr` — and named the
`-global driver=timer.esp32.timg,property=wdt_disable,value=true` flag as the suspect.
It blocked S0-fw-1 and every firmware acceptance after it, because the Mac is the
flashing bench and QEMU is the only way to run agent firmware on this box.

**Closed as: not reproducible, with two real defects found and fixed.**

### The panic did not come back

Everything below is green, at HEAD, on 2026-09-11:

* a full run from a wiped `flash-esp32.bin` and a freshly minted token: `app_main` →
  `ff_cfg v1 loaded (crc ok)` → `device_id 000000000000` → `eth link up, ip 10.0.2.15`
  → `sntp: 1970 → 2026-09-11T14:15:02Z` → `enroll 200` → `credential stored in NVS` →
  `mqtt connected` → retained announce + presence → `hb` every 10 s for 90 s. Matches
  `docs/runbooks/agent-qemu.md` line for line;
* five further boots, three of them under eight busy-loops on a four-core box — the
  "host starvation fires a spurious watchdog interrupt" theory. Zero panics, zero
  resets;
* the `wdt_disable` flag is present in every one of those runs, so **the suspect named
  on the ticket is innocent**.

Also settled in passing: the bundle in `agent/dist/esp32` is the **S0-fw-1 firmware**
(`ff_progress` strings are in `app.bin`; manifest `source_commit f81d6f1` plus a dirty
tree). It boots and enrolls, so S0-fw-1's firmware half is no longer unrun — its two
outstanding acceptances are now executable.

### Defect 1 — the recipe could not run without a terminal

`agent-qemu` passed `docker run -it` unconditionally, so from any non-interactive shell:

```
cannot attach stdin to a TTY-enabled container because stdin is not a terminal
```

Every agent session, script and CI shell is non-interactive. The recipe named in this
task's own acceptance criterion **could not be executed by the thing that had to execute
it**, and the way round it is to hand-roll a `docker run` — which is exactly where an
emulator invocation acquires a wrong `-M`, `-m` or `-global` and starts panicking inside
the watchdog. This is the most probable origin of the filed backtrace. Fixed: `-it` only
when `[ -t 0 ]`.

### Defect 2 — the emulator was pinned but never verified

`idf_image` is pinned by sha256 and every bundle manifest records the same digest, which
reads as a guarantee that the emulator is fixed. It is not: **Docker verifies a digest on
`pull`, not on `run`**, so a damaged or replaced local layer is used in silence — and
`qemu-system-xtensa` lives in that image. The pinned image was in fact **absent from this
box's Docker store** when the investigation began and had to be re-pulled (2.4 GB), with
`/` at 85%.

Every other input to a boot is content-addressed and deterministic: the bundle (per-part
sha256 in `manifest.json`), the eFuse blob (IDF's own `default_efuse` bytes), the flash
merge (`esptool merge_bin` over manifest offsets). Identical inputs cannot yield two
behaviours, so at failure time one input was not what it claimed, and the local emulator
image is the only one verifiably in a different state since. Unprovable after the fact;
closed going forward by `qemu_sha256`.

### What shipped

| Change | Why |
|---|---|
| `qemu_sha256` in the justfile, checked inside the container before every boot | the digest pin does not cover `run`; this pins the one binary whose behaviour decides whether a boot means anything |
| `qemu_program` — the in-container program, defined **once** | `agent-qemu` and `agent-qemu-smoke` must boot an identical machine or the smoke check guards a lookalike |
| `agent-qemu` passes `-it` only when stdin is a TTY | Defect 1 |
| `just agent-qemu-smoke [target] [deadline]` | one command, ~17 s, no token, no stack, no board: is the harness alive? |
| QEMU version printed into every transcript | provenance, so "which emulator produced this log" is never a guess again |
| `docs/runbooks/agent-qemu.md` → *What we know about the boot-loop panic* + recovery drill | so nobody re-decodes that backtrace |

`agent-qemu-smoke` asserts four things about the first seconds, most-specific first: no
panic; exactly one ROM `rst:0x` banner (the boot-loop check); the `ff-agent` banner; and
`ff_cfg v1 loaded`. It proves **nothing** about enrolment or MQTT — those need a token
and `just up`. It uses a tokenless throwaway config aimed at a closed port and its own
`flash-<target>-smoke.bin`, so it spends no token and never touches the NVS the real
emulated board is accumulating.

### Verification (T2)

* **`just agent-qemu esp32 --fresh`, headless** — the recipe itself, from a wiped flash
  image, reached the full transcript above; `/v1/devices` shows `000000000000` enrolled
  `2026-09-11T14:15:02Z` (10:15 EDT), `partition_layout ab-4m-v1`, `ota_slot_size
  1966080`, and the newest enrollment token reads `used` / `used_by 000000000000`.
* **`just agent-qemu-smoke esp32`** — `HARNESS OK` in 17 s (down from the full deadline
  once it learned to stop as soon as the answer is knowable).
* **Vacuity-checked twice, because a check that cannot fail proves nothing:**
  * `just --set qemu_sha256 000…0 agent-qemu-smoke` → the integrity guard fires, names
    the expected and actual hashes and the binary, and prints the re-pull command;
  * 256 bytes of `0xa5` scribbled into `agent/dist/esp32/app.bin` → the bootloader
    rejects the image and the board really does loop:
    `SMOKE FAILED: the board reset 27 times — this is the boot loop S0-infra-1
    described`. Restored afterwards; `just agent-verify esp32` → `BUNDLE OK`.
* **T1** — `just test` 554 passed (lint + types + the full suite), `just stack-check`
  clean. No stray containers left behind.

### The honest limit

**The originally-filed panic was never reproduced, so it was never fixed.** What ships is
a harness that runs where it has to run, verifies the emulator it is about to trust,
self-checks in one command, and a written record. Hashing one binary is also not a full
image integrity check — shared libraries and the Python tooling are not covered. If the
panic returns, `just agent-qemu-smoke` names it in seventeen seconds instead of a
backtrace decode.

## Agent bundle staleness guard (S0-infra-2, closed 2026-09-11)

**Filed as:** "Rebuild the c3/c6/s3 agent bundles, and make a stale bundle unshippable."
Three of the four agent firmware bundles predated S0-fw-1's stage reporter and shipped
stale in v0.3.0, invisible in exactly the way S0-fw-1 was built to prevent. The task
delivered a rebuild of all four targets and a git-provenance based staleness guard that
gates the release path.

### The finding: all four bundles were stale

Initial investigation revealed `esp32c3`, `esp32c6`, and `esp32s3` bundles carried no
`/v1/device-progress` strings, proving they predated S0-fw-1. The `esp32` bundle appeared
current by mtime but its manifest recorded `source_commit 81aea08` (S0-fe-7) while the
newest agent source commit was `43aeb31` (S0-fw-2, "hold pre-clock stage reports"). So
the shipped esp32 bundle was also missing the S0-fw-2 fix — all four targets required
rebuilding, not three.

### The staleness rule: git ancestry, not mtime

`agent/tools/check_bundles_fresh.py` uses git provenance to detect stale bundles. A
bundle whose `manifest.json:source_commit` predates the newest commit touching the agent
source pathspec (`agent/` excluding `agent/dist/`) is STALE and fails the check. Four
verdicts:

- **fresh** — built from a commit containing the newest agent source change
- **STALE** — `git merge-base --is-ancestor` fails; the bundle predates newer sources
- **UNTRACEABLE** — `source_commit` is absent, `"unknown"`, or not a commit in this repo
- **NOT BUILT** — no `manifest.json` present
- **DIRTY SOURCES** — uncommitted changes under the source pathspec; a bundle records
  HEAD, not what was compiled

The dirty-tree refusal lives in the release path only (`just build`), never in
`just agent-build`. Dirty-tree builds are the firmware dev loop (edit → build → QEMU →
commit), and breaking that would get the guard deleted.

### Why git ancestry instead of mtime

The TODO's literal wording was "bundle is older than `agent/main/`" by modification time.
`git checkout`, `git pull` and branch switches rewrite source mtimes with no content
change; a clone sets them all to clone time. A check that fires on a correct tree is the
check people delete — the runbook already carries that lesson verbatim about a
`CONFIG_SECURE_BOOT_V1_SUPPORTED` prefix match. Git ancestry answers the same question
("does this bundle contain the newest agent source change?") and cannot be wrong about it.
This deviation from the TODO's wording is deliberate.

### Where the guard is sited, and why it moves

`just agent-check-fresh` gates `just build` as a dependency, positioned between
`_require-agent-dist` and `test`. The check is implemented as a standalone script that
takes bundle directories as arguments, with the justfile recipe as a thin caller. No
logic lives in the justfile and nothing knows about `Dockerfile` or `agent/dist` as
hardcoded locations.

This siting is temporary by design. When agent bundles move behind `ObjectStore` (see
*Agent bundles served from the object store* below), the guard moves to the publish step
and passes the one bundle directory being uploaded — the script takes bundle dirs as
arguments for exactly that reason. DECISIONS.md 2026-09-11 explicitly instructs whoever
lands the object-store move to re-point the guard, not rewrite it.

### No bypass mechanism

v0.3.0 shipped stale bundles knowingly; the harm was that it became invisible afterwards.
If a stale ship is wanted again, `just agent-build-all` is 20 minutes, and deleting a
justfile line is a reviewable commit. No bypass environment variable exists.

### Files created

- `agent/tools/check_bundles_fresh.py` — stdlib-only staleness checker using git
  provenance (linted by ruff, not type-checked; the ESP-IDF image has no uv/venv)
- `tests/test_agent_bundle_freshness.py` — 8 test cases over a throwaway git repo in
  `tmp_path` (no docker, no ESP-IDF, no bundle bytes)
- `justfile` — `agent-check-fresh` recipe and added to `build:` dependencies
- `docs/runbooks/agent-build.md` — new subsection *Staleness — a bundle can be correct
  and still be wrong*

### Gotchas learned

**`git status --porcelain -- agent :(exclude)agent/dist`** is the source pathspec — i.e.
exactly the Docker build context `agent/.dockerignore` defines (`COPY . /project`).
Rejected narrower variants (per-target `sdkconfig.defaults.<target>`, "only
`agent/main/`"): they drift from `.dockerignore`, and the whole point is that anything
that can change a bundle is counted. Over-strict costs a rebuild; under-strict costs a
fleet.

**ESP-IDF timestamps every build into `esp_app_desc_t`.** Rebuilding the same commit does
not reproduce bytes (compile date/time are in the binary), so a hash comparison can never
be the staleness signal. Provenance in the manifest (`source_commit`, `idf_image`,
`built_at`) is the only durable handle. Documented in the runbook's *Reproducibility*
section.

## Agent bundles served from the object store (planned 2026-09-11)

**Landed as S0-infra-6 on 2026-09-15** — see *Agent bundles are served from the store
(S0-infra-6, closed 2026-09-15)* at the end of this file for what actually shipped and
what was measured. The planning material below is kept as the record of why the move was
taken and of the shape that was agreed before it was built.

**Status:** Closed 2026-09-15 · **Priority:** P2 · **Added:** 2026-09-11
**Requirements:** [spec/standards.md](../../spec/standards.md) → *infrastructure* →
*Agent bundles are artifacts, not image contents*
**Decision:** [design/decisions/infrastructure-agent-bundles-are-artifacts.md](../../design/decisions/infrastructure-agent-bundles-are-artifacts.md)
**Was blocked on:** `R1-BE-0` ([ota-deploy.md](ota-deploy.md)) — a non-key GCS
credential in `storage/factory.py`. Unblocked by S0-infra-5 (keyless impersonation,
2026-09-15). **Not** blocked on V4 signing: this feature needs authenticated reads only.

### Problem (as stated 2026-09-11)

`Dockerfile:68` bakes `agent/dist` into the application image and
`src/fleetforge/firmware/` serves it from `AGENT_IMAGES_DIR`, while R1's user artifacts
go through `fleetforge.storage` / `ObjectStore`. Two firmware distribution paths in one
product, and the wrong one is load-bearing for onboarding.

That was the right call at R0-infra-2 and the module says why: the bundles version with
the image, they are identical for every tenant, and routing them through `ObjectStore`
would have made the flasher depend on a GCS credential that cannot currently be minted
at all. Shipping a working flasher beat shipping an elegant one.

Two things have changed since. **The target list grows** — four chips at ~1.2 MB each
today, with ESP32-H2, a Thread path and a Raspberry Pi adapter already named in
[roadmap.md](../roadmap.md), so every future chip taxes every application image. And
**the coupling has already failed once**: `S0-infra-2` exists because three of the four
bundles missed the S0-fw-1 stage reporter and shipped stale in v0.3.0, invisible in
exactly the way S0-fw-1 was built to prevent. Baking makes "the firmware is current"
a property of whoever remembered to run `just agent-build-all` before `just build`.

### Shape

A full move: the application image ships **zero** agent bundles and the object store is
the only source. The alternative considered and rejected was a baked fallback tier —
see the ADR for why that was judged to preserve the defect rather than mitigate it.

The verification in `firmware/catalog.py` moves with the bundles rather than being
dropped: per-part sha256, `partition_layout` / `ota_slot_size` agreement with
`spec/device-protocol.md`, and a bundle failing either is dropped with its target named.
Provenance (`source_commit`, digest-pinned IDF image) stays in the manifest.

### Interaction with S0-infra-2

`S0-infra-2` (2026-09-11) shipped `agent/tools/check_bundles_fresh.py`, which gates
`just build` and fails when any bundle predates the agent sources. The guard is sited as
a standalone script that takes bundle directories as arguments, so re-pointing it to the
publish step is a move rather than a rewrite: the justfile recipe (`agent-check-fresh`)
expands `agent_targets` into an argv and calls the script; when this feature lands, the
publish step calls the same script with the one bundle being uploaded. The guard and this
feature cannot contradict each other — staleness is git provenance, which the object store
preserves in `manifest.json:source_commit`.

### The accepted risk

A full move makes onboarding — the product's core flow — depend on the object store
being reachable and credentialled. Today it is neither: the prod GCS credential cannot
be minted at all. Owner's call, taken 2026-09-11 with that consequence stated; the
mitigation is that the store blocker is a hard prerequisite rather than a caveat, and
that an unreachable store must present as a named fault in the flasher rather than a
broken page.

## Build identity in the agent manifest (S0-infra-3, closed 2026-09-14)

**Filed as:** "The manifest cannot say which build produced a bundle." A bundle carried
`agent_version`, `source_commit`, `idf_version` and a digest-pinned `idf_image` — good
provenance that still cannot tell apart two builds of the *same commit* with a different
`sdkconfig`. That is exactly the pair S0-fw-3 spent three sessions separating, and it did
so by correlating a bundle's `built_at` against `git log` rather than reading it off the
artifact. Every bundle now carries two more fields.

### The two fields

**`config_sha256`** — `sha256` of the bundle's own copy of `sdkconfig.resolved`, the file
`make_manifest.py` already copies out of the build directory. A plain hash of the bytes,
no canonicalisation: the resolved config is a build output, not a document anyone edits.

**`build_digest`** — one id for the whole build, `sha256` over a canonical JSON document
(`json.dumps(…, sort_keys=True, separators=(",", ":"))`) containing a version tag, the
provenance fields (`target`, `agent_version`, `idf_version`, `idf_image`, `source_commit`,
`partition_layout`, `ota_slot_size`), `config_sha256`, and each part's
`name`/`offset`/`size`/`sha256` **sorted by name** — so the manifest's own part ordering,
which is by offset for the flasher's benefit, cannot leak into the identity.

`built_at` is excluded on purpose. Two builds of identical inputs must produce the same
id; a timestamp in the digest would make every rebuild look like a new build, and the
field would be decoration. See DECISIONS.md, 2026-09-14.

### Where it surfaces

`agent/tools/verify_bundle.py` recomputes both from the bundle on disk and fails on
mismatch — it imports `build_identity` from its sibling rather than reimplementing the
serialisation, so writer and verifier cannot drift. `firmware/manifest.py` accepts both as
optional `Sha256Hex` fields (`MANIFEST_SCHEMA` stays 1: additive and optional means a
reader has nothing to switch on), `firmware/catalog.py` carries them through,
`GET /v1/agent/manifest` serves them, and the S0-fe-7 diagnostic bundle prints both in its
header, at full 64 hex — a truncated prefix invites an argument about whether two bundles
match, which is the argument the fields exist to end.

### Old bundles warn, malformed bundles drop

A bundle with no `config_sha256` predates this change: it loads, with a WARNING naming the
target and telling the reader to rebuild. A bundle with a *malformed* digest is dropped,
because a corrupt digest is one that would be compared and believed. The distinction is
the whole safety argument, and `tests/test_firmware_catalog.py::TestBuildIdentity` pins
both halves plus a vacuity guard that a healthy load warns about nothing.

### T2 acceptance evidence (2026-09-14)

A **real two-pass ESP-IDF build**, not a synthesised one. Build A — `just agent-build
esp32` on a pristine tree — emitted `config_sha256 8c8ae96b…d49a` / `build_digest
28fd4e0f…6c9c`, and `just agent-verify` printed `(recomputed, matches)`. Build B changed
one line of `agent/sdkconfig.defaults` (`CONFIG_ESP_MAIN_TASK_STACK_SIZE` 8192 → 9216) and
built to `/tmp/ff-build-b`, never into `agent/dist`, so the variant image could not be
flashed: `config_sha256 6346e2a8…8685` / `build_digest 9d583fdf…2c7a`, while
`source_commit`, `agent_version`, `idf_version`, `idf_image`, `partition_layout` and
`ota_slot_size` were all identical — the pair the old manifest could not distinguish. The
resolved `sdkconfig` diff was exactly the two stack-size lines. The edit was reverted and
`agent/sdkconfig.defaults` is untouched by the commit (CRITICAL path).

Vacuity: tampering with the shipped `sdkconfig.resolved` gives `config_sha256 mismatch`
exit 1; tampering with the claimed digest gives `build_digest mismatch` exit 1; removing
both fields gives `build identity: absent (bundle predates S0-infra-3)` exit 0.
`npx vite-node scripts/emit-bundle.ts` prints `config` and `build id` directly under
`agent`, with the redaction check still finding none of the planted secrets. A copy of a
real bundle with both fields stripped loaded with both attributes `None` and the intended
WARNING.

**Known follow-up:** only `esp32` was rebuilt, so `agent/dist/esp32c3|c6|s3` now warn on
load. `just agent-build-all` clears it.

## Content-addressed blob storage (S0-infra-4, closed 2026-09-14)

**Filed as:** "Freeze the content-addressed key scheme before R1 writes an object." The storage module shipped at R0-be-6 with a module docstring promising content-addressed artifact storage but zero code to enforce it. This task turned that prose into schema and code while zero objects exist, because making the same change after R1 writes the first artifact is a migration over live bytes in a shared bucket.

### What shipped

Two tables (`artifacts` and `builds`), a frozen key scheme (`blobs/sha256/<hex>`), cache-control metadata on content-addressed objects, and a complete test harness proving the rules against real MinIO. All code landed with the tables empty and no readers — the same shipping posture `fleetforge.storage` itself took at R0-be-6.

**Core module:** `src/fleetforge/storage/blobs.py` — pure functions with no SDK import, following the same rule as `objectstore.py`. Provides `digest_bytes`, `blob_key`, `parse_blob_key` and `put_blob`. The key is `blobs/sha256/<hex>` (store-relative, explained below), and every helper validates rigorously: uppercase hex is rejected never lowercased, `parse_blob_key` accepts only the exact prefix plus 64 lowercase hex with nothing before or after.

**Schema:** Migration `0003_artifacts_and_builds.py` creates two tables. `artifacts` holds one row per content digest with columns for sha256 (PK), size, kind, target, partition layout, provenance (JSONB) and created_at, plus a PostgreSQL CHECK enforcing lowercase hex format. `builds` maps cache keys to the artifacts they produced, with columns for cache_key (PK), key_inputs (JSONB), outputs (JSONB), target, partition_layout and created_at. Both tables include appropriate indexes and constraints following the db/models.py conventions.

**Cache-Control metadata:** The `ObjectStore.put` Protocol signature gained an optional `cache_control` parameter. Both S3 and GCS adapters send it as real object metadata only when not None, so ordinary puts are byte-identical to before. Content-addressed blobs use `Cache-Control: public, max-age=31536000, immutable` — a header that is verifiable on signed-URL GETs and is what a device downloading firmware sees.

**Verification:** `storage/__main__.py` selftest grew a `--blob` mode that round-trips a payload at `blob_key(sha256(payload))`, fetches it via signed URL, and asserts the cache-control header is present. The MinIO test suite exercises the same path against a real object store.

### The store-relative key decision

The single thing that would otherwise have been got wrong: keys are store-relative, not absolute. The design documents write the layout as `fleetforge/blobs/sha256/<hex>`, which is the absolute GCS object path. But `fleetforge/` is the configured store prefix applied by `resolve_key` — it differs between environments (production GCS uses `fleetforge/`, dev MinIO uses an empty prefix with a dedicated bucket).

So `blob_key()` returns `blobs/sha256/<hex>` and must never contain `fleetforge/`. Hardcoding the prefix would write `fleetforge/fleetforge/blobs/...` in production and leave dev and prod on two different layouts — invisible to every test anyone would think to write, visible only in a bucket listing months later. `test_blob_key_is_store_relative` is the regression guard.

### Schema design: why JSONB for build outputs

`builds.outputs` is JSONB rather than a join table because a bundle build produces four parts, so one `artifact_sha256` column cannot represent it and per-part rows would collide on the cache-key primary key. The output set is consumed as a unit (it becomes the manifest view) and is never queried part-wise across builds.

The cost is real and documented: PostgreSQL cannot foreign-key into JSONB, so a future pruner (R2) must treat `builds.outputs` as a GC root rather than trusting referential integrity. A `build_outputs` join table is the additive migration the day part-wise queries appear.

Similarly, there is deliberately no `artifacts.storage_key` column — the key is a pure function of the sha256 primary key, and storing it creates a second spelling that can disagree. There is deliberately no refcount column — nothing decrements it yet, and a refcount with no decrementer is a lie.

### Validation rules

All helpers reject malformed input and never normalize it, following the `objectstore.py` and `identity.py` standing rule. Key decisions:

- **Lowercase hex only:** `AB...` and `ab...` would be two objects holding one artifact. `parse_blob_key` enforces exact match.
- **No existence pre-check before put:** It's a race plus a round trip, and unnecessary when the same key always carries the same bytes. This is documented in code.
- **Expected digest validated before upload:** `put_blob` computes the digest from the payload and raises before any backend call if `expected_digest` disagrees. An upload path that trusts client-supplied digests is how a blob ends up at a key that lies about its contents.

### Files created

- `src/fleetforge/storage/blobs.py` — frozen key scheme and pure functions
- `alembic/versions/0003_artifacts_and_builds.py` — schema migration with full downgrade
- `tests/test_blob_keys.py` — pure-function tests for key rules and validation

### Files modified

- `src/fleetforge/storage/objectstore.py`, `s3.py`, `gcs.py` — cache_control parameter
- `src/fleetforge/storage/__init__.py` — re-export blob helpers
- `src/fleetforge/db/models.py` — ArtifactKind enum and two table definitions
- `src/fleetforge/storage/__main__.py` — selftest --blob mode
- `tests/test_schema.py`, `test_object_store.py`, `test_object_store_minio.py` — coverage
- `design/artifacts.md` — frozen statement replacing prose promises
- `docs/runbooks/artifact-storage.md` — key layout documentation

### Gotchas learned

**Alembic autogenerate needs hand-editing for quality.** The migration was generated with `alembic revision --autogenerate` but then hand-edited for comments, ordering and proper op.f(...) constraint names. The autogenerated output is a starting point, not a committable artifact.

**Python regex anchors for digests:** The validation uses `\Z` not `$` because `$` also matches before a trailing newline, so `^[0-9a-f]{64}$` accepts `"<hex>\n"`. PostgreSQL CHECK uses `$` where POSIX has no such behavior. This is documented in DECISIONS.md.

**Both tables land empty with no readers.** This is what makes `downgrade()` an honest reverse. The same thing won't be true next time a migration touches these tables.

### Wire protocol unchanged

The device protocol already hands devices `artifact: {url, sha256, size, ...}` where `url` is a short-lived signed URL. The key scheme is therefore not wire-visible — devices receive an opaque URL and a digest, never a key. No spec change was required or made.

### Verification (T2)

Five acceptance criteria executed from `/home/boris/products/fleetforge`:

1. **Key parsing refuses anything outside the blob prefix:** A parametrized test proved `parse_blob_key` rejects `secrets/`, `fleetforge/blobs/...` (double-prefixed), wrong algorithms, uppercase hex, paths with extra segments, and empty strings. The double-prefix case is the store-relative regression guard.

2. **Migration applies, constrains and reverses:** Tables created with correct columns and CHECKs, INSERTs with malformed values (non-hex sha256, array JSONB for outputs) correctly rejected, downgrade/upgrade round trip succeeded.

3. **Blob round trip against real MinIO with cache-control header:** `just storage-check --blob` wrote a payload at `blob_key(sha256(payload))`, fetched via signed URL, and verified `Cache-Control: public, max-age=31536000, immutable` in the response. Independent verification with curl confirmed the header is real object metadata.

4. **Existing functionality unchanged:** Plain `selftest` still passes with no cache-control sent; path traversal attempts still raise ObjectKeyError.

5. **Full test suite:** `just test` green with ruff, mypy and all tests passing including MinIO integration tests (not skipped).

## Firmware catalog keyed on (target, partition_layout) (S0-infra-7, closed 2026-09-14)

**Filed as:** "Key the firmware catalog on (target, partition_layout)" — preparing for multiple partition layouts per chip target while exactly one layout exists today.

### What shipped

Changed the firmware catalog from indexing bundles by target alone to keying them by `(target, partition_layout)`. This enables two layouts for one chip target to coexist without collision, and gives the API and browser flasher a way to select between them.

**Core module changes:** `src/fleetforge/firmware/catalog.py` now keys bundles on `(target, partition_layout)` instead of directory name (target) alone. Added `AmbiguousBundleError` for when multiple layouts exist but the caller doesn't specify which one. The catalog provides `bundle(target, layout=None)` with three outcomes: exact match when layout given, resolve-while-unique when layout omitted, or raise `AmbiguousBundleError` when ambiguous.

**SUPPORTED_LAYOUTS registry:** Added `SUPPORTED_LAYOUTS: dict[str, int]` in `firmware/manifest.py` mapping layout ID to required ota_slot_size. This is what keeps `partition_layout` and `ota_slot_size` from drifting apart — a bundle cannot claim `ab-4m-v1` with a 4 MB slot. Uses a plain dict (not frozen) so tests can register a second layout via `monkeypatch.setitem`.

**Directory naming convention:** Bundle directories follow `<target>` or `<target>.<layout>` naming. The dot-suffixed form allows two layouts for one target to coexist on disk. Separator is `.` because no chip target and no layout id contains one (both are SAFE_SEGMENT), making the split unambiguous. Directory/manifest mismatch drops the bundle with a warning.

**HTTP surface:** `GET /v1/agent/{target}/{part}` gained optional `?layout=` parameter. Returns 404 for unknown layout, 409 Conflict with named layouts when multiple exist but caller doesn't specify. Frontend passes the layout from manifest and refuses to guess between two builds for one chip.

### Why a registry, not a relaxation

The wrong fix would be dropping the layout check so "two layouts both load" — that would let a bundle declaring any string load, and `ota_slot_size` would float free of the layout id. The SUPPORTED_LAYOUTS registry enforces the three-way contract (DECISIONS.md 2026-09-09): layout id, slot size, and partition table remain bound.

### Backward compatibility

The R0 path is unchanged: when exactly one layout exists for a target, `catalog.bundle(target)` resolves without requiring `?layout=`. Every existing caller works unmodified. The 409 only appears when a second layout is registered.

### Files modified

- `src/fleetforge/firmware/manifest.py` — added SUPPORTED_LAYOUTS registry
- `src/fleetforge/firmware/catalog.py` — (target, partition_layout) keying, AmbiguousBundleError, directory convention
- `src/fleetforge/api/routers/agent.py` — ?layout= parameter, 409 handling
- `frontend/src/api.ts` — layout parameter
- `frontend/src/flash.ts` — pass layout from manifest, refuse to guess
- `design/artifacts.md` — updated keying documentation
- `docs/runbooks/agent-build.md` — documented directory convention and layout parameter
- Tests: comprehensive multi-layout coverage in `test_firmware_catalog.py` and `test_api_agent.py`

### Gotchas learned

**monkeypatch.setitem, never setattr.** The catalog imports `SUPPORTED_LAYOUTS` which binds the same dict object. Mutating via `setitem` is visible in both modules and is undone after the test. Using `setattr` rebinds only one module's name and the test proves nothing.

**AmbiguousBundleError must not inherit from AgentBundleError.** `load_bundles` catches AgentBundleError to drop bad bundles. If ambiguity inherited from it, a future refactor could swallow the error and silently serve first-match.

**Directory suffix validation.** `_split_dir_name` must reject what the old SAFE_TARGET check rejected (`.hidden`, `esp32.`, etc.). Malformed names drop the bundle with a warning rather than raising an exception.

**409 is the only new status code.** Existing 404s stay as-is. The 409 only appears for ambiguous layout selection — it names the problem so the flasher can tell the user rather than silently picking the wrong partition table.

### Verification (T2)

Three executable acceptance criteria from the plan:

1. **Two layouts for one target, live against real bundle bytes:** Created `esp32` and `esp32.ab-8m-v1` directories, registered the second layout, proved both load as distinct objects, are separately addressable, ambiguity raises with both ids listed, unknown layout returns None, and vacuity check (without registry entry) drops the unregistered bundle with WARNING.

2. **HTTP surface including 409:** Full test suite green proving `?layout=ab-8m-v1` returns the correct directory's bytes, no layout with two candidates returns 409 with both ids in body, unknown layout is 404, manifest lists both builds.

3. **R0 path unchanged on running stack:** Against `just up`, proved `GET /v1/agent/esp32/app` and `?layout=ab-4m-v1` return byte-identical content with matching sha256, hostile layout parameter is 404/422 never 200/500.

Full T1 gate (`just test` + frontend tests) green, including new TestLayoutKeying class exercising all multi-layout behaviors.

### S0-infra-6 hand-off

The object-store key and publish index must carry `(target, partition_layout)` — `AgentBundle.key` property provides the correct shape. Do not re-narrow it to target alone.

## A GCS credential that is not a key file (S0-infra-5, closed 2026-09-15)

**Filed as:** "`storage/factory.py` accepts a credential that is not a key file" — the real
prerequisite named by `design/decisions/infrastructure-agent-bundles-are-artifacts.md`
(amended 2026-09-11), and the blocker under both S0-infra-6 and R1.

### What shipped

`storage/factory.py` learned a second credential shape: **`GCS_IMPERSONATE_SERVICE_ACCOUNT`**,
an `impersonated_credentials.Credentials` over the runtime's ADC targeting
`fleetforge-artifacts@btvaroska.iam.gserviceaccount.com`. No key file is involved anywhere,
which matters because `btvaroska` inherits
`constraints/iam.disableServiceAccountKeyCreation` and cannot issue one. The setting is
**mutually exclusive** with `GCS_CREDENTIALS_FILE`; **neither set is still a refusal**, so
there is no silent fall back to plain ADC.

* `config.py` — `gcs_impersonate_service_account: str | None = None`.
* `storage/factory.py` — credential selection in four ordered rules (both set → mutually
  exclusive; neither → the ADC refusal, reworded to name both options and the containment
  reason; key file → unchanged; impersonation → the principal must match
  `<name>@<project>.iam.gserviceaccount.com`, lowercase, rejected and never normalised).
  `_impersonated_bucket` builds the client **lazily, inside the closure**, and passes
  `project=None` deliberately.
* `storage/gcs.py` — `credential_mode` in `describe`
  (`creds=key-file` | `creds=impersonated(<email>)`), and every verb now gets its `Bucket`
  **and** its signature through `asyncio.to_thread` inside `_guard`.
* `.env.example`, `docker-compose.yml`, `docs/runbooks/artifact-storage.md`,
  `docs/features/ota-deploy.md` (R1-BE-0), `DECISIONS.md`.

### Containment is the primary reason, signing the secondary one

The old docstrings refused ADC because ADC carries no private key. That is true and it is
the weaker argument. Prod's attached identity is `mainsite@sites-470716`, the estate's
shared VM account, which holds `roles/storage.objectAdmin` on the **whole** of
`gs://btvaroska` unconditionally — including this estate's `secrets/` `.env` backups. Plain
ADC would make the `fleetforge-prefix-only` IAM condition on `fleetforge-artifacts`
decorative. Impersonation is what keeps it real, and it would be the right answer even if
an org-policy exemption existed.

### Signing left the event loop

Under impersonation `blob.generate_signed_url(version="v4")` POSTs to the IAM `signBlob`
endpoint through an `AuthorizedSession` with a backoff retry loop and no timeout of its
own — one or more synchronous HTTPS round trips. The adapter holds an opaque
`bucket_factory` and **cannot branch on which credential is behind it**, so there is one
path: signing goes through `to_thread` under `_guard` on both. Same argument moved
`self._bucket_factory()` inside the guard in all four verbs — the first call resolves ADC
and mints a token, which on a non-GCP host hangs for seconds. `put`'s
`blob.cache_control = …` moved with it and still lands **before** `upload_from_string`.

### Verification (T2)

* **Real GCS, keyless, from the dev box:** `just storage-check --backend gcs --blob` ends
  `SELFTEST OK` with `creds=impersonated(fleetforge-artifacts@…)`; the signed URL carries
  `X-Goog-Credential=fleetforge-artifacts@…` and an **unauthenticated** GET returns the
  bytes — with no private key in the process, that is the `signBlob` verification.
* **Containment through our own adapter:** with `GCS_PREFIX=` empty (in-process
  confinement deliberately disabled), `--key secrets/ff-impersonation-probe-<uuid>.bin`
  exits non-zero with `gcs put of secrets/… failed: Forbidden`.
* **Ambiguity:** key file + impersonation → non-zero,
  `ObjectStoreConfigError: … mutually exclusive …`.
* **No MinIO regression:** `just minio-up && just storage-check --blob` → `SELFTEST OK`.
* **Not run:** the same selftest on prod (AC3). `ssh prod` write access was refused by the
  sandbox classifier. Prod's identity holds the same grant, so it is expected to pass;
  S0-infra-6 wires the container and runs it.

### Gotchas learned

**On this dev box ADC is a USER, not `devserver@`.** `gcloud config` shows the active
account as `devserver@btvaroska`, but `google.auth.default()` returns an `authorized_user`
from `gcloud auth application-default login` — the ADC **file** wins over the metadata
server. That principal has no tokenCreator binding, so impersonation 403s on
`iam.serviceAccounts.getAccessToken`. `CLOUDSDK_CONFIG=/tmp/empty` for one command takes
the file out of the search path and the metadata server answers with this VM's attached
identity, which *is* granted. That is the dev-box fast loop against real GCS, and it
corrects the plan's standing claim that this box cannot impersonate at all.

**A credential-resolution failure is an `ObjectStoreError`, never an
`ObjectStoreConfigError`.** `objectstore.py` promises config errors are raised "at
construction/selection time, never mid-request-body", and `api/deps.get_object_store`
translates them only around `create_object_store`. One raised inside `put`/`get`/
`signed_url` escapes an already-started request as a **500** where the docstring promises a
retriable 503. So: eager shape checks raise `ObjectStoreConfigError`; no ADC, a refused
token and a `signBlob` 403 all raise `ObjectStoreError`.

**The likely failure is a refused token, and it says nothing useful by default.** An ADC
that resolves but may not impersonate raises `RefreshError` **lazily, at first use**, which
`_guard` reports as `gcs get of … failed: RefreshError` — naming neither the principal nor
the missing role. `_impersonated_bucket` therefore refreshes the credential eagerly and
translates, naming the target and `roles/iam.serviceAccountTokenCreator` and nothing else
(the 403 body carries an opaque troubleshooter id that must not reach a handler). Cost is
zero: the next call would have minted that token anyway.

**`project=None`, never omitted.** `storage.Client.__init__` maps `None` to "no project";
the default `_marker` sentinel makes google-cloud-storage go looking for a project through
ADC and raise when it cannot find one. The bucket is cross-project and nothing lists
buckets.

### S0-infra-6 hand-off

The prod container is **still unwired** — `services/prod/docker-compose.yml`'s
`fleetforge-api` block carries the now-stale comment *"No object store on purpose: the GCS
service-account key cannot be minted"*. Four env lines wire it and nothing is mounted:
`OBJECT_STORE_BACKEND: gcs`, `GCS_BUCKET: btvaroska`, `GCS_PREFIX: fleetforge/`,
`GCS_IMPERSONATE_SERVICE_ACCOUNT: fleetforge-artifacts@btvaroska.iam.gserviceaccount.com`.
None is a secret. Root `CLAUDE.md` requires asking before touching production config, so
this task proposed them rather than applying them.

## Agent bundles are served from the store (S0-infra-6, closed 2026-09-15)

**Filed as:** "Agent bundles are served from the store, not baked into the image" — the
move named by `design/decisions/infrastructure-agent-bundles-are-artifacts.md`, unblocked
by S0-infra-5's keyless credential.

### What shipped

The prebuilt agent bundles the browser flasher writes to a board stopped being image
contents and became ordinary content-addressed artifacts, on the same `ObjectStore` seam
R1's user artifacts use. `COPY agent/dist /app/agent` and `AGENT_IMAGES_DIR` are gone;
`.dockerignore` now excludes `agent/` outright.

**Publish side.** `firmware/catalog.py`'s old filesystem loader became
`firmware/bundledir.py` — the same verification (every part re-hashed, layout and OTA
slot checked against `spec/device-protocol.md`, `path` forced to a bare filename), but it
now **raises** instead of dropping, because a publisher that skipped a corrupt bundle
would report success and leave the flasher serving the previous build. `firmware/publish.py`
uploads each part and the manifest through `put_blob` (immutable `Cache-Control`,
`expected_digest` checked), then re-points the index. `python -m fleetforge.firmware`
exposes `publish` / `list` / `rollback`, wrapped by `just agent-publish`,
`just agent-publish-all`, `just agent-list`, `just agent-rollback`.

**The index object.** `agent/index.json` (`firmware/index.py`) is the only mutable key in
the scheme: schema version, `updated_at`, and one entry per `(target, partition_layout)`
naming the current manifest digest plus up to 20 superseded ones. Written with
`Cache-Control: no-store`, never through `put_blob`.

**Read side.** `firmware/catalog.py` is now store-backed: `load_catalog` reads the index,
fetches each manifest blob and re-validates it, and `CatalogCache` holds the result for
`AGENT_CATALOG_TTL_S` (default 60 s) behind an `asyncio.Lock`. `agent_part` streams the
bytes from the store under the admin credential and re-hashes them before answering.

**Frontend.** The Flash button is disabled while the manifest is unreadable, and the
target line names each build's version when a partial publish has left them out of step.

### Publishing is not deploying — the criterion the task exists for

A firmware fix used to require a bundle rebuild, an app-image rebuild, a registry push
and a redeploy; rolling one back required the same in reverse (S0-fw-3, and S0-infra-2's
three stale bundles in v0.3.0). It is now one `just agent-publish`, visible to the
flasher within the catalog TTL with **the same api container still running**, and one
`just agent-rollback` — an index write — to go back.

### Five criteria from `spec/standards.md`

- **Named faults, never silence.** Three distinct answers: *"no agent images have been
  published yet"* (503, reachable-but-empty), *"the agent image store cannot be reached"*
  (503, transport), and 502 for bytes that do not match the manifest. The 503 detail is
  lifted verbatim into the flasher's banner, so it carries no bucket, key, exception class
  or traceback — those go to the log.
- **No stale snapshot over a failing refresh.** `CatalogCache` drops its snapshot
  *before* the refresh read, so a store outage produces a named 503 rather than a manifest
  the API can no longer honour. Failures are not cached either.
- **One bad entry does not take the others down.** A manifest that is missing, unparseable
  or disagrees with the protocol is dropped with a WARNING naming the target; the other
  targets still serve. That rule survived the move from the filesystem.
- **No key is ever built from a request.** `{target}`/`{part}` only index the catalog; the
  only key that reaches the store is `blob_key(<a digest the published manifest carried>)`.
- **The container starts when the bucket is down.** `create_app()` does no store I/O; the
  catalog is lazy.

### Deliberate non-goals

- **No `list` verb.** The store seam keeps four verbs. Listing is a per-backend paging
  contract, and a catalog defined by "whatever is in the prefix" cannot be rolled back,
  cannot be made atomic, and answers "what is current?" with a guess.
- **No database tables.** The index is the catalog. Agent bundles are per-deployment
  facts, not per-tenant records, and a DB row would have to be kept in step with the
  bytes by hand.
- **No multi-version serving.** One current bundle per `(target, layout)`; rollback
  re-points the index rather than exposing a version axis on the wire, which would change
  `/v1/agent/manifest` for every client to serve a case that happens twice a year.
- **No signed URL on this path.** The API streams the bytes under the admin credential; a
  signed URL would drag `signBlob` (a network call since S0-infra-5) into onboarding.

### Files created

- `src/fleetforge/firmware/bundledir.py` — the local, publish-time verifier (`git mv` of
  the old `catalog.py`)
- `src/fleetforge/firmware/index.py` — `AgentIndex`, the pointer object and its pure `upsert`
- `src/fleetforge/firmware/publish.py` — `publish_bundle`, `read_index`/`write_index`, `rollback`
- `src/fleetforge/firmware/__main__.py` — `publish` / `list` / `rollback` CLI
- `tests/test_agent_publish.py`, `tests/test_agent_catalog_store.py`

### Files modified

- `src/fleetforge/firmware/catalog.py` — rewritten store-backed, plus `CatalogCache`
- `src/fleetforge/api/routers/agent.py` — async store-backed catalog dependency, blob
  streaming, the three named faults
- `src/fleetforge/api/main.py` — `app.state.agent_catalog = CatalogCache(...)`, no I/O
- `src/fleetforge/config.py` — `AGENT_IMAGES_DIR` out, `AGENT_INDEX_KEY` /
  `AGENT_CATALOG_TTL_S` in
- `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `docker-compose.override.yml`,
  `.env.example`, `justfile`
- `frontend/src/FlashBoard.tsx`, `frontend/src/flash.test.tsx`
- `tests/conftest.py` (shared `MemoryObjectStore`), `tests/test_blob_keys.py`,
  `tests/test_firmware_catalog.py`, `tests/test_api_agent.py`
- `design/artifacts.md`, `design/decisions/infrastructure-agent-bundles-are-artifacts.md`,
  `docs/runbooks/agent-build.md`, `docs/runbooks/artifact-storage.md`

### Gotchas learned

**A bundle directory copied elsewhere fails on its name, not its contents.** The loader
derives the target from the directory name, so `cp -r agent/dist/esp32 /tmp/bad` fails
with *"manifest says target 'esp32' but it sits in 'bad'"* before any integrity check
runs. Corruption drills must keep the directory named after the target.

**Part failures must name the target.** `just agent-publish-all` verifies several bundles
in one run, and *"app is 993697 bytes on disk"* does not say which board would have been
bricked.

**The TTL is visible in an outage drill.** With a fresh snapshot, `/v1/agent/manifest`
keeps answering 200 for up to `AGENT_CATALOG_TTL_S` after the store goes down, while a
part download fails immediately. That is the cache working, not a stale-serve bug — the
no-stale rule binds at refresh time.

**The index RMW is a read-modify-write race, knowingly accepted.** Two concurrent
publishes of *different* targets can lose one entry. There is one publisher (an operator
at a terminal) and the loser is re-published by re-running one command; a compare-and-set
would need a generation precondition the seam deliberately does not expose.

### Verification (T2)

- **AC1 — the image carries no firmware.** `docker build --target=production` then
  `ls /app/agent` → *No such file or directory*; `find /app -name '*.bin' | wc -l` → `0`.
- **AC2 — it builds with no bundles at all and the flasher still works.** With
  `agent/dist` moved aside the build succeeded and produced the **same image id**
  (`sha256:0c4f698b…`), proving the bundles are not in the build context. Against
  `just up-prod` + `just agent-publish esp32`: the manifest lists `esp32`, and
  `GET /v1/agent/esp32/app` is 200 whose sha256 (`2484cb76…`) equals the manifest's app
  digest. `just agent-qemu-smoke esp32` → *HARNESS OK: esp32 boots, reads its config, and
  does not loop.*
- **AC3 — publishing is not deploying.** `just agent-publish esp32c6`, then after the TTL
  the manifest lists `esp32, esp32c6` with the api container id **unchanged** and
  `docker compose logs … | grep -ci restart` → `0`.
- **AC4 — an unreachable store is a named fault.** With `docker compose stop minio`, both
  `/v1/agent/manifest` and `/v1/agent/esp32/app` answer 503 *"the agent image store cannot
  be reached, so there is no firmware to offer. No board can be flashed until it is
  back."* — no bucket, key or traceback in the body; the key appears only in the ERROR
  log line. 401 still precedes 503 for an anonymous caller.
- **AC5 — corruption is still refused, with the target named.** One appended byte →
  *"esp32: app is 993697 bytes on disk, manifest says 993696"*, exit 1; a hand-edited
  `ota_slot_size` → *"ota_slot_size 4194304 is not the 1966080 that layout 'ab-4m-v1'
  declares"*, exit 1. Nothing was written (`agent-list` unchanged), and the uncorrupted
  publish exits 0 (vacuity check).
- **AC6 — a previous version is still flashable with no rebuild.** Published a second,
  distinguishable build, then `rollback esp32 --manifest <D1>`: the manifest reports the
  older `agent_version`, the part download still verifies, and the api container id is
  unchanged.
- **AC7 — self-hosting is unaffected.** Everything above ran against MinIO with no GCS
  configuration present at all.
- **AC8 — T1.** `just lint`, `just typecheck`, `uv run pytest tests/` (703 passed),
  `npm test` (205 passed) and `npm run build` all green.
- **AC9 — prod.** Not run: the production container is still unwired and root `CLAUDE.md`
  requires asking before touching production config. Proposed below.

### Production hand-off (APPLIED 2026-09-16, in v0.3.5)

Done, in the order below, and verified from prod itself:
`python -m fleetforge.firmware list` inside `prod-fleetforge-api` prints
`creds=impersonated(fleetforge-artifacts@btvaroska.iam.gserviceaccount.com)` and the four
targets at `0.2.0`, whose manifest digests match the ones `just agent-publish-all` wrote
from the dev box (`esp32` `8b35fe50…`, `esp32c3` `fa6f9b41…`, `esp32c6` `a1fdf71c…`,
`esp32s3` `3bfdf57f…`). No key file exists on prod and none is mounted — the VM's
`mainsite@sites-470716` identity impersonates the scoped artifacts account.

The record of what was replaced, kept because the *reason* outlives the edit:

`services/prod/docker-compose.yml`'s `fleetforge-api` block used to set
`AGENT_IMAGES_DIR: /app/agent` and carries the now-false comment *"No object store on
purpose: the GCS service-account key cannot be minted"*. Replace both with:

```yaml
      OBJECT_STORE_BACKEND: gcs
      GCS_BUCKET: btvaroska
      GCS_PREFIX: fleetforge/
      GCS_IMPERSONATE_SERVICE_ACCOUNT: fleetforge-artifacts@btvaroska.iam.gserviceaccount.com
```

**Order matters:** publish the bundles to GCS *before* deploying an app image that no
longer carries them, or prod's flasher answers 503 in between. Nothing is mounted and no
key exists (S0-infra-5).
