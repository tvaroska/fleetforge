# Enrollment & Provisioning

**Priority:** P0
**Target:** R0
**Flow:** [flows.md](../../spec/flows.md) → Flow 1

## Overview

Register a physical ESP32 board into the fleet and watch it come online — no
toolchain, no CLI. Detection + flashing happen **in the dashboard via Web Serial**
(`esptool-js` / ESP Web Tools). Trust is established by **auto-enroll via a scoped,
revocable token** baked in at flash time.

The dashboard builds against the **public API + SSE event stream from R0**. It is
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
  MQTT — so the broker never authenticates a client it never heard of. Tokens are
  short-lived, group-scoped, revocable and **single-use**: the device trades its token
  once for a per-device broker credential kept in NVS. Every enrolled device is visible
  and removable. Per-device mTLS drops into the same exchange post-v1.
  See [device-protocol.md](../../spec/device-protocol.md).

## Phase 1: R0 — Enroll a board

**Open tasks live in [TODO.md](../../TODO.md) → R0**, which is the single source for
status. Completed R0 work is archived in the sections below. This file holds the
decisions behind all of it. No counts here — they go stale the moment a task lands.

**Architecture guardrails**
- Dashboard builds against the public API + SSE event stream, so HA / CLI / MCP are
  cheap later clients and none has special privilege.
- The agent flashed here ships the **flash-time immutable layer** — wrong at R0 means
  physically recovering every deployed board. See [design/architecture.md](../../design/architecture.md).
- Broker and artifact endpoint are public from day one: per-device credentials, pattern
  ACLs and single-use tokens are R0 work, not hardening.

**Done when:** connect a board, flash & register from the browser, watch it come online.

### E2E on real hardware — the "Done when", met (R0-test-2)

**2026-09-19. Flash → enroll → online, on a physical board, against production**.
Everything below this section is a component of that sentence. This is the first time the
sentence itself was true. R0's stated risk (onboarding, board recognition,
device↔server connection) retires. Checked against the prod database 2026-09-22.

**The board**. Device `94a990dd09a4`, an **ESP32-S3**, flashed from `bingo.tvaroska.sk`
with the v0.2.0 agent (`d705652` — `-Os`, 80 MHz, max modem sleep, TX-power ladder) that
prod served since v0.3.5. `partition_layout` `ab-4m-v1`, `ota_slot_size` 1966080 — the
contract value, so the three-way agreement with `agent/partitions.csv` held on real
silicon. No toolchain and no CLI, which is the claim.

**The ladder, from `device_progress` — 13 seconds, no retries:**

| at (UTC) | stage |
|---|---|
| 20:18:01.8 | `link_up` (`wifi`) |
| 20:18:03.8 | `time_synced` |
| 20:18:05.9 | `enrolling` |
| 20:18:10.4 | `enrolled` |
| 20:18:14.1 | `mqtt_connected` |

`last_seen` is 20:19:14 (66 s past enrollment) so the board was genuinely live on the
broker, not merely registered. Note what is *absent*: no `brownout` stage. Thus, this board
never entered the fault `S0-fw-3` describes.

**It was a different board from the one in the brownout loop. That is the whole reason
the run happened**. `R0-test-2` carried a practical dependency on `S0-fw-3` since we wrote it: the only board on hand could not get through RF calibration. Thus, it could not
enroll. Thus, the release could not be proven. A second board **dissolved that dependency
instead of meeting it**.

