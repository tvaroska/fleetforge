# Enrollment & Provisioning

**Status:** In progress (R0)
**Priority:** P0
**Target:** R0
**Flow:** [flows.md](../../spec/flows.md) → Flow 1

## Overview

Register a physical ESP32 board into the fleet and watch it come online — no
toolchain, no CLI. Detection + flashing happen **in the dashboard via Web Serial**
(`esptool-js` / ESP Web Tools). Trust is established by **auto-enroll via a scoped,
revocable token** baked in at flash time.

The dashboard is built against the **public API + SSE event stream from R0** — it is
the first client of a headless core, not privileged over future clients (HA/CLI/MCP).

## Decisions (from flows.md)

- **Detection/flashing:** in-dashboard Web Serial (`esptool-js`). Chrome/Edge only,
  needs `localhost`/HTTPS, one mandatory user click to grant the port. No batch
  enrollment in v1 (CLI flasher is post-v1).
- **ID granularity:** chip-level auto-detect + user-confirmed board shortlist
  (chip + flash + PSRAM matched to a board DB), "enter manually" always available.
  The running agent self-reports authoritative `platform_type` + capabilities.
- **`device_id` = eFuse MAC** (stable, factory-unique) — satisfies the identity contract.
- **Provisioning = USB config flash (v1):** broker URL + Wi-Fi creds + enrollment
  token baked in at flash time. SoftAP captive portal is post-v1 (Wi-Fi change = re-flash).
- **Trust = auto-enroll via token, exchanged over HTTPS** (`POST /v1/enroll`), not over
  MQTT — so the broker never authenticates a client it has never heard of. Tokens are
  short-lived, group-scoped, revocable and **single-use**: the device trades its token
  once for a per-device broker credential kept in NVS. Every enrolled device is visible
  and removable. Per-device mTLS drops into the same exchange post-v1.
  See [device-protocol.md](../../spec/device-protocol.md).

## Phase 1: R0 — Enroll a board

**Tasks live in [TODO.md](../../TODO.md) → R0** — 16 tasks, ~20d, with build order.
TODO.md is the single source; this file holds the decisions behind them.

**Architecture guardrails**
- Dashboard is built against the public API + SSE event stream, so HA / CLI / MCP are
  cheap later clients and none is privileged.
- The agent flashed here ships the **flash-time immutable layer** — wrong at R0 means
  physically recovering every deployed board. See [design/architecture.md](../../design/architecture.md).
- Broker and artifact endpoint are public from day one: per-device credentials, pattern
  ACLs and single-use tokens are R0 work, not hardening.

**Done when:** plug in a board, flash & register from the browser, watch it come online.

### Token Issuance (R0-be-2)

The admin API provides enrollment token lifecycle management through three endpoints:
`POST /v1/enrollment-tokens` (issue), `GET /v1/enrollment-tokens` (list), and
`POST /v1/enrollment-tokens/{id}/revoke`. Tokens are short-lived (24 h TTL from
`config.enrollment_token_ttl_hours`), group-scoped (or ungrouped via `group_id: null`),
and strictly single-use.

The plaintext token (format `ffe_<uuid>.<secret>`) is returned exactly once in the POST
response body so an operator can copy it into the flasher's baked config. The secret is
hashed with argon2id and never stored, logged, or re-derivable. The list endpoint shows
token status (active / used / revoked / expired) but never exposes the plaintext or hash.

Single-use enforcement relies on a conditional UPDATE statement (`BURN_SQL` in
`fleetforge.auth.enrollment`) that atomically marks a token as used only if it is still
active (not used, not revoked, not expired). This statement is shipped as an importable
constant rather than copy-paste prose, so `R0-be-4` (device enrollment) and the invariant
tests all reference the same implementation. The burn predicate (`used_at IS NULL AND
revoked_at IS NULL AND expires_at > now()`) defines what "active" means, and the API's
derived status must match it exactly—a dashboard showing "active" for a token the burn
would reject causes field enrollment failures.

Revocation uses `POST .../revoke` rather than `DELETE` because revoked tokens are retained
for 90 days (per `spec/prd.md` retention policy) and `used_by_device_id` provides
enrollment provenance for the fleet. Group CRUD deliberately does not exist in R0—tokens
can be group-scoped but nothing creates groups yet—so all R0 tokens are ungrouped in
practice. This is acceptable because bulk deployment is a V3 feature.

### Ingest & derived presence (R0-be-3)

