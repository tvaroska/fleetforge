# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-09-08 — Object store: one Protocol, two adapters, and the prefix is a security boundary (R0-be-6)

- **One `ObjectStore` Protocol, two real adapters, selected by configuration** — the same
  shape as `fleetforge.broker` (`BrokerProvisioner` / Null / Dynsec). MinIO (S3) in dev
  and for V2 self-hosting, GCS in production. Both SDKs are imported **lazily inside the
  factory**, so neither is on the API's import path and an unconfigured deployment pays
  nothing. Four verbs only — `put`, `get`, `signed_url`, `delete`. **No `list`**: nothing
  in R1 needs it, and it is the one verb an IAM prefix condition cannot constrain (see
  below), so adding it would silently widen the grant.
- **`GCS_PREFIX` is a security boundary, not tidiness.** `gs://btvaroska` is *shared* —
  it holds this estate's `.env` backups under `secrets/`, plus the boris podcast audio.
  Object keys arrive from an HTTP request body (R1's upload), so the prefix is confined
  **twice and independently**: `resolve_key()` in-process, and an IAM condition on the
  service account (`resource.name.startsWith(".../objects/fleetforge/")`). Either alone
  is one bug away from writing into `secrets/`.
- **`resolve_key()` rejects, never normalises** — the rule `identity.py` already
  established for device IDs. `..`, a leading `/`, `//`, backslashes, control or
  non-ASCII bytes, `?`/`#`, over 512 chars: all `ObjectKeyError`, which is a `ValueError`
  and deliberately **not** an `ObjectStoreError`, because a bad key is a 400 (the caller
  is wrong) while everything else is a 404 or a 503 (we are). Path normalisation is how
  traversal bugs get written: `a/../../b` has an obvious "sane" reading, and acting on it
  is exactly the mistake.
- **Two S3 endpoints, because a presigned URL signs the `Host` header.**
  `S3_ENDPOINT_URL` (`minio:9000`) is what the API talks to; `S3_PUBLIC_ENDPOINT_URL`
  (`localhost:9000`) is what URLs are *signed against*, because the device is not on the
  compose network. Rewriting the host after signing invalidates the signature — there is
  no post-hoc fix, so the split has to exist at signing time. The container selftest
  therefore cannot fetch the URL it prints, and says so instead of failing.
- **Both backends configured is an error, not a precedence rule.** "Which bucket did my
  firmware go to?" must not be answered by reading a factory. Unset one or set
  `OBJECT_STORE_BACKEND`. Likewise **no ADC fallback for GCS**: ADC on a GCE VM carries
  no private key (so no V4 signing) and resolves to the project-wide compute default SA —
  the exact credential the prefix condition exists to avoid. Missing credentials fail
  loudly at construction.
- **Unconfigured is a WARNING plus a 503, never a startup crash.** `create_app()` stays
  constructible with no environment at all (the R0-be-1/R0-be-4 precedent); artifact
  routes will answer 503 until storage is configured.
- **GCS has never been round-tripped against the real service.** `btvaroska` inherits
  `constraints/iam.disableServiceAccountKeyCreation`, so the key the adapter requires
  cannot be minted, and the keyless alternative needs an IAM grant this task was not
  authorised to make. The SA and its conditional binding exist; the credential does not.
  **Do not read a green dev stack as evidence that production storage works** — the
  options (impersonation + `signBlob`, or a policy exemption) are written up in
  `docs/runbooks/artifact-storage.md`, and one of them is a prerequisite for R1.

## 2026-09-08 — SSE: one listener per worker, and a reconnect ends every stream (R0-be-5)

- **One dedicated asyncpg connection per API process, never a pooled one.** `LISTEN`
  only delivers to a backend that is between transactions, and `pool_pre_ping`/recycle
  would drop the registration with nothing in the log — the symptom is a stream that
  connects and stays empty forever. `db/base.asyncpg_dsn()` converts the SQLAlchemy
  URL; the connection sets `application_name = 'fleetforge-events'` so
  `pg_stat_activity` answers "is anything listening?" without reading code. This is the
  one documented exception to "`get_sessionmaker()` is the only door into the database
  from the API".
- **A listener reconnect closes every SSE stream.** The hub cannot know what was missed
  while the connection was down, and a client that keeps reading after a gap silently
  shows a stale fleet. Ending the stream makes `EventSource` reconnect and re-read
  `GET /v1/devices`, which is the same self-healing path as the slow-client case. That
  is also why there is no "resync" event type.
- **A slow client is disconnected, not buffered.** Bounded per-client queues
  (`sse_queue_size`); on overflow the queue is drained and a sentinel ends that one
  stream. Dropping individual events instead would leave a client silently wrong, and
  unbounded buffering is a memory leak in a 256 M container.
- **The NOTIFY payload is validated and then forwarded *verbatim*.** `fw_version` comes
  off the wire from a board, and SSE framing is newline-delimited: a payload containing
  a raw newline would let a device inject a forged event into the operator's stream. It
  cannot happen today (`model_dump_json` escapes control characters), which is why it is
  asserted rather than assumed. Forwarding the original rather than a re-serialization
  keeps the additive-evolution rule — re-serializing would strip fields a newer ingestor
  adds.
- **Auth is checked once, at connect, so a stream is capped at 15 min**
  (`sse_max_stream_s`). Instant revocation is the reason JWT was rejected (R0-be-1), and
  an unbounded stream would quietly outlive a revoked token. The cap is deliberately
  under nginx's `proxy_read_timeout 3600s`. Browser `EventSource` cannot send an
  `Authorization` header at all — the stream authenticates on the `ff_session` cookie,
  which is R0-be-1's "one credential, two transports" paying for itself. **A token in
  the query string was rejected:** nginx's access-log format logs `$request`.
- **`GET /v1/devices` shipped here, not in R0-fe-2.** `events.py` and `presence.py` both
  already define the contract as "the event is a hint; re-read `GET /v1/devices`", and
  no task owned that endpoint — an SSE stream whose documented contract is "go read an
  endpoint that 404s" is not a finished artifact. Presence is computed on read via
  `presence.is_online`, with one `now` for the whole response; `presence_reported` is
  deliberately not exposed, so no client can re-derive the rule.
- **Gotcha, and it will bite the next streaming endpoint too:** `httpx`'s
  `ASGITransport` buffers the entire response body before returning, so
  `client.stream()` against an endless SSE generator hangs the whole suite. The tests
  drive the ASGI app directly (`tests/test_events_stream.py::drive_sse`); only responses
  that never stream (401, 503) go through the normal client. Related: Starlette
  *cancels* the generator on disconnect for ASGI spec_version < 2.4 (uvicorn reports
  2.3), so the subscription is released in a `finally:` inside the generator, not after
  it — anywhere else leaks one subscriber per page reload.
- **`asyncpg.InterfaceError` is caught alongside `PostgresError`/`OSError`** in the
  listener's reconnect loop. asyncpg raises it for "connection is closed", which the
  keepalive `SELECT 1` hits when the socket died between two ticks; letting it escape
  would kill the listener task for the life of the process — the exact silent failure
  this module exists to prevent, with nothing unhealthy anywhere. asyncpg also ships no
  `py.typed`, so it gets one `ignore_missing_imports` override in `pyproject.toml`
  rather than a `# type: ignore` at every call site.
- **No Redis and no broadcaster abstraction.** One backend, and the "no Redis" decision
  is already recorded under *The ingestor is the only MQTT subscriber*. There is no
  second implementation of this boundary and none is planned, so no adapter pair.

---

## 2026-09-08 — Enrollment: commit, then provision; and the grace window (R0-be-4)

- **Verify the token secret before calling `BURN_SQL`.** The statement keys on `id`
  alone, and an `ffe_` token's id is not a secret — it is in the issuance response and
  in the api log. Burning before `averify_secret` would let anyone who has read a log
  line destroy every outstanding token: a bench full of boards that will not enroll,
  with the dashboard reporting them `used` and nothing failing loudly. Order is
  `require_admin`'s: parse → row → `dummy_verify` on a miss → verify → burn.
- **The device row is INSERTed before the burn, in the same transaction**, because
  `enrollment_tokens.used_by_device_id` is a real FK. A refused burn rolls both back.
- **The transaction commits BEFORE the broker is provisioned.** `db/models.py::Device`
  put `broker_provisioned_at` in the schema "so provisioning can be reconciled and
  retried idempotently after a partial enrollment" — the schema already chose this.
  Holding a row lock and a pooled connection across an MQTT round-trip turns a broker
  outage into `idle in transaction` on a 256 M container. The inverse failure —
  a broker credential for a device that is not enrolled — is prevented by the order,
  not by a transaction.
- **A burned token may be re-presented by the SAME `device_id` for 600 s**
  (`config.enroll_retry_window_s`) and gets a freshly provisioned password. The device
  writes NVS only after it reads the response body, so a dropped packet on first boot
  otherwise leaves a board that is enrolled and has no credential, holding a token that
  can never burn again — a re-flash, in the field. Single use is intact: the lookup
  matches on `used_by_device_id`, so one token still enrolls exactly one board forever,
  and `FOR UPDATE` keeps a concurrent revoke from racing it. **PROPOSED for
  `spec/prd.md` → *Security & data posture*** (protected, so not written there): state
  the grace window next to "cannot be replayed from a recovered board".
- **`mqtt_username` is `device_id`, unnormalised.** The `%u` pattern ACLs are the entire
  fleet authz, so the eFuse-MAC format check runs before any credential exists and a
  non-canonical `device_id` is rejected rather than lowercased. `DEVICE_ID_RE` moved to
  `fleetforge/identity.py` — the API must not import from `fleetforge.ingestor`, same
  precedent as `clock.py` leaving `api/deps.py`.
- **`NullProvisioner` leaves `broker_provisioned_at` NULL on purpose.** The dev broker
  is anonymous until `R0-sec-1`, and `WHERE broker_provisioned_at IS NULL` is then the
  honest reconcile list rather than a column that lies. Selection is by the presence of
  `MQTT_DYNSEC_USERNAME`/`_PASSWORD`, with a startup WARNING — the same shape as
  `ADMIN_PASSWORD_HASH`.
- **Dynsec gotchas, all verified against Mosquitto 2.0.22's protocol:** responses come
  back only to the issuing client on `$CONTROL/dynamic-security/v1/response`, so
  subscribe before publishing; an error is a *key in the response body*, not a transport
  failure; `correlationData` is echoed and must be matched, or a stale reply from a
  timed-out command is read as this one's success; the dynsec client id carries a random
  suffix, because two API workers sharing one kick each other off mid-command and the
  symptom is an intermittent 503. `clientid` is deliberately not bound to the credential:
  `spec/device-protocol.md` does not specify the agent's client id and `R0-fw-1` is
  unwritten. **PROPOSED for `spec/device-protocol.md`**: state that the agent connects
  with `client_id = device_id`, which would make that binding free hardening later.
- **`ingestor/store.py` finally has rows to update.** Until this task, nothing in the
  codebase inserted a device, so every published message was dropped by design.

---

## 2026-09-08 — Ingest: derived presence, and the retained-replay trap (R0-be-3)

- **`last_seen` advances only on a live message that is not `presence{online:false}`.**
  Retained `announce`/`presence` replay on every ingestor reconnect (the process
  re-`subscribe`s, so the broker re-sends the whole retained set), and the LWT is
  published by the *broker*, not the device. Either one, treated as evidence of life,
  marks a dead fleet alive — and for `sleepy` boards, where "the LWT fires on every
  normal sleep and means nothing", it never self-corrects. MQTT's `retain` flag on
  delivery is the discriminator: set only for a retained replay. Verified live —
  `docker compose restart ingestor` replays `up/presence` with `retain=True` and
  `last_seen` does not move.
- **`last_seen = GREATEST(last_seen, :at)`.** QoS 1 is at-least-once; monotonicity is
  one SQL function, not a comparison in Python.
- **The ingestor `UPDATE`s and never `INSERT`s.** The only way into the registry is a
  burned enrollment token (R0-be-4). An `INSERT … ON CONFLICT` here would make anyone
  who can publish to the broker a fleet member — the dev broker is anonymous today, so
  `ingestor/store.py` is the file that stops it. A decommissioned device is dropped by
  the same `WHERE`, with a log line, rather than resurrecting its row.
- **`pg_notify` runs in the write's transaction, via `SELECT pg_notify(:channel, :payload)`.**
  `NOTIFY` takes no bind parameters, so the string form is an injection with a
  device-controlled payload; and transactional delivery means SSE can never announce a
  row the database does not have. Payload capped at 7500 B against PostgreSQL's 8000 B
  limit, and an oversized event is skipped rather than allowed to fail the write.
  `fleetforge.events` ships `EVENTS_CHANNEL` and `DeviceEvent`; R0-be-5 imports both
  rather than restating either — a channel name spelled twice is a silently empty SSE
  stream with nothing failing loudly.
- **`presence.is_online()` is the single rule, and presence stays uncomputed in the
  database.** The event's `online` is a snapshot for the SSE consumer; the API
  recomputes on read, because a sleepy device goes offline with no message arriving at
  all. The 2.5 tolerance lives once, in `config.presence_tolerance`.
- **An announce whose `power_class` would violate a CHECK loses that field, not the
  whole message.** `fw_version` is what tells the operator the OTA landed; dropping the
  announce over a barely-used field would be the wrong trade. The pair is validated in
  Python, the CHECK stays the backstop.
- **A payload `device_id` that disagrees with the topic is dropped.** The topic is
  authoritative — it is what the `%u` pattern ACL binds to the broker username.
- **One message never kills the process.** Specific exception families
  (`SQLAlchemyError`, `OSError`, `ValueError`) around the per-message write, and the
  heartbeat file touched even on failure: liveness is broker-connectedness, and
  restarting the container does not fix Postgres. Verified by stopping Postgres under
  load — one ERROR line per message, container still healthy, full recovery on restart.
- **`now_utc()` moved to `fleetforge/clock.py`.** It lived in `api/deps.py`, and the
  ingestor must not import `fleetforge.api` — pulling FastAPI's app factory into a
  process with no HTTP server would drag its settings validation along with it.
- **Gotcha fixed in passing:** `just mqtt-pub` wrapped the payload in a double-quoted
  shell word, so the shell ate every `"` in a JSON body and the broker received
  `{proto:1,…}`. It presents as a `JSONDecodeError` from the ingestor and looks like an
  ingest bug. The payload now travels in the environment; the recipe also takes a
  `retain` argument, since retained state is most of what this task had to be tested
  against.
- **PROPOSED for `spec/` (protected, so not written there):** `spec/device-protocol.md`
  → *Open items for R0* asks whether `up/log` ships in R0 — the answer this task
  implements is "accepted and dropped: it only moves `last_seen`, storage is R3".

---

## 2026-09-08 — Enrollment token issuance: one predicate, two readers (R0-be-2)

- **`BURN_SQL` ships as an importable constant** in `fleetforge.auth.enrollment`, not
  as prose to copy. `R0-be-4` imports it, and `tests/test_invariants.py`'s four burn
  tests — including the two-connection race — now exercise the shipped statement
  rather than a duplicate of it. Supersedes the "R0-be-4 must copy this verbatim"
  instruction in the `EnrollmentToken` docstring and `R0-db-1` §12.
- **The API's derived `status` is *defined* as the burn predicate** — `active` iff the
  burn would succeed — and there is a parametrized equivalence test over all four
  states so the two cannot drift. A dashboard that says "active" about a token the
  burn rejects sends someone to the bench with a board that will not enroll. The
  database clock stays the authority: `token_status()` is a display value and never an
  authorization decision, which is always the conditional UPDATE.
- **The plaintext is in the `POST` response body on purpose**, unlike the admin login
  token. A human copies it into the flasher's baked config (`spec/flows.md` Flow 1), so
  it must be readable exactly once; it is never logged (ids only), never re-derivable,
  and the list response model has no field that could carry it.
- **24 h lives in `config.enrollment_token_ttl_hours` and nowhere else** — no DB
  default, no per-request override. The number's home is `spec/prd.md`; a `ttl_hours`
  in the request body would be a second place the rule can be violated.
- **Revoke is `POST …/revoke`, not `DELETE …`.** Revoked rows are retained 90 days
  (`spec/prd.md` → *Retention*) and `used_by_device_id` is the fleet's enrollment
  provenance; a `DELETE` verb would invite someone to actually delete it. Revocation is
  the same conditional-UPDATE idiom as admin-token revocation, so a second call is
  idempotent rather than a moved timestamp.
- **Group CRUD deliberately does not exist.** Tokens are group-scoped and the schema
  supports it, but nothing creates or lists `device_groups`, so R0 tokens are ungrouped
  in practice. That is correct for R0 (bulk deploy is V3); flagged as a follow-up task
  rather than smuggled in.
- **`bearer_scheme` / `cookie_scheme` moved to `api/deps.py`.** They were private to
  `routers/auth.py`; every protected router needs them, and one credential deserves one
  declaration. `auto_error=False` on both remains essential — with the default, FastAPI
  403s before `require_admin` runs.
- **PROPOSED for `spec/prd.md` → *Requirements & targets*** (spec is protected): promote
  the enrollment-token TTL from the PROPOSED prose in *Security & data posture* into the
  *Timing* table as **enrollment token lifetime = 24 h**, so it sits with the other
  numbers code resolves against.

---

## 2026-09-08 — Admin auth: one credential, two transports (R0-be-1)

- **One credential type.** The login cookie carries *the same* `ffa_` token a CLI
  would send in `Authorization: Bearer`, verified by one code path
  (`api/deps.py::require_admin`). There is no session table and no second credential
  kind, so revoking a dashboard session is the same single `UPDATE` as revoking a
  CLI token. Confirms `design/architecture.md` → *v1 admin auth*.
- **Argon2id pinned to `t=2, m=19 MiB, p=1` behind an `anyio.CapacityLimiter(2)`.**
  The library defaults (64 MiB, and Starlette's 40-thread threadpool) would peak
  around 760 MiB inside a 256 M container — an OOM kill under concurrent logins.
  Verification reads the parameters out of the stored PHC string, so the profile can
  change later without invalidating existing hashes.
- **The verification cache memoizes the hash comparison only.** The row is read and
  `revoked_at` / `expires_at` re-checked on **every** request; only the ~40 ms argon2
  comparison is skipped, keyed by `(token_id, sha256(secret))` for 60 s. Caching an
  `AuthContext` instead would silently break instant revocation — which is the entire
  reason JWT was rejected. Checks run parse → row → revoked/expired → verify, so a
  revoked token also cannot burn CPU.
- **`ADMIN_PASSWORD_HASH` holds the hash, never the password, and must be
  SINGLE-QUOTED in `.env`.** Verified empirically: unquoted, docker compose
  interpolates the `$argon2id` / `$v` / `$m` segments away and the container receives
  `=19=19456`; the failure mode is a login that can never succeed and a log line that
  does not say why. `python-dotenv` strips the quotes, so one quoted line serves both
  the host process and compose interpolation. `docker-compose.yml` uses
  `${ADMIN_PASSWORD_HASH:?…}` with **no default** — a shipped default admin
  credential is worse than a stack that refuses to boot.
- **`Secure` is unconditional.** `http://localhost` is a secure context, so there is
  no dev/prod cookie switch for anyone to flip in production. Cookie attributes are
  `HttpOnly; Secure; SameSite=Strict; Path=/`, set and cleared identically. No CSRF
  token: the dashboard is same-origin by construction, which is also why CORS
  middleware must never appear. Rejected `__Host-`: no subdomains, `Path=/` and
  `Secure` already fixed, and inconsistent browser behaviour over `http://localhost`.
- **Login rate limiting is per-process** (one uvicorn worker per container), keyed on
  the leftmost `X-Forwarded-For` entry with a **global backstop bucket**, because
  Traefik appends to that header rather than replacing it and the key is therefore
  client-spoofable. Only failures are counted, and both buckets are checked before
  any argon2 work.
- **`db/base.get_session()` deleted.** It called the `lru_cache`d
  `get_sessionmaker()` directly, so `dependency_overrides[get_sessionmaker]` did not
  affect it and a test would have quietly used the developer's dev database. **All**
  API database access goes through `Depends(get_sessionmaker)`. Supersedes the
  hand-off note in `.claude/plans/R0-db-1-schema.md` §12.
- **PROPOSED for `spec/prd.md` → *Requirements & targets*** (spec is protected, so
  these are not written there): session/cookie lifetime **7 days**; login rate limit
  **5 failures / 60 s per client IP, 30 / 60 s global**; argon2id profile
  **t=2, m=19 MiB, p=1**.

---

## 2026-09-08 — The standalone Compose stack is the dev environment (R0-infra-1)

- **The stack is both the dev loop and the V2 self-host artifact, and it is the
  default dev environment specifically so it cannot rot.** `spec/prd.md` promises
  "ships as one Docker Compose stack" while production is a *fragment* of a shared
  stack; the only way both stay true is to use the whole thing every day.
  `just up-prod` (base compose only: built images, nginx, no bind mounts, no
  `--reload`) is the guard that the production-shaped path still builds, and it is
  meant to be run before every commit.
- **One image, two commands.** A single root `Dockerfile`; `api` and `ingestor` are
  the same image with a different `command:`. Confirms `design/production.md` →
  *Open decisions*.
- **MQTT reaches the broker only through Traefik's `mqtt` entrypoint, even in dev.**
  Mosquitto publishes no host port, so the dev path and the prod path are the same
  path. Dev uses ``HostSNI(`*`)`` with no TLS; prod (`R0-infra-3`) uses
  ``HostSNI(`bingo.tvaroska.sk`)`` + `tls.certresolver` and forwards plaintext
  internally. Traefik therefore joins the `backend` network here, which the shared
  Traefik in `services/prod` does not yet do.
- **The API is not routed by Traefik at all.** nginx in the frontend container owns
  `/v1` on the dashboard's origin, which makes "no CORS" structural rather than
  configured. `tests/test_api_health.py::test_no_cors_headers` exists so that a
  future "quick CORS fix" fails loudly; a browser CORS error against this app means
  the nginx proxy is wrong.
- **Dev-only anonymous broker access is quarantined** in
  `mosquitto/conf.d/10-dev-anonymous.conf`, the single file `R0-sec-1` deletes.
  Nothing in `mosquitto.conf` grants or restricts topic access, so the broker's
  security posture is a directory listing rather than a config audit.
- **`.env` is the HOST configuration and is never `env_file:`d into a container.**
  It holds `localhost:5433` for alembic/pytest/just; containers get
  `postgres:5432` set explicitly. Compose still reads `.env` for `${VAR}`
  interpolation. pydantic-settings gives real environment variables precedence over
  `.env`, so an `env_file:` here would silently point the API at its own namespace.
- **TLS is deliberately absent.** `http://localhost` is a secure context, so Web
  Serial (`R0-fe-3`) and `Secure` cookies (`R0-be-1`) both work; V2's TLS problem is
  left unsolved but unobstructed (a commented ACME block in the Traefik command and
  an `FF_ACME_EMAIL` placeholder).
- **Gotchas learned, all of which cost time:** (1) the mosquitto CLI clients force
  TLS whenever the port is 8883 and cannot be talked out of it, so the plaintext dev
  broker on the prod-parity port must be exercised with paho — `just mqtt-pub` /
  `just mqtt-sub` exist for exactly this, and the failure mode (`Protocol error`)
  looks like a broken TCP router. (2) **Traefik silently skips containers that are
  not `healthy`**, so a broken healthcheck presents as a 404 from the entrypoint,
  not as an unhealthy badge; `node:22-slim` has neither `wget` nor `curl`, and nginx
  listens on IPv4 only, so container probes must use `127.0.0.1`, never `localhost`.
  (3) The production image does not chown `/app` to the runtime user — the code is
  root-owned and read-only to `appuser`.

---

## 2026-09-08 — Schema, and the conventions the codebase inherits (R0-db-1)

The first code in the repo, so these are settled for everything after it.

- **Spelling is `enrollment` / `enroll` (US), everywhere.** `spec/device-protocol.md` is
  the near-frozen wire contract and it says `POST /v1/enroll`; `TODO.md` and
  `design/architecture.md` say `/v1/enrol`. The spec wins. **Proposed correction:** fix
  those two documents to `/v1/enroll` as part of R0-be-4, which owns the endpoint.
- **No PostgreSQL ENUM types.** The server must tolerate agents it cannot update
  (`spec/device-protocol.md` → *Evolution rules*), and a PG enum needs a migration before
  it can store a value a future agent invents — an ingest that raises on an unknown
  `link_type` or `state` is a silent fleet-visibility outage. Vocabulary lives in Python
  `StrEnum`s; the columns are `TEXT`. **Sole exception:** `devices.power_class` has a
  CHECK, because derived presence is only *defined* for `always_on` / `sleepy`.
- **Token wire format is `{prefix}_{uuid-hex}.{secret-b64url}`** (`ffa_` admin, `ffe_`
  enrollment). Argon2id hashes are salted and therefore not searchable, so the row's UUID
  must ride in the token as the indexed lookup key; only the secret half is verified
  against `secret_hash`. Plaintext is never stored.
- **The enrollment burn is one conditional `UPDATE … RETURNING`**, correct under
  PostgreSQL's default READ COMMITTED; zero rows back means already burned/revoked/expired.
  Never SELECT → check → UPDATE. Statement is in the `EnrollmentToken` docstring and
  proven by a two-connection race test.
- **Devices soft-delete (`decommissioned_at`) and `deploy_events.device_id` is
  ON DELETE RESTRICT.** `deploy_events` is kept forever ("the metric history is the
  product's evidence") while every device must stay removable; RESTRICT makes destroying
  KPI history impossible rather than merely discouraged.
- **`devices.device_id` (eFuse MAC, `^[0-9a-f]{12}$`) is the natural PK**, because it is
  also the MQTT username the two `%u` pattern ACLs depend on. The format CHECK is a
  security control, not tidiness.
- **Layout: `src/fleetforge/`, uv, SQLAlchemy 2.0 async + asyncpg, Alembic revisions
  `NNNN_slug`, ruff + mypy, pytest against a real Postgres migrated by Alembic.** One
  package because one image ships two entrypoints (`api`, `ingestor`). Dev Postgres
  publishes **5433** — 5432 on this host belongs to an unrelated container.
- **`deploy_events` is deliberately not a Timescale hypertable:** forever retention, tiny
  volume, and an outgoing FK. R3's telemetry table is the hypertable case.

---

## 2026-09-08 — Bingo retirement completed (R0-infra-0)

- **Decision:** Bingo deployment fully retired from production. Containers stopped and
  removed, database backed up to `gs://btvaroska/retired/bingo/` then dropped, all
  deployment scripts and runbooks updated. Domain `bingo.tvaroska.sk` now free for
  fleetforge. Repository and Artifact Registry images intentionally kept as historical
  artifacts.
- **Why:** Freed 384 MB of declared container limits on a host swapping ~1 GB. Fleetforge
  needs ~512 MB, so net addition is ~128 MB. Also freed the domain with existing Let's
  Encrypt cert (kept to avoid fresh ACME challenge).
- **Gotchas learned:** (1) Removing services from docker-compose.yml does not stop running
  containers - must explicitly stop before deploy. (2) Smoke tests must be updated in same
  commit that removes services to avoid deploy auto-rollback. (3) Init scripts are inert
  on existing volumes - database drop requires explicit `DROP` commands. (4) Found
  `prod/bingo.env` tracked in git despite being in `.gitignore` (gitignore doesn't apply
  to already-tracked files) - filed as separate security task for other tracked env files.
- **Verification:** Post-retirement checks confirmed container count 12→10, memory freed,
  Traefik route 404, database/role dropped with backup verified restorable, full deploy
  pipeline green, other services unaffected.
- **Task:** R0-infra-0 completed 2026-09-08. Details in
  [docs/features/infrastructure.md](docs/features/infrastructure.md).

---

## 2026-09-08 — Adopted gen-3 planning layout

- **Decision:** Migrated from `PLAN.md` + `docs/` to the gen-3 layout used by every
  other repo in the estate: `TODO.md` (live status only), `spec/` (the WHAT,
  status-free), `design/` (the HOW, status-free), `docs/` (planning/ops), this file,
  and `CRITICAL.md`.
- **Moves:** `docs/SPEC.md` → `spec/prd.md`; `docs/device-protocol.md` →
  `spec/device-protocol.md`; `docs/FLOWS.md` → `spec/flows.md`; `docs/DESIGN.md` →
  `design/architecture.md`; `docs/architecture.md` → `design/production.md`;
  `docs/RELEASES.md` → `docs/releases.md`; `PLAN.md` → `TODO.md` (task IDs lowercased,
  `R0-BE-1` → `R0-be-1`, tables → checkbox items).
- **Why:** fleetforge was the last repo on the old layout, and root `CLAUDE.md` still
  documented it. The restructure done the same day had already rebuilt gen-3's
  *distinctions* (requirements vs. design vs. tasks) under the old filenames, so the
  migration was mechanical.

---

## 2026-09-08 — Artifacts in GCS, MinIO for self-hosting

- **Decision:** Artifact bytes live in GCS (`gs://btvaroska/fleetforge/`) behind a narrow
  object-store adapter (`put` / `get` / `signed_url` / `delete`). MinIO is the
  self-hosted backend, S3-compatible, arriving with V2 turnkey self-hosting.
- **Why:** GCS signed URLs *are* the mechanism the `stage` command already specifies —
  short-lived, signature-as-authorization, range-capable, served without touching the
  API process. Artifact bytes also stay off the prod VM's 5.5 G of free disk and off its
  bandwidth.
- **Cost, accepted:** v1 has a cloud dependency for artifact storage. The adapter
  boundary is what keeps removing it a configuration change. Recorded honestly in
  `spec/prd.md` → *Security & data posture*.
- **Details:** [design/production.md](design/production.md) → *Artifact storage*.

---

## 2026-09-08 — Retire bingo; fleetforge takes `bingo.tvaroska.sk`

- **Decision:** The unfinished bingo app is retired from production and fleetforge reuses
  its domain. Not a public product until V3, so the domain is an operational detail.
- **Why:** frees 384 M of declared container limits on a box already swapping ~1 G, plus
  an existing Let's Encrypt route. Fleetforge needs ~512 M, so the net addition is ~128 M.
- **Not decided:** whether to delete the bingo repo or its Artifact Registry images.
  Retiring the deployment is not deleting the project. The bingo **database must be
  backed up before the role is dropped** — the one irreversible step (`R0-infra-0`).

---

## 2026-09-08 — The ingestor is the only MQTT subscriber

- **Decision:** A single-instance ingestor process is the sole MQTT subscriber; it writes
  to Postgres and `NOTIFY`s. API workers `LISTEN` and fan out over SSE. The API never
  subscribes.
- **Why:** N uvicorn workers each holding a subscription would ingest every message N
  times, and an SSE client on worker A would never see an event ingested by worker B.
  Both failures are silent until the worker count goes above one.
- **Why not Redis:** Postgres `LISTEN/NOTIFY` is sufficient at this scale and the
  database is already there. Mirrors the `content-api` / `content-worker` split already
  running on the same host.
- **Details:** [design/production.md](design/production.md) → *The single-subscriber rule*.

---

## 2026-09-08 — Requirements & targets written down (PROPOSED)

- **Decision:** `spec/prd.md` gained a *Requirements & targets* table — capacity, timing,
  retention, KPI thresholds — marked **PROPOSED** pending review. Downstream docs resolve
  against it instead of each deciding for themselves.
- **Why:** the doc set specified mechanisms with no numbers. `device-protocol.md` had
  "heartbeat default interval" as an open item — a spec decision leaking into a protocol
  doc. Metrics existed with no thresholds, so they could not fail.
- **Consequence:** exposed three missing tasks, now in R1 — a signed-URL + range download
  endpoint (Flow 2 promised resumable download with nothing to serve it), writing
  `deploy_events` from R1 (or R5 arrives with two KPIs and no history), and enforcing
  retention rather than only ingesting.

---

## 2026-09-08 — Enrolment over HTTPS, not MQTT; tokens are single-use

- **Decision:** A device exchanges its enrolment token at `POST /v1/enrol` over HTTPS for
  a per-device broker credential, then connects to the broker already credentialed. The
  token burns on use.
- **Why:** the original flow had the device present its token *to the broker*, which
  would force Mosquitto to authenticate clients it has never heard of against a
  group-scoped token — a custom auth plugin bridging broker to control plane. Instead the
  broker only ever sees fully-credentialed clients and its authz collapses to two pattern
  ACLs. The agent already needs an HTTPS client for artifact download, so this is free.
- **Single-use:** a group-scoped token surviving in flash would let anyone with physical
  access to one board enrol arbitrary devices, and on a public-facing broker there is no
  LAN perimeter to hide behind.
- **Details:** [spec/device-protocol.md](spec/device-protocol.md).

---

## 2026-09-08 — The device owns the reboot, and the rollback

- **Decision:** Two authority rules, both device-side. The device decides *when* to apply
  and may sit in `awaiting_safe_window` indefinitely; and the confirm timer is armed on
  the device before the reboot, so rollback is the device's decision, never a server
  command.
- **Why:** a drone rebooting mid-flight falls out of the sky, and a board that cannot
  reach the broker is exactly the board that must roll back — it will never receive a
  command telling it to. The server observes and records; it never forces a reboot.
- **Details:** `design/architecture.md` principle 5; `spec/prd.md` → *Scope*.

---

## 2026-09-08 — MQTT is the control plane only

- **Decision:** MQTT carries identity, presence, commands, status and telemetry. **HTTPS
  carries artifact bytes.** MQTT never carries payload.
- **Why:** MQTT has no range requests, so any drop restarts the whole transfer, and the
  broker would buffer the image per subscriber on a group deploy. The `stage` command
  carries a short-lived signed artifact URL instead.
- **Supersedes:** the initial spec's implication that MQTT was the delivery transport.

---

## 2026-09-08 — Flash-time immutables frozen at R0

- **Decision:** The full A/B partition table (`nvs`/`otadata`/`phy_init`/`ota_0`/`ota_1`,
  4 M flash minimum), `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, and the eFuse posture
  (Secure Boot v2 **off**, anti-rollback **off**, flash encryption **off**) ship from the
  very first flash at R0 — even though nothing writes the second slot until R2.
- **Why:** an OTA image writes *into* a partition; it cannot rewrite the partition table,
  and the bootloader is the one update with no rollback path. Wrong at R0 means
  physically retrieving every deployed board — the exact intervention this product exists
  to remove.
- **Consequence:** R5 ships **app-level signature verification**, not Secure Boot v2.
  Secure Boot v2 burns a key digest to eFuse and needs a re-signed bootloader, so it can
  never be enabled on an already-deployed board — it is post-v1 and new-devices-only.
  The two are not interchangeable.
- **Details:** [design/architecture.md](design/architecture.md) → *Flash-time immutables*.

---

## 2026-09-08 — Version structure: V1 safe OTA, V2 build, V3 swarm

- **Decision:** V1 = R0–R5, ~5 heterogeneous boards, safe OTA. V2 = R6–R10, VCS +
  server-side compile + simulation. V3 = robotic swarm (gateway + drones).
- **Moved out of v1:** groups & bulk deploy (five different builds have nothing to
  bulk-deploy) → V3; the advisory simulation gate → V2/R8.
- **Rejected as v1 scope:** a 100+ node swarm. Aspirational, not v1. What v1 *does* pay
  for is schema and shape only — `link_type` / `power_class` / `parent_device_id`,
  derived presence, device-owned reboots — never speculative machinery.
- **Rule learned:** take what is a schema or config decision; defer what is machinery.

---

## 2026-09-08 — v1 is a hosted instance; IP-bearing links only

- **Decision:** v1 ships as a single hosted, single-tenant instance on a public domain
  with Let's Encrypt TLS. Turnkey self-hosting is V2. The device contract requires an
  IP-bearing link and TLS, nothing more — never "Wi-Fi".
- **Why hosted:** onboarding friction is the make-or-break risk, and the hard part of
  self-hosting is TLS without public DNS (a local CA the browser *and* the device trust).
  Deferred, not solved — it returns in full at V2.
- **Consequence:** the broker and artifact endpoint are on the public internet from R0,
  so per-device broker credentials, topic ACLs and single-use enrolment tokens are R0
  requirements, not post-v1 hardening. There is no LAN perimeter to fall back on.
- **Gateway-mediated non-IP radios** (Zigbee/BLE/LoRa) cannot reach a hosted server at
  all and need an on-site gateway — a second product, deferred to V2+.