**What the S3 does and does not tell us**. It de-escalates `S0-fw-3`: that task no longer
blocks a release, and is now a recovery defect held open at P1. This is because the flaw it names is a fleet property — any board that browns out during calibration is permanently stuck,
against a product that promises self-recovery. But it is **not** evidence about our
startup current. The tempting inference ("our agent survived a cold full calibration, so
the 2026-09-14 reading was wrong") does not hold. The S3 is different silicon with a
different regulator and supply from the DevKit v1, and never ran that experiment. The
2026-09-14 conclusion stands, and v0.2.0 onto the stuck DevKit v1 is still the untried
single-variable test.

**It also unblocked `S0-test-2`**, parked since 2026-09-11 on "no C3, C6 or S3 on hand".
One is on hand, already flashed and known-good.

**Current state: nothing is online**. `presence_reported = f` and `last_seen` is
2026-09-19. We unplugged the board since then. The pass was a live connection, not a
standing fleet member — re-plug it before `R1-test-1`.

**What this does not prove, and what R0 still waits on**. The operator was the person who
wrote the flasher. That is exactly the evidence `S0-test-3` exists to collect and the
reason [roadmap.md](../roadmap.md) put *unaided onboarding* in front of the release on
2026-09-11: R0's risk is onboarding, and a successful flash by its author measures the
stack, not the on-ramp. R0 closes on `S0-test-3`, not here.

**It also unblocked R1**. `R1-test-1` depended on this in practice (a board that cannot
enroll cannot be deployed to) so R1's last task became runnable the moment the board came
online. See [ota-deploy.md](ota-deploy.md).

### Device protocol v1 (R0-spec-1)

Done 2026-09-08, the first R0 task — everything else in this phase is written against it.
The artifact *is* [spec/device-protocol.md](../../spec/device-protocol.md). This entry
only records why it came first and what it committed to.

**Near-frozen by construction**. An R0 agent speaks this wire format until someone
physically gets the board. Thus, the protocol had to be settled before any firmware
shipped. The spec's own *Evolution rules* section carries that constraint forward.

What it fixed: the `up/`/`dn/` topic namespace (where the split *is* the authorization
design — it is what makes the two pattern ACLs in R0-sec-1 expressible at all), the
retain and QoS rules, the `announce` / `hb` / `status` / `cmd` payload schemas, and the
update state machine. Three decisions in it are load-bearing elsewhere:

- **Presence comes, never a socket state** — consumed by R0-be-3's ingestor and
  R0-be-5's read model, and the reason a sleepy board can go offline with no event.
- **Enrollment happens over HTTPS, not MQTT** — the broker never authenticates a client it
  did not already hear of (R0-be-4).
- **SNTP before the first TLS handshake** — the clock ordering that R0-fw-1 implements and
  that S0-fw-2 later proved was violated for `link_up`.

`dn/cfg` scope is deliberately bounded rather than left open. *Open items for R0* records
what was knowingly deferred.

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
revoked_at IS NULL AND expires_at > now()`) defines what "active" means. The API's
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
notification per ingested message, which R0-be-5 distributes over SSE. It **never
inserts a device**. Every statement is `UPDATE devices … WHERE device_id = :id AND
decommissioned_at IS NULL`, so publishing to the broker cannot join the fleet — only a
burned enrollment token can. Zero rows back logs and drops.

**Presence comes, never stored** (`fleetforge/presence.py`, one implementation
shared with the API's device list). An `always_on` device is online iff the retained
`up/presence` value says so. A `sleepy` device is online iff `now - last_seen <
presence_tolerance × expected_wake_interval_s` (2.5, from `spec/prd.md` → *Timing*).
Its LWT is ignored entirely because it fires on every normal sleep.

The counterpart rule is when `last_seen` can move: **only on a live (`retain=False`)
message that is not `presence{"online":false"}`**. Retained `announce`/`presence` are
replayed to the ingestor on every reconnect. The LWT publishes by the broker
rather than the board. Treating either as evidence of life would mark a dead fleet
alive. It is written as `GREATEST(last_seen, :at)` so an at-least-once QoS 1
re-delivery cannot move a device backwards.

An announce applies field by field (anything the payload omits means "no change")
and a `power_class` / `expected_wake_interval_s` pair that would violate a DB CHECK drops from the update rather than allowed to lose the whole announce (the
`fw_version` in it is what tells the operator an OTA landed). A payload `device_id`
that disagrees with the topic is an impersonation attempt and drops. The topic is
what the `%u` pattern ACL binds to the broker username.

`up/status`, `up/telemetry` and `up/log` are accepted and only move `last_seen`.
Persisting them is R1 (`deploy_events`) and R4 (telemetry) respectively.

### Device enrollment (R0-be-4)

`POST /v1/enroll` is the device-facing half of the credential and the **only
unauthenticated write endpoint in the API** — the `ffe_` token in the body *is* the
credential. It is also the codebase's only `INSERT` into `devices`
(`fleetforge/registry.py`). The body is **flat** (`{token, device_id, platform_type,
link_type, power_class, …}`, `extra="ignore"`) and the response is exactly
`{device_id, mqtt_username, mqtt_password}`, with `mqtt_username == device_id`
unnormalized — the two `%u` pattern ACLs are the entire fleet authz. Thus, the eFuse-MAC
format check rejects a non-canonical id rather than lowercasing it.

**The order is the design**, and each step prevents one specific failure:

1. **Rate-limit, then parse, then look the row up, then check the secret — and only
   then burn**. `BURN_SQL` keys on `id` alone and an `ffe_` token's id is not a secret
   (it is in the issuance response and in the api log), so burning before
   `averify_secret` would let anyone who has read one log line destroy every
   outstanding token.
2. **Check the identity before the burn** (`api/schemas.py::EnrollRequest`). A burn
   followed by a DB CHECK violation is a token destroyed by a firmware typo. The
   board then needs a re-flash to get another one. The DB CHECKs stay the backstop.
3. **INSERT the device before the burn, in the same transaction**.
   `enrollment_tokens.used_by_device_id` is a real FK, so burning first fails on the
   happy path — and a refused burn rolls the device row back, or a rejected enrollment
   leaves a fleet member behind.
4. **Commit, then provision the broker**. Holding a row lock and a pooled connection
   across an MQTT round-trip turns a broker outage into `idle in transaction`. The
   inverse failure (a broker credential for a device that is not enrolled) is prevented
   by the order, not by a transaction.

**The grace window**. A burned token can be re-presented by the **same** `device_id`
for `config.enroll_retry_window_s` (600 s) and gets a freshly provisioned password.
The agent writes NVS only after it reads the response body. Thus, a dropped packet on a
first boot otherwise leaves a board that is enrolled, has no credential, and holds a
token that can never burn again — a re-flash, in the field. Single use is intact: the
lookup (`RETRY_LOOKUP_SQL`) matches on `used_by_device_id`. Thus, one token still enrolls
exactly one board forever. Its `FOR UPDATE` keeps a concurrent revoke from racing
it. A different `device_id` on a burned token is a `409`, with the device row rolled
back.

**`broker_provisioned_at`**. Stamped only once the broker really holds the credential.
The provisioner is a seam (`fleetforge/broker/`): `DynsecProvisioner` talks Mosquitto
dynamic-security over MQTT, and `NullProvisioner` (selected when
`MQTT_DYNSEC_USERNAME`/`_PASSWORD` are unset, with a startup WARNING) provisions
nothing. It returns `False`, so the column stays NULL and `WHERE decommissioned_at IS
NULL AND broker_provisioned_at IS NULL` is the honest reconcile list rather than a
column that lies. A provisioning failure is a `503` with `Retry-After`. The enrollment
is already committed and the grace window is what makes the retry work.

**Since R0-sec-1 this path is live**. `docker-compose.yml` makes both dynsec variables
mandatory (`${VAR:?}`) precisely so `NullProvisioner` cannot be selected by accident.
A real board now gets a real broker client: `createClient` with the device_id as
the **username**. This is what the two `%u` pattern ACLs in `mosquitto/acl` bind to.
The dynsec `device` role the client starts with is deliberately empty — dynsec is
authentication only (DECISIONS.md 2026-09-08). One consequence has no fix: **devices
enrolled during the Null era cannot reconcile**. The password only ever existed in
the enrollment response. Thus, the server cannot re-provision one the board would know.
They must re-enroll with a fresh token.

Re-enrollment is an upsert: a re-flashed board legitimately enrolls again with a *new*
token. The announced identity is overwritten, `decommissioned_at` and
`presence_reported` are cleared (a retired board that re-enrolls is revived, and the old
retained presence describes a session that no longer exists), and `name` and `last_seen`
— operator-set and historical — stays the same. An ungrouped token never un-groups a
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
publishing into an in-process `EventHub` that distributes to one bounded
`asyncio.Queue` per attached client. Postgres delivers a `NOTIFY` to every listening
backend, so this is correct with N API workers. This is the reason the design routes
events through the database rather than having the API subscribe to MQTT.

The connection cannot be a pooled one: `LISTEN` only delivers to a backend that is
between transactions, and `pool_pre_ping`/recycle would drop the registration with
nothing in the log. It sets `application_name = 'fleetforge-events'`, so
`pg_stat_activity` answers "is anything listening?" without reading code.

**Every failure ends the stream rather than degrading it**. A client that stops reading
overflows its queue and is disconnected (not buffered — that is a memory leak in a
256 M container, and dropping individual events instead would leave it silently
stale). A listener reconnect closes *every* stream. This is because the hub cannot know what was
missed. Both cases are the same self-healing path: `EventSource` reconnects after
`retry: 2000` and re-reads `GET /v1/devices`. That is also why there is no replay, no
`Last-Event-ID` (Postgres `NOTIFY` has no backlog) and no "resync" event type.

**`GET /v1/devices` computes presence on read** through `presence.is_online`, with one
`now` for the whole response. `presence_reported` is deliberately not exposed. `online` is the answer, and exposing the ingredient invites a client to re-derive the rule.
Decommissioned rows are absent. A client must **not** patch its state from event
payloads. A sleepy board goes offline with no event at all.

Security shape (this route is the API's first long-lived authenticated connection):
auth undergoes a check once, at connect. Thus, the stream is capped at 15 min
(`config.sse_max_stream_s`) and a revoked token cannot outlive that. The browser
authenticates with the `ff_session` cookie because `EventSource` cannot set a header,
and **a token in the query string triggered a rejection** — nginx logs `$request`. The payload undergoes a check (`DeviceEvent`) and refused if it contains `\r`/`\n`, since `fw_version`
comes off the wire from a board and SSE framing is newline-delimited. What is forwarded
is the **original** string. Thus, a field a newer ingestor adds survives.

### Device simulator (R0-test-1)

`python -m fleetforge.simulator` (`just sim`, `just sim-fleet`) — a fake ESP32 that
does the six steps of `spec/device-protocol.md` for real: `POST /v1/enroll`,
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

- **The QoS/retain matrix**. `announce` and `presence` retained, `hb` never — a
  retained heartbeat is replayed to the ingestor on every reconnect, which treats a
  replay as not-live, so `last_seen` would silently stop advancing.
- **The Last Will carries the retain flag. A clean DISCONNECT does not fire it**. The graceful
  path thus publishes its own `{"online":false}`. `--crash-after` (`os._exit(1)`,
  a TCP FIN with no DISCONNECT) is the only honest way to exercise the real will.
- **It imports nothing from the server but `fleetforge.identity`** (an AST tripwire
  enforces it). A simulator that shares the server's parsing agrees with the server by
  construction and proves nothing. `fleetforge.config` in particular would make
  `DATABASE_URL` mandatory to run a fake board.
- **An apply ends the session, because that is what a reboot does** (S0-test-4,
  2026-09-23). Adopting the new `fw_version` into `StageRunner.identity` is invisible on
  its own: the running session captured the old frozen identity. Thus, an `always_on` board
  announced the version it applied only if something else happened to disconnect it.
  Write-up in `ota-deploy.md`.
- **A single-use token is never spent by accident**. The server checks everything checkable
  before the token arrives, and a credential already on disk means *no*
  enrollment — never a silent re-enroll. `mqtt_password` reaches exactly one place:
  `.sim/<device_id>.json`, mode 0600, gitignored. It is in no transcript line and no
  log record.

### "Enroll a board" page (R0-fe-1)

The operator half of Flow 1, and the dashboard's first real screen. It is a client of
`R0-be-2` and nothing more. The frontend holds no token logic, because the API's
derived `status` *is* the burn predicate and a second implementation would eventually
disagree with it about whether a token can still enroll.

**It ships the login gate**. This was not in the task line but is implied by it: the
token endpoints require admin auth from `R0-be-1`, so without a login screen the page
is unreachable in a browser and only `curl` can enroll a board. There is no client-side
session state. The credential is an HttpOnly cookie the page cannot read, so "am I
signed in?" is `GET /v1/auth/me`. Any later 401 returns to the form. A transport
failure is deliberately *not* rendered as "logged out". That would invite an operator to
re-type the admin password at an API that is simply down.

**The plaintext is component state and nothing else**. The server cannot re-derive it.
Thus, a token that leaves the screen before the operator copies it is dead. It is never
written to `localStorage`, `sessionStorage`, a URL or an error message — three of the
component tests exist only to fail if that changes. Revoking the token currently on
screen also clears it. Thus, the UI cannot advertise a credential that no longer works.

Every request uses a **relative** path (`/v1/...`) with `credentials: 'same-origin'`.
The one-origin invariant is what makes the cookie work without CORS. An absolute URL
here is what eventually gets "fixed" by adding CORS middleware to the API.

Test configuration lives in a separate `frontend/vitest.config.ts`, **not** in
`vite.config.ts`. The `frontend` container's `node_modules` carries runtime and build
dependencies only. Thus, a `vitest/config` import in the shared config makes the dev server
die with `ERR_MODULE_NOT_FOUND` — which reaches the operator as a bare 404 from Traefik.
This is because the router drops a backend that is not healthy. That failure was hit and fixed
during this task. The comment in `vitest.config.ts` is there to stop it recurring.

No group picker: R0 has no group CRUD. Thus, every token is issued ungrouped.

**T2 evidence**. Chromium drove the real page at `http://localhost:8080`: logged in
through the form, clicked *Generate enrollment token*, and read the `ffe_` plaintext out
of the live DOM. That token then enrolled a simulated board
(`python -m fleetforge.simulator run --device-id aa11bb22cc33`) — `POST /v1/enroll` 200,
broker connect as the provisioned credential, retained announce/presence, heartbeats.
The token's row flipped `active` → `used` with `used_by_device_id=aa11bb22cc33`, the
board appeared in `GET /v1/devices`, and replaying the burned token from a second
`device_id` triggered a refusal `409 enrollment token is not usable`. Storage/URL leak checks
ran against the real browser context, not jsdom.

### The board half of Flow 1 — the connect-only agent (R0-fw-1)

The other end of everything above: real ESP-IDF firmware that reads its config from the
`ff_cfg` partition, sets its clock, spends the `ffe_` token exactly once, stores the
broker password in NVS and comes online. It applies no firmware — `capabilities` is `[]`
and a `dn/cmd` logs rather than executed, because a board that claims a capability
it does not have gets offered a deployment it cannot do. OTA is R2.

**Two rules the boot sequence exists to keep**. A credential in NVS means *never enroll
again* — so `ff_store_load()` distinguishes *absent* (enroll) from *corrupt* (park). A torn write can never cost a second token. And the credential goes to NVS
**before** the broker is contacted. The password exists exactly once, in the HTTP
response body that was just parsed, and a crash between parse and connect would cost a
token for nothing.

**Nothing reboots on failure**. A board that reboot-loops on a revoked token is
indistinguishable from a hardware fault, and every reset discards the serial log that
says which one it is. Every failure path either waits (60 s → 15 min, sized so the
server's 600 s enrollment grace window contains several attempts) or parks with one line
naming what to re-flash.

**Nothing secret is ever logged**. The config dump prints `token 80 chars, passphrase 0
chars (never printed)`. The enroll response is memset before it is freed. The build-time
tripwire (`test_agent_holds_no_credential`) is what keeps it that way. It is why the
config keys are `ssid`/`psk`/`mqtt_pass` and why one string literal in `ff_enroll.c` is
deliberately split.

**T2 evidence — a board with no board**. `docs/runbooks/agent-qemu.md` boots the *shipped*
`agent/dist/esp32` bundle in the QEMU that comes inside the pinned ESP-IDF image, on the
emulated OpenCores NIC, against the local stack. First boot: `ff_cfg v1 loaded (crc ok)`
→ `device_id` → `eth link up, ip 10.0.2.15` → `sntp: 1970-01-01T00:00:02Z → 2026-09-10…`
→ `recording the ff_cfg enrollment token as <fp>; nothing was stored to invalidate`
(S0-fw-4, on a board that enrolled before under a *different* token this is instead the
loud `the ff_cfg enrollment token has changed (… -> …): erasing the stored credential`)
→ `enroll 200` → `credential stored in NVS` → MQTT connect, `subscribe …/dn/#`, retained
`announce` + `presence`, `hb` every 10 s (not retained). Server side: `"online": true`
with `partition_layout "ab-4m-v1"` and `ota_slot_size 1966080`, the token flipped to
`used` with `used_by_device_id`, exactly one `enrolled device` line in the api log. A
second boot on the same flash image logged `reusing the stored credential (no
enrollment)` and made no HTTP request at all. Killing QEMU ungracefully flipped the fleet
view to `"online": false` within ~45 s off the retained LWT.

**What that run found**. `CONFIG_MBEDTLS_HAVE_TIME_DATE` is **off** in ESP-IDF by default.
Thus, a board at epoch 0 completed a real TLS handshake instead of failing it — the protocol
spec's *Clock — SNTP before TLS* section was describing a failure that could not happen.
An expired server certificate would were accepted by the whole fleet. It is now
enabled, required by `verify_bundle.py`, and asserted in `tests/test_agent_partitions.py`.
An option like this compiles in: no OTA adds it to a board already flashed.

### Live device list (R0-fe-2)

The screen R0 is judged on: flash a board, watch it come online without touching the
page. It is the consumer half of `R0-be-5`. It holds that contract literally —
**a frame on `/v1/events` is only a hint that says "go re-read"**, and the re-read
(`GET /v1/devices`) is the only thing that ever sets a row. Nothing is patched from an
event payload. Thus, the table cannot disagree with the server about a board.

**Three refresh triggers, and the boring one is load-bearing**. An event (coalesced
250 ms, so a burst of announce+presence+heartbeat is one read). `onopen`, which is the
resync after the 15-minute cap or after a listener reconnect closed every stream. And a
plain 10 s interval. The interval is not a fallback — **a sleepy board goes offline with
no event at all** (presence expiry publishes nothing). Thus, a purely event-driven list
shows a dead board as online forever, and passes every test one would naturally write
for it. `fleet.test.tsx` has the test that fails if the interval disappears.

**A transport failure is not "logged out"** (the R0-fe-1 rule, now applied to a
long-lived connection). Only a 401 bounces to the login form. A dead API keeps the last
list on screen under a banner and a `Reconnecting…` status. Reconnect policy mirrors the
server's own: the browser owns the retry while `EventSource` is CONNECTING, and only a
CLOSED source (a non-200 status — 401, or `sse_max_clients`) is re-opened manually,
1 s → 30 s waited, matching `api/eventstream.py`.

**The read model is also the liveness detector**. `EventSource` is not reliable to
report a dead stream: behind the Vite dev proxy, stopping the api leaves the socket open
and `onerror` never fires. Thus, the page reported `Live` at a stream that was gone and
never recovered when the api returned. A failed poll now marks the stream suspect, and
the next successful read tears the source down and rebuilds it. This was found in T2,
not in review.

**Last-seen shows single seconds on purpose**. A "just now" bucket wider than the
heartbeat interval (5 s) freezes the column, and a frozen column is indistinguishable
from a page that stopped updating. This is the one thing this screen must make
obvious. `format.test.ts` pins it.

The stream authenticates with the same `ff_session` cookie over a **relative** URL and
no `withCredentials`: one credential, two transports, one origin. jsdom has no
`EventSource` at all. Thus, the hook takes an injectable `EventSourceFactory` (a structural
type a real `EventSource` satisfies). The seam exists for testability and adds no
dependency.

**T2 evidence**. Chromium against the real stack, dev (Vite) then production shape
(nginx). A board enrolled at `05:22:02` (api log) had its row in the DOM at
`05:22:02.673` through nginx — **~0.7 s, no reload, no click** — online, `1.4.2`, with
last-seen ticking 0→4 s and resetting on every heartbeat. A `--crash-after` board flipped
to `offline` **~1.4 s** after the LWT. The sleepy case is the one that matters: after the
will, `curl -N /v1/events` showed nothing but `: keepalive`, and the row still flipped to
`offline` 29 s after its last message (2.5 × 10 s wake + poll granularity). Also proved:
`docker compose restart postgres` (every stream closed by design) recovered to `Live` in
~2 s with the table never blanked. `SSE_MAX_STREAM_S=20` rotated ~10 times in 115 s
invisibly. 12 Reloads oscillated the api's open-client count 1↔2 and never climbed.
Stopping the api showed `Reconnecting…` with the list intact and **no password form**,
and starting it recovered on its own.

### Web Serial flasher (R0-fe-3)

The last screen of R0's done-when: plug a board into the laptop running the dashboard,
press two buttons, and watch it appear in the fleet table above. No `esptool.py`, no
copy-pasted token, no terminal. Chromium-only — Web Serial exists nowhere else. That
limit passes in `spec/prd.md`.

**Every write address comes from `GET /v1/agent/manifest`**. `builds[].parts[].offset`
for the images and `config_partition.offset` for the 4 KB `ff_cfg` blob, never a
constant: the bootloader lives at `0x1000` on ESP32 and `0x0` on the RISC-V parts. A
hardcoded offset flashes cleanly and never boots. `planWrite` is the only place a write
is constructed and `flash.test.tsx` asserts its addresses against the manifest.

**The chip matches, not guessed**. `chip_family` in the manifest is exactly
`ESPLoader.chip.CHIP_NAME`, so selection is `===`. There is no translation table to drift.
The board picker below it is a *label for the operator* and selects nothing on the server
— detection is chip-level. An ESP32 DevKitC is indistinguishable from a WROVER over
serial. Flash size undergoes a check here too, because `flashSize: 'keep'` (see below) makes
esptool-js skip its own fit check, and a 2 MB board given the 4 MB A/B layout is a
mystery boot loop rather than a clean refusal.

**The server mints the token last and revokes it on failure**. Order: check the form → read the
manifest → check chip and flash → download every part and check its sha256 → *then*
`POST /v1/enrollment-tokens`. Minting first would spend a single-use fleet-join credential
on every failed attempt. Leaving a live one baked into a half-flashed board would leave an
orphan credential nobody is tracking. Thus, a failed write revokes it and says so (the token
*id* appears, never the plaintext). The plaintext lives in one local `const` for the
length of one call — not React state, not the log panel, not the DOM, and this feature
touches neither `localStorage` nor `sessionStorage` at all. Same rule as R0-fe-1, extended
to the Wi-Fi passphrase, and `flash.test.tsx` asserts all four places for both secrets.

**The flasher erases nothing, and the board invalidates its own credential** (S0-fw-4).
A board that already enrolled keeps its broker credential in NVS and reuses it (R0-fw-1
logs "reusing the stored credential"). Thus, a fresh token baked into a re-flashed board has
to be made to matter somehow. It used to be the flasher's job (a part in the write plan
that filled the whole `nvs` partition with 0xFF) and that was the wrong place: NVS is
also where IDF caches the RF calibration, in its `phy` namespace. Thus, every flash cost the
board the cold full calibration on every subsequent boot. The flasher writes raw bytes and
cannot act on one namespace. The agent can. `ff_store_sync_token()` now compares a
fingerprint of the `ff_cfg` token against the one stored beside the credential and erases
the `ff` namespace (only that namespace) when they differ. The flasher mints a fresh
token on every flash, so the operator-visible behavior stays the same. There is no checkbox
any more, and boards re-flashed in the field with `agent/tools/ff_cfg.py`, which a browser
flasher never reaches, are covered too.

**The form checks as it is typed**, running the same `buildFfCfgFields` +
`validateFfCfg` pair the engine runs first. The Flash button is dead until it passes.
Belt and braces on purpose: `power=sleepy` with no wake interval `POST /v1/enroll` refuses it (422), and reaching that refusal costs a token.

**Three implementations of the `ff_cfg` contract now exist** — `agent/tools/ff_cfg.py`
(the writer), `agent/main/ff_cfg.c` (the firmware reader) and `frontend/src/ffcfg.ts`
(the browser writer). They share a golden vector,
`frontend/src/ffcfg.vector.json`: the digest in it came from the Python writer, and
both `frontend/src/ffcfg.test.ts` and `tests/test_ff_cfg.py::TestTypeScriptWriterAgrees`
assert it from their own side, so neither writer can move a byte alone. The vector is
ASCII-only, because that is the only region where `json.dumps` (`ensure_ascii=True`) and
`JSON.stringify` agree.

**esptool-js settings that look arbitrary and are not**. `flashMode`/`flashFreq`/
`flashSize` are all `'keep'`, or esptool-js rewrites the bootloader's flash-parameter byte
and recomputes the image SHA. The bundle bytes must reach the chip exactly as ESP-IDF
produced them. `compress: true`, so progress reports in *compressed* bytes and must
not be compared with the manifest's `size`. No MD5 read-back verification: Web Crypto has
no MD5, and the failure that actually happens (a truncated or corrupted download) the sha256 catches it check before anything is written. esptool-js will log a warning that
the blob at the config offset "does not look like an image file". That is the `ff_cfg`
blob. This is not an ESP image.

**Structure**. `flasher.ts` is the seam (types + `explainFlashError`, no esptool import),
`esptoolFlasher.ts` is the only file in the app that imports esptool-js or touches
`navigator.serial`. It is loaded through a dynamic `import()` so a Firefox visitor who
can never flash anything does not download pako and the ROM stubs (~104 kB / 32 kB gzipped
in its own chunk). `flash.ts` holds every rule and runs in jsdom against a fake.
`explainFlashError` lives in the seam and not in the adapter because the commonest failure
of all (the operator dismissing the port chooser) is thrown by `requestPort()` before an
adapter object exists. Untranslated it reads `Failed to execute 'requestPort' on 'Serial':
No port selected by the user.`, which sounds like a fault. It is "No board selected."

**T2 evidence**. Hardware-free, end to end: the frontend's own TypeScript encoder
(`frontend/scripts/emit-ffcfg.ts`, run with `vite-node`) produced `.qemu/ff_cfg.bin` (4096 bytes, mode 0600, accepted by `agent/tools/ff_cfg.py::decode`) and `just agent-qemu
esp32 --fresh` booted on it: `ff_cfg v1 loaded (crc ok), 209 byte payload from 0x12000`,
the api_base/mqtt_uri/link it was given, `device_id 000000000000`, `enroll 200`, `mqtt
connected`, and host-side `GET /v1/devices` showed that board `"online": true,
"fw_version": "0.1.0"` while its token read `used` by it. No `ff_cfg:` error line. In real
headless Chromium against both the dev stack and the production shape (nginx, real CSP):
the section renders, `GET /v1/agent/manifest` returns 200 with all four targets listed,
clicking "Select port and detect" loads the second chunk and Chromium's port chooser,
dismissing it shows "No board selected." with the page still usable, `power: sleepy` with
no wake interval shows the validation error and issues **no** `POST
/v1/enrollment-tokens`, and `localStorage.length === sessionStorage.length === 0` with
zero console errors after sign-in.

### Serial console after flashing (S0-fe-1)

**2026-09-10**. Filed and shipped the same day the flasher's dead end was hit for real. A
board flashed cleanly (the token was minted, so every part wrote and checked) and then
went silent. No `POST /v1/enroll` ever arrived. The page said *"it must appear in the
fleet above within a few seconds"* and had nothing else to offer. Determine why needed
`screen` on a second machine. The cause was never isolated.

**What we built**. A fourth section on the flash page, *4 · Watch the board*. After a
flash it opens the port with no click and no user gesture, reopens at 115200, and streams
`agent_main`'s output under a five-step checklist: Agent running → Network up → Clock set
→ Enrolled → On the fleet. Plus a **Reboot the board** button and a **Release the port**
button.

**Key approach — the console is a second session, not a held flasher**. The obvious
implementation is to keep the `BoardFlasher` alive past `flash.ts`'s `finally`. That triggered a rejection: esptool-js's `Transport` owns the port at the *flash* baud (921600 by default).
This is not the console baud, and unwinding Rule 4 — *the port is always released* —
would leak a port on every error path in the flasher. So `flash.ts` stays the same. The
console opens the same physical port afresh. It can, because nothing ever calls
`port.forget()`: the Chromium grant from the flash is still live, so
`navigator.serial.getPorts()` returns the port without a chooser. That single existing
invariant is what makes the whole feature gesture-free.

Three files, split the way `flash.ts`/`esptoolFlasher.ts` already are:

| file | role |
|---|---|
| `frontend/src/boardConsole.ts` | pure classifier + the `useBoardConsole` hook. No `navigator.serial`. |
| `frontend/src/serialConsole.ts` | the Web Serial adapter. The only file that opens a port for the console. |
| `frontend/src/BoardConsole.tsx` | the panel. |

**Naming the cause**. `classifyConsoleLine` strips the ANSI colors and the
`I (1234) ff-wifi: ` preamble, tags each line with a milestone and, where it can, a plain
English hint. Every string it matches exists in `agent/main/*.c`. This is what
`boardConsole.test.ts` pins. The hints that matter: `esp_wifi` disconnect reason codes
(201 → *no AP with that SSID, check the network has a 2.4 GHz band, the ESP32 radio cannot
see 5 GHz at all*, 2/15/202/204 → wrong PSK), `sntp: no answer` → the clock is unset so
TLS cannot check, `cannot reach https://…` → *if the clock milestone is still open this
is a TLS failure caused by the unset clock, not a routing problem*, every `enroll
401/409/429/503`, `broker refused the connection`, and `halted:`.

**Design flaw the tests caught**. "Most recent explained line wins" is wrong. On a
stranded board the last line is always `agent_main.c:167`'s *"no network yet. Waiting for
the link"*, reprinted every 5 s — true, useless, and it buried the reason code above it.
Hints now carry a `hintKind` of `'generic'` or `'specific'`. A generic note fills an empty
slot but never displaces a named cause. A milestone clears the fault outright. Thus, a board
that recovers after three bad handshakes does not keep "the PSK is wrong" on screen.

**No inactivity timeout, deliberately**. `agent_main.c:166` retries the link forever at 5 s
intervals, so a Wi-Fi failure is a permanently-logging state with no window to catch. A
timer would only ever drop the slow failure it exists to find. The session ends when the
operator releases it, when the page unmounts, or when the device disappears.

**Reboot pulses EN, not IO0**. `SerialConsole.reboot()` drives RTS high with DTR held low,
which asserts EN through the standard auto-reset circuit while leaving IO0 high — the
operator wants a boot log from the first line, not a ROM download prompt.

**Release means release**. `close()` cancels the pending `read()` first (otherwise
`port.close()` triggers a rejection for a locked stream), then closes the port, and never calls
`forget()`. Unmount does the same. Without that the device stays owned until the tab
closes and the operator's next `screen` fails with "Resource busy" for no visible reason.

**T1**. 103 frontend tests pass, 32 of them new. `tsc -b` and the vite build clean.

**T2 evidence — software half proven, hardware half deferred to S0-test-1**. Against a
fake port replaying real `agent/main/*.c` output: `autoWatch` opens exactly one session
with `acquire: 'granted'` at 115200 with no click. A full happy-path log drives all five
milestones to done and shows the "on the fleet" panel. **The 2026-09-10 silent board's own
log** (`disconnected (reason 201)` + the retry heartbeat) produces `waitingFor: 'link'` and
the 5 GHz diagnosis. This is the acceptance criterion "reproduce today's silent board and
have the page name the cause". Release closes the port and returns the panel to "Watch a
board". Watching again never holds two ports. Unmount closes. A `NotFoundError` from the
chooser renders "No board selected." rather than an empty panel.

Four properties cannot be proven in jsdom because they are properties of a USB bridge chip
and an OS: `getPorts()` re-acquisition after `hard_reset` on the native-USB parts, clean
decoding at 115200, the EN pulse landing in the app rather than the ROM loader, and
`screen` actually getting the device back. Tracked as **S0-test-1**, to be run on the bench (Windows + Chrome since 2026-10-02, originally the Mac,
the Linux dev box does not enumerate boards over WebSerial).

### Boot & enroll stage reports (S0-fw-1)

**2026-09-10 server half, 2026-09-11 firmware half**. The companion to the serial console
above, for the board that is *not* on your bench: between "flashed" and "online" the
dashboard showed nothing at all. This is exactly the window that fails.

**What we built**. `POST /v1/device-progress` (`api/routers/progress.py`), a
`device_progress` table trimmed to 20 rows per device on write, `arrivals` riding on the
existing `GET /v1/devices` response, the fleet view's arriving list — and on the board,
`agent/main/ff_progress.c`, called from the stage transitions `agent_main.c` and
`ff_mqtt.c` already walked: `link_up` → `time_synced` → `enrolling` → `enrolled` →
`mqtt_connected`, plus `mqtt_refused` and `halted`.

**Key approach**. A pre-enrollment board holds no MQTT credential (that is what it is
trying to get) so reports travel over the same HTTPS channel and under the same `ffe_`
enrollment token as `/v1/enroll`, and the server **checks that token without ever
burning it**. `stalled` comes on read (60 s), never stored. The reporter is never
fatal, never retried, and self-disables after a 401. Full rationale in `DECISIONS.md`
(2026-09-10, 2026-09-11).

**T2 evidence — driven by `ff_progress.c` on an emulated board, not by curl**. The 2026-09-10
attempt could only reach the server side (`just sim` + curl). This is because the QEMU harness was
believed dead. S0-infra-1 showed it was not. Every acceptance below was re-run against
`agent/dist/esp32` built at a clean HEAD (`58bf54b`, `BUNDLE OK`).

*The healthy path*. One board, one token: `arrivals` shows it at `link_up` **before it
exists in the fleet at all** (the gap this feature was filed to close) then `enrolling`.
All five stages land in `device_progress` in order (15:12:59 → 15:13:01). It goes
`online: true` and **leaves `arrivals` in the same read**, because arrivals excludes
whatever `is_online` currently counts as present. The token reads `used` exactly once with
`used_by_device_id`, so four progress reports cost no enrollment. No `ffe_`, password or
passphrase string anywhere in the transcript.

*A board that gets partway, stalled at `enrolling`*. With `mosquitto` stopped,
`POST /v1/enroll` 503s on broker provisioning and the board loops (`enrolling` re-reported
at 60 s, 120 s, 240 s) showing in the dashboard as **arriving and stalled with its stage**
rather than as nothing. Restarting the broker recovered it to `enrolled` →
`mqtt_connected` → `online` on the *same single token*.

*A board that gets partway, stalled at `mqtt_refused`*. Credential rotated out from under
a board holding one in NVS: `offline` in the fleet and, in arrivals, `mqtt_refused` with
`detail='broker connack 5'` — the dashboard names the cause, not just the absence.

*Token discipline (acceptance 3), re-confirmed live.* no token → 422. Garbage token → 401.
Burned token from the **same** device → 202 (this is what makes `enrolled` and
`mqtt_connected` reportable at all). Burned token from a **different** device → 401. An
unknown stage (`teleported`) stored verbatim, because the R0 agent is flash-baked and the
server must tolerate one it can never update. `Link Up; DROP` and a `\n` in `detail` both
422 — which is why `ff_progress.c::sanitize_detail` strips control characters before they
are sent.

**Acceptance 1 was re-worded, deliberately**. It asked for "a board flashed with a
deliberately wrong PSK". A wrong PSK means no link. The feature's stated limit is that
a board with no route reports nothing — so the literal test asserts something this feature
does not claim, and QEMU has no radio to run it on besides. Owner-confirmed substitution:
the two *partway* cases above. This are what the feature does claim.

**The honest limit, now measured rather than asserted**. A board whose enrollment token triggers a refusal is **invisible**: the progress 401 disables the reporter for the boot. Thus, the
`halted` that `park()` reports never leaves the board (checked with a revoked token —
zero rows in `device_progress`, and the board silent in the dashboard). The only clue is
the `ff-progress` warning on the console. That is why S0-fe-1 and this task are
complements and neither replaces the other.

**Filed on the way**. S0-fe-3 — an offline board keeps reappearing in `arrivals` as
"stalled at `mqtt_connected`" for the 15-minute progress window, which duplicates a row
the fleet list already shows as offline. Harness fix landed here: `just agent-qemu` names
its container and refuses to start a second board. `just agent-qemu-stop` exists,
because six concurrent emulators all claiming `000000000000` silently invalidated the
first round of this evidence (`docs/runbooks/agent-qemu.md`).

### The first stage survives the clock (S0-fw-2)

**Done 2026-09-11**. The one stage the feature above exists for — `link_up`, the report
that makes a board visible *before it exists in the fleet at all* — could never arrive at
a real server. `agent_main.c` reports it the moment the link starts. This is before
`ff_time_sync()`, and with `CONFIG_MBEDTLS_HAVE_TIME_DATE=y` (R0-fw-1) a TLS handshake at
epoch 0 fails certificate validity. The POST never opened, the failure logs at DEBUG
by design, and reports never retry. It only worked in the plaintext lab it was
tested in.

**What we built**. `ff_progress.c` now holds a report the transport cannot yet carry
(3 deep, static, no malloc, oldest dropped when full) and drains it, oldest first, at the
top of the first report made once the transport is ready — **before** that report's own
POST. Thus, the server's receipt order matches the board's. The gate is
`!tls || ff_time_is_sane()`, decided from the scheme of the built URL. A clock-only gate
would held `link_up` forever in the `http://` + `--no-ntp` lab. This is the one
configuration where the stage always worked. A held stage gets exactly one attempt, a
failed send abandons the rest of the queue (bounding the added boot latency to one 5 s
timeout, not depth × 5 s). A 401 clears the queue as it disarms the reporter.
`agent_main.c` stays the same apart from comments. `link_up` is still reported at link time.
This is what makes it true.

**T2 evidence — against prod, vacuity-checked first**. With the *pre-fix* bundle and a
fresh prod token, an emulated board reached `mqtt connected` and `device_progress` held
`time_synced` → `enrolling` → `enrolled` → `mqtt_connected` and **no `link_up`**: the bug,
reproduced on the deployed server. After the fix (rebuild, `BUNDLE OK`, fresh token, same
commands) the same query returned `link_up|ethernet` **first** at 00:42:24, ahead of
`time_synced` at 00:42:27 (held over the sync and flushed with it, three seconds late and
in exact order) followed by the rest of the sequence to `mqtt_connected`, with a `devices`
row and `broker_provisioned_at` set. The plaintext lab was re-run to prove the trap: over
`http://` with `--no-ntp` the clock stays at 1970 and `link_up` still lands *immediately*
(170 ms ahead of `time_synced`), not held.

**Not deployed by this commit**. `agent/dist/` stays in .gitignore. The flasher serves the
bundle baked into the app image (`COPY agent/dist /app/agent`, R0-infra-2), so prod keeps
issue firmware with this bug until the next release build. That coupling is what the
*agent bundles are artifacts* decision (DECISIONS.md, 2026-09-11) exists to delete.

> **Deleted by S0-infra-6 (2026-09-15):** the bundles are artifacts in the object store
> now. A firmware fix reaches the flasher with `just agent-publish <target>` — no app
> image, no release build, no restart.

### An arrival that finished stops arriving (S0-fe-3)

**Done 2026-09-11**. Filed by the S0-fw-1 verification above, and it is the second clause
of the arrivals rule rather than a bug fix.

**The problem**. `arrivals` showed every board with a recent stage that was *not currently
online*. That one clause cannot tell a board on its way up from one that started an hour
ago and has since lost power — both are offline with a recent stage. So the moment presence
decayed, a board that reached `mqtt_connected` walked back into the arriving list,
labeled **"stalled at `mqtt_connected`"**, and stayed for the rest of the 900 s
`progress_window_s`, duplicating a row the fleet list directly above it was already showing
as offline, and describing a completed arrival as a stuck one.

**The rule**. `progress.has_already_arrived(device, stage_at=…)` — a new pure predicate
alongside the two rules that module already owns. An arrival is suppressed when the board
**has a `devices` row**, **has a `broker_provisioned_at`**, **has a `last_seen`**. Its
newest stage is **not newer than** that `last_seen`. Each conjunct earns its place:

- *the row* — a board mid-arrival has none. This is the whole feature.
- *`broker_provisioned_at`* — the last step of the sequence, set by `/v1/enroll` once the
  dynsec credential lands. `enrolled_at` is in the same family but is `NOT NULL` with a
  default, so testing it decides nothing.
- *`last_seen`* — the board actually spoke to the broker once. Without it, a board that
  enrolled and was then refused by the broker would be hidden — the case that earns the
  feature.
- *`stage_at <= last_seen`* — **this is what keeps a re-flashed board visible, for free**.
  Re-enrollment deliberately leaves `last_seen` untouched (`registry.py`) and the ingestor
  only ever advances it (`GREATEST`, `ingestor/store.py`). Thus, a board reporting stages again
  after a re-flash reports them strictly newer than its stale `last_seen`.

Decommissioned rows are absent from the query the router already runs. Thus, they never match
and keep showing in arrivals — preserving the original comment's intent. No extra query:
the filter reads the same rows the fleet list builds from. No frontend change — `fleet.ts`
assigns `result.arrivals` verbatim, so the rule lives only on the server.

**T2, live against `just up` through nginx, on device `a4cf12b3dea0`:**

1. *Arriving*. `link_up` → `time_synced` → `enrolling` posted under one enrollment token →
   the board is in `arrivals` and has no fleet row at all.
2. *Arrived*. `enrolled` + `mqtt_connected` posted, then `just sim` enrolled and heartbeated
   it for 20 s → `online: true`, `arrivals` empty.
3. **The criterion**. The board then lost power ungracefully (`--crash-after 12`, so the
   broker's LWT fired, not a clean goodbye) → the fleet row reads `online: false` and
   `arrivals` is **`[]`**. Confirmed in SQL that all five stage rows sit behind `last_seen`
   with `broker_provisioned_at` set. That is,suppressed by the rule, not by falling out of the
   window.
4. **Re-flash still arrives**. A fresh `link_up` (`detail='re-flashed'`), now newer than
   `last_seen`, put the same board straight back into `arrivals` while it stayed `offline`
   in the fleet list.
5. *Vacuity, live*. Neutering the clause to `return False` in the running api brought the
   duplicate row back verbatim. Restoring it emptied `arrivals` again. Neutering it to
   `return True` and dropping the `last_seen` guard each failed exactly the intended test
   (`test_a_re_flashed_board_arrives_again`, `test_a_board_that_enrolled_but_never_connected_still_arrives`).

T1: 563 tests green (up from 556) plus ruff, `ruff format --check`, mypy. `just frontend-test`
108 green, unchanged and untouched.

**Accepted limit**. A board that reports `mqtt_connected` and dies *before* any live message
advances `last_seen` past that report still reads as arriving. It never completed a
heartbeat, so that is honest rather than wrong. Tightening it would need a rule about how
many messages count as having arrived.

### Escalation is one click (S0-fe-7)

**2026-09-11**. The fourth and last software layer of *Unaided onboarding*. S0-fe-4 named
the fault, S0-fe-5 made sure the panel saw the boot and S0-fe-6 turned the software remedies
into buttons — but when none of them helps, the operator is holding a diagnosis they cannot
get out of the page. On 2026-09-11 the brownout log *was* on screen and the fault was still
found by re-typing UART output into a chat window, because selecting text in an unlabelled
`<pre>` is not an affordance anyone finds.

**What we built**. A **Copy diagnostic bundle** button beside **Clear**. It assembles one
plain-text artifact — a header (when, which page, which browser, which server, which agent,
what the board says it is running, the device id the board reported), the fault in plain
English with a pointer to the log line that names it, the boot progress and reboot-loop
count, the chip and flash identification, the `ff_cfg` this page would write, and the entire
console log verbatim — writes it to the clipboard and renders it in a `readOnly`
`<textarea>` below the log. New pure module `frontend/src/diagnostics.ts` holds all of it.
`frontend/scripts/emit-bundle.ts` prints a bundle from the bench fixture at a shell, the same
`vite-node` idiom as `emit-ffcfg.ts`.

**Key decisions:**

- **Redaction is one choke point**. `redactSecrets` runs once over the fully assembled
  string as the last statement of `buildDiagnosticBundle`. Per-field redaction fails open —
  the next section someone adds stays unredacted by default, and the section most likely to
  carry a live credential is the raw board log, which nobody remembers to filter.
- **Three rules, because a secret arrives three ways**. A literal scrub of what the page received (`split`/`join`, never `new RegExp(secret)`: a passphrase is arbitrary text and
  `.*` would eat the bundle) with a four-character floor so a short "secret" cannot shred
  the log. URI userinfo `scheme://user:pw@host` → `user:[REDACTED]@`, which catches a broker
  password the page was never given. This is because the firmware printed it. And `ff[ae]_` token
  shapes with a `{6,}` floor so the panel's own prose about a "fresh `ffe_` token" survives.
  The bundle must not depend on the firmware's discretion — `ff-cfg` happens to print
  lengths only, but a `401` line elsewhere in the agent prints the token.
- **Two agent versions**. What this page would flash, and what the board's banner says it is
  running. They differ exactly when the board carries a stale flash, which neither number
  reveals alone.
- **`window.location.origin`, never `href`**. A path or query string can carry a token.
- **The `/v1/healthz` get is silent**. It never sets `manifestError` and never triggers
  `onSessionExpired`. A bundle that cannot name the server version is still worth pasting.
- **The textarea is a snapshot, set before the clipboard write**. The summary ticks at 1 Hz.
  Thus, a derived box would drift from what was copied. And a browser that refuses the
  clipboard still leaves the full bundle on screen, with a sentence saying to select it.
- **Deviation from the plan**. The plan's format example echoed the offending log line in
  the fault section, which made `E BOD: Brownout detector was triggered` appear four times
  while the same plan's acceptance requires exactly three — once per cycle, so "how many
  times did this board brown out?" is answerable by eye. The fault section prints the hint
  plus `named by  line N of the console log below` instead of reprinting the line.

**Files touched:** new `diagnostics.ts`, `diagnostics.test.ts`, `scripts/emit-bundle.ts`.
`BoardConsole.tsx` (the button, the snapshot state, the textarea), `FlashBoard.tsx` (the
`DiagnosticContext` and the silent healthz get), `index.css` (`textarea.log`), plus
`BoardConsole.test.tsx` and `flash.test.tsx`.

**T2 evidence**. `npx vite-node scripts/emit-bundle.ts` replays the bench session through the
real classifier, summariser and builder with fake secrets planted in the log and the config:
`E BOD: Brownout detector was triggered` appears exactly 3 times, the brownout diagnosis is
on line 11, the reboot loop and `bench-2g` are present, and neither the passphrase nor the
broker password survives — what remains is `ffe_[REDACTED]`, `mqtts://fleet:[REDACTED]@` and
`passphrase 28 chars (never printed)`. Through the rendered panel, one click on **Copy
diagnostic bundle** calls `navigator.clipboard.writeText` once with the fault text, the same
string is in the textarea. A *rejecting* clipboard still leaves the bundle on screen.
An end-to-end test drives the real `<FlashBoard>` with both seams faked and a
userinfo-bearing broker URI typed into the form.

**Vacuity-checked six ways**, each confirmed failing and reverted: deleting the redaction
call (4 tests fail, both secret greps hit), and each of the three rules on its own (3, 3 and
2 tests). Returning an empty bundle (10 tests). And dropping the snapshot `setBundle`
(2 tests). The URI-userinfo rule initially broke nothing, because the literal scrub already
covered the only password in play — the tests now include a URI the page never saw. T1: 190
frontend tests green (up from 175), `tsc -b --noEmit` and `vite build` clean. Backend
untouched.

**What this does not do**. It redacts what it can recognize. An operator who pastes a
free-form secret into the SSID field, or firmware that prints a credential in a shape none
of the three rules matches, is not covered. The defense there is that the config section
prints lengths, never values.

### Recovery is a button (S0-fe-6)

**2026-09-11**. The third layer of *Unaided onboarding*. S0-fe-4 taught the panel to name
the fault and S0-fe-5 made sure it saw the boot. But every remedy was still prose. On a
spent token the panel said "Tokens are single-use: flash the board again to mint a fresh
one". This is three manual steps (scroll back up, re-select the port, press **Flash this
board**) described to an operator who is not expected to know what a token is.

**What we built**. A fault now carries a `Remedy` alongside its hint, and the panel renders
it as one button inside the fault box. There are exactly two remedies, because the panel's
only channel to the board is the serial port: `reboot` (pulse EN through the console
session) and `reflash` (release the port, re-acquire it with esptool, mint a fresh
single-use token, write `ff_cfg` + the agent — and since S0-fw-4 it erases nothing: the
fresh token is what makes the board discard its credential, on its own, at next boot). "Retry enroll" and "mint a fresh
token" from the task text are not separate mechanisms (the agent exposes no serial command
surface, and a token that is not written into `ff_cfg` changes nothing) so both collapse
into `reflash`. `useFlashBoard` grew a `reflash(request)` that chains `connect()` into the
existing `flash()`, inheriting its mint-last and revoke-on-failure discipline unchanged.

**Key decisions:**

- **A remedy is offered only when the board will not fix itself AND the action changes the
  outcome**. `agent_main.c` decides the table: `enroll_until_credentialed()` parks forever
  on a 401/409, so only a re-flash moves that board. Everything else retries by itself
  (60 s → 15 min for a 503, forever for the link), so a button that restarts a retry
  already in progress is a button that cannot work. Brownout, wrong PSK, blocked NTP and
  DHCP silence thus render *no* button — the acceptance's second half.
- **The recovery re-flash always mints a fresh token**. A board that already enrolled keeps
  its broker credential in NVS and reuses it. Thus, a token written beside it that the board
  has seen before is dead on arrival. The operator would press the button and see the
  identical fault, the worst possible outcome for this feature. It used to force an NVS
  erase for this reason. S0-fw-4 moved the erase into the agent, which does it per
  namespace. Thus, the fresh token alone is now sufficient *and* the board keeps its cached RF
  calibration — which matters most on exactly this path, where the board is already
  misbehaving. The form's checkbox is gone. There is nothing left to override.
- **`halted:` became a *generic* hint, correcting the plan's table**. `park()` logs its
  reason strictly after the failure it reports, so on the flagship case the stopped line was
  replacing "this token is single-use, flash the board again" with the engineer-facing
  "re-flash ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)". Same derivative
  shape as `Backtrace:` and `SW_CPU_RESET`. It is the one generic hint that still carries a
  remedy. The action is the same whatever evidence names it, and a board that parks with
  nothing diagnosed above it (`no usable ff_cfg partition`, `no eFuse MAC`) has no other
  line to carry the button.
- **The log is deliberately not cleared on recovery**. The re-flashed board's first
  `ff-agent` line is a `boot` milestone, which clears the fault by S0-fe-4's existing rule —
  and if the recovery flash fails, the evidence is still on screen for S0-fe-7.
- **At most one action on screen**. The fault claims it. The overdue banner renders one only
  when the fault has none. Two identical buttons are both confusing and an ambiguous
  `getByRole` in every future test.
- **`flash()` stopped reading the `chip` state**. `reflash` calls `connect()` and `flash()`
  in the same tick. The state has not re-rendered yet. A stale chip would select the
  bundle for the previous board, and an S3 bundle on a C3 erases cleanly and never boots.
  `chipRef` is now the source of truth. `setChip` stays because the view renders from it.
- **The loop closes itself**. The recovery flash drives `phase` through `flashing` → `done`,
  so `autoWatch` goes false and true again, the panel's `armed` ref resets, and `watch()`
  re-opens the port and pulses EN. No new code.

**Files touched:** `boardConsole.ts` (`Remedy`, `REMEDY_LABELS`, `MILESTONE_REMEDY`, the
remedy on `Hint`/`ConsoleEvent`/`fault`/`MilestoneStall`), `flash.ts` (`chipRef`, `reflash`),
`flasher.ts` (the missing-gesture message), `BoardConsole.tsx` (`onReflash` /
`reflashBlockedReason` / `remedyAction`), `FlashBoard.tsx` (wiring, `eraseAll: true`),
`index.css` (`.remedy`), plus 35 new tests.

**T2 evidence**. The headline proof is *"a spent token has a fix by pressing a button, not by
following an instruction"* in `flash.test.tsx`, driven through the real `<FlashBoard>` with
both seams faked. A board flashes. The fake console emits the `enroll 409` line and the
agent's `halted:` line. The fault box names the spent token, a **Re-flash the board** button
appears, and one click closes console session #1, creates a second flasher, mints a second
token (`POST /v1/enrollment-tokens` twice), writes with `eraseAll: true` and the *second*
plaintext at `CONFIG_OFFSET`, after which console session #2 opens by itself, all five
milestones go green, `console-online` renders and the fault is gone — with neither plaintext
nor the passphrase anywhere in the DOM. Vacuity-checked four ways, each confirmed failing
and reverted: nulling the 409's remedy (no button), deleting the `await state.release()`
(port still held), forcing `eraseAll` back to the checkbox, and giving the brownout rule a
`reflash` remedy (the "no button for a brownout" test fails).

**The "on a real board" half is not runnable on this host**. The flashing bench is Windows + Chrome (originally the Mac),
and ESP32 boards do not enumerate over WebSerial on the Linux dev box. The bench script is in
the plan (flash a board twice without erase so the baked token is spent on arrival, press the
button, expect the port chooser once, the console re-opening by itself and the board reaching
**On the fleet** without the operator touching the form). S0-test-3 is the unaided version of
the same run.

### The boot appears automatically (S0-fe-5)

**2026-09-11**. The second layer of *Unaided onboarding*, filed hours after S0-fe-4 landed.
The console missed the boot it existed to show: `flash.ts` ended with `hard_reset` and
released the port, and by the time the console re-opened it as a second session, the entire
boot log was printed to nobody. The panel sat with zero events and no log region at
all—indistinguishable from a dead cable.

**What we built**. The console now pulses EN itself whenever it opens the port (both
automatic and manual), resetting the board so the log always starts from the first line.
Panel notices (the reset message, disconnect reasons) became first-class events with
`source: 'panel'` versus `'board'`, allowing the log region to render with a "Waiting for the
first line from the board…" placeholder even when no board output arrived yet. A
commanded reset sets a `commandedReset` flag consumed by the next boot boundary, preventing
the reboot-loop detector from crying wolf when the panel itself triggered the restart.

**Key decisions:**

- **Every `watch()` pulses, both automatic and manual**. A board watched five minutes after
  flash has the same silence problem. Deliberate residual: a board mid-OTA-download gets
  restarted and starts again. Acceptable at R0 (the console is a bench tool). Documented in
  DECISIONS.md so it is not later reported as a mystery.
- **The notice is appended synchronously, the pulse runs concurrently, the read loop attaches
  immediately**. Awaiting `session.reboot()` before reading would miss the first ~150 ms at
  115200 baud. Appending the notice after the pulse makes its stream position racy and breaks
  the loop suppression.
- **A failed pulse is not a fault**. `setSignals` is unsupported on some adapters. The panel
  degrades to a log notice, never to a red error that implies a board problem.
- **Native-USB re-enumeration**. C3/C6/S3 parts drop off the bus on reset and return as a new
  `SerialPort`. The disconnect message now distinguishes a reset-induced drop from a cable
  fault, but automatic re-acquire is S0-test-2 (blocked on acquiring that hardware).

**Files touched:** `boardConsole.ts` (panel notices, `requestReboot`, commanded-boot
suppression, disconnect message), `BoardConsole.tsx` (always-rendered log region + heading +
placeholder), `index.css` (`.console` min-height), `FlashBoard.tsx` (one sentence of copy),
plus new tests.

**T2 evidence:** Test 6 in `BoardConsole.test.tsx` is the centrepiece—a fake board that
emits nothing until EN is pulsed. Without the auto-reset, the test hangs at zero lines,
reproducing the bench failure. With it, the boot log arrives and the checklist completes.
Vacuity-checked by deleting the `requestReboot()` call and confirming the test fails.

### The console always names a diagnosis (S0-fe-4)

**2026-09-11**. The first layer of *Unaided onboarding*, and the P0 that gated R0. Filed
from the first hardware bench, where an ESP32-DevKit v1 sat in a brownout reset loop for a
whole session while the panel said nothing useful and showed a green **Network up**.

**Three defects, all in `frontend/src/boardConsole.ts`, and they compounded**.

1. **Tagless lines were structurally invisible**. `LOG_LINE` requires `E (1234) tag: msg`
   and `hintFor` was keyed entirely on the tag. `E BOD: Brownout detector was triggered`,
   `rst:0x…`, the boot-ROM banner and any panic backtrace have neither. Thus, they classified
   as `level: 'plain', tag: null` and could not become a fault but many times the board
   printed them. The most common first-board failure mode in existence produced zero
   diagnostic output *by construction*.
2. **The checklist reported milestones that were no longer true**. `summarizeConsole` folded
   `reached` into a `Set` nothing reset. Thus, one early boot's `link up` left ✓ **Network up**
   on screen across dozens of later resets. That is worse than silence. It sent the
   diagnosis in the wrong direction. It did.
3. **Nothing looked for the reset loop**, and no milestone had a deadline.

**What we built.**

- **`BARE_RULES`** — a table consulted for lines with no ESP-IDF preamble: brownout, panic
  (`Guru Meditation` / `assert failed:`), `Backtrace:`, `waiting for download` (the inverted
  EN/DTR case S0-test-1 watches for), `invalid header` / `flash read err`. A rule raises the
  event's level as well as supplying the hint, because `summarizeConsole` only accepts a
  fault from a warn or an error. It matches the **whole cleaned line**, not the split-off
  message. Thus, a build that does route one of these through the normal logger
  (`E (403) BOD: …`) is still caught.
- **`resetBannerRule`** — `rst:0x… (REASON)` parses for its reason. `BROWN_OUT` is an
  error with the same physical remedy. `*WDT*` is a warn. `SW_(CPU_)RESET` is a *generic*
  hint on purpose. This is because a panic prints its cause immediately before the reset that hides
  it and a specific hint there would displace the real one. `POWERON_RESET` stays plain and
  hintless. It is the normal top of every boot, and a panel that cries wolf on every board
  stops being read.
- **`reached` is per-boot**. A `bootMarker` (`'rom'` for the ROM banner, `'agent'` for the
  agent's own first line) clears it. The two markers of one boot are counted once, via a
  `romPending` flag — counting both would report every board in existence as looping.
- **`rebootLoop`** is set when a boot starts while the previous boot had not reached the
  fleet, and cleared by one that does. That is the task's "twice is proof" without crying
  wolf when the operator deliberately reboots a healthy board. It renders as its **own**
  banner rather than in the fault slot: on 2026-09-11 the board was both browning out and
  restarting. The operator needed to be told both.
- **Deadlines**. Events carry `at`. `summarizeConsole` takes `now`. `useBoardConsole` ticks
  once a second **only while watching**, so a deadline can pass with no new line arriving.
  This is exactly the stalled case. `MILESTONE_DEADLINE_MS` is grounded in the agent's own
  constants (`NET_TIMEOUT_MS` 30 s, `SNTP_TIMEOUT_MS` 15 s, `ENROLL_TIMEOUT_MS` 30 s) plus
  room for a retry: boot 5 s · link 45 s · clock 30 s · enroll 60 s · fleet 30 s. Each measures from the previous milestone. Thus, a slow link does not instantly declare the clock
  overdue. `MILESTONE_STALL` says what must happened, what usually prevents it and
  what to try.

**A fault is not cleared by a reboot, unlike a milestone**. A milestone is a positive claim
that must be true *now*. A fault is a description of something that happened. The reset
it caused does not make it untrue. Progress still clears it, as before.

**T2 — the acceptance, replayed**. `frontend/src/fixtures/bench-2026-09-11.ts` is the bench
session: three brownout cycles, the first reaching `link up`. Through the classifier and the
summary it yields fault = *"The board is browning out … not a hub"*, `rebootLoop` =
`{ boots: 3 }`, and `reached` = `['boot']` with `waitingFor` = `link` — the ✓ is gone.
Through the rendered panel (`BoardConsolePanel`, jsdom) the fault box names the brownout,
the reboot-loop banner reads *"keeps restarting — 3 times so far"*, and **Network up** is
the waiting row, not a done one. Separately, fake timers advance 47 s past a board stuck
after `wifi sta starting` with no further line, and the overdue banner names the link stall
including the 5 GHz cause.

**Vacuity-checked three ways**, each reverted: deleting the BOD rule failed the brownout
tests and nothing stood in for it. Deleting the `reached` reset failed the stale-✓ tests.
Deleting the loop assignment failed the loop tests. T1: 129 frontend tests green (up from
121), `tsc -b --noEmit` and `vite build` clean. Backend untouched.

**The fixture is a reconstruction, and says so in its header**. The session's raw UART log
was pasted into a chat window and never committed. This is itself what S0-fe-7 exists to
fix. Every line the session record quotes verbatim is verbatim. The ROM banners around them
are what a DevKit v1 prints on a brownout reset.

**What this does not do**. It names causes the board *prints*. A board that says nothing at
all is still only covered by the deadlines. The remedies are still prose rather than
buttons. That is S0-fe-6. Whether this belongs in a parser at all, versus structured faults
from firmware, stays open in `spec/open-questions.md`.

### Someone who did not see the code onboards a board unaided (S0-test-3) — **PASSED 2026-09-22**

**The criterion that actually decides *Unaided onboarding***. Everything else in this
feature is a component of it. It was deliberately written as a task rather than replaced by
the parts a test runner can check. This is because it is not automatable. Its four component tasks
(S0-fe-4 → S0-fe-7, above) had all landed before the run.

**The bar**. Two runs, no assistance and no access to the repo, using only what is on
screen. One run onboards a board end to end. The other diagnoses a deliberately induced
fault, chosen from brownout (thin cable through a hub), a wrong Wi-Fi passphrase and a spent
enrollment token. Both pass only if the operator never reads a UART log and never asks a
question. Anything they get stuck on comes back as a new S0 task. The fact that they got
stuck is the finding, not their skill.

**Result**. Passed on the bench on 2026-09-22, with no UART reading and no repo knowledge.
It was R0's last gate, so **R0 closed the same day**.

**What it did not cover**. The run used the ESP32-S3 (`94a990dd09a4`), which has native USB
and needs no driver. It never touched the bridge-chip path that every cheap
CP2102/CH340 DevKit takes. The next bench session found the gap there: on Windows a board
with no VCP driver has no COM port, and the page says nothing about it. That is `S0-fe-8`.

### A board that browns out during RF calibration cannot escape it (S0-fw-3) — **WITHDRAWN 2026-09-23, not fixed**

**Why withdrawn**. The task is about one classic ESP32-DevKit v1. That board is out of
consideration. It is old, the ESP32-S3 carries every bench path that matters. The one
remaining experiment needs the stuck DevKit v1 specifically. It was closed rather than
parked because no session would ever pick it up.

**The fault**. Found 2026-09-12 in an operator's diagnostic bundle. There were six boots,
and each one printed `phy_init: failed to load RF calibration data (0x1102), falling back to
full calibration` followed by `E BOD: Brownout detector was triggered`. The loop sustains
itself. Full calibration is the biggest current draw in startup, the rail collapses during
it. The result is only cached once a boot survives. Thus, every boot is identical.

**Two results survive the closure and must not be re-derived:**

1. **The fault is ours, not the supply's** (settled 2026-09-14). A brand-new board on the
   same cable and port ran ESPHome through a cold full RF calibration and survived. So this
   supply can carry a cold calibration, and our startup draws more current than it needs
   to. The "marginal supply / add bulk capacitance" reading retires
   (`DECISIONS.md` 2026-09-14).
2. **The reporting half shipped in v0.3.3 and is correct**. `FF_PROGRESS_BROWNOUT`
   (`ff_progress.h`) → `agent_main.c::log_power_fault()` → `DeviceProgressStage.BROWNOUT`
   → `FleetView.tsx` "recovered from a power fault". The console also shows a
   calibration-specific hint. It does nothing on healthy boards. Re-checked end to end on
   2026-09-23. Keep it.

**What did not work**. `CONFIG_ESP_PHY_REDUCE_TX_POWER=y` (v0.3.3). A 2026-09-13 bundle
contains the A/B comparison in a single log: boot 1 had the option inactive and boots 2–6
had it active. All six died at the same `phy_init` line. The lever is aimed correctly (IDF
v5.5.5 applies the reduced `init_data` *during* the full calibration). It simply does not
move this board, so TX power is not the dominant draw. Every brownout on record came from a
160 MHz, `-Og` build (agent `19b0a0b`), which predates `d705652`.

**What stays unproven: whether any board that browns out during calibration can escape**.
That is a property of the fleet, not of one board. If it appears again on a board we care
about, it comes back as a new S0 task. Untried levers, cheapest first:

1. **Flash the stock esp32 bundle** (`-Os`, 80 MHz, `REDUCE_TX_POWER`), confirmed in
   `agent/dist/esp32/sdkconfig.resolved` on 2026-09-23. At 160 MHz the CPU running flat
   out during calibration draws roughly 20–30 mA. 80 MHz applies on every boot, before
   `app_main`.
2. **Diff our `sdkconfig.resolved` against ESPHome's**. Our half is already recorded:
   `dio`/`40m` flash, no SPIRAM, no PM, `STATIC_RX_BUFFER_NUM=10`,
   `DYNAMIC_RX/TX_BUFFER_NUM=32`, `XTAL_FREQ=40`.
3. **Brownout mechanism, not threshold**. `ESP_BROWNOUT_DET_LVL=0` (2.43 V) is already the
   lowest level. Thus, there is no headroom to buy. `ESP32_REV_MIN_0` force-selects
   `ESP_BROWNOUT_USE_INTR`, though. If the board is rev ≥1 and ESPHome builds for a higher
   minimum revision, the two images use different detectors on the same silicon. The
   revision is in the boot banner.

Since S0-fw-4 the flasher erases nothing. One survived calibration is thus cached in
NVS and ends the loop for good.

### A board with no COM port is no longer a dead end (S0-fe-8) — 2026-10-01

**The gap**. Found at the bench on 2026-09-23 (Windows 11 + Chrome, a CP2102 DevKit,
`10c4:ea60`). The board enumerated as a USB device but no VCP driver was bound. Thus, the OS
had no COM port to offer and the chooser listed only the motherboard's `COM1`. Nothing on
the page explained it. The code was not at fault. `requestPort()` is called with no
filters, so Chrome already offers everything the OS has. The gap was what the page says
when what the OS has is nothing.

**What we built.**

- **"My board does not appear"** (`frontend/src/PortHelp.tsx`) sits under the port button. It
  says COM1 is never the board, that boards on the chip's own USB (S3/C3/C6) need no
  driver, and names the two bridges with their driver links (CP2102/CP2104 → Silicon
  Labs, CH340/CH9102 → WCH). It says macOS and Linux have both built in while Windows
  usually does not, then: unplug, replug, *Select port* again, and what the new entry is
  called. Last come the charge-only cable and Linux `dialout`.
- **No "identify your chip" step**. That would need Device Manager, which the acceptance
  rules out. Instead it says: not sure which chip, install both, they do not conflict.
- **It opens itself at the moment it is necessary**. That is when the chooser is dismissed
  (`NO_BOARD_SELECTED`), because an operator who sees nothing of theirs closes it, and when
  COM1 triggers a refusal. It never closes itself.
- **COM1 name refuses it** (`checkChosenPort` in `flasher.ts`, called right after
  `requestPort()` in both `esptoolFlasher.ts` and the console's prompt path). A port with
  no USB vendor id builds into the computer. Before this, esptool spent its whole sync
  window on it and then said "the board did not answer", which sends the operator to the
  BOOT button for a board that was never on the line. Ports with a USB id always go
  through. Espressif `303a`, Silicon Labs `10c4`, WCH `1a86` and FTDI `0403` appear by name in
  the log. Anything else logs as unrecognised and tried anyway.

**Evidence**. `flasher.test.ts` covers the refusal and the vendor table. Three new
`flash.test.tsx` cases cover the help starting closed with both links, the help opening on
a dismissed chooser, and COM1 refused with no chip detected and the help open. The real
`FlashBoard` was also rendered in headless Chrome with a stubbed chooser, in all three
states. The CH340 link answers 200. The Silicon Labs page answers 403 from Akamai to the
dev box (it blocks datacenter IPs), so that link is unverified from here.

**Accepted 2026-10-01 on that evidence, with the bench half still owed**. The criterion
asks for an operator who has never installed a VCP driver to reach a working COM port using
only the page. That is a Windows bench run. It is folded into `S0-test-1`, which needs
the same bridge-chip board on the same bench.

## Planned Work

### Unaided onboarding: flash → on the fleet (Priority: P0)

- **Problem:** A technician with no ESP32 knowledge cannot get a board onto the fleet
  without an engineer reading raw UART. Proven on 2026-09-11, the first real-hardware
  session: an ESP32-DevKit v1 brownouts during Wi-Fi PHY calibration and resets forever.
  The board printed the cause on every cycle (`E BOD: Brownout detector was triggered`)
  and the panel showed a green **Network up** checkmark and nothing else. The fault was
  found by pasting a serial log into a chat window. Every individual piece of R0 works.
  The *experience* of onboarding does not, and R0's stated risk is onboarding.
- **Scope:** Starts at the flasher page, ends when the board is green in the fleet list.
  Account setup, token minting and getting to the page are out of scope. The repeat path
  (board #2..#N) is out of scope for now — see Post-v1.
- **Target operator:** a technician who can connect USB and follow instructions, and who
  does not know what a brownout, a DTR line or a partition table is. That is the bar the
  work is judged against, not "an embedded engineer can figure it out".
- **Shape:** three layers, in dependency order.
  1. **Never be silent**. Every failure the board can express appears by name in plain language
     with a concrete physical or software next action — including the failures that carry
     no ESP-IDF log tag, which today are discarded before they can be classified.
  2. **Recover in place**. Where the fix is software, it is a button: retry enroll,
     re-flash, mint a fresh token, reboot. The operator must not have to know which.
  3. **Escalate cleanly**. When neither works, one click produces a diagnostic bundle (full log, config summary, chip info, firmware and server versions, the fault) with
     secrets redacted. Thus, a stuck operator can hand it to someone who can help. That
     click is the thing that did not exist on 2026-09-11.
- **Outcome:** all three layers (S0-fe-4, S0-fe-5, S0-fe-6, S0-fe-7) landed 2026-09-11.
  See *Escalation is one click* above. The unaided run that decides the feature,
  S0-test-3, **passed 2026-09-22** and closed R0 — see its entry above.
- **Added:** 2026-09-11
- **Tasks:** ~~S0-fe-4 (diagnosis)~~ done, ~~S0-fe-5 (never miss the boot)~~ done,
  ~~S0-fe-6 (recovery actions)~~ done, ~~S0-fe-7 (diagnostic bundle)~~ done,
  S0-test-3 (unaided acceptance run).
  S0-fw-2 is adjacent: the first stage a board reports can never reach an HTTPS server.

## Post-v1

- **Device decommissioning** — `POST /v1/devices/{device_id}/decommission` setting
  `decommissioned_at`, calling a new `BrokerProvisioner.delete_client`, and publishing
  empty retained payloads to each of the device's retained topics
  (`spec/device-protocol.md` → *Decommissioning*: "or the registry resurrects ghosts").
  Filed here rather than in R0: enrollment does not depend on it, but nothing else
  deletes a board.
- CLI flasher for batch/CI enrollment.
- SoftAP captive-portal provisioning (Wi-Fi change without re-flash).
- Per-device mTLS certs (replace token-only trust).

## Operator-flow additions (2026-10-04, planned, nothing built)

Implementation notes behind `spec/flows.md` Flow 1 and the 2026-10-04 entries in
`DECISIONS.md`. The spec says what the operator sees; this says what it would take.

- **Status strip (R2b-fe-1, built).** One sticky strip, first element of the page, replaces
  the footer's UI/API versions (the footer keeps only the Web Serial line). Sources:
  `buildInfo`, `GET /v1/healthz` (polled every 60 s so a tab open across a server deploy
  shows the mismatch), and the selected board's device row. "Selected" is the last board
  picked in the Fleet table, deployed to, or detected/flashed in the flasher; with nothing
  picked and exactly one board, that board. "UI and API differ" compares version and the
  8-char commit prefix, and is a word, not a colour. Signed out it shows versions only. The
  strip's state comes from the device row (presence, deploy state, `deployOutcome`) or the
  arrival row; the onboarding console milestone ("Waiting for clock (18 s)") joins in
  R2b-fe-3/fe-5. The page opens one `useFleet` (in `Dashboard`) shared by table and strip.
  Observed on dev (real Chromium): signed out `[ UI 0.4.2 · API 0.4.2 ]`, footer
  `Web Serial available`; rect top 0 after scrolling; stubbed healthz 0.4.3 shows "UI and
  API differ", gone within 58 s of removing the stub; a deploy of 1.5.0 to a sim board
  read `Board b26a938324ab · esp32c6 · fw 1.4.2 → 1.5.0 · online · updating: rebooting`,
  then `fw 1.5.0 · online · last update good`, matching the row; one `/v1/events` stream.
- **Pre-flight card (R2b-fe-2, built).** As soon as a chip is detected, a card between
  "Which board is this?" and the Flash button says what flashing will do. Four kinds
  (`data-kind`): `known` (predicted id is on the fleet: name, platform, fw, online/offline,
  an in-progress update, a layout change, and "Re-flashing issues a new token and re-enrols
  it; its current baseline ends."), `new` ("New board", plus a prior boot-progress arrival),
  `checking` (fleet not loaded or read failed; states the consequence conditionally) and
  `unknown-id` (no MAC). Sources: `predictDeviceId`, the page's one fleet (passed to
  `FlashBoard` as a prop) and the manifest. It never blocks Flash; the button reads
  "Re-flash and re-enrol this board" for `known`. "Keep identity" is not offered (no
  mechanism yet); mechanism found, see the R2b-spec-3 spike findings below. Hidden once the flash is done. Observed: `flash.test.tsx -t pre-flight`
  and `Dashboard.test.tsx` pass, one EventSource per page. Not run in a real browser (no
  board, so detect cannot run); detecting the enrolled bench S3 is folded into `R2b-test-1`.
- **Result card (R2b-fe-3, built).** One card (`ResultCard.tsx`, `data-testid="result-card"`,
  `data-outcome` = `success` / `failure` / `flash-failed`); the judgement is in
  `onboardingResult.ts`. Sources: the console (preferred: it is what runs now; facts reset
  at every boot marker), then the device row from the page's one fleet (layout, firmware
  fallback, "server: online"), then what this tab flashed, and the UI/API `VersionLine`
  from `Dashboard`. Rows: device id, firmware, partition layout, link (SSID · ip), clock
  source ("NTP (server)", "Kept across the reset (no answer from server)" on the 2026-10-04
  bench, "Not set (…)"), enrolled, on the fleet (+ server online/offline/not seen yet), UI
  and API. Show rules: success when the console reaches `fleet`; failure when not, and the
  fault has a remedy, a milestone is overdue, or the board is looping; otherwise no card
  (the checklist is the view while the board progresses). One cause per failure, a
  category chosen at the classifier (`Cause` on every specific hint: power, wifi, clock,
  server, broker, token, download-mode, firmware), then power for an unexplained loop, then
  the stalled milestone's. The card holds the ONE remedy button (moved out of the fault and
  overdue paragraphs, which keep their text) or a text next action, and the ONE "Copy
  diagnostic bundle" (the toolbar's is hidden while a failure card shows). The success card
  absorbs "This board enrolled and is on the fleet". Server-truth success when the console
  lost the port is R2b-fe-5 (built); a status-strip onboarding segment is not built and no
  task is filed. Observed: `flash.test.tsx
  -t "result card"`, `BoardConsole.test.tsx` and `onboardingResult.test.ts` pass, plus the
  full suite, typecheck and build. Not run in a real browser (no board, so no console);
  the bench S3 ending in the success card is folded into `R2b-test-1`.
- **Name from the card (R2b-fe-6, built).** Frontend only. The success result card ends with
  a "Board name" form (`NameBoard.tsx`) that calls `PATCH /v1/devices/{id}` with exactly
  `{"name": ...}` (blank clears it with `null`; `group_id` is never sent). It renders only
  when the page gave the panel the `naming` capability (`FlashBoard` with a fleet) and the
  fleet holds a row for the board, so a standalone panel and a failure card get no form.
  `boardName.ts` checks the name before the round trip (64 code points, no control
  characters, not 12 hex); duplicates are the server's 409, shown verbatim. `api.ts::detailOf`
  now turns a FastAPI 422 array into its messages. The group/tag half of Flow 1 is not built.
- **Layout profile.** Arduino IDE and PlatformIO hardcode the app offset (`0x10000`), which
  `ab-4m-v1` does not use. `ab-4m-arduino-v1` (`ff_cfg` at `0x3D0000`) exists for that
  case. Offering it at onboarding means the stock starter agent has to be built for each
  (chip, layout) pair and published as a bundle, so the build matrix grows. It depends on
  R3's library, so until R3 the flasher defaults to `ab-4m-v1` and shows the choice only
  under "Advanced".
- **Two-source watch (R2b-fe-5, built).** Frontend only (`serverWatch.ts`, `BoardConsole.tsx`,
  `FlashBoard.tsx`, `onboardingResult.ts`); no server, agent, spec or endpoint change, and no
  new EventSource. The second source already existed: `Dashboard`'s one `useFleet`
  (`GET /v1/devices` re-read on every `/v1/events` frame, plus the 10 s poll), already handed
  to the panel as `ResultContext.devices`. Rules (`describeServerView`), judged against a
  baseline snapshot of the fleet, never the browser clock: after a flash in this tab,
  *Enrolled* needs `broker_provisioned_at` set and an `enrolled_at` that differs from the
  baseline's; *On the fleet* also needs `online` and `last_seen > enrolled_at` (the board
  spoke after THIS enrolment; same server clock). With no flash here (a manual "Watch a
  board"), a held credential is enrolled and *On the fleet* needs `last_seen` to move past
  the baseline. That closes the re-flash trap: a known board's stale row says `online: true`
  with the old `enrolled_at` until `/v1/enroll` runs. The flash baseline is taken by
  `FlashBoard` on every entry into `flashing` and cleared on "Flash another board"; pressing
  "Watch a board" after a drop does NOT re-take it. The watch baseline is taken by the panel
  on the first watch with no flash baseline, kept across re-watches, dropped by Clear. A
  fleet not yet loaded at the anchor is captured on first arrival (accepted limit: it could
  already hold the new enrolment). `mergeServerView` adds `enroll` / `fleet` to the
  console's summary and re-applies the furthest-reached rule; the classifier is unchanged.
  What shows once the console stopped (acquire failed, or the stream ended): the checklist
  stays, server-marked items read `— from the server` (`data-source="server"`), a
  `console-server-view` line (`Watching the server for <id>: not enrolled yet.` / `has
  enrolled` / `sees <id> on the fleet`), the console's error text unchanged but muted once
  the server has enrolled the board, and the success card (zero console lines allowed) with
  `Enrolled: yes (from the server)` and `On the fleet: yes · server: online (the console did
  not see it)`. 90 s (`SERVER_WAIT_MS` = enroll + fleet deadlines) after the console stopped
  with the board not on the fleet, `console-server-overdue` says so. Arrival stages are not
  used for Network up / Clock set; the status strip and the diagnostic bundle are unchanged.
  Observed: `vitest run src/serverWatch.test.ts`, `src/BoardConsole.test.tsx -t "R2b-fe-5"`,
  `src/flash.test.tsx -t "R2b-fe-5"`, `src/onboardingResult.test.ts -t "R2b-fe-5"` and
  `src/Dashboard.test.tsx` (one EventSource) pass, plus the full suite, typecheck and build.
  Not run against a real board; the bench proof is `S0-test-2` Check F.
- **Boot count and reset reason (R2b-fe-4, built).** Frontend only (`boardConsole.ts`,
  `BoardConsole.tsx`, `onboardingResult.ts`, `diagnostics.ts`); no agent, server or spec change.
  `summarizeConsole` now returns `lastReset` (why the current boot started), `restarts`
  (every uncommanded boot boundary after the first boot seen, oldest first) and `retracted`
  (milestones an earlier boot had reached that a restart took away and this boot has not won
  back). Reason precedence: download mode (the `boot:` half of the banner) > what the boot
  that just ended showed (`E BOD: Brownout detector was triggered` gives brownout; a Guru
  Meditation / `assert failed:` / `abort()` line gives panic; brownout outranks panic) > the
  banner's own name > `unknown`. The agent's `the previous boot ended in a BROWNOUT` line then
  refines the current boot (and its restart) to brownout. **The SW_RESET gotcha:** with
  `CONFIG_ESP_BROWNOUT_USE_INTR=y` a real brownout prints `rst:0x3 (SW_RESET)`, so the banner
  alone would say "software restart"; the evidence is the BOD line above it and the agent's
  line below it. `RTCWDT_BROWN_OUT_RESET` is tested before `WDT`. `POWER_GLITCH_RESET`,
  `EFUSE_*` and similar are `unknown` and shown as `other (ROM_NAME)`: naming "brownout" is
  only as good as the reset reason the chip reports. A restart is an uncommanded boundary:
  this panel's own EN pulse is neither a restart nor a retraction (it empties the lost set).
  The sentence (`describeRestarts`): `Rebooted 3×`, then a stage (`during network startup`,
  `after reaching the fleet`, ...) only when every restart agrees, then `: brownout` or, for
  mixed reasons, `: brownout 2×, panic 1×`. Where it shows: the checklist (`↺ lost at the
  restart`; a lost milestone that is also the one being waited on stays `waiting` with
  `data-retracted`), a `Boot N · reset: reason` line, the reboot-loop banner headline (which
  now says restarts, not boots: "rebooted 2×" for the 2026-09-11 fixture, which has 3 boots)
  or, with no loop, a `console-restarts` line, the result card's `Restarts` row, and the
  diagnostic bundle (`last reset`, `restarts`, `lost at restart`; `boots seen` and `reboot
  loop` unchanged). Counts are within the visible 500-line window, as `boots` always was.
  Observed: `vitest run src/boardConsole.test.ts` (reason table, fixture
  `reboot-during-watch.ts`, panic after fleet, commanded, re-reached) and
  `src/BoardConsole.test.tsx -t "reboots during watch"` (real panel, "rebooted 3×: brownout",
  `Boot 4 · reset: brownout`, Clock set retracted, Network up waiting + retracted). Not run
  against a real board or browser; a real reboot during watch is folded into `R2b-test-1`.
  Out of scope: the status strip's boot count (a strip onboarding segment is not built and no
  task is filed);
  the server-side onboarding session resource (`spec/flows.md` "Onboarding is an API
  resource") is a gap with no task filed.
- **Flash failures (R2b-fe-3, built).** Only a throw from `flasher.write` gets the
  flash-failed card; failures before the write keep the plain alert paragraph. The cause is
  one of four kinds read off the esptool-js text (`flashFailure.ts`): the board
  disconnected, stopped answering, rejected a block ("did not verify"), or other, plus the
  part and address from the last progress report. The ladder, counted per board (predicted
  id, else chip name) in memory only, never in storage, and reset by a good write:
  attempt 1 says another cable or port first (and "retry at 115200" above that baud), with
  "Try the flash again"; a repeat above 115200 offers "Retry at 115200" (one click lowers
  the baud and re-flashes); a repeat at 115200 is the only place "the board's flash chip
  may be defective" appears. The connect-time "Hold BOOT" advice is never shown mid-write:
  the details line carries esptool's raw text and the token-revoked sentence. A deliberately
  bad cable/hub on the bench is folded into `R2b-test-1`.
- **Unaided re-run (R2b-test-1, owed).** The run script is `docs/runbooks/unaided-onboarding.md`
  (two runs: a known-board re-flash of `94a990dd09a4`, then a wrong-passphrase fault). What
  landed 2026-10-04 is the software half, run in a real Chromium on the dev stack at HEAD
  `4b06281`: `frontend/scripts/onboarding-rehearsal.mjs` replays real agent log lines through
  a fake `navigator.serial` and grades the result card against the S0-test-3 bar. **6/6 PASS**
  (`happy`, `bench-2026-10-04`, `brownout-loop`, `reboot-during-watch`, `wrong-psk`,
  `spent-token`): every failure card has one headline, one next action, at most one remedy
  button, one working "Copy diagnostic bundle" (clipboard non-empty) and no raw log token in
  the headline or next text; `happy` and `bench-2026-10-04` show `data-outcome=success` with
  Clock source and UI / API rows (the latter "Kept across the reset (no answer from
  pool.ntp.org)"); `happy` named a board from the card, saw it in the fleet table, and cleared
  it again (dev DB left as found); breaking one expectation made that scenario FAIL with exit 1.
  Card texts that matter: `wrong-psk` "Wi-Fi: the board could not join the network" / "Check the
  network name and passphrase in step 2 (2.4 GHz only), then re-flash."; `spent-token`
  "Enrolment refused: the token or credential is not valid" with the one button "Re-flash the
  board"; `brownout-loop` "Power: the board's supply is collapsing (brownout)" / "Use a short,
  thick USB cable straight into the computer (no hub). If nothing changes, suspect the board's
  own supply."; `reboot-during-watch` ends "Rebooted 3×: brownout", `Boot 4 · reset: brownout`,
  Network up and Clock set marked lost at the restart. Cold-read notes, none filed as defects:
  the `wrong-psk` card is the generic Wi-Fi cause even though the log line under it names the
  password (reason 15), so a board that is merely out of range reads the same; the `spent-token`
  card shows "Fill in the network details in step 2 to re-flash from here." instead of a button
  until step 2 is filled in, so an operator who reloaded the page sees text where they expect
  a button; the panel under the card still prints log-flavoured lines ("disconnected (reason
  15)", "E BOD: ...") that a technician is not meant to need. This is a proxy: it does not cover
  chip detection, the flash itself or a real native-USB re-enumeration, and it is NOT the
  acceptance. The human run waits on the R2b release to prod (prod is 0.4.2, commit `9200e0f`,
  no R2b commit), the agent publish (`S0-infra-10`: prod serves 0.3.2 for esp32s3, repo is
  0.4.5) and `S0-bug-1`'s power-cycle diagnosis. Not marked passed.
- **Diagnostic bundle.** Must be redacted: no token, Wi-Fi passphrase or broker credential
  (the CUJ-1 hard-fail trap).

### Known networks: wire proposal (R2b-spec-1, 2026-10-04) — ACCEPTED; Patch A applied in 8cb5335, Patch B in the R2b-fw-1 commit

`spec/` is protected during `/implement`, so this is the wire half of Flow 3 written as
paste-ready patches for a later `spec:` commit (the route R2-spec-1 took). **Nothing here is
built.** No agent, server, frontend, schema, migration, simulator or test change has been
made, and `spec/` is untouched. The decision is logged in `DECISIONS.md` (2026-10-04,
R2b-spec-1), as proposed only. `R2b-fw-1`, `R2b-fe-12` and `R2b-be-5` are marked blocked in
`TODO.md` until the owner accepts and applies it.

**What exists today.**

- **The `ff_cfg` reader ignores unknown keys** (`agent/main/ff_cfg.c`, header comment and
  `parse_payload()`). A new key is additive: a fielded agent that has never heard of `nets`
  still boots and joins the top-level `ssid`.
- **The header version check is terminal.** `ff_cfg_load()` refuses any
  `header.version != FF_CFG_VERSION` (1), so a version bump would idle every fielded agent.
- **One network today:** `ssid` (`FF_CFG_MAX_SSID` 33 = 32 octets + NUL) and `psk`
  (`FF_CFG_MAX_PSK` 65). An open network already works (`ff_net_wifi.c` sets
  `threshold.authmode = WIFI_AUTH_OPEN`).
- **Payload budget** is 4080 bytes (`FF_CFG_MAX_PAYLOAD`); a typical payload is a few hundred.
  `spec/open-questions.md` keeps room for a possible CA root (~1.3–2 KB RSA, under 1 KB ECDSA).
- **Wi-Fi bring-up** (`agent/main/ff_net_wifi.c`) connects directly to one SSID with no scan,
  reconnects forever on a 1 s → 30 s doubling backoff, and walks a TX-power ladder
  (`TX_LADDER_*`).
- **The announce is republished on every MQTT connect** (`ff_mqtt.c::on_connected()` calls
  `ff_identity_announce_json()`), so a board that changed network re-announces with no new
  mechanism. It is built in one place, `agent/main/ff_identity.c::announce_object()`, and the
  enroll body is that object plus `token`, so new announce fields reach `POST /v1/enroll` too.
- **The server tolerates unknown fields:** `ingestor/protocol.py::AnnouncePayload` and
  `api/schemas.py::EnrollRequest` are both `extra="ignore"`. A new agent against an old server
  is safe.
- **Credential tripwire.** `tests/test_agent_partitions.py::test_agent_holds_no_credential`
  greps `agent/` for `wifi[_-]?(ssid|password)` next to a quoted value. The proposed names
  (`nets`, `ssid`, `psk`, `known_networks`) never trip it.
- **Coupling gotcha.** `tests/test_ff_cfg.py::TestAnnounceMatchesTheSpec::test_the_firmware_builds_exactly_the_spec_keys`
  parses the JSON example under `` ### `up/announce` — identity `` and asserts that
  `ff_identity.c` emits every key in it. If the example gains `"ssid"` before R2b-fw-1 lands,
  `just test` goes red. Hence two patches: **A** (prose, test-neutral) and **B** (the two
  example lines, in the R2b-fw-1 commit).
- **Redaction gotcha.** `agent/tools/ff_cfg.py::describe()` redacts only top-level
  `SECRET_KEYS` (`token`, `psk`), so it would print a nested `nets[].psk`. The same holds for
  `ff_cfg_log()`, the frontend diagnostic bundle and `ffcfg.ts` error messages. This is an
  obligation on the follow-ups, not a spec change.
- **Key-list tripwires.** `tests/test_ff_cfg.py::TestCSourceAgrees` requires every key in
  `ff_cfg.KNOWN_KEYS` to appear in `ff_cfg.c`; `frontend/src/ffcfg.ts::KEY_ORDER` mirrors the
  list and `frontend/src/ffcfg.vector.json` is the ASCII-only golden vector. Adding `nets`
  touches all of them, in R2b-fw-1 and R2b-fe-12.

**The decisions.**

1. **Format "A": top-level first, `nets` for the rest.** The top-level `ssid` / `psk` stay
   the first, highest-priority network. A new optional key `nets` is an array of
   `{"ssid": "...", "psk": "..."}` objects holding the remaining networks in priority order.
   An old blob is a list of one with no branching, and an old agent, or a maker's R3 firmware
   that predates the list, joins network 1. No "top-level disagrees with `nets[0]`" rule is
   needed. *Rejected:* `nets` holding all networks with the top level mirroring `nets[0]`
   (two sources of truth, needs a conflict rule); `nets` only, with no top level (an old
   reader gets no SSID and idles); a version bump (every fielded reader refuses the blob).
2. **At most 4 networks in total** (top level + 3 in `nets`): home, shed, phone hotspot,
   travel router. Budget: a worst-case entry with JSON escaping is about 340 B, so 4 are about
   1.4 KB; with a worst-case baseline of about 600 B (two 159-char URIs and a 127-char token)
   about 2 KB stays free for the CA root. RAM: 3 extra fixed entries in `ff_cfg_t` are about
   300 B of struct, no heap. **Writers refuse more than 4** (`ff_cfg.py validate`, and
   `ffcfg.ts validateFfCfg` before a token is minted). **A reader that finds more uses the
   first ones it has room for and logs a warning; it never refuses to boot over length**, so a
   future flasher may raise the cap.
3. **Entry rules.** `psk` absent or `""` is an open network. Unknown keys inside an entry are
   ignored. A malformed `nets` is a bad config and the board idles loudly, consistent with
   `ff_cfg.c`'s "present but wrong type is an error" rule. Malformed: not an array, an entry
   that is not an object, a missing or empty `ssid`, a non-string field, an over-length
   `ssid` / `psk`.
4. **Selection: fixed priority (list order), not signal strength.** The board joins the first
   known network its scan sees, and within one SSID (mesh, several APs) the strongest AP. It
   **never leaves a working association** for a higher-priority network; it re-selects only
   after losing the link, which avoids flapping between overlapping APs and a link drop
   mid-OTA. A visible network that does not get the board an address (wrong passphrase, MAC
   filter) does not block the others: the next attempt takes the next visible known network.
   When no visible known network works, the networks the scan did not see are tried directly,
   in order, so **hidden SSIDs still work**; then it rescans. **With exactly one network it
   connects directly, as today**, with no scan, so the QEMU/openeth run and every
   single-network board behave identically. *Rejected:* strongest-in-range. It flaps between
   two networks at similar RSSI and makes "on: shed" unpredictable for the operator.
5. **No known network in range.** One console line per full attempt cycle, naming how many
   networks it knows, e.g. `no known network in range (2 known); scanning again in 30 s`. It
   keeps trying forever on the existing 1 s → 30 s backoff, and never reboots, opens an AP or
   captive portal, or falls back to anything. **It cannot tell the server** (no link): the
   server sees an offline board and nothing more. Only the onboarding watch and result card,
   which read the console, can say "none of its N known networks is in range"; the Fleet row
   can say "offline, last on: shed" and must not claim the cause. This corrects
   `spec/flows.md` Flow 3 step 5 and TODO `R2b-fe-13`, which implied the fleet view could see it.
6. **Announce: two optional, flat fields,** placed after `link_type` in the example.
   `ssid` (string | null) is the SSID this broker session runs over, exactly as written in
   `ff_cfg` (always valid UTF-8, since `ff_cfg` is JSON). `known_networks` (integer | null) is
   how many networks the board will try, after any it dropped for room. `known_networks` is
   **new beyond the TODO line**: Flow 3 step 5's "knows 2 networks" has no other source, since
   the server never sees `ff_cfg`. Both are `null` on `link_type: ethernet` and absent on
   agents older than R2b-fw-1; the server treats absent and `null` alike, as "not reported".
   The passphrase and the other SSIDs are never sent. Not on `up/hb`: the announce already
   repeats every session. Enroll carries both by construction; the server stores them and
   never rejects them (a malformed value is stored as null, as R2-spec-1 decided for enroll).
7. **Storage: `ff_cfg` only in v1. The agent never writes the list.** NVS vs `ff_cfg` for a
   list the agent edits stays open (R2b-spec-3 / Improv). Answered below (R2b-spec-3): NVS overlay.
8. **Downgrade safety.** An OTA that puts a pre-list agent, or a maker image without the list,
   on a board in range of only `nets[...]` can join only network 1. It never reaches the
   broker and never confirms, so the R2 confirm timer rolls it back unattended. The R3
   consequence stands: the library must read `nets`, or every OTA to a maker image works only
   within range of network 1.

All of it is additive: no field is renamed, re-typed or repurposed, `proto` stays 1, the
`ff_cfg` `version` stays 1, and the announce fields are flat (the CBOR drop-in holds).

**Obligations on the follow-ups** (notes, not new tasks).

- **R2b-fw-1:** read `nets` into a fixed array; truncate and warn above capacity; add the scan
  and selection loop, leaving the single-network path unchanged; the "no known network"
  console line; emit `ssid` / `known_networks` in `announce_object()`; redact nested `psk` in
  `ff_cfg_log()` (and `ff_cfg.py describe()`); extend `KNOWN_KEYS` and `ff_cfg.c` together.
  How selection interacts with the TX ladder is the implementer's call, but it must stay
  bounded. No scans while associated. Apply Patch B in the same commit.
- **R2b-fe-12:** extend `ffcfg.ts` (`nets`, the cap of 4, a nested `psk` never in an error
  message or the diagnostic bundle); add an ASCII golden vector with `nets`; keep the
  no-browser-storage rule.
- **R2b-be-5:** `AnnouncePayload`, `IDENTITY_FIELDS`, device columns (a migration is
  CRITICAL), the read model and the simulator (`simulator/device.py`); store, never reject, on
  enroll; validate `ssid` at ≤ 32 bytes and `known_networks` as a small non-negative integer,
  storing an invalid value as null.
- **R2b-fe-13:** the honest offline copy, "offline, last on: shed", never the cause.

**Paste-ready patches.** Generated with `git diff` against `9088e29`. Apply Patch A any time
after the owner accepts this proposal (it is test-neutral). Apply Patch B on top of A **in the
R2b-fw-1 commit**, never before: it adds `"ssid"` and `"known_networks"` to the announce
example, and `test_the_firmware_builds_exactly_the_spec_keys` fails until `ff_identity.c`
emits them. Extract each block by its marker line and `git apply` it from the repo root.

<!-- R2b-spec-1 patch A -->
```diff
diff --git a/spec/device-protocol.md b/spec/device-protocol.md
index 0a19981..f40e4ec 100644
--- a/spec/device-protocol.md
+++ b/spec/device-protocol.md
@@ -117,6 +117,15 @@ own receipt time**, never the device's timestamp — see *Clock* below.
 has no data source. `partition_layout` also lets the server detect and quarantine boards
 flashed with a superseded layout.
 
+`ssid` and `known_networks` say which network the board is on, never how it joined it.
+`ssid` is the SSID of the network this broker session runs over, as written in `ff_cfg`.
+`known_networks` is how many networks the board will try (after any it dropped for room).
+Both are `null` when `link_type` is `ethernet` and absent from agents older than the
+known-networks list. The server treats absent and `null` alike, as "not reported". The
+announce is republished on every broker connect, so a board that moved to another network
+reports it in its next session. The passphrase and the other networks' SSIDs are never
+sent.
+
 #### Partition layouts
 
 A layout id names a **whole flash map**, and it is a flash-time immutable: no OTA can
@@ -140,6 +149,36 @@ board needs before it has ever spoken to the server: API origin, broker URI, lin
 credentials and the enrollment token. The flasher writes it per board. A board finds it
 by subtype through the partition table, never by a hardcoded offset.
 
+**Known networks.** A Wi-Fi board may know several networks:
+
+```
+"ssid": "home", "psk": "…", "nets": [{"ssid": "shed", "psk": "…"}, {"ssid": "bench"}]
+```
+
+- The top-level `ssid` / `psk` are the first, highest-priority network, so an old blob is a
+  list of one. `nets` (optional) holds the rest, in priority order.
+- At most 4 networks in all. Writers refuse more. A reader that finds more uses the first
+  ones it has room for, logs a warning, and never refuses to boot over length.
+- `psk` absent or `""` is an open network. Unknown keys inside an entry are ignored. A
+  malformed `nets` (not an array, an entry that is not an object, a missing or empty
+  `ssid`, a non-string field, an over-length `ssid` / `psk`) is a bad config: the board
+  idles.
+- The `ff_cfg` header `version` stays `1`. Readers refuse any other version, so a bump
+  would idle every fielded board; readers that predate `nets` ignore it and join the
+  top-level network.
+- Selection is fixed priority: the board joins the first known network its scan sees, and
+  the strongest AP of that SSID. It never leaves a working association for a
+  higher-priority network; it re-selects only after losing the link. A visible network
+  that does not get it an address does not block the next one. Networks the scan did not
+  see are then tried directly (hidden SSIDs), and it rescans. With one network it
+  connects directly, with no scan.
+- With no known network in range the board says so on its console once per attempt cycle
+  and keeps trying on the 1 s → 30 s backoff. It never reboots, never opens an access
+  point and never falls back to anything else. With no link it cannot tell the server,
+  which sees an offline board and nothing more.
+- Passphrases are never logged, announced or sent. In v1 the agent never writes the list;
+  only the flasher does.
+
 ### `up/hb` — heartbeat
 
 ```json
diff --git a/spec/flows.md b/spec/flows.md
index 6417b52..241e108 100644
--- a/spec/flows.md
+++ b/spec/flows.md
@@ -184,8 +184,11 @@ VCS integration and the server-side compiler are **automated artifact producers*
 4. JOIN    The board scans, picks a known network it can see, and joins. The next
            `announce` names the network by SSID (never the passphrase).
 5. SEE     The Fleet row and the result card show "on: shed" and "knows 2 networks".
-           If none is in range after a deadline, the card says so in plain language
-           ("none of its 2 known networks is in range") and the board keeps trying.
+           If none is in range after a deadline, the result card (which reads the
+           console) says so in plain language ("none of its 2 known networks is in
+           range") and the board keeps trying. With no link the server cannot tell
+           out of range from powered off, so the Fleet row says only "offline, last
+           on: shed".
 ```
 
 **Later, in this order** (planned, not specified here):
@@ -197,7 +200,8 @@ VCS integration and the server-side compiler are **automated artifact producers*
 - **A network that was not on the list needs a re-flash,** until Improv lands. This is the accepted limit of v1.
 - **Passphrases stay out of the server and out of browser storage.** Only the SSID is ever reported.
 - **The board's own code must keep the list.** A maker's firmware (R3) has to read the same network list and, later, carry the Improv handler; otherwise the OTA that made the board useful would strand it on its current network.
-- **Open, not decided:** how many networks, and the selection rule (a fixed priority order, or the strongest of those in range); the `ff_cfg` format change that carries a list (a proposal to `spec/device-protocol.md`, which is protected); how a board reports "no known network in range" (the announce cannot be sent without a link); whether a network list or credentials belong in NVS or `ff_cfg` once the agent can write them.
+- **Decided (R2b-spec-1):** up to four networks, in the operator's order; the first one in range wins, and a board that has joined one stays on it until the link drops; with none in range the board keeps trying and says so on its console. Format and selection rule: [device-protocol.md](device-protocol.md) → *Known networks*.
+- **Still open:** whether a network list or credentials belong in NVS or `ff_cfg` once the agent can write them (R2b-spec-3); and how "no known network in range" could ever reach the server, since the board has no link to send it over.
 
 ## Where the pieces line up
 - The **self-test** appears in Flow 2 step 3 (sim gate) and step 6 (device confirm) — the same code, two enforcement points.
diff --git a/spec/open-questions.md b/spec/open-questions.md
index 1bfa1db..eb00ac9 100644
--- a/spec/open-questions.md
+++ b/spec/open-questions.md
@@ -77,7 +77,9 @@ records the intended posture, but nothing on the wire confirms it. A `rollback_c
 boolean in `up/announce` is additive and would let the server quarantine a board that
 lies. Unverified against a real Arduino-built board.
 
-**Field Wi-Fi change.** `flows.md` accepts re-flash for a credential change. Whether
-that holds once boards are sealed in boxes (CUJ-1) is open; see the *repeat path*
-question above. Candidate: copy `ssid`/`psk` from `ff_cfg` to NVS on enrolment and
-let `dn/cmd` `set_cfg` rewrite them. Not decided.
+**Field Wi-Fi change.** Partly answered (R2b-spec-1). A board flashed with a list of
+known networks moves between them with no re-flash ([device-protocol.md](device-protocol.md)
+→ *Known networks*, `flows.md` Flow 3). A network not on the list still needs a re-flash,
+and whether that holds once boards are sealed in boxes (CUJ-1) is open; see the *repeat
+path* question above. The writable store (NVS or `ff_cfg`), and `dn/cmd` `set_cfg` versus
+Improv as the way in, move to R2b-spec-3. Not decided.
```

<!-- R2b-spec-1 patch B -->
```diff
diff --git a/spec/device-protocol.md b/spec/device-protocol.md
index f40e4ec..9f56aba 100644
--- a/spec/device-protocol.md
+++ b/spec/device-protocol.md
@@ -103,6 +103,8 @@ own receipt time**, never the device's timestamp — see *Clock* below.
   "fw_version": "1.4.2",
   "agent_version": "0.3.2",
   "link_type": "wifi",
+  "ssid": "shed",
+  "known_networks": 2,
   "power_class": "always_on",
   "expected_wake_interval_s": null,
   "parent_device_id": null,
```

### Known networks: built in the agent (R2b-fw-1, 2026-10-05)

Agent **0.4.6**. Decision: `DECISIONS.md` 2026-10-05 (R2b-fw-1). Patch B applied in the same
commit; nothing else under `spec/` changed.

As built:

- **`ff_cfg`.** `ff_cfg_t.nets[FF_CFG_MAX_NETS = 4]`, `[0]` the top-level ssid/psk, so an old
  blob is a list of one. `parse_nets()` idles on a malformed `nets` (`config key 'nets' is not
  an array`, `config nets[N] is malformed — the board idles until it is re-flashed`), keeps the
  first 4 of a longer list with `config lists N networks; this agent keeps the first 4 and
  ignores the rest`, and never logs a value. `ff_cfg_log()` adds one line only when there is
  a list: `networks  N known: home, shed, bench` (wifi, SSIDs only) or
  `networks  N in ff_cfg, unused (link is ethernet)`.
- **Selection.** One network: the old path, unchanged, plus
  `no known network in range (1 known); trying again in N s` on reason 201. Several: scan
  once, try the seen networks in list order (strongest AP of that SSID), then the unseen ones
  directly, then one line per cycle (`no known network in range (N known); scanning again in
  N s`), then the 1 s → 30 s backoff. A 20 s DHCP watchdog moves past a network that
  associates but gives no address. No scan is ever started while associated; a working link
  is never left. Console lines: `wifi sta starting, N known networks; scanning`,
  `scan: A access points, V of N known networks in range`,
  `trying "shed" (known network 2 of 3[, not seen in the scan])`,
  `joined "shed" (known network 2 of 3)`,
  `disconnected (reason N); trying "bench" next`,
  `disconnected (reason N); link to "shed" lost; re-selecting in N ms`.
  The classifier phrase for R2b-fe-12/13 is the prefix `no known network in range (`.
- **The seam.** `ff_net_bring_up()` starts the adapter once and only waits on later calls
  (a retry used to re-run `esp_wifi_init()` and drop a GOT_IP that arrived between calls).
  `ff_net_ssid()` reports the joined SSID to the announce.
- **Announce.** `"link_type":…,"ssid":…,"known_networks":…,"power_class":…`; both null on
  ethernet; never a passphrase or the other SSIDs. The enroll body carries them too.
- **Writer.** `agent/tools/ff_cfg.py --net SSID [PSK]` (repeatable); `validate()` refuses
  what the reader idles on, more than 4, `nets` without a top-level `ssid`, and duplicates;
  messages name an index, never a value; `describe()` hides nested `psk`. The browser writer
  (`ffcfg.ts`) learns `nets` in R2b-fe-12 (a strict xfail in `tests/test_ff_cfg.py` forces
  the cleanup).

QEMU evidence (esp32, dev stack on 8088; trimmed; no secret appears in any log):

```
# A. old single-network ethernet blob, fresh board
I ff-agent: fleetforge agent 0.4.6 (idf v5.5.5)
I ff-cfg: ff_cfg v1 loaded (crc ok), 188 byte payload from 0x12000
I ff-cfg:   link      ethernet
I ff-cfg:   secrets   token 80 chars, passphrase 0 chars (never printed)
I ff-enroll: enroll 200 http://10.0.2.2:8088/v1/enroll
I ff-mqtt: announce acknowledged by the broker
up/announce {…"agent_version":"0.4.6","link_type":"ethernet","ssid":null,"known_networks":null,"power_class":"always_on",…}

# B. keep identity, --ssid home --psk … --net shed … --net bench (link ethernet)
  …, psk=<16 chars, not shown>, link='ethernet', hb_s=10, nets=[{'ssid': 'shed', 'psk': '<13 chars, not shown>'}, {'ssid': 'bench'}]
I ff-cfg:   networks  3 in ff_cfg, unused (link is ethernet)
I ff-cfg:   secrets   token 0 chars, passphrase 16 chars (never printed)
I ff-store: reusing the stored credential (no enrollment): 000000000000, …
I ff-mqtt: announce acknowledged by the broker

# C. "nets": "shed"  /  "nets": [{"ssid": "", "psk": "x"}]   (validator bypassed)
E ff-cfg: config key 'nets' is not an array
E ff-agent: halted: no usable ff_cfg partition — re-flash it (agent/tools/ff_cfg.py)
E ff-cfg: config nets[0] has an empty 'ssid'
E ff-cfg: config nets[0] is malformed — the board idles until it is re-flashed
E ff-agent: halted: no usable ff_cfg partition — re-flash it (agent/tools/ff_cfg.py)

# D. six networks in all
W ff-cfg: config lists 6 networks; this agent keeps the first 4 and ignores the rest
I ff-cfg:   networks  4 in ff_cfg, unused (link is ethernet)
I ff-mqtt: announce acknowledged by the broker
```

**Wi-Fi selection is unproven until R2b-test-4.** QEMU has no radio, so the scan/selection
loop, the DHCP watchdog, the "no known network in range" line on real radio and the
strongest-AP choice are proven only by build (-Werror on four toolchains) and the text
tripwires in `tests/test_agent_known_networks.py` until the bench run.

### Known networks: ingested and exposed (R2b-be-5, 2026-10-05)

Backend only, migration `0005`. Decision: `DECISIONS.md` 2026-10-05 (R2b-be-5). `spec/`
untouched (Patch B had already landed with R2b-fw-1).

As built:

- **Columns.** `devices.ssid TEXT NULL`, `devices.known_networks SMALLINT NULL`, no default,
  no CHECK. NULL is "not reported". Existing rows start NULL; the broker's retained
  announces fill 0.4.6 boards back in when the ingestor reconnects.
- **One normaliser, two edges.** `src/fleetforge/announce_fields.py`
  (`normalize_ssid`, `normalize_known_networks`) is called by `mode="before"` validators on
  `AnnouncePayload` and `EnrollRequest`, and it never raises. `ssid`: a `str`, 1-32 UTF-8
  bytes, no control character (NUL included), encodable (no lone surrogate), stored exactly
  as sent; `""` is "not reported". `known_networks`: an `int`, not a `bool`, `0..64`. Anything
  else is NULL and one INFO line naming the field, the reason and the device id, never the
  value: `device 0000000be502 announced an unusable ssid (33 bytes > 32); stored as null`.
- **Announce.** The pair is written on **every** announce (`store.py::_network_values`),
  so a key that is absent or null clears the stored value. This is the one exception to
  "absent means no change". The value stays between announces, so an offline board keeps
  its last network.
- **Enroll.** `IDENTITY_FIELDS` gained both, so a re-enrolment without them stores NULL. A
  malformed value is stored as NULL and returns 200, never 422.
- **Read model.** `DeviceSummary.ssid` / `.known_networks` come right after `link_type`,
  and `deploy` stays last. The PATCH response comes from the same builder. `frontend/src/api.ts` is
  unchanged: its mirror gains the two fields in R2b-fe-13.
- **Simulator.** `DeviceIdentity.ssid` / `.known_networks` always appear in the announce, in spec
  order. `just sim --ssid … --known-networks …`. A wifi sim with no flags announces
  `sim-wifi` / 1, and ethernet announces null / null.

T2 evidence (dev stack on 8088, api migrated on restart; trimmed):

```
$ psql -c "SELECT version_num FROM alembic_version"                       -> 0005
  ... information_schema.columns ...                                        -> known_networks|smallint  ssid|text
A  just sim --token … --name be5a --ssid shed --known-networks 2           -> {"link_type":"wifi","ssid":"shed","known_networks":2}
B  just sim --name be5a --ssid home --known-networks 3  (reuses .sim/)     -> {"link_type":"wifi","ssid":"home","known_networks":3}
C  mqtt-pub announce without the keys                                      -> {"link_type":"wifi","ssid":null,"known_networks":null}
D  mqtt-pub {"fw_version":"9.9.9",…,"ssid":42,"known_networks":-1}       -> psql: 9.9.9||
   ingestor: "device ? announced an unusable ssid (not a string); stored as null"
             "device ? announced an unusable known_networks (out of range); stored as null"
             (no "42" anywhere in the ingestor log; "?" because that payload had no device_id)
E  mqtt-pub {…,"device_id":"82616fc9cb49","fw_version":"9.9.10","ssid":"a\u0000b","known_networks":1}
                                                                            -> psql: 9.9.10||1, no asyncpg error
   ingestor: "device 82616fc9cb49 announced an unusable ssid (control character); stored as null"
F  just sim --token … --name be5e --link-type ethernet                     -> {"link_type":"ethernet","ssid":null,"known_networks":null}
G  curl POST /v1/enroll …"ssid":"bench","known_networks":1                 -> HTTP 200, {"ssid":"bench","known_networks":1}
H  curl POST /v1/enroll …"ssid":"aaa…(33)","known_networks":"two"          -> HTTP 200, {"ssid":null,"known_networks":null}
   api: "… unusable ssid (33 bytes > 32) …", "… unusable known_networks (not an integer) …"
I  .devices[0] | keys_unsorted -> […,"link_type","ssid","known_networks","power_class",…,"online","deploy"]
J  git diff --stat main -- spec/                                           -> (empty)
```

**Release note (deploy order).** When the ingestor runs this image against a schema older
than `0005`, every ingest fails with `UndefinedColumnError: column devices.ssid does not
exist`. The ORM maps the column in every `RETURNING` and `SELECT`. T2 hit this on dev for
about a minute: the ingestor was recreated on the new code before the api restarted and
migrated. In prod, `services/scripts/deploy.sh` recreates `INFRA_SERVICES` (which includes
`fleetforge-ingestor`) **before** `docker rollout fleetforge-api` migrates, so the same
window opens there. Restart `fleetforge-ingestor` after the api rollout so it re-subscribes
and replays the retained set against the migrated schema. See DECISIONS.

### Known networks: built in the flasher (R2b-fe-12, 2026-10-05)

Frontend only (plus `tests/test_ff_cfg.py`). Decision: `DECISIONS.md` 2026-10-05 (R2b-fe-12).
`spec/` untouched.

As built:

- **The form.** Step 2 → *Network* → Wi-Fi shows one row (*Network 1*: SSID, Passphrase) and
  "Add another network", up to 4 rows; at the cap the button disables and "A board knows at
  most 4 networks." shows. Rows 2..N have a *Remove* button; network 1 cannot be removed. A
  muted line says the priority rule: network 1 is tried first; the board joins the first
  network on the list it can see and stays on it until the link drops. Rows carry a stable
  React key, so removing row 2 never shifts a typed passphrase into another row.
- **Encoder (`ffcfg.ts`).** `KEY_ORDER` ends with `'nets'`; `MAX_NETWORKS = 4`,
  `MAX_SSID_BYTES = 32`, `MAX_PSK_BYTES = 64` (retyped; a Python tripwire greps them).
  `buildFfCfgFields` writes `nets` only when there are extra rows, so one network is
  byte-identical to the old format (tested against `ffcfg.vector.json`). An empty passphrase
  omits the entry's `psk` (open network), never `""`. Blank rows are not dropped: they are
  refused by number. `validateFfCfg` mirrors `ff_cfg.py::_validate_networks` branch for
  branch (top-level lengths in UTF-8 bytes, list type, network 1 required, cap, entry object,
  SSID non-empty/≤ 32 bytes, psk text/≤ 64 bytes, duplicates including network 1). Messages
  say "network N" in form numbering and never quote a value.
- **Ethernet writes no Wi-Fi fields.** A leftover SSID/passphrase typed before switching is not
  baked in, and a hidden row cannot block Flash. The encoder still accepts `nets` on
  ethernet (as Python does), which is what the QEMU proof uses.
- **Password manager.** Each row is its own `<form noValidate>` whose submit does nothing; the
  SSID is `autocomplete="username"` and the passphrase `type=password
  autocomplete="current-password"`, so Chrome saves each network as its own credential for
  this origin. Nothing is written by the app: no `localStorage`, `sessionStorage`,
  `indexedDB`, cookie or URL. **Autofill hazard:** the admin login on the same origin is a
  password-only form, so the browser may offer the dashboard password in a Wi-Fi field; a
  hint under the rows says to check it filled the Wi-Fi passphrase.
- **Old-agent warning.** With ≥ 2 networks and a known build older than 0.4.6
  (`agentReadsNets`, from `installFor(manifest, chip)`), a non-blocking warn line: "This board
  gets agent X, which joins only network 1. Networks 2-N need agent 0.4.6 or later."
- **Result card.** With several networks the page claims no SSID (`flashed.ssid = null`);
  `consoleFacts` reads `joined "<ssid>" (known network K of N)` (`ff_net_wifi.c:450`).
- **Diagnostic bundle.** `networks     N known: a, b, c` right after the `link` line on wifi
  with more than one network (as `ff_cfg_log()`); the secrets line keeps network 1's
  passphrase length; every row's passphrase is scrubbed.
- **Second golden vector.** `frontend/src/ffcfg.nets.vector.json` (3 networks, one open),
  digest computed by `agent/tools/ff_cfg.py`; both suites reproduce it, and
  `buildFfCfgFields` reproduces its fields byte for byte. `emit-ffcfg.ts` takes repeatable
  `--net SSID [PSK]` and prints `nets=<K networks, passphrases not shown>`.

T2 evidence (dev, agent 0.4.6, QEMU esp32, keep identity), a blob written by the browser's
encoder with `--link ethernet --ssid home --psk … --net shed … --net bench`:

```
I (5001) ff-cfg: ff_cfg v1 loaded (crc ok), 200 byte payload from 0x12000
I (5011) ff-cfg:   networks  3 in ff_cfg, unused (link is ethernet)
I (8811) ff-store: reusing the stored credential (no enrollment): …
I (8951) ff-mqtt: announce acknowledged by the broker
```

No passphrase in the log or emit output; no `halted:`. emit-ffcfg refuses 5 networks, a
duplicate of network 1 and `nets` without network 1 (rc=1, no passphrase echoed). Headless
Chromium against the dev page: 1 → 4 rows then disabled, per-row `username` /
`current-password` forms, the duplicate in row 3 named without a passphrase and Flash
disabled, storage and IndexedDB empty, Enter does not navigate. **Not proven here:** Chrome's
actual save/fill bubble (needs headed Chrome at the bench) and a real join of network 2 (no
radio in QEMU; R2b-test-4).

### Agent-written networks and keep identity: spike findings (R2b-spec-3, 2026-10-04) — FINDINGS, nothing built

No agent, server, frontend, schema, migration, simulator or test change was made, and
`spec/` is untouched. The decision is logged in `DECISIONS.md` (2026-10-04, R2b-spec-3).
The three answers, up front: **(1)** a list the running agent edits goes in an **NVS
overlay**, in its own namespace, keyed to the `ff_cfg` it extends; `ff_cfg` stays the
flasher-written base list. **(2)** "Re-flash, keep identity" already works on the device:
a flash that leaves `nvs` alone and writes an `ff_cfg` **with no token** keeps the
credential and spends nothing. It was shown in QEMU below. What is missing is a flasher mode
that does not mint. **(3)** Improv over serial needs an **input path the agent does not have**
(and on the S3's native USB the default console cannot read), a frame parser that shares the
port with the log, and the overlay from (1) as its write path. It needs no protocol change.

**Q1. `ff_cfg` or NVS for a list the agent edits.** NVS, as an overlay on top of a read-only
`ff_cfg`. The TODO's reason ("NVS is lost on Erase All Flash") does not tell the two apart:
a whole-chip erase wipes `ff_cfg` too (`0x12000` in `ab-4m-v1`, `0x3D0000` in
`ab-4m-arduino-v1`). The differences that do decide it:

| | `ff_cfg` (agent rewrites it) | NVS overlay |
|---|---|---|
| Crash safety | One 4 KB sector (`FF_CFG_PARTITION_SIZE`, frozen in `agent/partitions.csv`). A rewrite is erase + write; a power cut between them leaves an erased sector, `ff_cfg.c::ff_cfg_load` returns `ESP_ERR_NOT_SUPPORTED` and `agent_main.c::app_main` parks ("no usable ff_cfg partition"). The board has also lost `api_base`, `mqtt_uri`, link. USB is the only recovery. No in-place A/B without a format change: readers take the header at offset 0 and refuse `version != 1` | Journaled per entry; `nvs_commit` is the atomic point (S0-fw-4 already relies on it for erase + `tok_fp`) |
| Who owns it | The browser flasher rewrites it on **every** flash (the operator's declared intent). An agent edit is silently lost on the next flash | Never touched by the flasher (`frontend/src/flash.ts::assertLeavesNvsAlone`). Survives a flash, so it needs a "new flash wins" rule (below) |
| Survives OTA | yes | yes |
| Survives Arduino upload (no erase) | yes | yes |
| Survives whole-chip erase | no | no |
| Cost | none extra | NVS is already linked and used by the agent (`ff_store`, `ff_txn`); the +7460 B in `ota-library.md` was the R3 library's cost, not the agent's. 4 entries are roughly 400 B of the 24 KB partition shared with IDF's `phy` calibration |

- **The "new flash wins" rule: tag the overlay with the fingerprint of the `ff_cfg` it was
  learned on.** Store the `ff_cfg` header `crc32` (the payload CRC `ff_cfg_load` already
  checks) beside the entries. At boot a different CRC means the operator flashed a new config,
  so the overlay is discarded; an equal CRC keeps it. It is the S0-fw-4 `tok_fp` pattern and
  inherits its "absent ⇒ adopt" lesson: an OTA never writes `ff_cfg`, so the CRC does not move
  on OTA and nothing is lost. A keep-identity re-flash with **identical** fields keeps what the
  board learned; one that changes any field (a network, a URL, a token) supersedes it. Small
  agent change needed: `ff_cfg_t` does not expose the header CRC today.
- **Merge rule against the cap of 4** (R2b-spec-1 decision 2). Effective list = overlay
  entries (newest first) then the `ff_cfg` networks in their order, deduplicated by SSID (an
  overlay entry replaces a base entry's `psk`), cut at 4. What falls off is the lowest-priority
  base entry; it is logged and comes back on the next flash. The Improv-provided network goes
  first because the operator just said "this is where the board is", and since the board never
  leaves a working association (R2b-spec-1 decision 4), priority only matters after a link
  loss. `known_networks` in the announce counts the merged list after the cut.
- **Namespace: not `ff`.** `ff_store.c::ff_store_sync_token` erases `ff` on a token change,
  and a new token is about trust, not Wi-Fi. A separate namespace (e.g. `ff_net`, keys such as
  `cfg_crc`, `n`, `ssid0`/`psk0`… — namespace and key names ≤ 15 chars) keeps the credential
  eraser's blast radius as it is, and lets the R3 library read the overlay without reading the
  credential layout. The names never trip
  `tests/test_agent_partitions.py::test_agent_holds_no_credential` (`wifi[_-]?(ssid|password)`).
- **Consequences.** The R3 library must read the overlay too, so its namespace and keys become
  a contract: a spec proposal (a "board-written state" section in `spec/device-protocol.md`,
  which is CRITICAL). The R2b-spec-1 redaction obligation extends to it: no overlay `psk` in a
  log, `ff_cfg_log()`, the diagnostic bundle or an error message.
- *Rejected:* the agent rewrites `ff_cfg` (crash safety above, and the next flash erases the
  edit). *Rejected:* the overlay inside `ff` (erased with the credential; mixes two contracts).
  *Rejected:* the `spec/open-questions.md` candidate, copy `ssid`/`psk` to NVS at enrolment and
  let `dn/cmd set_cfg` rewrite them. The copy is a second source of truth that still needs the
  fingerprint rule to let a later flash win; boards already enrolled have no copy; and
  `set_cfg` would carry passphrases through the server, against Flow 3's "passphrases stay out
  of the server". Remote Wi-Fi change, if ever wanted, is a separate protocol proposal.

**Q2. Re-flash, keep identity.** The device already does it: a flash that writes no token
into `ff_cfg` and leaves `nvs` alone keeps the credential; the flasher only has to not mint.

- **Why it works** (all read, none changed). The credential (`dev_id`, `mqtt_user`,
  `mqtt_pass`, `api_base`, `enrolled_at`, `tok_fp`) is in NVS namespace `ff`
  (`agent/main/ff_store.h`). The flasher erases nothing and refuses a plan that lands in `nvs`
  (`flash.ts::assertLeavesNvsAlone`, table read by `partitionTable.ts`). `ff_store_sync_token`
  returns at once on an empty token ("a config with no token never erases anything").
  `ff_store_load` then finds the credential and logs `reusing the stored credential (no
  enrollment)`, with no HTTP. `device_id` is the eFuse MAC, stable across any flash.
- **What makes every flash re-enrol today is only the frontend.** `flash.ts` mints on every
  flash (`FlashBoard.tsx`: "Every flash mints a fresh single-use token"). Nothing else stands
  in the way: `ffcfg.ts::validateFfCfg` does not require a token, `buildFfCfgFields` omits a
  blank one, and `encodeFfCfg` requires only `api_base` and `mqtt_uri`. `agent/tools/ff_cfg.py
  --token` defaults to `""`.
- **Server side needs nothing.** No enroll call happens, so `registry.py::enroll_device`'s
  upsert and the broker password rotation never run: `enrolled_at`, `group_id`, `name` and the
  broker client are kept. The next `up/announce` refreshes the identity on its own:
  `ingestor/store.py::apply_announce` writes every `ANNOUNCE_FIELDS` entry (`proto`,
  `platform_type`, `fw_version`, `agent_version`, `link_type`, `partition_layout`,
  `ota_slot_size`, `capabilities`) plus `power_class`/`expected_wake_interval_s`, so a
  keep-identity flash to a new agent version shows correctly. **Finding:** only
  `parent_device_id` (and `group_id`, from the token) is enroll-only in `IDENTITY_FIELDS`; no
  direct-connected ESP32 sets a parent, so nothing is lost.
- **When NVS is gone, the board parks; it does not retry.** `ff_enroll.c::ff_enroll` logs
  `this board has no credential and its ff_cfg carries no enrollment token: it cannot join a
  fleet` and returns `ESP_ERR_INVALID_ARG`; `agent_main.c::enroll_until_credentialed` parks on
  it (no backoff), and `park()` repeats the `halted:` line every 300 s forever. Stage reporting
  is off without a token (`ff_progress.c::ff_progress_init`), so **the server hears nothing**:
  the device row just goes offline. The console classifier already names it
  (`frontend/src/boardConsole.ts`, `carries no enrollment` → cause `token`, remedy `reflash`);
  a keep-identity result card would offer "re-enrol instead" on that line. **Finding:** the
  `halted:` reason that follows reads "this board's enrollment token was refused for good",
  which is wrong for a tokenless board (`ESP_ERR_INVALID_ARG` shares the branch with a refused
  token). A wording fix in the agent, not filed.
- **Gotchas for any keep-identity task.**
  1. **Layout change ⇒ refuse keep-identity.** `ab-4m-v1` has `nvs` at `0x9000`/`0x6000`
     (`agent/partitions.csv`); `ab-4m-arduino-v1` has `0x9000`/`0x5000` and its `otadata` at
     `0xe000`, inside `ab-4m-v1`'s `nvs` (`design/decisions/arduino-gets-its-own-layout-id.md`).
     Either direction leaves a credential that is partly cut off or an NVS that
     `nvs_ready()` erases wholesale. The pre-flight already computes `layoutChange`
     (`preflight.ts::describePreflight`): with one, offer re-enrol only.
  2. **`api_base` must match.** `ff_store_matches_api_base` only warns; a credential from
     server A used against server B fails broker auth forever. Offer keep-identity only for a
     board that is a live row on THIS server (pre-flight `known`; a retired board is not in
     `GET /v1/devices`, and its broker client may be gone), and write the same `api_base`. The
     browser cannot read NVS to check which server issued it.
  3. **otadata reset + stale transaction record.** The bundle writes `ota-data-initial.bin` at
     `0xF000`, so a board that had OTA'd to `ota_1` boots `ota_0` after the flash, while
     namespace `ff_txn` (`FF_TXN_NAMESPACE`, not `ff`) still holds any open record.
     `ff_mqtt.c::classify_txn` finds no evidence and logs `stale transaction record for … —
     discarded`; a torn record is `incomplete transaction record … — discarded`
     (`ff_txn.c`). No outcome is reported, so the server's open deploy stays non-terminal.
     A re-enrol has exactly the same exposure (`enroll.py` does not touch deploys), so
     keep-identity is no worse; the pre-flight's `updating` should warn or block either way.
  4. **The browser cannot see NVS.** Options: (a) trust the server row (live, same layout) and,
     if the board prints the no-credential line, offer a one-click re-enrol; (b) `esptool-js`
     `readFlash` of the `nvs` range and parse NVS pages for `ff/mqtt_pass` before choosing:
     read-only, but more code and another flash read. Recommend (a); nothing read blocks it.
  5. **A whole-chip erase** (Arduino "Erase All Flash", `esptool erase_flash`) loses NVS and
     `ff_cfg`. Keep-identity after it is impossible by construction; only a new token helps.
     That is acceptable and the copy should say so plainly.
- *Rejected:* a server-side "re-flash token" bound to a `device_id` that re-issues the same
  broker credential. It needs a new endpoint and a protocol section (protected spec), and it
  turns a lost board into a credential oracle. The NVS-preserving path needs none of it.

**Evidence (QEMU, `docs/runbooks/agent-qemu.md`, esp32, agent 0.4.5 bundle `ab-4m-v1`).**
On this dev box the stack's HTTP port is 8088 (`FF_HTTP_PORT` in `.env`; 8080 belongs to
another container), so `BASE=http://localhost:8088` and `--api-base http://10.0.2.2:8088`.
Token plaintext never left the shell; the board prints fingerprints only.

```text
# E0 baseline: fresh token, just agent-cfg … --link ethernet --hb 10 --token "$FFE",
#    just agent-qemu esp32 --fresh                    used tokens before: 111
I (4452) ff-store: recording the ff_cfg enrollment token as a49bb787c7f99071; nothing was stored to invalidate
I (7322) ff-enroll: enroll 200 http://10.0.2.2:8088/v1/enroll
I (7972) ff-store: credential stored in NVS
I (8232) ff-mqtt: announce acknowledged by the broker
#    used: 112; enrolled_at 2026-10-04T19:23:56.138597Z, online   (a re-enrol costs a token and moves enrolled_at)

# E1 keep identity, ff_cfg only: just agent-qemu-stop esp32;
#    just agent-cfg … --link ethernet --hb 10   (no --token); just agent-qemu-recfg esp32; just agent-qemu esp32
wrote ff_cfg at 0x12000 — NVS untouched
I (4913) ff-cfg:   secrets   token 0 chars, passphrase 0 chars (never printed)
I (8433) ff-store: reusing the stored credential (no enrollment): 000000000000, issued 2026-10-04T19:23:56Z by http://10.0.2.2:8088
I (8513) ff-mqtt: mqtt connected as 000000000000 (mqtt://10.0.2.2:8883)
I (8553) ff-mqtt: announce acknowledged by the broker
#    ff-enroll lines: 0; used: 112 -> 112; enrolled_at unchanged; online

# E2 the browser flasher's write plan minus the token: board stopped, a throwaway script
#    wrote every manifest part (bootloader 0x1000, partition-table 0x8000, ota-data 0xf000,
#    app 0x20000) plus the tokenless ff_cfg at 0x12000 into .qemu/flash-esp32.bin,
#    asserting no part overlaps nvs 0x9000..0xf000 (read from the table) and that the nvs
#    bytes were unchanged afterwards; then just agent-qemu esp32
I (4706) ff-cfg:   secrets   token 0 chars, passphrase 0 chars (never printed)
I (9286) ff-store: reusing the stored credential (no enrollment): 000000000000, issued 2026-10-04T19:23:56Z by http://10.0.2.2:8088
I (9366) ff-mqtt: mqtt connected as 000000000000 (mqtt://10.0.2.2:8883)
I (9416) ff-mqtt: announce acknowledged by the broker
#    ff-enroll lines: 0; used: 112 -> 112; enrolled_at unchanged; online

# E3 no NVS: just agent-qemu esp32 --fresh with the tokenless ff_cfg, watched 100 s
E (5852) ff-enroll: this board has no credential and its ff_cfg carries no enrollment token: it cannot join a fleet. Re-flash ff_cfg with --token ffe_…
E (5852) ff-agent: halted: this board's enrollment token was refused for good — re-flash ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)
#    parked: one attempt, no retry line, no HTTP; used: 112 -> 112; the server row just went offline
```

The board was then restored with a new token (`--fresh`, `enroll 200`, used 112 → 113,
enrolled_at 2026-10-04T19:31:44.326993Z) and E1 was repeated: `reusing the stored credential
(no enrollment)`, no `ff-enroll` line, used 113 → 113, enrolled_at unchanged, online.

**Q3. Improv over serial on the agent.** The agent needs an RX path, a frame parser that
coexists with the log on one port, and the Q1 overlay as its write path; on the S3's native
USB the default console cannot take input, so that part of the work is a console decision.

- **Protocol** (primary source: <https://www.improv-wifi.com/serial/>, flow at
  <https://www.improv-wifi.com/>). A frame is `IMPROV`, version `1`, type, length, data,
  checksum byte. Types: `0x01` current state, `0x02` error state (device → client), `0x03` RPC
  command (client → device), `0x04` RPC result. States: `0x00` stopped, `0x02` ready, `0x03`
  provisioning, `0x04` provisioned (the result of "send Wi-Fi settings" carries a redirect URL
  as its first string, possibly empty). Errors: `0x01` invalid RPC, `0x02` unknown command,
  `0x03` unable to connect, `0x05` bad hostname, `0xFF` unknown. RPCs: `0x01` send Wi-Fi
  settings (**one** SSID + password), `0x02` current state, `0x03` device info (firmware name,
  version, chip, device name), `0x04` scanned networks (SSID, RSSI, auth; an empty result ends
  the list), `0x05` hostname, `0x06` device name, `0x07` network state (optional; unknown
  command is a valid reply). On each command the device first sends error state `0x00`. The
  serial page does not spell out the checksum algorithm; take it from the reference
  implementation, not from memory. The flow is: device powered, client sends credentials,
  device joins and returns a URL.
- **An RX path.** Today nothing reads the console. `agent/sdkconfig.defaults*` set no
  `CONFIG_ESP_CONSOLE_*`, so the IDF v5.5.5 defaults apply: UART0 primary and, on chips with
  USB-Serial-JTAG (S3, C3, C6), `ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG`, which IDF's own
  Kconfig (`components/esp_system/Kconfig`, read in the pinned image) says "currently only
  supports non-blocking mode" output, and "input through USB_SERIAL_JTAG port" needs it to be
  the primary console. The bench S3 is on native USB (no UART bridge), so Improv there needs
  either the console moved to USB-Serial-JTAG primary (an `sdkconfig.defaults*` edit: a
  CRITICAL path, auto-escalated, though not partition or eFuse: the app's console choice
  ships inside the app image, so it reaches fielded boards by OTA; the bootloader's own output
  is a separate, flash-time setting) or the `usb_serial_jtag` driver installed by the app for reads alongside the
  secondary output (to measure on the bench before choosing). The esp32 dev boards with a
  UART bridge need only a UART0 reader.
- **Framing next to `ESP_LOG`.** Frames are binary between text lines on the same port. The
  agent must write a frame atomically with respect to log output (one lock or a single
  write), and the dashboard's line splitter and classifier must skip `IMPROV` frames.
- **The write path is Q1's overlay.** "Send Wi-Fi settings" means "connect to this now": the
  board must try it, which drops the current broker session. Refuse with an error state while
  an OTA transaction is open (a `ff_txn` record exists or `ff_ota` is handling a stage, the
  check `ff_ota_is_handling` already does). Persist the entry only after the new network gives
  an address; on failure, report `0x03` unable to connect and fall back to the old list, so a
  typo never strands the board. On ethernet (QEMU) answer state stopped.
- **Budget.** A task with a ~4 KB stack that blocks on the reader, plus a parser and the
  overlay writer: an estimate of 3–6 KB of app (more if the `usb_serial_jtag` driver is not
  already linked), no build. The ratcheted per-target gate is
  `tests/test_agent_power_and_size.py::APP_SIZE_BUDGET_BYTES` (esp32 1,018,304 B against a
  1,966,080 B slot), so the Improv task raises it to the measured byte, as R2-fw-6 did.
- **Dashboard side.** The Web Serial port is held by `BoardConsole` through
  `serialConsole.ts::SerialConsole`, one reader on `port.readable`. An Improv client must share
  that reader (a demultiplexer that hands frames to Improv and lines to the console), never
  open a second one. On native USB, `esptool-js` resets by DTR/RTS and the port re-enumerates
  (S0-test-2 Check F context), so Improv must start only after the console has the port back.
- **Security.** Anyone with USB can already re-flash the board, so Improv over serial adds no
  exposure. Improv over Bluetooth does (proximity, no cable); that is for its own design.
- **No protocol change** for Improv over serial itself (it never touches MQTT or HTTP); the
  overlay contract from Q1 is the only spec-facing piece.

**Proposed follow-up tasks (not filed).** Filing is the owner's call (`/new-feature`,
`/replan`); none is in `TODO.md`.

- *Frontend: "Re-flash, keep identity".* For pre-flight `known` with no `layoutChange` (and
  no open `updating`), offer it beside re-enrol; write `ff_cfg` without a token and the same
  `api_base`; the result card offers re-enrol on the `carries no enrollment` line. Not CRITICAL
  (no token is minted; token issuance itself is unchanged).
- *Spec: the NVS overlay contract* (namespace, keys, the CRC rule, merge rule) for the agent and
  the R3 library, plus Flow 3's open item. **CRITICAL** (`spec/device-protocol.md`).
- *Agent: overlay reader and merge* (depends on R2b-fw-1 and the spec above). **CRITICAL**
  path class (agent firmware), and it must expose the `ff_cfg` header CRC.
- *Agent: Improv over serial* (RX path, parser, try-then-persist, OTA refusal). **CRITICAL**
  if it touches `agent/sdkconfig.defaults*` (the S3 console).
- *Dashboard: Improv client* sharing `SerialConsole`'s reader; a "Change Wi-Fi" action.
- *Agent wording:* the `halted:` reason for a tokenless board without a credential (Q2), and the
  stale comments `ff_cfg.h` (`token` "\"\" once used up") and `ff_progress.c` ("a board that has
  already enrolled has an empty token") that describe a convention nothing writes today.

**Proposed spec wording (not applied).** Prose, not a `git apply` patch: R2b-spec-1 Patch A
edits the same `spec/open-questions.md` paragraph, so a second patch would collide. Apply
after Patch A, as an amendment to it.

- `spec/flows.md`, Flow 1 decision "Re-flash, keep identity is a requirement, not yet a
  mechanism": "**Re-flash, keep identity** is a flash that mints no token. The board keeps its
  credential in NVS, which the flasher never writes, and an `ff_cfg` without a token never
  erases it. It is offered only for a board on this server's fleet with an unchanged
  partition layout; after a whole-chip erase only a new token helps."
- `spec/open-questions.md`, *Field Wi-Fi change*: "Networks the board learns after the flash
  (Improv) are kept in NVS as an overlay on `ff_cfg`, tagged with the `ff_cfg` CRC they were
  learned on; a flash with a different `ff_cfg` discards them. `ff_cfg` stays flasher-written.
  `dn/cmd set_cfg` is not the way in: it would carry passphrases through the server."
