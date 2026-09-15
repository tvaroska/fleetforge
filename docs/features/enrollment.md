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

### The board half of Flow 1 — the connect-only agent (R0-fw-1)

The other end of everything above: real ESP-IDF firmware that reads its config from the
`ff_cfg` partition, sets its clock, spends the `ffe_` token exactly once, stores the
broker password in NVS and comes online. It applies no firmware — `capabilities` is `[]`
and a `dn/cmd` is logged rather than executed, because a board that claims a capability
it does not have gets offered a deployment it cannot perform. OTA is R2.

**Two rules the boot sequence exists to keep.** A credential in NVS means *never enroll
again* — so `ff_store_load()` distinguishes *absent* (enroll) from *corrupt* (park), and
a torn write can never cost a second token. And the credential is written to NVS
**before** the broker is contacted: the password exists exactly once, in the HTTP
response body that was just parsed, and a crash between parse and connect would cost a
token for nothing.

**Nothing reboots on failure.** A board that reboot-loops on a revoked token is
indistinguishable from a hardware fault, and every reset throws away the serial log that
says which one it is. Every failure path either backs off (60 s → 15 min, sized so the
server's 600 s enrollment grace window contains several attempts) or parks with one line
naming what to re-flash.

**Nothing secret is ever logged.** The config dump prints `token 80 chars, passphrase 0
chars (never printed)`; the enroll response is memset before it is freed. The build-time
tripwire (`test_agent_holds_no_credential`) is what keeps it that way, and it is why the
config keys are `ssid`/`psk`/`mqtt_pass` and why one string literal in `ff_enroll.c` is
deliberately split.

**T2 evidence — a board with no board.** `docs/runbooks/agent-qemu.md` boots the *shipped*
`agent/dist/esp32` bundle in the QEMU that comes inside the pinned ESP-IDF image, on the
emulated OpenCores NIC, against the local stack. First boot: `ff_cfg v1 loaded (crc ok)`
→ `device_id` → `eth link up, ip 10.0.2.15` → `sntp: 1970-01-01T00:00:02Z → 2026-09-10…`
→ `recording the ff_cfg enrollment token as <fp>; nothing was stored to invalidate`
(S0-fw-4; on a board that has enrolled before under a *different* token this is instead the
loud `the ff_cfg enrollment token has changed (… -> …): erasing the stored credential`)
→ `enroll 200` → `credential stored in NVS` → MQTT connect, `subscribe …/dn/#`, retained
`announce` + `presence`, `hb` every 10 s (not retained). Server side: `"online": true`
with `partition_layout "ab-4m-v1"` and `ota_slot_size 1966080`, the token flipped to
`used` with `used_by_device_id`, exactly one `enrolled device` line in the api log. A
second boot on the same flash image logged `reusing the stored credential (no
enrollment)` and made no HTTP request at all; killing QEMU ungracefully flipped the fleet
view to `"online": false` within ~45 s off the retained LWT.

**What that run found.** `CONFIG_MBEDTLS_HAVE_TIME_DATE` is **off** in ESP-IDF by default,
so a board at epoch 0 completed a real TLS handshake instead of failing it — the protocol
spec's *Clock — SNTP before TLS* section was describing a failure that could not happen,
and an expired server certificate would have been accepted by the whole fleet. It is now
enabled, required by `verify_bundle.py`, and asserted in `tests/test_agent_partitions.py`.
An option like this is compiled in: no OTA adds it to a board already flashed.

### Live device list (R0-fe-2)

The screen R0 is judged on: flash a board, watch it come online without touching the
page. It is the consumer half of `R0-be-5`, and it holds that contract literally —
**a frame on `/v1/events` is only a hint that says "go re-read"**, and the re-read
(`GET /v1/devices`) is the only thing that ever sets a row. Nothing is patched from an
event payload, so the table cannot disagree with the server about a board.

**Three refresh triggers, and the boring one is load-bearing.** An event (coalesced
250 ms, so a burst of announce+presence+heartbeat is one read); `onopen`, which is the
resync after the 15-minute cap or after a listener reconnect closed every stream; and a
plain 10 s interval. The interval is not a fallback — **a sleepy board goes offline with
no event at all** (presence expiry publishes nothing), so a purely event-driven list
shows a dead board as online forever, and passes every test one would naturally write
for it. `fleet.test.tsx` has the test that fails if the interval is deleted.

**A transport failure is not "logged out"** (the R0-fe-1 rule, now applied to a
long-lived connection). Only a 401 bounces to the login form; a dead API keeps the last
list on screen under a banner and a `Reconnecting…` status. Reconnect policy mirrors the
server's own: the browser owns the retry while `EventSource` is CONNECTING, and only a
CLOSED source (a non-200 status — 401, or `sse_max_clients`) is re-opened manually,
1 s → 30 s backed off, matching `api/eventstream.py`.

**The read model is also the liveness detector.** `EventSource` cannot be trusted to
report a dead stream: behind the Vite dev proxy, stopping the api leaves the socket open
and `onerror` never fires, so the page reported `Live` at a stream that was gone and
never recovered when the api returned. A failed poll now marks the stream suspect, and
the next successful read tears the source down and rebuilds it. This was found in T2,
not in review.

**Last-seen shows single seconds on purpose.** A "just now" bucket wider than the
heartbeat interval (5 s) freezes the column, and a frozen column is indistinguishable
from a page that has stopped updating — which is the one thing this screen must make
obvious. `format.test.ts` pins it.

The stream authenticates with the same `ff_session` cookie over a **relative** URL and
no `withCredentials`: one credential, two transports, one origin. jsdom has no
`EventSource` at all, so the hook takes an injectable `EventSourceFactory` (a structural
type a real `EventSource` satisfies) — the seam exists for testability and adds no
dependency.

**T2 evidence.** Chromium against the real stack, dev (Vite) then production shape
(nginx). A board enrolled at `05:22:02` (api log) had its row in the DOM at
`05:22:02.673` through nginx — **~0.7 s, no reload, no click** — online, `1.4.2`, with
last-seen ticking 0→4 s and resetting on every heartbeat. A `--crash-after` board flipped
to `offline` **~1.4 s** after the LWT. The sleepy case is the one that matters: after the
will, `curl -N /v1/events` showed nothing but `: keepalive`, and the row still flipped to
`offline` 29 s after its last message (2.5 × 10 s wake + poll granularity). Also proved:
`docker compose restart postgres` (every stream closed by design) recovered to `Live` in
~2 s with the table never blanked; `SSE_MAX_STREAM_S=20` rolled over ~10 times in 115 s
invisibly; 12 reloads oscillated the api's open-client count 1↔2 and never climbed;
stopping the api showed `Reconnecting…` with the list intact and **no password form**,
and starting it recovered on its own.

### Web Serial flasher (R0-fe-3)

The last screen of R0's done-when: plug a board into the laptop running the dashboard,
press two buttons, and watch it appear in the fleet table above. No `esptool.py`, no
copy-pasted token, no terminal. Chromium-only — Web Serial exists nowhere else, and that
limit is accepted in `spec/prd.md`.

**Every write address comes from `GET /v1/agent/manifest`.** `builds[].parts[].offset`
for the images and `config_partition.offset` for the 4 KB `ff_cfg` blob, never a
constant: the bootloader lives at `0x1000` on ESP32 and `0x0` on the RISC-V parts, and a
hardcoded offset flashes cleanly and never boots. `planWrite` is the only place a write
is constructed and `flash.test.tsx` asserts its addresses against the manifest.

**The chip is matched, not guessed.** `chip_family` in the manifest is exactly
`ESPLoader.chip.CHIP_NAME`, so selection is `===`; there is no translation table to drift.
The board picker below it is a *label for the operator* and selects nothing on the server
— detection is chip-level, and an ESP32 DevKitC is indistinguishable from a WROVER over
serial. Flash size is checked here too, because `flashSize: 'keep'` (see below) makes
esptool-js skip its own fit check, and a 2 MB board given the 4 MB A/B layout is a
mystery boot loop rather than a clean refusal.

**The token is minted last and revoked on failure.** Order: validate the form → read the
manifest → check chip and flash → download every part and verify its sha256 → *then*
`POST /v1/enrollment-tokens`. Minting first would spend a single-use fleet-join credential
on every failed attempt; leaving a live one baked into a half-flashed board would leave an
orphan credential nobody is tracking, so a failed write revokes it and says so (the token
*id* is shown, never the plaintext). The plaintext lives in one local `const` for the
length of one call — not React state, not the log panel, not the DOM, and this feature
touches neither `localStorage` nor `sessionStorage` at all. Same rule as R0-fe-1, extended
to the Wi-Fi passphrase, and `flash.test.tsx` asserts all four places for both secrets.

**The flasher erases nothing, and the board invalidates its own credential** (S0-fw-4).
A board that already enrolled keeps its broker credential in NVS and reuses it (R0-fw-1
logs "reusing the stored credential"), so a fresh token baked into a re-flashed board has
to be made to matter somehow. It used to be the flasher's job — a part in the write plan
that filled the whole `nvs` partition with 0xFF — and that was the wrong place: NVS is
also where IDF caches the RF calibration, in its `phy` namespace, so every flash cost the
board the cold full calibration on every subsequent boot. The flasher writes raw bytes and
cannot act on one namespace; the agent can. `ff_store_sync_token()` now compares a
fingerprint of the `ff_cfg` token against the one stored beside the credential and erases
the `ff` namespace — only that namespace — when they differ. The flasher mints a fresh
token on every flash, so the operator-visible behaviour is unchanged; there is no checkbox
any more, and boards re-flashed in the field with `agent/tools/ff_cfg.py`, which a browser
flasher never reaches, are covered too.

**The form validates as it is typed**, running the same `buildFfCfgFields` +
`validateFfCfg` pair the engine runs first, and the Flash button is dead until it passes.
Belt and braces on purpose: `power=sleepy` with no wake interval is refused by
`POST /v1/enroll` (422), and reaching that refusal costs a token.

**Three implementations of the `ff_cfg` contract now exist** — `agent/tools/ff_cfg.py`
(the writer), `agent/main/ff_cfg.c` (the firmware reader) and `frontend/src/ffcfg.ts`
(the browser writer). They are held together by a golden vector,
`frontend/src/ffcfg.vector.json`: the digest in it was produced by the Python writer, and
both `frontend/src/ffcfg.test.ts` and `tests/test_ff_cfg.py::TestTypeScriptWriterAgrees`
assert it from their own side, so neither writer can move a byte alone. The vector is
ASCII-only, because that is the only region where `json.dumps` (`ensure_ascii=True`) and
`JSON.stringify` agree.

**esptool-js settings that look arbitrary and are not.** `flashMode`/`flashFreq`/
`flashSize` are all `'keep'`, or esptool-js rewrites the bootloader's flash-parameter byte
and recomputes the image SHA — the bundle bytes must reach the chip exactly as ESP-IDF
produced them. `compress: true`, so progress is reported in *compressed* bytes and must
not be compared with the manifest's `size`. No MD5 read-back verification: Web Crypto has
no MD5, and the failure that actually happens (a truncated or corrupted download) is
caught by the sha256 check before anything is written. esptool-js will log a warning that
the blob at the config offset "doesn't look like an image file" — that is the `ff_cfg`
blob, which is not an ESP image.

**Structure.** `flasher.ts` is the seam (types + `explainFlashError`, no esptool import),
`esptoolFlasher.ts` is the only file in the app that imports esptool-js or touches
`navigator.serial`, and it is loaded through a dynamic `import()` so a Firefox visitor who
can never flash anything does not download pako and the ROM stubs (~104 kB / 32 kB gzipped
in its own chunk). `flash.ts` holds every rule and runs in jsdom against a fake.
`explainFlashError` lives in the seam and not in the adapter because the commonest failure
of all — the operator dismissing the port chooser — is thrown by `requestPort()` before an
adapter object exists; untranslated it reads `Failed to execute 'requestPort' on 'Serial':
No port selected by the user.`, which sounds like a fault. It is "No board selected."

**T2 evidence.** Hardware-free, end to end: the frontend's own TypeScript encoder
(`frontend/scripts/emit-ffcfg.ts`, run with `vite-node`) produced `.qemu/ff_cfg.bin` —
4096 bytes, mode 0600, accepted by `agent/tools/ff_cfg.py::decode` — and `just agent-qemu
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

**2026-09-10.** Filed and shipped the same day the flasher's dead end was hit for real: a
board flashed cleanly — the token was minted, so every part wrote and verified — and then
went silent. No `POST /v1/enroll` ever arrived. The page said *"it should appear in the
fleet above within a few seconds"* and had nothing else to offer. Finding out why needed
`screen` on a second machine, and the cause was never isolated.

**What was built.** A fourth section on the flash page, *4 · Watch the board*. After a
flash it opens the port with no click and no user gesture, reopens at 115200, and streams
`agent_main`'s output under a five-step checklist: Agent running → Network up → Clock set
→ Enrolled → On the fleet. Plus a **Reboot the board** button and a **Release the port**
button.

**Key approach — the console is a second session, not a held flasher.** The obvious
implementation is to keep the `BoardFlasher` alive past `flash.ts`'s `finally`. That was
rejected: esptool-js's `Transport` owns the port at the *flash* baud (921600 by default),
which is not the console baud, and unwinding Rule 4 — *the port is always released* —
would leak a port on every error path in the flasher. So `flash.ts` is untouched, and the
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

**Naming the cause.** `classifyConsoleLine` strips the ANSI colours and the
`I (1234) ff-wifi: ` preamble, tags each line with a milestone and, where it can, a plain
English hint. Every string it matches exists in `agent/main/*.c`, which is what
`boardConsole.test.ts` pins. The hints that matter: `esp_wifi` disconnect reason codes
(201 → *no AP with that SSID; check the network has a 2.4 GHz band, the ESP32 radio cannot
see 5 GHz at all*; 2/15/202/204 → wrong PSK), `sntp: no answer` → the clock is unset so
TLS cannot verify, `cannot reach https://…` → *if the clock milestone is still open this
is a TLS failure caused by the unset clock, not a routing problem*, every `enroll
401/409/429/503`, `broker refused the connection`, and `halted:`.

**Design flaw the tests caught.** "Most recent explained line wins" is wrong. On a
stranded board the last line is always `agent_main.c:167`'s *"no network yet; waiting for
the link"*, reprinted every 5 s — true, useless, and it buried the reason code above it.
Hints now carry a `hintKind` of `'generic'` or `'specific'`; a generic note fills an empty
slot but never displaces a named cause. A milestone clears the fault outright, so a board
that recovers after three bad handshakes does not keep "the PSK is wrong" on screen.

**No inactivity timeout, deliberately.** `agent_main.c:166` retries the link forever at 5 s
intervals, so a Wi-Fi failure is a permanently-logging state with no window to catch — a
timer would only ever drop the slow failure it exists to find. The session ends when the
operator releases it, when the page unmounts, or when the device disappears.

**Reboot pulses EN, not IO0.** `SerialConsole.reboot()` drives RTS high with DTR held low,
which asserts EN through the standard auto-reset circuit while leaving IO0 high — the
operator wants a boot log from the first line, not a ROM download prompt.

**Release means release.** `close()` cancels the pending `read()` first (otherwise
`port.close()` is rejected for a locked stream), then closes the port, and never calls
`forget()`. Unmount does the same. Without that the device stays owned until the tab
closes and the operator's next `screen` fails with "Resource busy" for no visible reason.

**T1.** 103 frontend tests pass, 32 of them new; `tsc -b` and the vite build clean.

**T2 evidence — software half proven, hardware half deferred to S0-test-1.** Against a
fake port replaying real `agent/main/*.c` output: `autoWatch` opens exactly one session
with `acquire: 'granted'` at 115200 with no click; a full happy-path log drives all five
milestones to done and shows the "on the fleet" panel; **the 2026-09-10 silent board's own
log** (`disconnected (reason 201)` + the retry heartbeat) produces `waitingFor: 'link'` and
the 5 GHz diagnosis, which is the acceptance criterion "reproduce today's silent board and
have the page name the cause"; Release closes the port and returns the panel to "Watch a
board"; watching again never holds two ports; unmount closes; a `NotFoundError` from the
chooser renders "No board selected." rather than an empty panel.

Four properties cannot be proven in jsdom because they are properties of a USB bridge chip
and an OS: `getPorts()` re-acquisition after `hard_reset` on the native-USB parts, clean
decoding at 115200, the EN pulse landing in the app rather than the ROM loader, and
`screen` actually getting the device back. Tracked as **S0-test-1**, to be run on the Mac
(the Linux dev box does not enumerate boards over WebSerial).

### Boot & enrol stage reports (S0-fw-1)

**2026-09-10 server half, 2026-09-11 firmware half.** The companion to the serial console
above, for the board that is *not* on your bench: between "flashed" and "online" the
dashboard showed nothing at all, which is exactly the window that fails.

**What was built.** `POST /v1/device-progress` (`api/routers/progress.py`), a
`device_progress` table trimmed to 20 rows per device on write, `arrivals` riding on the
existing `GET /v1/devices` response, the fleet view's arriving list — and on the board,
`agent/main/ff_progress.c`, called from the stage transitions `agent_main.c` and
`ff_mqtt.c` already walked: `link_up` → `time_synced` → `enrolling` → `enrolled` →
`mqtt_connected`, plus `mqtt_refused` and `halted`.

**Key approach.** A pre-enrolment board holds no MQTT credential — that is what it is
trying to get — so reports travel over the same HTTPS channel and under the same `ffe_`
enrollment token as `/v1/enroll`, and the server **verifies that token without ever
burning it**. `stalled` is derived on read (60 s), never stored. The reporter is never
fatal, never retried, and self-disables after a 401. Full rationale in `DECISIONS.md`
(2026-09-10, 2026-09-11).

**T2 evidence — driven by `ff_progress.c` on an emulated board, not by curl.** The 2026-09-10
attempt could only reach the server side (`just sim` + curl) because the QEMU harness was
believed dead; S0-infra-1 showed it was not, and every acceptance below was re-run against
`agent/dist/esp32` built at a clean HEAD (`58bf54b`, `BUNDLE OK`).

*The healthy path.* One board, one token: `arrivals` shows it at `link_up` **before it
exists in the fleet at all** — the gap this feature was filed to close — then `enrolling`;
all five stages land in `device_progress` in order (15:12:59 → 15:13:01); it goes
`online: true` and **leaves `arrivals` in the same read**, because arrivals excludes
whatever `is_online` currently counts as present. The token reads `used` exactly once with
`used_by_device_id`, so four progress reports cost no enrolment. No `ffe_`, password or
passphrase string anywhere in the transcript.

*A board that gets partway, stalled at `enrolling`.* With `mosquitto` stopped,
`POST /v1/enroll` 503s on broker provisioning and the board loops — `enrolling` re-reported
at 60 s, 120 s, 240 s — showing in the dashboard as **arriving and stalled with its stage**
rather than as nothing. Restarting the broker recovered it to `enrolled` →
`mqtt_connected` → `online` on the *same single token*.

*A board that gets partway, stalled at `mqtt_refused`.* Credential rotated out from under
a board holding one in NVS: `offline` in the fleet and, in arrivals, `mqtt_refused` with
`detail='broker connack 5'` — the dashboard names the cause, not just the absence.

*Token discipline (acceptance 3), re-confirmed live.* no token → 422; garbage token → 401;
burned token from the **same** device → 202 (this is what makes `enrolled` and
`mqtt_connected` reportable at all); burned token from a **different** device → 401; an
unknown stage (`teleported`) stored verbatim, because the R0 agent is flash-baked and the
server must tolerate one it can never update; `Link Up; DROP` and a `\n` in `detail` both
422 — which is why `ff_progress.c::sanitize_detail` strips control characters before they
are sent.

**Acceptance 1 was re-worded, deliberately.** It asked for "a board flashed with a
deliberately wrong PSK". A wrong PSK means no link, and the feature's stated limit is that
a board with no route reports nothing — so the literal test asserts something this feature
does not claim, and QEMU has no radio to run it on besides. Owner-confirmed substitution:
the two *partway* cases above, which are what the feature does claim.

**The honest limit, now measured rather than asserted.** A board whose enrollment token is
refused is **invisible**: the progress 401 disables the reporter for the boot, so the
`halted` that `park()` reports never leaves the board (verified with a revoked token —
zero rows in `device_progress`, and the board silent in the dashboard). The only clue is
the `ff-progress` warning on the console, which is why S0-fe-1 and this task are
complements and neither replaces the other.

**Filed on the way.** S0-fe-3 — an offline board keeps reappearing in `arrivals` as
"stalled at `mqtt_connected`" for the 15-minute progress window, which duplicates a row
the fleet list already shows as offline. Harness fix landed here: `just agent-qemu` names
its container and refuses to start a second board, and `just agent-qemu-stop` exists,
because six concurrent emulators all claiming `000000000000` silently invalidated the
first round of this evidence (`docs/runbooks/agent-qemu.md`).

### The first stage survives the clock (S0-fw-2)

**Done 2026-09-11.** The one stage the feature above exists for — `link_up`, the report
that makes a board visible *before it exists in the fleet at all* — could never arrive at
a real server. `agent_main.c` reports it the moment the link comes up, which is before
`ff_time_sync()`, and with `CONFIG_MBEDTLS_HAVE_TIME_DATE=y` (R0-fw-1) a TLS handshake at
epoch 0 fails certificate validity. The POST never opened, the failure is logged at DEBUG
by design, and reports are never retried. It only worked in the plaintext lab it was
tested in.

**What was built.** `ff_progress.c` now holds a report the transport cannot yet carry
(3 deep, static, no malloc, oldest dropped when full) and drains it, oldest first, at the
top of the first report made once the transport is ready — **before** that report's own
POST, so the server's receipt order matches the board's. The gate is
`!tls || ff_time_is_sane()`, decided from the scheme of the built URL; a clock-only gate
would have held `link_up` forever in the `http://` + `--no-ntp` lab, which is the one
configuration where the stage always worked. A held stage gets exactly one attempt, a
failed send abandons the rest of the queue (bounding the added boot latency to one 5 s
timeout, not depth × 5 s), and a 401 clears the queue as it disarms the reporter.
`agent_main.c` is unchanged apart from comments: `link_up` is still reported at link time,
which is what makes it true.

**T2 evidence — against prod, vacuity-checked first.** With the *pre-fix* bundle and a
fresh prod token, an emulated board reached `mqtt connected` and `device_progress` held
`time_synced` → `enrolling` → `enrolled` → `mqtt_connected` and **no `link_up`**: the bug,
reproduced on the deployed server. After the fix (rebuild, `BUNDLE OK`, fresh token, same
commands) the same query returned `link_up|ethernet` **first** at 00:42:24, ahead of
`time_synced` at 00:42:27 — held over the sync and flushed with it, three seconds late and
in exact order — followed by the rest of the sequence to `mqtt_connected`, with a `devices`
row and `broker_provisioned_at` set. The plaintext lab was re-run to prove the trap: over
`http://` with `--no-ntp` the clock stays at 1970 and `link_up` still lands *immediately*
(170 ms ahead of `time_synced`), not held.

**Not deployed by this commit.** `agent/dist/` is gitignored, and the flasher serves the
bundle baked into the app image (`COPY agent/dist /app/agent`, R0-infra-2), so prod keeps
handing out firmware with this bug until the next release build. That coupling is what the
*agent bundles are artifacts* decision (DECISIONS.md, 2026-09-11) exists to remove.

> **Removed by S0-infra-6 (2026-09-15):** the bundles are artifacts in the object store
> now. A firmware fix reaches the flasher with `just agent-publish <target>` — no app
> image, no release build, no restart.

### An arrival that finished stops arriving (S0-fe-3)

**Done 2026-09-11.** Filed by the S0-fw-1 verification above, and it is the second clause
of the arrivals rule rather than a bug fix.

**The problem.** `arrivals` showed every board with a recent stage that was *not currently
online*. That one clause cannot tell a board on its way up from one that came up an hour
ago and has since lost power — both are offline with a recent stage. So the moment presence
decayed, a board that had reached `mqtt_connected` walked back into the arriving list,
labelled **"stalled at `mqtt_connected`"**, and stayed for the rest of the 900 s
`progress_window_s`, duplicating a row the fleet list directly above it was already showing
as offline, and describing a completed arrival as a stuck one.

**The rule.** `progress.has_already_arrived(device, stage_at=…)` — a new pure predicate
alongside the two rules that module already owns. An arrival is suppressed when the board
**has a `devices` row**, **has a `broker_provisioned_at`**, **has a `last_seen`**, and its
newest stage is **not newer than** that `last_seen`. Each conjunct earns its place:

- *the row* — a board mid-arrival has none, which is the whole feature;
- *`broker_provisioned_at`* — the last step of the sequence, set by `/v1/enroll` once the
  dynsec credential lands. `enrolled_at` is in the same family but is `NOT NULL` with a
  default, so testing it decides nothing;
- *`last_seen`* — the board actually spoke to the broker once. Without it, a board that
  enrolled and was then refused by the broker would be hidden — the case that earns the
  feature;
- *`stage_at <= last_seen`* — **this is what keeps a re-flashed board visible, for free.**
  Re-enrolment deliberately leaves `last_seen` untouched (`registry.py`) and the ingestor
  only ever advances it (`GREATEST`, `ingestor/store.py`), so a board reporting stages again
  after a re-flash reports them strictly newer than its stale `last_seen`.

Decommissioned rows are absent from the query the router already runs, so they never match
and keep showing in arrivals — preserving the original comment's intent. No extra query:
the filter reads the same rows the fleet list is built from. No frontend change — `fleet.ts`
assigns `result.arrivals` verbatim, so the rule lives only on the server.

**T2, live against `just up` through nginx, on device `a4cf12b3dea0`:**

1. *Arriving.* `link_up` → `time_synced` → `enrolling` posted under one enrollment token →
   the board is in `arrivals` and has no fleet row at all.
2. *Arrived.* `enrolled` + `mqtt_connected` posted, then `just sim` enrolled and heartbeated
   it for 20 s → `online: true`, `arrivals` empty.
3. **The criterion.** The board then lost power ungracefully (`--crash-after 12`, so the
   broker's LWT fired, not a clean goodbye) → the fleet row reads `online: false` and
   `arrivals` is **`[]`**. Confirmed in SQL that all five stage rows sit behind `last_seen`
   with `broker_provisioned_at` set — i.e. suppressed by the rule, not by falling out of the
   window.
4. **Re-flash still arrives.** A fresh `link_up` (`detail='re-flashed'`), now newer than
   `last_seen`, put the same board straight back into `arrivals` while it stayed `offline`
   in the fleet list.
5. *Vacuity, live.* Neutering the clause to `return False` in the running api brought the
   duplicate row back verbatim; restoring it emptied `arrivals` again. Neutering it to
   `return True` and dropping the `last_seen` guard each failed exactly the intended test
   (`test_a_re_flashed_board_arrives_again`, `test_a_board_that_enrolled_but_never_connected_still_arrives`).

T1: 563 tests green (up from 556) plus ruff, `ruff format --check`, mypy; `just frontend-test`
108 green, unchanged and untouched.

**Accepted limit.** A board that reports `mqtt_connected` and dies *before* any live message
advances `last_seen` past that report still reads as arriving. It never completed a
heartbeat, so that is honest rather than wrong; tightening it would need a rule about how
many messages count as having arrived.

### Escalation is one click (S0-fe-7)

**2026-09-11.** The fourth and last software layer of *Unaided onboarding*. S0-fe-4 named
the fault, S0-fe-5 made sure the panel saw the boot and S0-fe-6 turned the software remedies
into buttons — but when none of them helps, the operator is holding a diagnosis they cannot
get out of the page. On 2026-09-11 the brownout log *was* on screen and the fault was still
found by re-typing UART output into a chat window, because selecting text in an unlabelled
`<pre>` is not an affordance anyone finds.

**What was built.** A **Copy diagnostic bundle** button beside **Clear**. It assembles one
plain-text artifact — a header (when, which page, which browser, which server, which agent,
what the board says it is running, the device id the board reported), the fault in plain
English with a pointer to the log line that names it, the boot progress and reboot-loop
count, the chip and flash identification, the `ff_cfg` this page would write, and the entire
console log verbatim — writes it to the clipboard and renders it in a `readOnly`
`<textarea>` below the log. New pure module `frontend/src/diagnostics.ts` holds all of it;
`frontend/scripts/emit-bundle.ts` prints a bundle from the bench fixture at a shell, the same
`vite-node` idiom as `emit-ffcfg.ts`.

**Key decisions:**

- **Redaction is one choke point.** `redactSecrets` runs once over the fully assembled
  string as the last statement of `buildDiagnosticBundle`. Per-field redaction fails open —
  the next section someone adds is unredacted by default, and the section most likely to
  carry a live credential is the raw board log, which nobody remembers to filter.
- **Three rules, because a secret arrives three ways.** A literal scrub of what the page was
  handed (`split`/`join`, never `new RegExp(secret)`: a passphrase is arbitrary text and
  `.*` would eat the bundle) with a four-character floor so a short "secret" cannot shred
  the log; URI userinfo `scheme://user:pw@host` → `user:[REDACTED]@`, which catches a broker
  password the page was never given because the firmware printed it; and `ff[ae]_` token
  shapes with a `{6,}` floor so the panel's own prose about a "fresh `ffe_` token" survives.
  The bundle must not depend on the firmware's discretion — `ff-cfg` happens to print
  lengths only, but a `401` line elsewhere in the agent prints the token.
- **Two agent versions.** What this page would flash, and what the board's banner says it is
  running. They differ exactly when the board carries a stale flash, which neither number
  reveals alone.
- **`window.location.origin`, never `href`.** A path or query string can carry a token.
- **The `/v1/healthz` fetch is silent.** It never sets `manifestError` and never triggers
  `onSessionExpired`: a bundle that cannot name the server version is still worth pasting.
- **The textarea is a snapshot, set before the clipboard write.** The summary ticks at 1 Hz,
  so a derived box would drift from what was copied; and a browser that refuses the
  clipboard still leaves the full bundle on screen, with a sentence saying to select it.
- **Deviation from the plan.** The plan's format example echoed the offending log line in
  the fault section, which made `E BOD: Brownout detector was triggered` appear four times
  while the same plan's acceptance requires exactly three — once per cycle, so "how many
  times did this board brown out?" is answerable by eye. The fault section prints the hint
  plus `named by  line N of the console log below` instead of reprinting the line.

**Files touched:** new `diagnostics.ts`, `diagnostics.test.ts`, `scripts/emit-bundle.ts`;
`BoardConsole.tsx` (the button, the snapshot state, the textarea), `FlashBoard.tsx` (the
`DiagnosticContext` and the silent healthz fetch), `index.css` (`textarea.log`), plus
`BoardConsole.test.tsx` and `flash.test.tsx`.

**T2 evidence.** `npx vite-node scripts/emit-bundle.ts` replays the bench session through the
real classifier, summariser and builder with fake secrets planted in the log and the config:
`E BOD: Brownout detector was triggered` appears exactly 3 times, the brownout diagnosis is
on line 11, the reboot loop and `bench-2g` are present, and neither the passphrase nor the
broker password survives — what is left is `ffe_[REDACTED]`, `mqtts://fleet:[REDACTED]@` and
`passphrase 28 chars (never printed)`. Through the rendered panel, one click on **Copy
diagnostic bundle** calls `navigator.clipboard.writeText` once with the fault text, the same
string is in the textarea, and a *rejecting* clipboard still leaves the bundle on screen.
An end-to-end test drives the real `<FlashBoard>` with both seams faked and a
userinfo-bearing broker URI typed into the form.

**Vacuity-checked six ways**, each confirmed failing and reverted: deleting the redaction
call (4 tests fail, both secret greps hit), and each of the three rules on its own (3, 3 and
2 tests); returning an empty bundle (10 tests); and dropping the snapshot `setBundle`
(2 tests). The URI-userinfo rule initially broke nothing, because the literal scrub already
covered the only password in play — the tests now include a URI the page never saw. T1: 190
frontend tests green (up from 175), `tsc -b --noEmit` and `vite build` clean. Backend
untouched.

**What this does not do.** It redacts what it can recognise. An operator who pastes a
free-form secret into the SSID field, or firmware that prints a credential in a shape none
of the three rules matches, is not covered — the defence there is that the config section
prints lengths, never values.

### Recovery is a button (S0-fe-6)

**2026-09-11.** The third layer of *Unaided onboarding*. S0-fe-4 taught the panel to name
the fault and S0-fe-5 made sure it saw the boot — but every remedy was still prose. On a
spent token the panel said "Tokens are single-use: flash the board again to mint a fresh
one", which is three manual steps (scroll back up, re-select the port, press **Flash this
board**) described to an operator who is not expected to know what a token is.

**What was built.** A fault now carries a `Remedy` alongside its hint, and the panel renders
it as one button inside the fault box. There are exactly two remedies, because the panel's
only channel to the board is the serial port: `reboot` (pulse EN through the console
session) and `reflash` (release the port, re-acquire it with esptool, mint a fresh
single-use token, write `ff_cfg` + the agent — and since S0-fw-4 it erases nothing: the
fresh token is what makes the board discard its credential, on its own, at next boot). "Retry enrol" and "mint a fresh
token" from the task text are not separate mechanisms — the agent exposes no serial command
surface, and a token that is not written into `ff_cfg` changes nothing — so both collapse
into `reflash`. `useFlashBoard` grew a `reflash(request)` that chains `connect()` into the
existing `flash()`, inheriting its mint-last and revoke-on-failure discipline unchanged.

**Key decisions:**

- **A remedy is offered only when the board will not fix itself AND the action changes the
  outcome.** `agent_main.c` decides the table: `enroll_until_credentialed()` parks forever
  on a 401/409, so only a re-flash moves that board; everything else retries by itself
  (60 s → 15 min for a 503, forever for the link), so a button that restarts a retry
  already in progress is a button that cannot work. Brownout, wrong PSK, blocked NTP and
  DHCP silence therefore render *no* button — the acceptance's second half.
- **The recovery re-flash always mints a fresh token.** A board that already enrolled keeps
  its broker credential in NVS and reuses it, so a token written beside it that the board
  has seen before is dead on arrival — and the operator would press the button and see the
  identical fault, the worst possible outcome for this feature. It used to force an NVS
  erase for this reason; S0-fw-4 moved the erase into the agent, which does it per
  namespace, so the fresh token alone is now sufficient *and* the board keeps its cached RF
  calibration — which matters most on exactly this path, where the board is already
  misbehaving. The form's checkbox is gone; there is nothing left to override.
- **`halted:` became a *generic* hint, correcting the plan's table.** `park()` logs its
  reason strictly after the failure it reports, so on the flagship case the halted line was
  replacing "this token is single-use, flash the board again" with the engineer-facing
  "re-flash ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)". Same derivative
  shape as `Backtrace:` and `SW_CPU_RESET`. It is the one generic hint that still carries a
  remedy: the action is the same whatever evidence names it, and a board that parks with
  nothing diagnosed above it (`no usable ff_cfg partition`, `no eFuse MAC`) has no other
  line to carry the button.
- **The log is deliberately not cleared on recovery.** The re-flashed board's first
  `ff-agent` line is a `boot` milestone, which clears the fault by S0-fe-4's existing rule —
  and if the recovery flash fails, the evidence is still on screen for S0-fe-7.
- **At most one action on screen.** The fault claims it; the overdue banner renders one only
  when the fault has none. Two identical buttons are both confusing and an ambiguous
  `getByRole` in every future test.
- **`flash()` stopped reading the `chip` state.** `reflash` calls `connect()` and `flash()`
  in the same tick, and the state has not re-rendered yet — a stale chip would select the
  bundle for the previous board, and an S3 bundle on a C3 erases cleanly and never boots.
  `chipRef` is now the source of truth; `setChip` stays because the view renders from it.
- **The loop closes itself.** The recovery flash drives `phase` through `flashing` → `done`,
  so `autoWatch` goes false and true again, the panel's `armed` ref resets, and `watch()`
  re-opens the port and pulses EN. No new code.

**Files touched:** `boardConsole.ts` (`Remedy`, `REMEDY_LABELS`, `MILESTONE_REMEDY`, the
remedy on `Hint`/`ConsoleEvent`/`fault`/`MilestoneStall`), `flash.ts` (`chipRef`, `reflash`),
`flasher.ts` (the missing-gesture message), `BoardConsole.tsx` (`onReflash` /
`reflashBlockedReason` / `remedyAction`), `FlashBoard.tsx` (wiring, `eraseAll: true`),
`index.css` (`.remedy`), plus 35 new tests.

**T2 evidence.** The headline proof is *"a spent token is fixed by pressing a button, not by
following an instruction"* in `flash.test.tsx`, driven through the real `<FlashBoard>` with
both seams faked. A board is flashed; the fake console emits the `enroll 409` line and the
agent's `halted:` line; the fault box names the spent token, a **Re-flash the board** button
appears, and one click closes console session #1, creates a second flasher, mints a second
token (`POST /v1/enrollment-tokens` twice), writes with `eraseAll: true` and the *second*
plaintext at `CONFIG_OFFSET`, after which console session #2 opens by itself, all five
milestones go green, `console-online` renders and the fault is gone — with neither plaintext
nor the passphrase anywhere in the DOM. Vacuity-checked four ways, each confirmed failing
and reverted: nulling the 409's remedy (no button), removing the `await state.release()`
(port still held), forcing `eraseAll` back to the checkbox, and giving the brownout rule a
`reflash` remedy (the "no button for a brownout" test fails).

**The "on a real board" half is not runnable on this host** — the flashing bench is the Mac,
and ESP32 boards do not enumerate over WebSerial on the Linux dev box. The bench script is in
the plan (flash a board twice without erase so the baked token is spent on arrival; press the
button; expect the port chooser once, the console re-opening by itself and the board reaching
**On the fleet** without the operator touching the form). S0-test-3 is the unaided version of
the same run.

### The boot appears automatically (S0-fe-5)

**2026-09-11.** The second layer of *Unaided onboarding*, filed hours after S0-fe-4 landed.
The console missed the boot it existed to show: `flash.ts` ended with `hard_reset` and
released the port, and by the time the console re-opened it as a second session, the entire
boot log had been printed to nobody. The panel sat with zero events and no log region at
all—indistinguishable from a dead cable.

**What was built.** The console now pulses EN itself whenever it opens the port (both
automatic and manual), resetting the board so the log always starts from the first line.
Panel notices (the reset message, disconnect reasons) became first-class events with
`source: 'panel'` vs `'board'`, allowing the log region to render with a "Waiting for the
first line from the board…" placeholder even when no board output has arrived yet. A
commanded reset sets a `commandedReset` flag consumed by the next boot boundary, preventing
the reboot-loop detector from crying wolf when the panel itself triggered the restart.

**Key decisions:**

- **Every `watch()` pulses, both automatic and manual.** A board watched five minutes after
  flash has the same silence problem. Deliberate residual: a board mid-OTA-download gets
  restarted and starts again. Acceptable at R0 (the console is a bench tool); documented in
  DECISIONS.md so it's not later reported as a mystery.
- **The notice is appended synchronously, the pulse runs concurrently, the read loop attaches
  immediately.** Awaiting `session.reboot()` before reading would miss the first ~150 ms at
  115200 baud; appending the notice after the pulse makes its stream position racy and breaks
  the loop suppression.
- **A failed pulse is not a fault.** `setSignals` is unsupported on some adapters. The panel
  degrades to a log notice, never to a red error that implies a board problem.
- **Native-USB re-enumeration.** C3/C6/S3 parts drop off the bus on reset and return as a new
  `SerialPort`. The disconnect message now distinguishes a reset-induced drop from a cable
  fault, but automatic re-acquire is S0-test-2 (blocked on acquiring that hardware).

**Files touched:** `boardConsole.ts` (panel notices, `requestReboot`, commanded-boot
suppression, disconnect message), `BoardConsole.tsx` (always-rendered log region + heading +
placeholder), `index.css` (`.console` min-height), `FlashBoard.tsx` (one sentence of copy),
plus new tests.

**T2 evidence:** Test 6 in `BoardConsole.test.tsx` is the centrepiece—a fake board that
emits nothing until EN is pulsed. Without the auto-reset, the test hangs at zero lines,
reproducing the bench failure. With it, the boot log arrives and the checklist completes.
Vacuity-checked by removing the `requestReboot()` call and confirming the test fails.

### The console always names a diagnosis (S0-fe-4)

**2026-09-11.** The first layer of *Unaided onboarding*, and the P0 that gated R0. Filed
from the first hardware bench, where an ESP32-DevKit v1 sat in a brownout reset loop for a
whole session while the panel said nothing useful and showed a green **Network up**.

**Three defects, all in `frontend/src/boardConsole.ts`, and they compounded.**

1. **Tagless lines were structurally invisible.** `LOG_LINE` requires `E (1234) tag: msg`
   and `hintFor` was keyed entirely on the tag. `E BOD: Brownout detector was triggered`,
   `rst:0x…`, the boot-ROM banner and any panic backtrace have neither, so they classified
   as `level: 'plain', tag: null` and could not become a fault however many times the board
   printed them. The most common first-board failure mode in existence produced zero
   diagnostic output *by construction*.
2. **The checklist reported milestones that were no longer true.** `summarizeConsole` folded
   `reached` into a `Set` nothing reset, so one early boot's `link up` left ✓ **Network up**
   on screen across dozens of later resets. That is worse than silence: it sent the
   diagnosis in the wrong direction, and it did.
3. **Nothing looked for the reset loop**, and no milestone had a deadline.

**What was built.**

- **`BARE_RULES`** — a table consulted for lines with no ESP-IDF preamble: brownout, panic
  (`Guru Meditation` / `assert failed:`), `Backtrace:`, `waiting for download` (the inverted
  EN/DTR case S0-test-1 watches for), `invalid header` / `flash read err`. A rule raises the
  event's level as well as supplying the hint, because `summarizeConsole` only accepts a
  fault from a warn or an error. It matches the **whole cleaned line**, not the split-off
  message, so a build that does route one of these through the normal logger
  (`E (403) BOD: …`) is still caught.
- **`resetBannerRule`** — `rst:0x… (REASON)` is parsed for its reason. `BROWN_OUT` is an
  error with the same physical remedy; `*WDT*` is a warn; `SW_(CPU_)RESET` is a *generic*
  hint on purpose, because a panic prints its cause immediately before the reset that hides
  it and a specific hint there would displace the real one. `POWERON_RESET` stays plain and
  hintless — it is the normal top of every boot, and a panel that cries wolf on every board
  stops being read.
- **`reached` is per-boot.** A `bootMarker` (`'rom'` for the ROM banner, `'agent'` for the
  agent's own first line) clears it. The two markers of one boot are counted once, via a
  `romPending` flag — counting both would report every board in existence as looping.
- **`rebootLoop`** is set when a boot begins while the previous boot had not reached the
  fleet, and cleared by one that does. That is the task's "twice is proof" without crying
  wolf when the operator deliberately reboots a healthy board. It renders as its **own**
  banner rather than in the fault slot: on 2026-09-11 the board was both browning out and
  restarting, and the operator needed to be told both.
- **Deadlines.** Events carry `at`; `summarizeConsole` takes `now`; `useBoardConsole` ticks
  once a second **only while watching**, so a deadline can pass with no new line arriving —
  which is exactly the stalled case. `MILESTONE_DEADLINE_MS` is grounded in the agent's own
  constants (`NET_TIMEOUT_MS` 30 s, `SNTP_TIMEOUT_MS` 15 s, `ENROLL_TIMEOUT_MS` 30 s) plus
  room for a retry: boot 5 s · link 45 s · clock 30 s · enroll 60 s · fleet 30 s. Each is
  measured from the previous milestone, so a slow link does not instantly declare the clock
  overdue. `MILESTONE_STALL` says what should have happened, what usually prevents it and
  what to try.

**A fault is not cleared by a reboot, unlike a milestone.** A milestone is a positive claim
that must be true *now*; a fault is a description of something that happened, and the reset
it caused does not make it untrue. Progress still clears it, as before.

**T2 — the acceptance, replayed.** `frontend/src/fixtures/bench-2026-09-11.ts` is the bench
session: three brownout cycles, the first reaching `link up`. Through the classifier and the
summary it yields fault = *"The board is browning out … not a hub"*, `rebootLoop` =
`{ boots: 3 }`, and `reached` = `['boot']` with `waitingFor` = `link` — the ✓ is gone.
Through the rendered panel (`BoardConsolePanel`, jsdom) the fault box names the brownout,
the reboot-loop banner reads *"keeps restarting — 3 times so far"*, and **Network up** is
the waiting row, not a done one. Separately, fake timers advance 47 s past a board stuck
after `wifi sta starting` with no further line, and the overdue banner names the link stall
including the 5 GHz cause.

**Vacuity-checked three ways**, each reverted: removing the BOD rule failed the brownout
tests and nothing stood in for it; removing the `reached` reset failed the stale-✓ tests;
removing the loop assignment failed the loop tests. T1: 129 frontend tests green (up from
121), `tsc -b --noEmit` and `vite build` clean. Backend untouched.

**The fixture is a reconstruction, and says so in its header.** The session's raw UART log
was pasted into a chat window and never committed — which is itself what S0-fe-7 exists to
fix. Every line the session record quotes verbatim is verbatim; the ROM banners around them
are what a DevKit v1 prints on a brownout reset.

**What this does not do.** It names causes the board *prints*. A board that says nothing at
all is still only covered by the deadlines, and the remedies are still prose rather than
buttons — that is S0-fe-6. Whether this belongs in a parser at all, versus structured faults
from firmware, stays open in `spec/open-questions.md`.

## Planned Work

### Unaided onboarding: flash → on the fleet (Priority: P0)

- **Problem:** A technician with no ESP32 knowledge cannot get a board onto the fleet
  without an engineer reading raw UART. Proven on 2026-09-11, the first real-hardware
  session: an ESP32-DevKit v1 brownouts during Wi-Fi PHY calibration and resets forever.
  The board printed the cause on every cycle — `E BOD: Brownout detector was triggered` —
  and the panel showed a green **Network up** checkmark and nothing else. The fault was
  found by pasting a serial log into a chat window. Every individual piece of R0 works;
  the *experience* of onboarding does not, and R0's stated risk is onboarding.
- **Scope:** Starts at the flasher page, ends when the board is green in the fleet list.
  Account setup, token minting and getting to the page are out of scope. The repeat path
  (board #2..#N) is out of scope for now — see Post-v1.
- **Target operator:** a technician who can plug in USB and follow instructions, and who
  does not know what a brownout, a DTR line or a partition table is. That is the bar the
  work is judged against, not "an embedded engineer can figure it out".
- **Shape:** three layers, in dependency order.
  1. **Never be silent.** Every failure the board can express is named in plain language
     with a concrete physical or software next action — including the failures that carry
     no ESP-IDF log tag, which today are discarded before they can be classified.
  2. **Recover in place.** Where the fix is software, it is a button: retry enrol,
     re-flash, mint a fresh token, reboot. The operator should not have to know which.
  3. **Escalate cleanly.** When neither works, one click produces a diagnostic bundle —
     full log, config summary, chip info, firmware and server versions, the fault — with
     secrets redacted, so a stuck operator can hand it to someone who can help. That
     click is the thing that did not exist on 2026-09-11.
- **Status:** In progress — all three layers (S0-fe-4, S0-fe-5, S0-fe-6, S0-fe-7) landed
  2026-09-11; see *Escalation is one click* above. Remaining: the unaided run (S0-test-3),
  which is what actually decides this feature.
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
  removes a board.
- CLI flasher for batch/CI enrollment.
- SoftAP captive-portal provisioning (Wi-Fi change without re-flash).
- Per-device mTLS certs (replace token-only trust).
