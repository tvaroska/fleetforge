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

**Follow-up**: re-run `just capacity-check-prod` after R0-infra-5 lands to confirm this
projection against live measurements on prod.

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
`agent-image` / `agent-push` / `agent-clean` drive it;
[docs/runbooks/agent-build.md](../runbooks/agent-build.md) is the operator's copy.

Server side: `src/fleetforge/firmware/` loads and verifies the bundles once per app into
`app.state.firmware_catalog`, and `GET /v1/agent/manifest` + `GET /v1/agent/{target}/{part}`
serve them behind the admin credential. `COPY agent/dist /app/agent` bakes them into the
app image; the dev override bind-mounts the working tree instead.

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