The ingestor (`src/fleetforge/ingestor/`) is the fleet's **single MQTT subscriber**. It
consumes `ff/v1/d/+/up/#`, updates the device row, and emits one `ff_events`
notification per ingested message, which R0-be-5 fans out over SSE. It **never
inserts a device**: every statement is `UPDATE devices … WHERE device_id = :id AND
decommissioned_at IS NULL`, so publishing to the broker cannot join the fleet — only a
burned enrollment token can. Zero rows back is logged and dropped.

**Presence is derived, never stored** (`fleetforge/presence.py`, one implementation
shared with the API's device list): an `always_on` device is online iff the retained
`up/presence` value says so; a `sleepy` device is online iff `now - last_seen <
presence_tolerance × expected_wake_interval_s` (2.5, from `spec/prd.md` → *Timing*),
and its LWT is ignored entirely because it fires on every normal sleep.

The counterpart rule is when `last_seen` may move: **only on a live (`retain=False`)
message that is not `presence{"online":false"}`**. Retained `announce`/`presence` are
replayed to the ingestor on every reconnect, and the LWT is published by the broker
rather than the board; treating either as evidence of life would mark a dead fleet
alive. It is written as `GREATEST(last_seen, :at)` so an at-least-once QoS 1
re-delivery cannot move a device backwards.

An announce is applied field by field — anything the payload omits means "no change" —
and a `power_class` / `expected_wake_interval_s` pair that would violate a DB CHECK is
dropped from the update rather than allowed to lose the whole announce (the
`fw_version` in it is what tells the operator an OTA landed). A payload `device_id`
that disagrees with the topic is an impersonation attempt and is dropped: the topic is
what the `%u` pattern ACL binds to the broker username.

`up/status`, `up/telemetry` and `up/log` are accepted and only move `last_seen`;
persisting them is R1 (`deploy_events`) and R3 (telemetry) respectively.

### Device enrollment (R0-be-4)

`POST /v1/enroll` is the device-facing half of the credential and the **only
unauthenticated write endpoint in the API** — the `ffe_` token in the body *is* the
credential. It is also the codebase's only `INSERT` into `devices`
(`fleetforge/registry.py`). The body is **flat** (`{token, device_id, platform_type,
link_type, power_class, …}`, `extra="ignore"`) and the response is exactly
`{device_id, mqtt_username, mqtt_password}`, with `mqtt_username == device_id`
unnormalised — the two `%u` pattern ACLs are the entire fleet authz, so the eFuse-MAC
format check rejects a non-canonical id rather than lowercasing it.

**The order is the design**, and each step prevents one specific failure:

1. **Rate-limit, then parse, then look the row up, then verify the secret — and only
   then burn.** `BURN_SQL` keys on `id` alone and an `ffe_` token's id is not a secret
   (it is in the issuance response and in the api log), so burning before
   `averify_secret` would let anyone who has read one log line destroy every
   outstanding token.
2. **Validate the identity before the burn** (`api/schemas.py::EnrollRequest`). A burn
   followed by a DB CHECK violation is a token destroyed by a firmware typo, and the
   board then needs a re-flash to get another one. The DB CHECKs stay the backstop.
3. **INSERT the device before the burn, in the same transaction.**
   `enrollment_tokens.used_by_device_id` is a real FK, so burning first fails on the
   happy path — and a refused burn rolls the device row back, or a rejected enrollment
   leaves a fleet member behind.
4. **Commit, then provision the broker.** Holding a row lock and a pooled connection
   across an MQTT round-trip turns a broker outage into `idle in transaction`. The
   inverse failure (a broker credential for a device that is not enrolled) is prevented
   by the order, not by a transaction.

**The grace window.** A burned token may be re-presented by the **same** `device_id`
for `config.enroll_retry_window_s` (600 s) and gets a freshly provisioned password.
The agent writes NVS only after it reads the response body, so a dropped packet on a
first boot otherwise leaves a board that is enrolled, has no credential, and holds a
token that can never burn again — a re-flash, in the field. Single use is intact: the
lookup (`RETRY_LOOKUP_SQL`) matches on `used_by_device_id`, so one token still enrolls
exactly one board forever, and its `FOR UPDATE` keeps a concurrent revoke from racing
it. A different `device_id` on a burned token is a `409`, with the device row rolled
back.

**`broker_provisioned_at`.** Stamped only once the broker really holds the credential.
The provisioner is a seam (`fleetforge/broker/`): `DynsecProvisioner` talks Mosquitto
dynamic-security over MQTT, and `NullProvisioner` — selected when
`MQTT_DYNSEC_USERNAME`/`_PASSWORD` are unset, with a startup WARNING — provisions
nothing. It returns `False`, so the column stays NULL and `WHERE decommissioned_at IS
NULL AND broker_provisioned_at IS NULL` is the honest reconcile list rather than a
column that lies. A provisioning failure is a `503` with `Retry-After`; the enrollment
is already committed and the grace window is what makes the retry work.

**Since R0-sec-1 this path is live.** `docker-compose.yml` makes both dynsec variables
mandatory (`${VAR:?}`) precisely so `NullProvisioner` cannot be selected by accident,
and a real board now gets a real broker client: `createClient` with the device_id as
the **username**, which is what the two `%u` pattern ACLs in `mosquitto/acl` bind to.
The dynsec `device` role the client is created with is deliberately empty — dynsec is
authentication only (DECISIONS.md 2026-09-08). One consequence has no fix: **devices
enrolled during the Null era cannot be reconciled.** The password only ever existed in
the enrollment response, so the server cannot re-provision one the board would know;
they must re-enroll with a fresh token.

Re-enrollment is an upsert: a re-flashed board legitimately enrolls again with a *new*
token. The announced identity is overwritten, `decommissioned_at` and
`presence_reported` are cleared (a retired board that re-enrolls is revived, and the old
retained presence describes a session that no longer exists), and `name` and `last_seen`
— operator-set and historical — are untouched. An ungrouped token never un-groups a
device an operator already placed (`COALESCE`).

The broker password exists in the response body and nowhere else: not in Postgres, not
in a log line, not in the dynsec store (which keeps a hash). Losing it means using the
grace window, or re-enrolling.

### Live event stream (R0-be-5)

`GET /v1/events` (SSE) and `GET /v1/devices` — the two halves of "the event is a hint,
the list is the truth".

The producer side already existed: the ingestor and `POST /v1/enroll` both call
`events.emit()`, which runs `SELECT pg_notify('ff_events', …)` **inside the writer's
transaction**. This task added the consumer: **one dedicated asyncpg connection per API
process** `LISTEN`ing on that channel (`api/eventstream.py::PostgresEventListener`),
publishing into an in-process `EventHub` that fans out to one bounded
`asyncio.Queue` per attached client. Postgres delivers a `NOTIFY` to every listening
backend, so this is correct with N API workers — which is the reason the design routes
events through the database rather than having the API subscribe to MQTT.

The connection cannot be a pooled one: `LISTEN` only delivers to a backend that is
between transactions, and `pool_pre_ping`/recycle would drop the registration with
nothing in the log. It sets `application_name = 'fleetforge-events'`, so
`pg_stat_activity` answers "is anything listening?" without reading code.

**Every failure ends the stream rather than degrading it.** A client that stops reading
overflows its queue and is disconnected (not buffered — that is a memory leak in a
256 M container, and dropping individual events instead would leave it silently
stale). A listener reconnect closes *every* stream, because the hub cannot know what was
missed. Both cases are the same self-healing path: `EventSource` reconnects after
`retry: 2000` and re-reads `GET /v1/devices`. That is also why there is no replay, no
`Last-Event-ID` (Postgres `NOTIFY` has no backlog) and no "resync" event type.

**`GET /v1/devices` computes presence on read** through `presence.is_online`, with one
`now` for the whole response. `presence_reported` is deliberately not exposed: `online`
is the answer, and exposing the ingredient invites a client to re-derive the rule.
Decommissioned rows are absent. A client must **not** patch its state from event
payloads — a sleepy board goes offline with no event at all.

Security shape (this route is the API's first long-lived authenticated connection):
auth is checked once, at connect, so the stream is capped at 15 min
(`config.sse_max_stream_s`) and a revoked token cannot outlive that; the browser
authenticates with the `ff_session` cookie because `EventSource` cannot set a header,
and **a token in the query string was rejected** — nginx logs `$request`. The payload is
validated (`DeviceEvent`) and refused if it contains `\r`/`\n`, since `fw_version`
comes off the wire from a board and SSE framing is newline-delimited; what is forwarded
is the **original** string, so a field a newer ingestor adds survives.

### Device simulator (R0-test-1)

`python -m fleetforge.simulator` (`just sim`, `just sim-fleet`) — a fake ESP32 that
performs the six steps of `spec/device-protocol.md` for real: `POST /v1/enroll`,
persist the credential, connect, retained `up/announce`, retained
`up/presence {"online":true}`, `up/hb` on an interval, and a retained goodbye on the
way out. It is the **client** of everything above: R0-be-2/3/4/5 had no consumer other
than hand-typed `curl` and `just mqtt-pub` until it existed, and R0-fw-1 (the real
agent) is not written yet. Operational recipes are in
`docs/runbooks/dev-stack.md` → *Simulated boards*.

*Not to be confused with a fault-injection harness* — it simulates a **board**, not a
network. The `--link slow` profile adds seeded latency and never drops a message,
because QoS 1 over a persistent session does not lose them and pretending otherwise
would teach something untrue about the protocol.

Four properties are the reason it is code rather than a shell script, and each has a
test in `tests/test_simulator.py` that fails when it regresses:

- **The QoS/retain matrix.** `announce` and `presence` retained, `hb` never — a
  retained heartbeat is replayed to the ingestor on every reconnect, which treats a
  replay as not-live, so `last_seen` would silently stop advancing.
- **The Last Will is retained, and a clean DISCONNECT does not fire it.** The graceful
  path therefore publishes its own `{"online":false}`; `--crash-after` (`os._exit(1)`,
  a TCP FIN with no DISCONNECT) is the only honest way to exercise the real will.
- **It imports nothing from the server but `fleetforge.identity`** (an AST tripwire
  enforces it). A simulator that shares the server's parsing agrees with the server by
  construction and proves nothing; `fleetforge.config` in particular would make
  `DATABASE_URL` mandatory to run a fake board.
- **A single-use token is never spent by accident.** Everything checkable is validated
  before the token is presented, and a credential already on disk means *no*
  enrollment — never a silent re-enroll. `mqtt_password` reaches exactly one place:
  `.sim/<device_id>.json`, mode 0600, gitignored. It is in no transcript line and no
  log record.

### "Enroll a board" page (R0-fe-1)

The operator half of Flow 1, and the dashboard's first real screen. It is a client of
`R0-be-2` and nothing more — the frontend holds no token logic, because the API's
derived `status` *is* the burn predicate and a second implementation would eventually
disagree with it about whether a token can still enroll.

**It ships the login gate**, which was not in the task line but is implied by it: the
token endpoints require admin auth from `R0-be-1`, so without a login screen the page
is unreachable in a browser and only `curl` can enroll a board. There is no client-side
session state — the credential is an HttpOnly cookie the page cannot read, so "am I
signed in?" is `GET /v1/auth/me`, and any later 401 drops back to the form. A transport
failure is deliberately *not* rendered as "logged out": that would invite an operator to
re-type the admin password at an API that is simply down.

**The plaintext is component state and nothing else.** The server cannot re-derive it,
so a token that leaves the screen before the operator copies it is dead. It is never
written to `localStorage`, `sessionStorage`, a URL or an error message — three of the
component tests exist only to fail if that changes. Revoking the token currently on
screen also clears it, so the UI cannot advertise a credential that no longer works.

Every request uses a **relative** path (`/v1/...`) with `credentials: 'same-origin'`.
The one-origin invariant is what makes the cookie work without CORS; an absolute URL
here is what eventually gets "fixed" by adding CORS middleware to the API.

Test configuration lives in a separate `frontend/vitest.config.ts`, **not** in
`vite.config.ts`. The `frontend` container's `node_modules` carries runtime and build
dependencies only, so a `vitest/config` import in the shared config makes the dev server
die with `ERR_MODULE_NOT_FOUND` — which reaches the operator as a bare 404 from Traefik,
because the router drops a backend that is not healthy. That failure was hit and fixed
during this task; the comment in `vitest.config.ts` is there to stop it recurring.

No group picker: R0 has no group CRUD, so every token is issued ungrouped.

**T2 evidence.** Chromium drove the real page at `http://localhost:8080`: logged in
through the form, clicked *Generate enrollment token*, and read the `ffe_` plaintext out
of the live DOM. That token then enrolled a simulated board
(`python -m fleetforge.simulator run --device-id aa11bb22cc33`) — `POST /v1/enroll` 200,
broker connect as the provisioned credential, retained announce/presence, heartbeats.
The token's row flipped `active` → `used` with `used_by_device_id=aa11bb22cc33`, the
board appeared in `GET /v1/devices`, and replaying the burned token from a second
`device_id` was refused `409 enrollment token is not usable`. Storage/URL leak checks
ran against the real browser context, not jsdom.

## Post-v1

- **Device decommissioning** — `POST /v1/devices/{device_id}/decommission` setting
  `decommissioned_at`, calling a new `BrokerProvisioner.delete_client`, and publishing
  empty retained payloads to each of the device's retained topics
  (`spec/device-protocol.md` → *Decommissioning*: "or the registry resurrects ghosts").
  Filed here rather than in R0: enrollment does not depend on it, but nothing else
  removes a board.
- CLI flasher for batch/CI enrollment.
- SoftAP captive-portal provisioning (Wi-Fi change without re-flash).
- Per-device mTLS certs (replace token-only trust).
